-- Run this in Supabase SQL Editor after the API deployment and schema migration.
-- All cron expressions are UTC; the application calculates target dates in Asia/Seoul.

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;
create extension if not exists supabase_vault with schema vault;

-- Create these two secrets once in SQL Editor, replacing the placeholders:
-- select vault.create_secret('https://YOUR-API.vercel.app', 'dugout_backend_url');
-- select vault.create_secret('THE-SAME-VALUE-AS-VERCEL-ADMIN_TOKEN', 'dugout_admin_token');

create or replace function public.invoke_dugout_refresh(refresh_league text, refresh_scope text)
returns bigint
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  backend_url text;
  admin_token text;
  request_id bigint;
begin
  if refresh_league not in ('KBO', 'MLB') then
    raise exception 'Unsupported league: %', refresh_league;
  end if;
  if refresh_scope not in ('full', 'nearby', 'tomorrow', 'market', 'checkpoints', 'lifecycle', 'splits', 'replay', 'discover', 'games', 'predict') then
    raise exception 'Unsupported scope: %', refresh_scope;
  end if;

  select decrypted_secret into backend_url
  from vault.decrypted_secrets where name = 'dugout_backend_url';
  select decrypted_secret into admin_token
  from vault.decrypted_secrets where name = 'dugout_admin_token';

  if backend_url is null or admin_token is null then
    raise exception 'dugout_backend_url or dugout_admin_token is missing from Vault';
  end if;

  select net.http_post(
    url := rtrim(backend_url, '/') || '/api/v1/admin/cron/refresh?league=' || refresh_league || '&scope=' || refresh_scope,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-admin-token', admin_token
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 290000
  ) into request_id;
  return request_id;
end;
$$;

revoke all on function public.invoke_dugout_refresh(text, text) from public, anon, authenticated;

create or replace function public.invoke_dugout_dated_refresh(
  refresh_league text,
  refresh_scope text,
  refresh_date date
)
returns bigint
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  backend_url text;
  admin_token text;
  request_id bigint;
begin
  select decrypted_secret into backend_url
  from vault.decrypted_secrets where name = 'dugout_backend_url';
  select decrypted_secret into admin_token
  from vault.decrypted_secrets where name = 'dugout_admin_token';
  if backend_url is null or admin_token is null then
    raise exception 'dugout_backend_url or dugout_admin_token is missing from Vault';
  end if;
  select net.http_post(
    url := rtrim(backend_url, '/') || '/api/v1/admin/cron/refresh?league=' || refresh_league
      || '&scope=' || refresh_scope || '&date=' || refresh_date::text,
    headers := jsonb_build_object('Content-Type', 'application/json', 'x-admin-token', admin_token),
    body := '{}'::jsonb,
    timeout_milliseconds := 290000
  ) into request_id;
  return request_id;
end;
$$;

revoke all on function public.invoke_dugout_dated_refresh(text, text, date) from public, anon, authenticated;

create or replace function public.invoke_dugout_game_chunk(
  refresh_league text,
  refresh_date date,
  refresh_game_ids text[],
  changed_only boolean
)
returns bigint
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  backend_url text;
  admin_token text;
  request_id bigint;
begin
  if coalesce(array_length(refresh_game_ids, 1), 0) = 0
     or array_length(refresh_game_ids, 1) > 5 then
    raise exception 'A refresh chunk must contain between 1 and 5 games';
  end if;
  select decrypted_secret into backend_url
  from vault.decrypted_secrets where name = 'dugout_backend_url';
  select decrypted_secret into admin_token
  from vault.decrypted_secrets where name = 'dugout_admin_token';
  if backend_url is null or admin_token is null then
    raise exception 'dugout_backend_url or dugout_admin_token is missing from Vault';
  end if;
  select net.http_post(
    url := rtrim(backend_url, '/') || '/api/v1/admin/cron/refresh?league=' || refresh_league
      || '&scope=games&date=' || refresh_date::text
      || '&game_ids=' || array_to_string(refresh_game_ids, ',')
      || '&only_changed=' || changed_only::text,
    headers := jsonb_build_object('Content-Type', 'application/json', 'x-admin-token', admin_token),
    body := '{}'::jsonb,
    timeout_milliseconds := 290000
  ) into request_id;
  return request_id;
end;
$$;

revoke all on function public.invoke_dugout_game_chunk(text, date, text[], boolean) from public, anon, authenticated;

create or replace function public.invoke_dugout_chunked_refresh(
  refresh_league text,
  refresh_date date,
  changed_only boolean
)
returns integer
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  chunk_ids text[];
  invoked integer := 0;
begin
  for chunk_ids in
    select array_agg(candidate.external_id order by candidate.start_at, candidate.external_id)
    from (
      select game.external_id, game.start_at,
        ((row_number() over (order by game.start_at, game.external_id) - 1) / 5)::integer as chunk_no
      from public.games game
      where game.league = refresh_league
        and game.game_date = refresh_date
        and game.status = 'SCHEDULED'
    ) candidate
    group by candidate.chunk_no
    order by candidate.chunk_no
  loop
    perform public.invoke_dugout_game_chunk(refresh_league, refresh_date, chunk_ids, changed_only);
    invoked := invoked + 1;
  end loop;
  return invoked;
