-- Move current production to manual-only refreshes. Apply this migration (or the matching
-- supabase/cron.sql block) in Supabase SQL Editor to remove already-created jobs.
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
