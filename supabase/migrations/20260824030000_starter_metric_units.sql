-- Keep archived starter FIP and K-BB inputs in explicit, reproducible units.
alter table public.game_starters add column if not exists prior_hit_batters integer not null default 0;
alter table public.game_starters add column if not exists prior_batters_faced integer not null default 0;
alter table public.game_starters add column if not exists metric_schema_version integer not null default 1;