end;
$$;

revoke all on function public.invoke_dugout_chunked_refresh(text, date, boolean) from public, anon, authenticated;

-- Idempotently replace only Dugout Lab jobs when this file is run again.
-- The command check also removes legacy jobs created before the dugout-* naming convention.
do $$
declare
  existing_job record;
begin
  for existing_job in
    select jobid
    from cron.job
    where jobname like 'dugout-%'
       or command like '%invoke_dugout_refresh%'
  loop
    perform cron.unschedule(existing_job.jobid);
  end loop;
end $$;

-- Refresh only at the two useful pre-game times. The daily jobs fetch the slate; the dispatcher
-- itself runs in Supabase each minute, but invokes Vercel only for a scheduled game that is
-- exactly 40 minutes from first pitch and has not yet captured that checkpoint.
create or replace function public.invoke_dugout_lineup_refresh(refresh_league text)
returns bigint
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  has_due_game boolean;
begin
  select exists(
    select 1
    from public.games game
    where game.league = refresh_league
      and game.status = 'SCHEDULED'
      and game.start_at > now() + interval '37 minutes 30 seconds'
      and game.start_at <= now() + interval '42 minutes 30 seconds'
      and not exists (
        select 1 from public.prediction_snapshots snapshot
        where snapshot.game_id = game.id
          and snapshot.stage = 'T_MINUS_40M'
          and snapshot.trigger = 'checkpoint_exact'
      )
  ) into has_due_game;
  if not has_due_game then
    return null;
  end if;
  return public.invoke_dugout_refresh(refresh_league, 'checkpoints');
end;
$$;

revoke all on function public.invoke_dugout_lineup_refresh(text) from public, anon, authenticated;

-- Cron expressions are UTC. MLB's 23:00 KST job targets the following KST slate.
select cron.schedule('dugout-kbo-daily-pregame', '0 4 * * *',
  $$select public.invoke_dugout_refresh('KBO', 'full')$$);
select cron.schedule('dugout-mlb-market', '0 13 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'market')$$);
-- The API performs a cheap local due-check first and contacts the odds provider only when an
-- upcoming game is missing its T-24h/T-3h/T-60m/T-15m quote.  Frequent dispatch therefore
-- improves closing-line coverage without multiplying provider calls for completed stages.
select cron.schedule('dugout-kbo-market-checkpoints', '*/10 * * * *',
  $$select public.invoke_dugout_refresh('KBO', 'market')$$);
select cron.schedule('dugout-mlb-market-checkpoints', '5-59/10 * * * *',
  $$select public.invoke_dugout_refresh('MLB', 'market')$$);
-- Publish a usable next-day baseline at 13:00 KST. This uses only committed records and is
-- intentionally independent from the slower starter/provider enrichment later in the day.
select cron.schedule('dugout-mlb-early-next-day-discovery', '50 3 * * *',
  $$select public.invoke_dugout_dated_refresh(
    'MLB', 'discover', (now() at time zone 'Asia/Seoul')::date + 1
  )$$);
select cron.schedule('dugout-mlb-early-prediction', '0 4 * * *',
  $$select public.invoke_dugout_dated_refresh(
    'MLB', 'predict', (now() at time zone 'Asia/Seoul')::date + 1
  )$$);
select cron.schedule('dugout-mlb-next-day-discovery', '55 13 * * *',
  $$select public.invoke_dugout_dated_refresh(
    'MLB', 'discover', (now() at time zone 'Asia/Seoul')::date + 1
  )$$);
select cron.schedule('dugout-mlb-daily-pregame', '0 14 * * *',
  $$select public.invoke_dugout_chunked_refresh(
    'MLB', (now() at time zone 'Asia/Seoul')::date + 1, false
  )$$);
-- Every two hours at minute 10 of even KST hours. These calls never collect lineups and the
-- API skips Monte Carlo when the prediction input hash has not changed.
select cron.schedule('dugout-kbo-changed-2h', '10 1-23/2 * * *',
  $$select public.invoke_dugout_chunked_refresh(
    'KBO', (now() at time zone 'Asia/Seoul')::date, true
  )$$);
select cron.schedule('dugout-mlb-changed-2h', '10 1-23/2 * * *',
  $$select public.invoke_dugout_chunked_refresh(
    'MLB', (now() at time zone 'Asia/Seoul')::date, true
  )$$);
select cron.schedule('dugout-kbo-lineup-40m-dispatch', '* * * * *',
  $$select public.invoke_dugout_lineup_refresh('KBO')$$);
select cron.schedule('dugout-mlb-lineup-40m-dispatch', '* * * * *',
  $$select public.invoke_dugout_lineup_refresh('MLB')$$);
