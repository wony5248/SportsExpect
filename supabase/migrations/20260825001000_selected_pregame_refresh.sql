-- Restore only the useful pre-game schedule after the previous manual-only migration.
-- The per-minute dispatchers run inside Supabase and call Vercel only for an uncaptured game
-- that is 40 minutes from first pitch.
create or replace function public.invoke_dugout_lineup_refresh(refresh_league text)
returns bigint
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  has_due_game boolean;
begin
  select exists(
    select 1
    from public.games game
    where game.league = refresh_league
      and game.status = 'SCHEDULED'
      and game.start_at > now() + interval '37 minutes 30 seconds'
      and game.start_at <= now() + interval '42 minutes 30 seconds'
      and not exists (
        select 1 from public.prediction_snapshots snapshot
        where snapshot.game_id = game.id
          and snapshot.stage = 'T_MINUS_40M'
          and snapshot.trigger = 'checkpoint_exact'
      )
  ) into has_due_game;
  if not has_due_game then
    return null;
  end if;
  return public.invoke_dugout_refresh(refresh_league, 'checkpoints');
end;
$$;

revoke all on function public.invoke_dugout_lineup_refresh(text) from public, anon, authenticated;

select cron.schedule('dugout-kbo-daily-pregame', '0 4 * * *',
  $$select public.invoke_dugout_refresh('KBO', 'full')$$);
select cron.schedule('dugout-mlb-market', '0 13 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'market')$$);
select cron.schedule('dugout-mlb-daily-pregame', '0 14 * * *',
  $$select public.invoke_dugout_refresh('MLB', 'full')$$);
select cron.schedule('dugout-kbo-lineup-40m-dispatch', '* * * * *',
  $$select public.invoke_dugout_lineup_refresh('KBO')$$);
select cron.schedule('dugout-mlb-lineup-40m-dispatch', '* * * * *',
  $$select public.invoke_dugout_lineup_refresh('MLB')$$);
