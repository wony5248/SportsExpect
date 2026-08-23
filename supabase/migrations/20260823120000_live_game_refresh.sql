-- Replace hourly nearby updates with five-minute live-state polling. The application limits
-- each request to games starting within the nearby window and skips pregame enrichment after
-- first pitch, so these runs only update authoritative status and final results.
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
