-- Share one compact, versioned history snapshot across concurrent per-game prediction workers.

create table if not exists public.prediction_context_cache (
  league varchar(8) primary key,
  fingerprint varchar(128) not null,
  algorithm_version integer not null,
  payload jsonb not null,
  updated_at timestamptz not null default now()
);

create index if not exists ix_prediction_context_cache_fingerprint
  on public.prediction_context_cache (fingerprint);

-- Internal server-side cache: browser clients must not read or mutate it. No anon/authenticated
-- policy is intentionally created; the backend's direct database role continues to use it.
alter table public.prediction_context_cache enable row level security;

revoke all on table public.prediction_context_cache from anon, authenticated;
