-- Versioned pregame player-level quality, arsenal, defense and catcher snapshots.
alter table public.pitcher_stats add column if not exists advanced jsonb not null default '{}'::jsonb;
alter table public.lineups add column if not exists advanced jsonb not null default '{}'::jsonb;
