-- Keep each late MLB provider refresh below the pg_net/Vercel request deadline by dispatching
-- one scheduled game per HTTP request. Each game can therefore commit its forecast independently.

create or replace function public.invoke_dugout_per_game_refresh(
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
  game_external_id text;
  invoked integer := 0;
begin
  for game_external_id in
    select game.external_id
    from public.games game
    where game.league = refresh_league
      and game.game_date = refresh_date
      and game.status = 'SCHEDULED'
    order by game.start_at, game.external_id
  loop
    perform public.invoke_dugout_game_chunk(
      refresh_league, refresh_date, array[game_external_id], changed_only
    );
    invoked := invoked + 1;
  end loop;
  return invoked;
end;
$$;

revoke all on function public.invoke_dugout_per_game_refresh(text, date, boolean) from public, anon, authenticated;

do $$
declare
  existing_job record;
begin
  for existing_job in
    select jobid from cron.job where jobname = 'dugout-mlb-daily-pregame'
  loop
    perform cron.unschedule(existing_job.jobid);
  end loop;
end $$;

select cron.schedule('dugout-mlb-daily-pregame', '0 14 * * *',
  $$select public.invoke_dugout_per_game_refresh(
    'MLB', (now() at time zone 'Asia/Seoul')::date + 1, false
  )$$);
