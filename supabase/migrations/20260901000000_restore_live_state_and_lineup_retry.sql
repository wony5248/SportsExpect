-- Restore lightweight five-minute live-state polling after the manual-only migration removed
-- every dugout-* job. The nearby endpoint now fetches only the official schedule/status feed.
do $$
declare
  existing_job record;
begin
  for existing_job in
    select jobid from cron.job
    where jobname in ('dugout-kbo-nearby', 'dugout-mlb-nearby')
  loop
    perform cron.unschedule(existing_job.jobid);
  end loop;
end $$;

select cron.schedule('dugout-kbo-nearby', '*/5 * * * *',
  $$select public.invoke_dugout_refresh('KBO', 'nearby')$$);
select cron.schedule('dugout-mlb-nearby', '2-59/5 * * * *',
  $$select public.invoke_dugout_refresh('MLB', 'nearby')$$);

-- The exact T-40 checkpoint remains the audited snapshot. If that one-shot collection returned
-- no complete lineup, retry only inside the final 90 minutes and no more than once per 10 minutes.
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
      and (
        (
          game.start_at > now() + interval '37 minutes 30 seconds'
          and game.start_at <= now() + interval '42 minutes 30 seconds'
          and not exists (
            select 1 from public.prediction_snapshots snapshot
            where snapshot.game_id = game.id
              and snapshot.stage = 'T_MINUS_40M'
              and snapshot.trigger = 'checkpoint_exact'
          )
        )
        or (
          game.start_at >= now() - interval '5 minutes'
          and game.start_at <= now() + interval '90 minutes'
          and (
            select count(*) from public.lineups lineup
            where lineup.game_id = game.id and lineup.confirmed is true
          ) < 18
          and not exists (
            select 1 from public.crawl_logs log
            where log.collector = lower(refresh_league) || '_lineups_' || game.external_id
              and log.finished_at > now() - interval '10 minutes'
          )
        )
      )
  ) into has_due_game;
  if not has_due_game then
    return null;
  end if;
  return public.invoke_dugout_refresh(refresh_league, 'checkpoints');
end;
$$;

revoke all on function public.invoke_dugout_lineup_refresh(text) from public, anon, authenticated;
