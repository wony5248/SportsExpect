-- Keep frequent refreshes inside Vercel's request ceiling:
-- * discover the following KST MLB slate just before 23:00,
-- * fan scheduled games out in chunks of at most five,
-- * run change-only KBO/MLB checks every two hours without lineup collection.

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

do $$
declare
  existing_job record;
begin
  for existing_job in
    select jobid
    from cron.job
    where jobname in (
      'dugout-mlb-daily-pregame',
      'dugout-mlb-next-day-discovery',
      'dugout-kbo-changed-2h',
      'dugout-mlb-changed-2h'
    )
  loop
    perform cron.unschedule(existing_job.jobid);
  end loop;
end $$;

select cron.schedule('dugout-mlb-next-day-discovery', '55 13 * * *',
  $$select public.invoke_dugout_dated_refresh(
    'MLB', 'discover', (now() at time zone 'Asia/Seoul')::date + 1
  )$$);
select cron.schedule('dugout-mlb-daily-pregame', '0 14 * * *',
  $$select public.invoke_dugout_chunked_refresh(
    'MLB', (now() at time zone 'Asia/Seoul')::date + 1, false
  )$$);
select cron.schedule('dugout-kbo-changed-2h', '10 1-23/2 * * *',
  $$select public.invoke_dugout_chunked_refresh(
    'KBO', (now() at time zone 'Asia/Seoul')::date, true
  )$$);
select cron.schedule('dugout-mlb-changed-2h', '10 1-23/2 * * *',
  $$select public.invoke_dugout_chunked_refresh(
    'MLB', (now() at time zone 'Asia/Seoul')::date, true
  )$$);
