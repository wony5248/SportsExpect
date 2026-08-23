-- WAITING_FOR_LIVE_VALIDATION is 27 characters and overflowed varchar(24), crashing
-- POST /api/v1/admin/cron/refresh?scope=lifecycle for any league without enough live samples.
alter table public.model_lifecycle_events alter column event_type type varchar(40);
