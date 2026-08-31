-- Dispatch market collection frequently; the API itself contacts the provider only when an
-- upcoming game is missing its T-24h/T-3h/T-60m/T-15m checkpoint.

do $$
declare
  existing_job record;
begin
  for existing_job in
    select jobid from cron.job
    where jobname in ('dugout-kbo-market-checkpoints', 'dugout-mlb-market-checkpoints')
  loop
    perform cron.unschedule(existing_job.jobid);
  end loop;
end $$;

select cron.schedule('dugout-kbo-market-checkpoints', '*/10 * * * *',
  $$select public.invoke_dugout_refresh('KBO', 'market')$$);

select cron.schedule('dugout-mlb-market-checkpoints', '5-59/10 * * * *',
  $$select public.invoke_dugout_refresh('MLB', 'market')$$);
