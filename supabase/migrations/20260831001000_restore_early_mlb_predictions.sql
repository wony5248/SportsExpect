-- Restore a next-day MLB baseline before the late provider enrichment window. The prediction
-- request reads committed schedule/team data only, so it stays well inside the pg_net deadline.

do $$
declare
  existing_job record;
begin
  for existing_job in
    select jobid from cron.job
    where jobname in ('dugout-mlb-early-next-day-discovery', 'dugout-mlb-early-prediction')
  loop
    perform cron.unschedule(existing_job.jobid);
  end loop;
end $$;

select cron.schedule('dugout-mlb-early-next-day-discovery', '50 3 * * *',
  $$select public.invoke_dugout_dated_refresh(
    'MLB', 'discover', (now() at time zone 'Asia/Seoul')::date + 1
  )$$);

select cron.schedule('dugout-mlb-early-prediction', '0 4 * * *',
  $$select public.invoke_dugout_dated_refresh(
    'MLB', 'predict', (now() at time zone 'Asia/Seoul')::date + 1
  )$$);
