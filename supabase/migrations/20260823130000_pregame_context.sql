alter table games add column if not exists pregame_context jsonb not null default '{}'::jsonb;
alter table games add column if not exists context_collected_at timestamptz;
alter table team_stats add column if not exists advanced jsonb not null default '{}'::jsonb;
alter table pitcher_stats add column if not exists recent jsonb not null default '{}'::jsonb;
alter table lineups add column if not exists batting_side varchar(4);
alter table lineups add column if not exists platoon_opponent_hand varchar(4);
alter table lineups add column if not exists platoon_plate_appearances integer;
alter table lineups add column if not exists platoon_ops double precision;
