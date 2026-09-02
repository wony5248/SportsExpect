-- Replace the one-request-per-game fan-out with bounded five-game chunks. Each chunk reuses the
-- same compact history and date-level MLB context snapshot, so fewer workers no longer risk
-- repeating the expensive common work.

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
  $$select public.invoke_dugout_chunked_refresh(
    'MLB', (now() at time zone 'Asia/Seoul')::date + 1, false
  )$$);
