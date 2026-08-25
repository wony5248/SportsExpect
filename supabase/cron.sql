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
  if refresh_scope not in ('full', 'nearby', 'tomorrow', 'market', 'checkpoints', 'lifecycle', 'splits', 'replay') then
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

-- Cron expressions are UTC: 13:00 KST, 22:00 KST, 23:00 KST respectively.
select cron.schedule('dugout-kbo-daily-pregame', '0 4 * * *',
  $$select public.invoke_dugout_refresh('KBO', 'full')$$);
select cron.schedule('dugout-mlb-market', '0 13 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'market')$$);
select cron.schedule('dugout-mlb-daily-pregame', '0 14 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'full')$$);
select cron.schedule('dugout-kbo-lineup-40m-dispatch', '* * * * *',
  $$select public.invoke_dugout_lineup_refresh('KBO')$$);
select cron.schedule('dugout-mlb-lineup-40m-dispatch', '* * * * *',
  $$select public.invoke_dugout_lineup_refresh('MLB')$$);
