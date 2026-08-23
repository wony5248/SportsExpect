"""Per-team bullpen leverage profiles.

A club's relief corps is not one undifferentiated unit: the high-leverage group (필승조) is
saved for close late innings and the low-leverage group (추격조) absorbs blowouts. The
simulation needs that split as run-rate multipliers against the club's own staff average.

Two things produce those multipliers:

* `derive_profile` seeds every team from data we actually collect (team ERA/WHIP against the
  league, plus how deep the rotation goes) combined with the league-wide leverage spread. It
  invents no per-arm numbers; the spread is a documented structural pattern, not a claim about
  any specific reliever.
* `apply_profile_update` accepts a replacement — from a Claude-assisted review, a roster feed,
  or a manual correction — and versions it, so a mid-season shake-up changes predictions from
  that moment and stays auditable.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.entities import Team, TeamBullpen, TeamBullpenEvent, TeamStat

TIERS = ("high_leverage", "middle", "chase", "mop_up")
# League-wide leverage spread: setup/closer arms run meaningfully better than mop-up arms.
# Applied around each club's own bullpen quality rather than replacing it.
LEAGUE_LEVERAGE_SPREAD = {"high_leverage": .82, "middle": 1.00, "chase": 1.12, "mop_up": 1.28}
# A profile may not distort a club beyond this band, whatever a source claims.
MULTIPLIER_BOUNDS = (.55, 1.60)
VALID_SOURCES = ("DERIVED", "CLAUDE", "OFFICIAL", "MANUAL")


def derive_profile(team_stat: Any, league_era: float) -> dict[str, float]:
    """Seed multipliers from collected team data plus the league leverage spread."""
    team_era = _positive(getattr(team_stat, "era", None), league_era)
    team_whip = _positive(getattr(team_stat, "whip", None), 1.35)
    # Team ERA is the only staff-wide signal we collect, so it sets the level; WHIP breaks ties
    # between clubs whose ERA hides traffic on the bases. Both are shrunk hard: this is a
    # structural seed, not a measurement of the bullpen itself.
    era_signal = _clip(team_era / max(league_era, 1.5), .80, 1.25)
    whip_signal = _clip(team_whip / 1.35, .88, 1.14)
    quality = _clip(.70 * era_signal + .30 * whip_signal, .82, 1.20)
    return {tier: round(_clip(spread * quality, *MULTIPLIER_BOUNDS), 3)
            for tier, spread in LEAGUE_LEVERAGE_SPREAD.items()}


def load_profiles(session: Session, league: str) -> dict[int, dict[str, Any]]:
    """Return stored bullpen profiles for one league, keyed by team id."""
    rows = session.execute(
        select(TeamBullpen).join(Team, Team.id == TeamBullpen.team_id).where(Team.league == league)
    ).scalars().all()
    return {row.team_id: _as_payload(row) for row in rows}


def seed_league(session: Session, league: str, effective_date: Any | None = None) -> dict[str, Any]:
    """Create or refresh derived profiles for every team in a league that has stats."""
    teams = session.scalars(select(Team).where(Team.league == league)).all()
    league_era = _league_era(session, league)
    created, updated, skipped = 0, 0, 0
    for team in teams:
        stat = _latest_stat(session, team.id, effective_date)
        if stat is None:
            skipped += 1
            continue
        existing = session.scalar(select(TeamBullpen).where(TeamBullpen.team_id == team.id))
        # Never overwrite a profile a real source supplied with a structural guess.
        if existing and existing.source != "DERIVED":
            skipped += 1
            continue
        result = apply_profile_update(session, team.id, derive_profile(stat, league_era), source="DERIVED",
                                      note=f"{league} 팀 기록 기반 자동 산출")
        created += int(result["created"])
        updated += int(result["changed"] and not result["created"])
    return {"league": league, "teams": len(teams), "created": created, "updated": updated, "unchanged_or_skipped": skipped}


def apply_profile_update(session: Session, team_id: int, multipliers: dict[str, float], source: str,
                         note: str | None = None, arms: dict[str, list[Any]] | None = None) -> dict[str, Any]:
    """Store a profile, recording an event only when a value actually moved."""
    if source not in VALID_SOURCES:
        raise ValueError(f"Unsupported bullpen source: {source}")
    clean = {tier: round(_clip(float(multipliers[tier]), *MULTIPLIER_BOUNDS), 3)
             for tier in TIERS if multipliers.get(tier) is not None}
    if len(clean) != len(TIERS):
        raise ValueError(f"Bullpen update needs every tier: {TIERS}")
    arms = arms or {}
    row = session.scalar(select(TeamBullpen).where(TeamBullpen.team_id == team_id))
    now = datetime.now(UTC)
    if row is None:
        session.add(TeamBullpen(
            team_id=team_id, source=source, note=note, revision=1, updated_at=now,
            **{f"{tier}_multiplier": clean[tier] for tier in TIERS},
            **{f"{tier}_arms": arms.get(tier, []) for tier in TIERS}))
        session.add(TeamBullpenEvent(team_id=team_id, revision=1, source=source,
                                     changes={tier: [None, clean[tier]] for tier in TIERS}, created_at=now, note=note))
        return {"created": True, "changed": True, "revision": 1, "multipliers": clean}
    current = {tier: getattr(row, f"{tier}_multiplier") for tier in TIERS}
    changes = {tier: [current[tier], clean[tier]] for tier in TIERS if abs(current[tier] - clean[tier]) > 1e-9}
    arm_changed = any(arms.get(tier) is not None and arms[tier] != getattr(row, f"{tier}_arms")
                      for tier in TIERS)
    if not changes and not arm_changed and row.source == source:
        return {"created": False, "changed": False, "revision": row.revision, "multipliers": current}
    for tier in TIERS:
        setattr(row, f"{tier}_multiplier", clean[tier])
        if arms.get(tier) is not None:
            setattr(row, f"{tier}_arms", arms[tier])
    row.source, row.note, row.revision, row.updated_at = source, note, row.revision + 1, now
    session.add(TeamBullpenEvent(team_id=team_id, revision=row.revision, source=source,
                                 changes=changes, created_at=now, note=note))
    return {"created": False, "changed": True, "revision": row.revision, "multipliers": clean}


def staff_payload(profile: dict[str, Any] | None, starter_multiplier: float,
                  starter_innings: float) -> dict[str, Any]:
    """Shape one club's pitching plan for simulate_scores."""
    bullpen = {tier: float(profile[tier]) for tier in TIERS} if profile else dict(LEAGUE_LEVERAGE_SPREAD)
    return {
        "starter_multiplier": round(_clip(starter_multiplier, .55, 1.70), 3),
        "starter_innings": round(_clip(starter_innings, 3.0, 7.5), 2),
        "bullpen": bullpen,
        "bullpen_source": (profile or {}).get("source", "LEAGUE_DEFAULT"),
        "bullpen_revision": (profile or {}).get("revision", 0),
    }


def _as_payload(row: TeamBullpen) -> dict[str, Any]:
    return {
        **{tier: getattr(row, f"{tier}_multiplier") for tier in TIERS},
        "source": row.source, "revision": row.revision,
        "arms": {tier: getattr(row, f"{tier}_arms") for tier in TIERS},
    }


def _latest_stat(session: Session, team_id: int, effective_date: Any | None) -> TeamStat | None:
    query = select(TeamStat).where(TeamStat.team_id == team_id)
    if effective_date is not None:
        query = query.where(TeamStat.effective_date <= effective_date)
    return session.scalars(query.order_by(TeamStat.effective_date.desc()).limit(1)).first()


def _league_era(session: Session, league: str) -> float:
    rows = session.execute(
        select(TeamStat.era).join(Team, Team.id == TeamStat.team_id)
        .where(Team.league == league, TeamStat.era.is_not(None))
        .order_by(TeamStat.effective_date.desc()).limit(60)
    ).scalars().all()
    values = [float(value) for value in rows if value]
    return sum(values) / len(values) if values else (4.60 if league == "KBO" else 4.10)


def _positive(value: Any, fallback: float) -> float:
    number = fallback if value is None else float(value)
    return number if number > 0 else fallback


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
