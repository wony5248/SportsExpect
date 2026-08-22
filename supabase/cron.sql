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
  if refresh_scope not in ('full', 'nearby', 'tomorrow', 'market', 'checkpoints', 'lifecycle') then
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

-- Full refresh hourly, with the opposite half-hour reserved for nearby-game updates.
select cron.schedule('dugout-kbo-full', '4 * * * *',
  $$select public.invoke_dugout_refresh('KBO', 'full')$$);
select cron.schedule('dugout-kbo-nearby', '34 * * * *',
  $$select public.invoke_dugout_refresh('KBO', 'nearby')$$);
select cron.schedule('dugout-mlb-full', '19 * * * *',
  $$select public.invoke_dugout_refresh('MLB', 'full')$$);
select cron.schedule('dugout-mlb-nearby', '49 * * * *',
  $$select public.invoke_dugout_refresh('MLB', 'nearby')$$);

-- Per-game immutable checkpoints. The lightweight endpoint runs every minute,
-- but calls official data providers only when a game enters a ±2.5 minute target window.
select cron.schedule('dugout-kbo-checkpoints', '* * * * *',
  $$select public.invoke_dugout_refresh('KBO', 'checkpoints')$$);
select cron.schedule('dugout-mlb-checkpoints', '* * * * *',
  $$select public.invoke_dugout_refresh('MLB', 'checkpoints')$$);

-- One structured market request per league/day. UTC schedules correspond to
-- 12:00 KST for KBO and 00:00 KST for MLB. Hourly refreshes provide catch-up
-- if either exact-time request fails, while the application daily gate prevents duplicates.
select cron.schedule('dugout-kbo-market', '0 3 * * *',
  $$select public.invoke_dugout_refresh('KBO', 'market')$$);
select cron.schedule('dugout-mlb-market', '0 15 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'market')$$);

-- 13:10 KST and 00:20 KST: discover the following day's schedule.
select cron.schedule('dugout-kbo-tomorrow', '10 4 * * *',
  $$select public.invoke_dugout_refresh('KBO', 'tomorrow')$$);
select cron.schedule('dugout-mlb-tomorrow', '20 15 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'tomorrow')$$);

-- Daily champion/challenger evaluation. Runs only after the result collectors
-- have had time to finalize the previous slate (05:30 and 16:30 KST).
select cron.schedule('dugout-kbo-model-lifecycle', '30 20 * * *',
  $$select public.invoke_dugout_refresh('KBO', 'lifecycle')$$);
select cron.schedule('dugout-mlb-model-lifecycle', '30 7 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'lifecycle')$$);

-- Inspect runs and HTTP responses:
-- select * from cron.job order by jobname;
-- select * from cron.job_run_details order by start_time desc limit 30;
-- select id, status_code, error_msg, created from net._http_response order by created desc limit 30;
