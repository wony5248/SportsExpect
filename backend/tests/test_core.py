from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import logging
from types import SimpleNamespace
import httpx
import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import Text, create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from backend.app.config import KST, database_url_from_environment, settings
from backend.app.database.base import Base
from backend.app.models import (Game, GameResult, LineupEntry, ModelVersion, PitcherStat, Prediction, PredictionEvaluation, PredictionSnapshot, Team,
                                ModelLifecycleEvent, TeamBullpenEvent, UserClaudeSetting)
from backend.app.repositories.repository import _prediction_changes, game_cards, game_dates, upsert_game
from backend.app.services.backtest import walk_forward_backtest
from backend.app.services.bullpen import apply_profile_update, derive_profile, load_profiles, seed_league
from backend.app.services import claude_advisor, personal_claude, runtime_secrets, user_auth
from backend.app.services.claude_advisor import blend_with_claude
from backend.app.collectors.kbo.client import (KBO_BASE_STATES, KboClient, _batter_base_states,
                                               _batter_pitcher_split, _data_id_table, _hitter_name,
                                               _pitcher_opponent_split, _rank_table, _record_rate,
                                               _scoreboard_innings, _flag)
from backend.app.collectors.kbo.client import SourcePayload
from backend.app.collectors.mlb.client import MLB_BASE_STATES, MlbClient, _linescore, _weather_context
from backend.app.collectors.odds import _consensus_event
from backend.app.services.feature_engineering import (_effective_lineup_ops, _lineup_matchup_summary,
                                                       _platoon_feature)
from backend.app.services.refresh import (SPLIT_FETCH_BUDGET, _collect_batter_splits, _market_event_date,
                                          _market_refresh_due, _months_for_recent, _optional,
                                          _prediction_stage, _recent_by_team, _split_budget)
from backend.app.services.batting import SINGLE, STATE_INDEX, build_batter_table
from backend.app.services.simulation import simulate_scores
from backend.app.services.simulation import evaluate_simulation_recipe
from backend.app.services.prediction import (SIMULATION_SUMMARY_SCHEMA_VERSION,
                                             _apply_daily_bullpen_workload,
                                             blend_classifier_into_means, build_score_estimates,
                                             predict_game)
from backend.app.services.jobs import (REPLAY_END_DATE, REPLAY_START_DATE,
                                       _missing_leagues_for_date, checkpoint_stage_for_minutes)
from backend.app.services.model_lifecycle import (_promotion_decision, _validation_partition,
                                                  predict_with_runtime)
from backend.app.services.historical_replay import run_historical_replay
from backend.app.services.runtime_secrets import decrypt_secret, encrypt_secret
from backend.app.services.team_residuals import (ResidualObservation, TeamResidualHistory,
                                                 apply_residual_adjustment, available_before,
                                                 residual_context)
from backend.app.services.pregame_context import prediction_context
from backend.app.services.data_integrity import summarize_pitcher_rows


def test_rank_and_data_tables_are_schema_driven():
    rank_html = """
    <table class='tData'><thead><tr><th>순위</th><th>팀명</th><th>경기</th><th>승</th><th>패</th><th>무</th><th>승률</th></tr></thead>
    <tbody><tr><td>1</td><td>LG</td><td>10</td><td>6</td><td>4</td><td>0</td><td>0.600</td></tr></tbody></table>
    """
    data_html = """
    <table class='tData'><tbody><tr><td>1</td><td>LG</td><td data-id='OPS_RT'>0.777</td><td data-id='HR_CN'>12</td></tr></tbody></table>
    """
    assert _rank_table(rank_html)["LG"]["승률"] == "0.600"
    assert _data_id_table(data_html)["LG"] == {"OPS_RT": "0.777", "HR_CN": "12"}
    assert _record_rate("8-1-3") == 8 / 11


def test_recent_form_excludes_target_date():
    rows = [
        {"game_date": date(2026, 8, 20), "away_name": "LG", "home_name": "KIA", "away_score": 5, "home_score": 3},
        {"game_date": date(2026, 8, 22), "away_name": "LG", "home_name": "KIA", "away_score": 9, "home_score": 0},
    ]
    result = _recent_by_team(rows, date(2026, 8, 22))
    assert result["LG"]["10"]["games"] == 1
    assert result["LG"]["10"]["avg_runs"] == 5
    assert result["KIA"]["10"]["win_rate"] == 0
    assert result["LG"]["matchups"]["KIA"]["avg_run_diff"] == 2


def test_kbo_scoreboard_inning_rows_are_parsed_and_trailing_blanks_removed():
    table = {"rows": [
        {"row": [{"Text": value} for value in ("1", "0", "2", "-", "-")]},
        {"row": [{"Text": value} for value in ("0", "1", "0", "-", "-")]},
    ]}
    result = _scoreboard_innings({"code": "100", "table2": json.dumps(table)})
    assert result == {"away": [1, 0, 2], "home": [0, 1, 0]}


def test_kbo_string_result_flag_does_not_finalize_a_live_game():
    assert _flag("0") is False
    assert _flag("1") is True
    response = {"game": [{
        "SR_ID": 0, "CANCEL_SC_ID": "0", "GAME_STATE_SC": "2", "GAME_RESULT_CK": "0",
        "G_ID": "20260823LGOB0", "G_TM": "18:30", "AWAY_ID": "LG", "AWAY_NM": "LG",
        "HOME_ID": "OB", "HOME_NM": "두산", "S_NM": "잠실", "T_SCORE_CN": "3",
        "B_SCORE_CN": "2", "START_PIT_CK": True, "LINEUP_CK": True,
    }]}
    client = KboClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)))
    try:
        game = client.games(date(2026, 8, 23)).data[0]
    finally:
        client.close()
    assert game["status"] == "LIVE"
    assert game["away_score"] is None
    assert game["home_score"] is None


def test_mlb_linescore_is_normalized_for_result_flow_comparison():
    assert _linescore({"innings": [
        {"away": {"runs": 1}, "home": {"runs": 0}},
        {"away": {"runs": 0}, "home": {"runs": 2}},
    ]}) == {"away": [1, 0], "home": [0, 2]}
    assert _linescore(None) is None


def test_pitcher_integrity_distinguishes_same_game_duplicates_from_normal_repeated_snapshots():
    game1 = SimpleNamespace(id=1, external_id="GAME-1", game_date=date(2026, 8, 20))
    game2 = SimpleNamespace(id=2, external_id="GAME-2", game_date=date(2026, 8, 25))
    def stat(row_id, game_id, side):
        return SimpleNamespace(
            id=row_id, game_id=game_id, side=side, player_id="42", name="Pitcher", confirmed=True,
            era=3.2, whip=1.1, war=1.2, games=20, avg_start_innings=5.5, quality_starts=10,
            fip=3.4, k_bb_rate=.18, rest_days=5, recent_pitches=0, handedness="R",
            opponent_games=1, opponent_innings=6.0, opponent_era=2.0, opponent_whip=1.0, recent={},
        )
    summary = summarize_pitcher_rows([
        (stat(1, 1, "home"), game1), (stat(2, 1, "home"), game1),
        (stat(3, 2, "away"), game2),
    ])
    assert len(summary["_same_game_side_rows"]) == 1
    assert len(summary["_same_game_player_rows"]) == 1
    assert len(summary["_repeated_signature_rows"]) == 1


def test_cancelled_game_keeps_schedule_but_purges_stale_starter_and_lineup_data():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        model = ModelVersion(name="TEST", algorithm="test", feature_schema={}, checksum="cancel")
        session.add_all([away, home, model]); session.flush()
        collected = datetime(2026, 8, 23, 12)
        game = Game(external_id="RAIN-1", league="KBO", game_date=date(2026, 8, 23),
                    away_team_id=away.id, home_team_id=home.id, status="SCHEDULED", source="test",
                    source_url="test", collected_at=collected, pregame_context={"weather": {"available": True}})
        session.add(game); session.flush()
        session.add(PitcherStat(game_id=game.id, side="away", player_id="P1", name="Pitcher", confirmed=True,
                                source="test", source_url="test", collected_at=collected))
        session.add(LineupEntry(game_id=game.id, side="away", batting_order=1, player_name="Hitter",
                                confirmed=True, source="test", source_url="test", collected_at=collected))
        prediction = Prediction(game_id=game.id, model_version_id=model.id, input_hash="cancel-input",
                                home_win_probability=.5, away_win_probability=.5, home_expected_runs=4,
                                away_expected_runs=4, confidence=.5, payload={}, training_eligible=True,
                                created_at=collected)
        session.add(prediction); session.flush()
        upsert_game(session, {
            "external_id": "RAIN-1", "game_date": date(2026, 8, 23), "away_code": "AW", "away_name": "Away",
            "home_code": "HM", "home_name": "Home", "status": "CANCELLED", "stadium": "Park",
        }, "official", collected, "KBO")
        session.flush()
        assert session.get(Game, game.id).status == "CANCELLED"
        assert session.scalars(select(PitcherStat).where(PitcherStat.game_id == game.id)).all() == []
        assert session.scalars(select(LineupEntry).where(LineupEntry.game_id == game.id)).all() == []
        assert session.get(Prediction, prediction.id).training_eligible is False
        assert session.get(Game, game.id).pregame_context == {}


def test_mlb_weather_adjustment_is_neutral_when_missing_and_capped_when_published():
    assert _weather_context({}, {"roofType": "Open"}) == {
        "available": False, "reason": "PREGAME_WEATHER_NOT_PUBLISHED", "run_multiplier": 1.0,
    }
    hot_windy = _weather_context({"temp": "96", "condition": "Clear", "wind": "18 mph, Out To CF"},
                                 {"roofType": "Open"})
    assert hot_windy["available"] is True
    assert hot_windy["run_multiplier"] == 1.08
    dome = _weather_context({"temp": "96", "condition": "Clear", "wind": "18 mph, Out To CF"},
                            {"roofType": "Dome"})
    assert dome["run_multiplier"] == 1.0


def test_mlb_lineup_reads_batting_side_from_official_game_data_people():
    response = {
        "gameData": {"players": {"ID7": {"batSide": {"code": "L"}}}},
        "liveData": {"boxscore": {"teams": {
            "away": {"battingOrder": [7] * 9, "players": {"ID7": {
                "person": {"fullName": "Test Hitter"}, "position": {"abbreviation": "RF"},
                "seasonStats": {"batting": {"ops": ".812"}},
            }}},
            "home": {"battingOrder": [], "players": {}},
        }}},
    }
    client = MlbClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)))
    try:
        rows = client.lineups({"game_pk": "1"}).data
    finally:
        client.close()
    assert rows[0]["batting_side"] == "L"
    assert rows[0]["confirmed"] is True


def test_daily_bullpen_workload_changes_relief_tiers_only_with_official_data():
    staff = {"starter_multiplier": .9, "starter_innings": 5.5,
             "bullpen": {"high_leverage": .8, "middle": 1.0, "chase": 1.1, "mop_up": 1.3}}
    neutral = _apply_daily_bullpen_workload(staff, None)
    assert neutral["bullpen"] == staff["bullpen"]
    tired = _apply_daily_bullpen_workload(staff, {
        "available": True, "source": "OFFICIAL_BOX_SCORE", "fatigue_index": .5,
        "pitches": {1: 85}, "high_load_arms": ["Closer"],
    })
    assert tired["bullpen"]["high_leverage"] == .84
    assert tired["daily_workload"]["high_load_arms"] == ["Closer"]


def test_mlb_slate_context_aggregates_only_relief_pitches_from_prior_box_scores():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/schedule":
            return httpx.Response(200, json={"dates": [{"games": [{
                "gamePk": 9, "gameDate": "2026-08-22T01:00:00Z",
                "status": {"abstractGameState": "Final"},
            }]}]})
        if "/game/9/" in request.url.path:
            return httpx.Response(200, json={
                "gameData": {"teams": {"away": {"id": 1}, "home": {"id": 2}}},
                "liveData": {"boxscore": {"teams": {
                    "away": {"pitchers": [11, 12], "players": {
                        "ID11": {"person": {"fullName": "Starter"}, "stats": {"pitching": {"gamesStarted": 1, "numberOfPitches": 90}}},
                        "ID12": {"person": {"fullName": "Reliever"}, "stats": {"pitching": {"gamesStarted": 0, "numberOfPitches": 35}}},
                    }},
                    "home": {"pitchers": [], "players": {}},
                }}},
            })
        return httpx.Response(200, json={
            "gameData": {"weather": {"temp": "80", "condition": "Clear", "wind": "5 mph, Out To RF"},
                         "venue": {"name": "Park", "location": {"defaultCoordinates": {"latitude": 40, "longitude": -75}},
                                   "fieldInfo": {"roofType": "Open", "turfType": "Grass"},
                                   "timeZone": {"id": "America/New_York"}}},
            "liveData": {},
        })

    client = MlbClient(transport=httpx.MockTransport(handler))
    try:
        source = client.slate_context(date(2026, 8, 23), [{
            "game_pk": "10", "external_id": "MLB-10", "away_code": "1", "home_code": "2",
        }])
    finally:
        client.close()
    context = source.data["MLB-10"]
    assert context["bullpen"]["away"]["pitches"][1] == 35
    assert context["bullpen"]["away"]["high_load_arms"] == ["Reliever"]
    assert context["weather"]["run_multiplier"] > 1
    assert context["venue"]["latitude"] == 40


def test_platoon_signal_is_sample_shrunk_and_requires_actual_split_rows():
    rows = [SimpleNamespace(side="home", value=.700, platoon_ops=.900, platoon_plate_appearances=200)] * 9
    rows += [SimpleNamespace(side="away", value=.700, platoon_ops=None, platoon_plate_appearances=None)] * 9
    diff, home, away, home_coverage, away_coverage = _platoon_feature(rows)
    assert 0 < diff < .6
    assert home > 0 and away == 0
    assert (home_coverage, away_coverage) == (9, 0)


def test_archive_replay_is_explicitly_scoped_to_2026():
    assert REPLAY_START_DATE == date(2026, 1, 1)
    assert REPLAY_END_DATE == date(2026, 12, 31)


def test_mlb_individual_feeds_fill_lines_missing_from_season_schedule():
    def handler(request: httpx.Request) -> httpx.Response:
        game_pk = request.url.path.split("/")[-3]
        return httpx.Response(200, json={"liveData": {"linescore": {"innings": [
            {"away": {"runs": int(game_pk) % 2}, "home": {"runs": 1}},
        ]}}})

    client = MlbClient(transport=httpx.MockTransport(handler))
    try:
        source = client.inning_lines(["10", "11"])
    finally:
        client.close()
    assert source.data == {
        "MLB-10": {"away": [0], "home": [1]},
        "MLB-11": {"away": [1], "home": [1]},
    }


def test_stored_simulation_recipe_reproduces_actual_result_frequencies():
    recipe = {
        "home_expected": 4.8, "away_expected": 4.1, "simulations": 5_000, "seed": 2026,
        "environment_variance": .08, "team_variance": .12, "league": "KBO",
        "home_staff": None, "away_staff": None, "home_lineup": None, "away_lineup": None,
    }
    evaluation = evaluate_simulation_recipe(recipe, {
        "away_score": 3, "home_score": 5,
        "innings": {"away": [0, 0, 1, 0, 0, 1, 0, 1, 0],
                    "home": [1, 0, 0, 2, 0, 0, 1, 1, 0]},
    })
    assert evaluation["simulation_count"] == 5_000
    assert evaluation["actual_score_count"] >= 0
    assert evaluation["actual_score_probability"] == round(evaluation["actual_score_count"] / 5_000, 6)
    assert evaluation["actual_outcome"] == "HOME_WIN"
    assert evaluation["actual_outcome_count"] > evaluation["actual_score_count"]
    assert evaluation["inning_data_available"] is True
    assert evaluation["actual_inning_path_count"] >= 0


def test_historical_replay_is_audited_evaluated_and_serialized_separately():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        base = datetime(2026, 4, 1, 18, 30)
        for index in range(6):
            start = base + timedelta(days=index)
            game = Game(
                external_id=f"REPLAY-{index}", league="KBO", game_date=start.date(),
                start_at=start, start_time=start.time(), away_team_id=away.id, home_team_id=home.id,
                status="FINAL", source="test", source_url="test", collected_at=start + timedelta(hours=3),
            )
            session.add(game); session.flush()
            session.add(GameResult(
                game_id=game.id, away_score=3 + index % 2, home_score=5 - index % 2,
                innings={"away": [0, 0, 1, 0, 1, 0, 1, 0, 0],
                         "home": [1, 0, 0, 1, 0, 1, 0, 2 - index % 2, 0]},
                finalized_at=start + timedelta(hours=3), source_url="test",
            ))
        session.flush()
        report = run_historical_replay(session, "KBO", limit=1)
        assert report["created"] == 1
        prediction = session.scalar(select(Prediction).where(Prediction.origin == "HISTORICAL_REPLAY"))
        assert prediction is not None
        assert prediction.training_eligible is True
        assert prediction.leakage_audit["passed"] is True
        assert prediction.leakage_audit["target_result_used_as_input"] is False
        evaluation = session.scalar(select(PredictionEvaluation).where(
            PredictionEvaluation.prediction_id == prediction.id,
        ))
        assert evaluation is not None
        assert evaluation.simulation_count == settings.simulations
        card = game_cards(session, date(2026, 4, 6), "KBO")[0]
        assert card["prediction"]["origin"] == "HISTORICAL_REPLAY"
        assert card["prediction"]["evaluation"]["actual_score_count"] >= 0


def test_legacy_live_forecast_keeps_original_and_adds_separate_exact_replay():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="MLB", code="AW", name="Away")
        home = Team(league="MLB", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        base = datetime(2026, 4, 1, 18, 30)
        target = None
        for index in range(6):
            start = base + timedelta(days=index)
            game = Game(
                external_id=f"LEGACY-REPLAY-{index}", league="MLB", game_date=start.date(),
                start_at=start, start_time=start.time(), away_team_id=away.id, home_team_id=home.id,
                status="FINAL", source="test", source_url="test", collected_at=start + timedelta(hours=3),
            )
            session.add(game); session.flush()
            session.add(GameResult(
                game_id=game.id, away_score=3, home_score=5,
                finalized_at=start + timedelta(hours=3), source_url="test",
            ))
            target = game
        legacy_model = ModelVersion(
            name="MLB_LEGACY_TEST", algorithm="legacy", feature_schema={}, checksum="legacy-test",
        )
        session.add(legacy_model); session.flush()
        session.add(Prediction(
            game_id=target.id, model_version_id=legacy_model.id, input_hash="legacy-input",
            origin="LIVE_PREGAME", data_cutoff=target.start_at - timedelta(hours=1),
            home_win_probability=.6, away_win_probability=.4,
            home_expected_runs=5.0, away_expected_runs=3.0, confidence=.5,
            # An exact but outdated live population remains immutable and receives a separate
            # current replay once the summary/model schema advances.
            payload={"summary_schema_version": SIMULATION_SUMMARY_SCHEMA_VERSION - 1,
                     "frequency_tables": {}, "model": {"name": "MLB_MATCHUP_V10"}},
            created_at=target.start_at - timedelta(hours=1),
        ))
        session.add(Prediction(
            game_id=target.id, model_version_id=legacy_model.id, input_hash="outdated-replay-input",
            origin="HISTORICAL_REPLAY", data_cutoff=target.start_at,
            home_win_probability=.55, away_win_probability=.45,
            home_expected_runs=4.8, away_expected_runs=4.0, confidence=.6,
            payload={"summary_schema_version": SIMULATION_SUMMARY_SCHEMA_VERSION - 1,
                     "coherence_valid": True, "engine": "INNING_RATE",
                     "model": {"name": "MLB_HISTORICAL_REPLAY_OLD"}},
            leakage_audit={"passed": True}, created_at=target.start_at + timedelta(hours=4),
        ))
        session.flush()

        missing_only = run_historical_replay(session, "MLB", limit=1, only_missing=True)
        assert missing_only["created"] == 1
        assert missing_only["skipped_existing_replay"] >= 1
        assert missing_only["outdated_replays_refreshed"] == 0
        target_rows = session.scalars(select(Prediction).where(Prediction.game_id == target.id)).all()
        assert len(target_rows) == 2

        report = run_historical_replay(session, "MLB", limit=1)
        assert report["created"] == 1
        assert report["legacy_live_replayed"] == 1
        assert report["outdated_replays_refreshed"] == 1
        card = game_cards(session, target.game_date, "MLB")[0]
        assert card["prediction"]["origin"] == "LIVE_PREGAME"
        assert card["prediction"]["evaluation"] is None
        assert card["replay_prediction"]["origin"] == "HISTORICAL_REPLAY"
        assert card["replay_prediction"]["summary_schema_version"] == SIMULATION_SUMMARY_SCHEMA_VERSION
        assert card["replay_prediction"]["evaluation"]["simulation_count"] == settings.simulations


def test_historical_replay_covers_leakage_safe_season_cold_start():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        start = datetime(2026, 3, 28, 14, 0)
        game = Game(
            external_id="COLD-START-1", league="KBO", game_date=start.date(),
            start_at=start, start_time=start.time(), away_team_id=away.id, home_team_id=home.id,
            status="FINAL", source="test", source_url="test", collected_at=start + timedelta(hours=3),
        )
        session.add(game); session.flush()
        session.add(GameResult(
            game_id=game.id, away_score=2, home_score=4,
            finalized_at=start + timedelta(hours=3), source_url="test",
        ))
        session.flush()

        report = run_historical_replay(session, "KBO", limit=10)
        assert report["created"] == 1
        assert report["cold_start_created"] == 1
        prediction = session.scalar(select(Prediction).where(Prediction.game_id == game.id))
        assert prediction.leakage_audit["passed"] is True
        assert prediction.leakage_audit["history_sufficient"] is False
        assert prediction.leakage_audit["target_result_used_as_input"] is False


def test_month_range_crosses_year_boundary():
    assert _months_for_recent(date(2026, 1, 10), 80) == [(2025, 10), (2025, 11), (2025, 12), (2026, 1)]


def test_market_refresh_slots_are_anchored_to_kst_once_per_day():
    kbo_latest = datetime(2026, 8, 21, 12, 0, tzinfo=KST)
    assert not _market_refresh_due("KBO", kbo_latest, datetime(2026, 8, 22, 11, 59, tzinfo=KST))
    assert _market_refresh_due("KBO", kbo_latest, datetime(2026, 8, 22, 12, 0, tzinfo=KST))
    mlb_latest = datetime(2026, 8, 22, 0, 0, tzinfo=KST)
    assert not _market_refresh_due("MLB", mlb_latest, datetime(2026, 8, 22, 23, 59, tzinfo=KST))
    assert _market_refresh_due("MLB", mlb_latest, datetime(2026, 8, 23, 0, 0, tzinfo=KST))
    assert _market_event_date("2026-08-22T15:30:00Z") == date(2026, 8, 23)


def test_exact_checkpoint_windows_do_not_use_broad_stage_buckets():
    assert checkpoint_stage_for_minutes(1440) == "T_MINUS_24H"
    assert checkpoint_stage_for_minutes(180) == "T_MINUS_3H"
    assert checkpoint_stage_for_minutes(60) == "T_MINUS_60M"
    assert checkpoint_stage_for_minutes(15) == "T_MINUS_15M"
    assert checkpoint_stage_for_minutes(170) is None


def test_candidate_promotion_requires_improvement_and_non_regression_guards():
    comparator = {"brier": .240, "log_loss": .690, "run_mae": 2.40}
    assert _promotion_decision(
        {"brier": .230, "log_loss": .680, "run_mae": 2.39}, comparator,
    )[0]
    assert not _promotion_decision(
        {"brier": .230, "log_loss": .680, "run_mae": 2.60}, comparator,
    )[0]


def test_trained_runtime_produces_bounded_probability_and_runs():
    runtime = {
        "feature_names": [], "feature_means": [], "feature_scales": [],
        "win_intercept": 0.4, "win_coefficients": [],
        "home_run_intercept": 12.0, "home_run_coefficients": [],
        "away_run_intercept": -2.0, "away_run_coefficients": [],
    }
    probability, home_runs, away_runs = predict_with_runtime(runtime, {}, 5.0, 4.0)
    assert .59 < probability < .61
    assert .6 <= away_runs < home_runs <= 10.0
    assert round(home_runs + away_runs, 6) == 10.6


def test_supabase_database_url_selects_psycopg_driver(monkeypatch):
    monkeypatch.setenv("BASEBALL_DATABASE_URL", "postgresql://user:pass@example.com:6543/postgres?sslmode=require")
    assert database_url_from_environment() == (
        "postgresql+psycopg://user:pass@example.com:6543/postgres?sslmode=require"
    )


def test_forecasts_never_run_below_twenty_thousand_simulations():
    assert settings.simulations >= 20_000


def test_model_algorithm_accepts_auditable_long_descriptions():
    assert isinstance(ModelVersion.__table__.c.algorithm.type, Text)


def test_http_client_info_logs_are_suppressed_for_query_string_secrets():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_game_cards_use_bounded_batch_queries():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 8, 22)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        session.add_all([
            Game(external_id=f"BATCH-{index}", league="KBO", game_date=target,
                 away_team_id=away.id, home_team_id=home.id, status="SCHEDULED",
                 source="test", source_url="test", collected_at=datetime(2026, 8, 22, 12, index))
            for index in range(6)
        ])
        session.commit()
        queries = 0

        def count_query(*_args):
            nonlocal queries
            queries += 1

        event.listen(engine, "before_cursor_execute", count_query)
        rows = game_cards(session, target, "KBO")
        event.remove(engine, "before_cursor_execute", count_query)
        assert len(rows) == 6
        assert queries <= 8


def test_final_game_card_uses_last_prediction_saved_before_game_start():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 8, 22)
    start = datetime(2026, 8, 22, 18, 30)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        model = ModelVersion(name="CARD-TEST", algorithm="test", feature_schema={}, checksum="card-test")
        session.add_all([away, home, model]); session.flush()
        game = Game(external_id="FINAL-CARD", league="KBO", game_date=target, start_at=start,
                    away_team_id=away.id, home_team_id=home.id, status="FINAL",
                    source="test", source_url="test", collected_at=start + timedelta(hours=3))
        session.add(game); session.flush()
        session.add_all([
            Prediction(game_id=game.id, model_version_id=model.id, input_hash="pregame",
                       home_win_probability=.6, away_win_probability=.4,
                       home_expected_runs=5.1, away_expected_runs=3.8, confidence=80,
                       payload={}, created_at=start - timedelta(minutes=15)),
            Prediction(game_id=game.id, model_version_id=model.id, input_hash="postgame",
                       home_win_probability=.99, away_win_probability=.01,
                       home_expected_runs=9, away_expected_runs=1, confidence=99,
                       payload={}, created_at=start + timedelta(minutes=5)),
        ])
        session.add(GameResult(game_id=game.id, away_score=3, home_score=5,
                               finalized_at=start + timedelta(hours=3), source_url="test"))
        session.commit()

        card = game_cards(session, target, "KBO")[0]
        assert card["result"] == {"away_score": 3, "home_score": 5}
        assert card["prediction"]["away_expected_runs"] == 3.8
        assert card["prediction"]["home_expected_runs"] == 5.1
        assert card["prediction"]["home_win_probability"] == .6


def test_mlb_games_are_grouped_by_kst_and_keep_official_us_date():
    def handler(request: httpx.Request) -> httpx.Response:
        games = []
        if request.url.params.get("date") == "2026-08-21":
            games = [{
                "gamePk": 123, "gameDate": "2026-08-22T01:10:00Z", "officialDate": "2026-08-21",
                "status": {"detailedState": "Scheduled", "abstractGameState": "Preview"},
                "venue": {"name": "Test Park"},
                "teams": {
                    "away": {"team": {"id": 1, "name": "Away"}},
                    "home": {"team": {"id": 2, "name": "Home"}},
                },
            }]
        return httpx.Response(200, json={"dates": [{"games": games}] if games else []})

    client = MlbClient(transport=httpx.MockTransport(handler))
    try:
        rows = client.games(date(2026, 8, 22)).data
    finally:
        client.close()
    assert len(rows) == 1
    assert rows[0]["game_date"] == date(2026, 8, 22)
    assert rows[0]["venue_date"] == date(2026, 8, 21)
    assert rows[0]["start_time"] == "10:10"


def test_kbo_monthly_schedule_keeps_future_games_for_season_archive():
    response = {
        "rows": [{"row": [
            {"Text": "09.01(화)", "Class": "day"},
            {"Text": "<b>18:30</b>", "Class": "time"},
            {"Text": "<span>LG</span><em><span>vs</span></em><span>두산</span>", "Class": "play"},
            {"Text": "", "Class": "relay"},
            {"Text": "", "Class": None}, {"Text": "", "Class": None},
            {"Text": "", "Class": None}, {"Text": "잠실", "Class": None},
            {"Text": "-", "Class": None},
        ]}],
    }
    client = KboClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)))
    try:
        rows = client.monthly_schedule(2026, 9).data
    finally:
        client.close()
    assert rows == [{
        "external_id": "20260901LGOB0", "game_date": date(2026, 9, 1),
        "venue_date": date(2026, 9, 1), "away_name": "LG", "away_code": "LG",
        "home_name": "두산", "home_code": "OB", "away_score": None, "home_score": None,
        "start_time": "18:30", "start_at": datetime(2026, 9, 1, 18, 30, tzinfo=KST),
        "stadium": "잠실", "status": "SCHEDULED",
    }]


def test_game_date_archive_counts_leagues_by_kst_date():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="MLB", code="1", name="Away")
        home = Team(league="MLB", code="2", name="Home")
        session.add_all([away, home]); session.flush()
        session.add_all([
            Game(external_id=f"DATE-{index}", league="MLB", game_date=date(2026, 8, 22),
                 venue_date=date(2026, 8, 21), away_team_id=away.id, home_team_id=home.id,
                 status="FINAL", source="test", source_url="test", collected_at=datetime.now())
            for index in range(2)
        ])
        session.commit()
        assert game_dates(session, 2026, "MLB") == [
            {"date": "2026-08-22", "games": 2, "kbo": 0, "mlb": 2},
        ]


def test_live_game_never_serializes_a_partial_score_as_final_result():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="KBO", code="LG", name="LG")
        home = Team(league="KBO", code="OB", name="두산")
        session.add_all([away, home]); session.flush()
        game = Game(
            external_id="LIVE-PARTIAL", league="KBO", game_date=date(2026, 8, 23),
            venue_date=date(2026, 8, 23), start_at=datetime(2026, 8, 23, 18, 30),
            start_time=datetime(2026, 8, 23, 18, 30).time(), away_team_id=away.id,
            home_team_id=home.id, status="LIVE", source="test", source_url="test",
            collected_at=datetime(2026, 8, 23, 19, 10),
        )
        session.add(game); session.flush()
        session.add(GameResult(
            game_id=game.id, away_score=3, home_score=2,
            finalized_at=datetime(2026, 8, 23, 19, 10), source_url="old-collector",
        ))
        session.flush()
        card = game_cards(session, date(2026, 8, 23), "KBO")[0]
        assert card["status"] == "LIVE"
        assert card["result"] is None


def test_simulation_is_reproducible_and_coherent():
    first = simulate_scores(5.2, 4.1, 20_000, 42)
    second = simulate_scores(5.2, 4.1, 20_000, 42)
    assert first == second
    assert abs(first["home_two_way_probability"] + first["away_two_way_probability"] - 1) < 1e-9
    assert abs(first["home_win_probability"] + first["away_win_probability"] + first["tie_probability"] - 1) < 1e-9
    assert first["totals"]["7.5"]["over"] >= first["totals"]["9.5"]["over"]
    assert abs(sum(first["totals"]["8"].values()) - 1) < 1e-9
    assert first["totals"]["8"]["push"] > 0
    assert first["totals"]["8.5"]["push"] == 0
    assert abs(first["handicap"]["home_minus_1_5"] + first["handicap"]["away_plus_1_5"] - 1) < 1e-9
    assert abs(first["handicap"]["away_minus_1_5"] + first["handicap"]["home_plus_1_5"] - 1) < 1e-9
    assert first["home_minus_1_5"] <= first["home_two_way_probability"]
    representative = first["top_scores"][0]
    assert representative["rank"] == 1
    assert representative["count"] == max(score["count"] for score in first["top_scores"])
    assert representative["probability"] == round(representative["count"] / 20_000, 4)
    assert len(representative["inning_line"]) >= 9
    assert sum(item["away"] for item in representative["inning_line"]) == representative["away"]
    assert sum(item["home"] for item in representative["inning_line"]) == representative["home"]
    assert 0 < representative["trajectory_probability_given_score"] <= 1
    assert first["team_quantiles"]["away"]["p10"] < first["team_quantiles"]["away"]["p90"]
    assert first["total_quantiles"]["p10"] < first["total_quantiles"]["p90"]
    assert 0 < first["game_shape"]["blowout_probability"] < 1
    for mode in first["simulation_modes"].values():
        assert mode["count"] > 0
        assert mode["probability"] == round(mode["count"] / 20_000, 4)


def test_pitcher_plan_reshapes_the_game_without_moving_expected_runs():
    ace = {"starter_multiplier": .72, "starter_innings": 6.8,
           "bullpen": {"high_leverage": .70, "middle": .92, "chase": 1.02, "mop_up": 1.15}}
    opener = {"starter_multiplier": 1.35, "starter_innings": 4.0,
              "bullpen": {"high_leverage": .95, "middle": 1.12, "chase": 1.25, "mop_up": 1.40}}
    neutral = simulate_scores(4.6, 4.2, 40_000, 11, league="MLB")
    with_ace = simulate_scores(4.6, 4.2, 40_000, 11, league="MLB", away_staff=ace)
    with_opener = simulate_scores(4.6, 4.2, 40_000, 11, league="MLB", away_staff=opener)
    # The plan decides when runs score, never how many: every profile keeps the target mean.
    for result in (neutral, with_ace, with_opener):
        assert abs(result["regulation_mean_runs"]["home"] / 4.6 - 1) < .03
        assert abs(result["regulation_mean_runs"]["away"] / 4.2 - 1) < .03
    # A starter who works deep leaves fewer innings for the bullpen.
    assert with_ace["bullpen_usage"]["away"]["starter_share"] > neutral["bullpen_usage"]["away"]["starter_share"]
    assert with_opener["bullpen_usage"]["away"]["starter_share"] < neutral["bullpen_usage"]["away"]["starter_share"]
    usage = neutral["bullpen_usage"]["away"]
    assert abs(sum(usage[key] for key in ("starter_share", "high_leverage_share", "middle_share",
                                          "chase_share", "mop_up_share")) - 1) < 1e-6
    # Close late innings call the high-leverage group far more often than a decided game does.
    assert usage["high_leverage_share"] > usage["mop_up_share"]


def test_bullpen_profile_updates_are_versioned_and_only_recorded_when_values_move():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        team = Team(league="KBO", code="BP", name="Bullpen")
        session.add(team); session.flush()
        derived = derive_profile(SimpleNamespace(era=4.20, whip=1.30), league_era=4.60)
        assert derived["high_leverage"] < derived["middle"] < derived["chase"] < derived["mop_up"]
        first = apply_profile_update(session, team.id, derived, source="DERIVED")
        assert first == {"created": True, "changed": True, "revision": 1, "multipliers": derived}
        # Re-applying the same derived values must not manufacture a change event.
        assert apply_profile_update(session, team.id, derived, source="DERIVED")["changed"] is False
        # A Claude-supplied replacement bumps the revision and records what moved.
        claude = {"high_leverage": .68, "middle": .95, "chase": 1.10, "mop_up": 1.30}
        update = apply_profile_update(session, team.id, claude, source="CLAUDE", note="마무리 교체")
        assert update["revision"] == 2 and update["changed"] is True
        events = session.scalars(select(TeamBullpenEvent).order_by(TeamBullpenEvent.revision)).all()
        assert [event.source for event in events] == ["DERIVED", "CLAUDE"]
        assert events[1].changes["high_leverage"] == [derived["high_leverage"], .68]
        session.commit()
        stored = load_profiles(session, "KBO")[team.id]
        assert (stored["high_leverage"], stored["source"], stored["revision"]) == (.68, "CLAUDE", 2)
        # A derived reseed must never overwrite a real source.
        assert seed_league(session, "KBO")["unchanged_or_skipped"] == 1
        # Values outside the guard band are clamped rather than trusted.
        clamped = apply_profile_update(session, team.id, {"high_leverage": .05, "middle": 1.0,
                                                          "chase": 1.1, "mop_up": 9.0}, source="MANUAL")
        assert clamped["multipliers"] == {"high_leverage": .55, "middle": 1.0, "chase": 1.1, "mop_up": 1.60}


SITUATION_HTML = """
<span id='cphContents_cphContents_cphContents_playerProfile_lblName'>테스트타자</span>
<table class='tbl tt'>
  <thead><tr><th>구분</th><th>AVG</th><th>AB</th><th>H</th><th>2B</th><th>3B</th><th>HR</th>
  <th>RBI</th><th>BB</th><th>HBP</th><th>SO</th><th>GDP</th></tr></thead>
  <tbody>
    <tr><td>주자없음</td><td>0.281</td><td>196</td><td>55</td><td>10</td><td>0</td><td>14</td>
        <td>14</td><td>21</td><td>2</td><td>47</td><td>0</td></tr>
    <tr><td>1루</td><td>0.257</td><td>70</td><td>18</td><td>3</td><td>0</td><td>5</td>
        <td>11</td><td>4</td><td>0</td><td>22</td><td>0</td></tr>
    <tr><td>만루</td><td>0.455</td><td>11</td><td>5</td><td>3</td><td>0</td><td>0</td>
        <td>13</td><td>1</td><td>0</td><td>1</td><td>1</td></tr>
    <tr><td>득점권</td><td>0.369</td><td>103</td><td>38</td><td>7</td><td>0</td><td>7</td>
        <td>67</td><td>17</td><td>0</td><td>17</td><td>6</td></tr>
  </tbody>
</table>
"""


def test_kbo_situational_page_yields_base_state_counts():
    states = _batter_base_states(SITUATION_HTML)
    assert _hitter_name(SITUATION_HTML) == "테스트타자"
    assert states["BASES_EMPTY"]["at_bats"] == 196
    assert states["SCORING_POSITION"] == {
        "at_bats": 103, "hits": 38, "doubles": 7, "triples": 0, "home_runs": 7, "walks": 17,
        "hit_by_pitch": 0, "strikeouts": 17, "sacrifice_flies": 0, "grounded_into_double_play": 6,
    }
    # Rows the page does not carry are simply absent rather than filled in with zeros.
    assert "RUNNER_23" not in states


def test_batter_table_uses_real_splits_and_shrinks_thin_samples():
    table = build_batter_table(_batter_base_states(SITUATION_HTML))
    empty, loaded = table[STATE_INDEX["BASES_EMPTY"]], table[STATE_INDEX["BASES_LOADED"]]
    assert abs(table.sum(axis=1) - 1).max() < 1e-9
    # 196 at-bats of bases-empty data survives nearly intact: 31 singles in 219 plate appearances.
    assert abs(empty[SINGLE] - 31 / 219) < .01
    # This hitter really is better with a runner in scoring position, and that carries through.
    assert table[STATE_INDEX["RUNNER_2"]][SINGLE] > empty[SINGLE]
    # Eleven bases-loaded at-bats cannot outvote the scoring-position sample behind it.
    raw_loaded_single = 2 / 12
    assert abs(loaded[SINGLE] - raw_loaded_single) > abs(loaded[SINGLE] - table[STATE_INDEX["RUNNER_2"]][SINGLE])
    # A hitter with almost no season is not modelled at all rather than modelled badly.
    assert build_batter_table({"BASES_EMPTY": {"at_bats": 8, "hits": 3}}) is None


def test_plate_engine_matches_expected_runs_and_obeys_the_ninth_inning_rules():
    table = build_batter_table(_batter_base_states(SITUATION_HTML))
    lineup = np.stack([table] * 9)
    staff = {"starter_multiplier": 1.0, "starter_innings": 5.3,
             "bullpen": {"high_leverage": .82, "middle": 1.0, "chase": 1.12, "mop_up": 1.28}}
    mlb = simulate_scores(4.8, 4.2, 20_000, 21, league="MLB", home_staff=staff, away_staff=staff,
                          home_lineup=lineup, away_lineup=lineup)
    kbo = simulate_scores(4.8, 4.2, 20_000, 21, league="KBO", home_staff=staff, away_staff=staff,
                          home_lineup=lineup, away_lineup=lineup)
    assert mlb["engine"] == "PLATE_APPEARANCE"
    # Batter-level play still lands on the run totals the team model expects. A club's season
    # rate counts extra-inning runs, so the full-game mean is the figure being matched.
    for result in (mlb, kbo):
        assert abs(result["mean_runs"]["home"] / 4.8 - 1) < .03
        assert abs(result["mean_runs"]["away"] / 4.2 - 1) < .03
        assert result["regulation_mean_runs"]["home"] <= result["mean_runs"]["home"]
    assert mlb["tie_probability"] == 0
    assert kbo["tie_probability"] > 0
    # The home club skips the ninth whenever it is already ahead, close to the real rate.

    # One lineup without collected splits is enough to fall back to the inning-rate model.
    assert simulate_scores(4.8, 4.2, 5_000, 21, league="MLB", home_lineup=lineup)["engine"] == "INNING_RATE"


def test_split_backfill_stays_inside_one_refresh_budget():
    """A full slate must not spend the whole serverless invocation fetching hitters."""
    fetched: list[int] = []

    class Client:
        def batter_splits(self, ids, season):
            fetched.append(len(ids))
            return SourcePayload({}, "url", datetime(2026, 8, 23))

    budget = _split_budget()
    errors: list[str] = []
    for game in range(15):
        entries = [{"player_id": f"g{game}-p{index}"} for index in range(18)]
        _collect_batter_splits(Client(), entries, 2026, "MLB", errors, budget)
    assert 0 < sum(fetched) <= SPLIT_FETCH_BUDGET
    # Once the budget is gone the remaining games stop calling out entirely.
    assert budget["remaining"] == 0
    _collect_batter_splits(Client(), [{"player_id": "later"}], 2026, "MLB", errors, budget)
    assert sum(fetched) <= SPLIT_FETCH_BUDGET


def test_optional_enrichment_degrades_instead_of_failing_the_refresh():
    errors: list[str] = []
    # A table that has not been migrated yet must not take the whole slate down with it.
    missing_table = OperationalError("select 1", {}, Exception("no such table: team_bullpen"))
    assert _optional(lambda: (_ for _ in ()).throw(missing_table), {}, "bullpen profiles", errors) == {}
    assert errors and "bullpen profiles" in errors[0]
    # A real programming error is not a database problem and must still surface.
    with pytest.raises(ZeroDivisionError):
        _optional(lambda: 1 / 0, {}, "bullpen profiles", errors)
    assert _optional(lambda: {"ok": 1}, {}, "bullpen profiles", errors) == {"ok": 1}


def test_lifecycle_event_type_column_fits_every_decision_constant():
    """WAITING_FOR_LIVE_VALIDATION (27 chars) once overflowed a varchar(24) column and crashed
    every lifecycle evaluation for a league without 40+ live samples. Guard the real constants
    used in run_model_lifecycle against whatever width the column currently has."""
    decision_constants = ["WAITING_FOR_DATA", "WAITING_FOR_LIVE_VALIDATION", "NO_NEW_DATA",
                         "PROMOTED", "REJECTED", "ROLLED_BACK"]
    column_length = ModelLifecycleEvent.__table__.c.event_type.type.length
    for value in decision_constants:
        assert len(value) <= column_length, f"{value} ({len(value)} chars) exceeds column width {column_length}"


def test_model_training_uses_audited_historical_holdout_until_live_sample_is_large_enough():
    start = datetime(2026, 1, 1)
    historical = [{"origin": "HISTORICAL_REPLAY", "captured_at": start + timedelta(days=index)}
                  for index in range(200)]
    validation, source = _validation_partition(historical)
    assert len(validation) == 40
    assert validation[0]["captured_at"] == start + timedelta(days=160)
    assert source == "LEAKAGE_AUDITED_CHRONOLOGICAL_HOLDOUT"

    live = [{"origin": "LIVE_PREGAME", "captured_at": start + timedelta(days=200 + index)}
            for index in range(50)]
    validation, source = _validation_partition(historical + live)
    assert len(validation) == 40
    assert all(row["origin"] == "LIVE_PREGAME" for row in validation)
    assert source == "LIVE_PREGAME_CHRONOLOGICAL_HOLDOUT"


def test_mlb_never_reports_a_tied_score_and_branches_beat_the_raw_mode():
    result = simulate_scores(4.6, 4.2, 20_000, 42, league="MLB")
    # No MLB game can end level, so nothing in the payload may describe one.
    assert result["tie_probability"] == 0
    assert "TIE" not in result["outcome_scores"]
    assert all(score["home"] != score["away"] for score in result["top_scores"])
    home_best = result["outcome_scores"]["HOME_WIN"][0]
    away_best = result["outcome_scores"]["AWAY_WIN"][0]
    assert home_best["home"] > home_best["away"]
    assert away_best["away"] > away_best["home"]
    # Conditioning on the outcome concentrates the estimate: the raw mode holds only a few
    # percent of all games, while the branch score holds far more of its own branch.
    assert home_best["probability_given_outcome"] > result["top_scores"][0]["probability"]
    # KBO can end level, so its draw branch is present and its scores are genuinely tied.
    kbo = simulate_scores(4.6, 4.2, 20_000, 42, league="KBO")
    assert kbo["tie_probability"] > 0
    assert all(score["home"] == score["away"] for score in kbo["outcome_scores"]["TIE"])


def test_simulated_run_distribution_tracks_real_results():
    """The fitted dispersion must keep the run histogram close to real team-game outcomes."""
    # Shape of ~3,900 real MLB team-games: mode at 2-3 runs with a long right tail.
    observed = [.064, .110, .140, .135, .120, .106, .094, .065, .053, .039, .026]
    result = simulate_scores(4.47, 4.47, 40_000, 11, league="MLB", team_variance=.26)
    simulated = result["team_run_distribution"]["away"][:len(observed)]
    total_variation = sum(abs(a - b) for a, b in zip(simulated, observed, strict=True)) / 2
    assert total_variation < .06
    # The old hand-picked dispersion was materially worse against the same data.
    legacy = simulate_scores(4.47, 4.47, 40_000, 11, league="MLB", team_variance=.11)
    legacy_variation = sum(abs(a - b) for a, b in
                           zip(legacy["team_run_distribution"]["away"][:len(observed)], observed, strict=True)) / 2
    assert total_variation < legacy_variation


def test_dense_intervals_are_tighter_than_central_80_band():
    result = simulate_scores(5.2, 4.1, 20_000, 42)
    for interval, quantiles in (
        (result["total_dense_interval"], result["total_quantiles"]),
        (result["team_dense_intervals"]["away"], result["team_quantiles"]["away"]),
        (result["team_dense_intervals"]["home"], result["team_quantiles"]["home"]),
    ):
        assert interval["low"] <= interval["high"]
        assert interval["mass"] >= .60
        assert interval["high"] - interval["low"] <= quantiles["p90"] - quantiles["p10"]
    # The mode always sits inside its own highest-density interval.
    mode_total = result["simulation_modes"]["total_runs"]["value"]
    assert result["total_dense_interval"]["low"] <= mode_total <= result["total_dense_interval"]["high"]


def test_mlb_extra_innings_always_decide_the_game():
    result = simulate_scores(4.6, 4.4, 20_000, 7, league="MLB")
    assert result["extra_innings"]["rule"] == "MLB_GHOST_RUNNER_UNTIL_DECIDED"
    assert result["tie_probability"] == 0
    assert abs(result["home_win_probability"] + result["away_win_probability"] - 1) < 1e-9
    assert result["home_two_way_probability"] == result["home_win_probability"]
    assert result["extra_innings"]["probability"] > 0
    assert result["simulation_modes"]["outcome"]["value"] in ("HOME_WIN", "AWAY_WIN")
    for score in result["top_scores"]:
        assert score["home"] != score["away"]
        assert sum(item["home"] for item in score["inning_line"]) == score["home"]
        assert sum(item["away"] for item in score["inning_line"]) == score["away"]


def test_kbo_plays_to_eleven_and_keeps_ties():
    result = simulate_scores(5.2, 5.0, 20_000, 7, league="KBO")
    assert result["extra_innings"]["rule"] == "KBO_MAX_11_TIES_STAND"
    assert result["extra_innings"]["probability"] > 0
    assert result["tie_probability"] > 0
    # Ties that survive inning 11 are excluded from the two-way market, not split.
    decided = result["home_win_probability"] + result["away_win_probability"]
    assert abs(decided + result["tie_probability"] - 1) < 1e-9
    assert abs(result["home_two_way_probability"] - result["home_win_probability"] / decided) < 1e-9
    # No trajectory may exceed eleven innings without a tiebreaker.
    assert all(len(score["inning_line"]) <= 11 for score in result["top_scores"])


def test_classifier_blend_flips_marginal_runs_favorite_when_records_disagree():
    # Runs model barely favors home (5.54 vs 5.43) but the classifier strongly favors away
    # (better record, form, and starter) - mirroring the LG @ Hanwha case.
    home_runs, away_runs = blend_classifier_into_means(.399, 5.54, 5.43)
    assert home_runs < away_runs
    # The expected total is preserved by the tilt.
    assert abs((home_runs + away_runs) - (5.54 + 5.43)) < 1e-9
    # When both signals agree, the tilt is small and direction is unchanged.
    agree_home, agree_away = blend_classifier_into_means(.62, 5.2, 4.4)
    assert agree_home > agree_away
    assert abs(agree_home - 5.2) < .45
    # A neutral classifier pulls an extreme runs edge only modestly, never past even.
    tempered_home, tempered_away = blend_classifier_into_means(.5, 6.9, 2.6)
    assert tempered_home > tempered_away
    assert tempered_home < 6.9


def test_score_estimates_combine_full_population_mean_mode_and_projection():
    top_scores = [
        {"away": 3, "home": 4, "count": 1200, "probability": .06},
        {"away": 4, "home": 3, "count": 1000, "probability": .05},
        {"away": 3, "home": 3, "count": 800, "probability": .04},
        {"away": 2, "home": 4, "count": 600, "probability": .03},
        {"away": 4, "home": 4, "count": 400, "probability": .02},
    ]
    representative = {**top_scores[0], "population_coverage": 1.0, "projects_favorite_cover": True}
    estimates = build_score_estimates(top_scores, representative, home_expected=4.27, away_expected=3.61)
    assert estimates["headline"] == "MEAN"
    assert estimates["mean"] == {"away": 3.6, "home": 4.3}
    assert estimates["mode"] == {"away": 3, "home": 4, "count": 1200, "probability": .06}
    assert estimates["representative"]["population_coverage"] == 1.0
    assert estimates["representative"]["projects_favorite_cover"] is True


def test_claude_advice_cannot_overpower_statistical_baseline():
    probability, home_runs, away_runs, weight = blend_with_claude(.55, 4.8, 4.2, {
        "home_win_probability": 1.0,
        "home_expected_runs": 20.0,
        "away_expected_runs": -5.0,
        "confidence": 100,
    })
    assert 0 <= weight <= .25
    assert .55 <= probability <= .58
    assert 4.8 <= home_runs <= 5.1
    assert 3.9 <= away_runs <= 4.2


def test_confirmed_lineup_change_creates_new_prediction_input():
    home_team, away_team = SimpleNamespace(name="Home"), SimpleNamespace(name="Away")
    recent = {"10": {"games": 10, "win_rate": .5}}
    home = SimpleNamespace(team=home_team, recent=recent, win_rate=.55, home_win_rate=.58, runs_per_game=4.8,
                           runs_allowed_per_game=4.2, ops=.750, era=4.10)
    away = SimpleNamespace(team=away_team, recent=recent, win_rate=.50, away_win_rate=.48, runs_per_game=4.4,
                           runs_allowed_per_game=4.5, ops=.720, era=4.40)
    pitcher = lambda player_id: SimpleNamespace(player_id=player_id, name=player_id, confirmed=True, era=4.0, whip=1.3, war=1.0)
    game = SimpleNamespace(external_id="MLB-1", league="MLB", stadium="Yankee Stadium")
    base = [SimpleNamespace(side=side, batting_order=i, player_id=f"{side}-{i}", player_name=f"P{i}",
                            value=.700, value_metric="OPS", confirmed=True) for side in ("away", "home") for i in range(1, 10)]
    changed = list(base)
    changed[-1] = SimpleNamespace(side="home", batting_order=9, player_id="replacement", player_name="Replacement",
                                  value=.900, value_metric="OPS", confirmed=True)
    before = predict_game(game, home, away, pitcher("home-p"), pitcher("away-p"), base)
    after = predict_game(game, home, away, pitcher("home-p"), pitcher("away-p"), changed)
    assert before["input_hash"] != after["input_hash"]
    assert before["home_expected_runs"] != after["home_expected_runs"]
    assert after["confidence"] >= 80
    score = after["payload"]["display_expected_score"]
    assert after["expected_total"] == round(score["away"] + score["home"], 1)
    assert after["statistical_expected_total"] == round(after["home_expected_runs"] + after["away_expected_runs"], 2)
    assert after["payload"]["summary_schema_version"] == SIMULATION_SUMMARY_SCHEMA_VERSION
    assert after["payload"]["coherence_valid"] is True
    assert any(
        score["away"] == after["payload"]["primary_score"]["away"]
        and score["home"] == after["payload"]["primary_score"]["home"]
        for score in after["payload"]["projected_score_candidates"]
    )
    assert after["payload"]["primary_score"]["selection_method"] == "FULL_DISTRIBUTION_PROJECTION_V2"
    assert after["payload"]["primary_score"]["population_coverage"] == 1.0
    estimates = after["payload"]["score_estimates"]
    # Hybrid headline: the displayed score is the distribution mean, while the exact-score
    # mode and the complete-distribution integer projection travel alongside it for the UI.
    assert after["payload"]["display_expected_score"] == estimates["mean"]
    assert estimates["headline"] == "MEAN"
    assert estimates["mode"]["away"] == after["payload"]["top_scores"][0]["away"]
    assert estimates["mode"]["home"] == after["payload"]["top_scores"][0]["home"]
    assert 0 < estimates["mode"]["probability"] <= 1
    assert estimates["representative"]["away"] == after["payload"]["primary_score"]["away"]
    assert estimates["representative"]["home"] == after["payload"]["primary_score"]["home"]
    assert estimates["representative"]["population_coverage"] == 1.0
    assert after["home_win_probability"] == after["payload"]["simulation_home_probability"]
    assert after["away_win_probability"] == round(1 - after["home_win_probability"], 4)


def test_integer_projection_uses_full_distribution_and_respects_run_line_majority():
    strong = simulate_scores(6.8, 3.1, 20_000, 20260824, league="MLB")
    primary = strong["projected_score"]
    assert primary["population_coverage"] == 1.0
    assert primary["projects_favorite_cover"] is True
    assert primary["home"] - primary["away"] >= 2
    assert len(strong["projected_score_candidates"]) >= 3
    assert all(score["selection_method"] == "FULL_DISTRIBUTION_PROJECTION_V2"
               for score in strong["projected_score_candidates"])

    close = simulate_scores(4.5, 4.4, 20_000, 20260824, league="MLB")
    close_primary = close["projected_score"]
    favorite_margin = (close_primary["home"] - close_primary["away"]
                       if close["home_two_way_probability"] >= .5
                       else close_primary["away"] - close_primary["home"])
    expected_minimum = 2 if close_primary["projects_favorite_cover"] else 1
    assert favorite_margin >= expected_minimum


def test_ui_registered_secret_is_encrypted_and_requires_same_master_key():
    raw = "sk-ant-test-key-that-must-never-be-returned"
    encrypted = encrypt_secret(raw, "test-master-secret-long-enough")
    assert raw not in encrypted
    assert decrypt_secret(encrypted, "test-master-secret-long-enough") == raw
    with pytest.raises(RuntimeError):
        decrypt_secret(encrypted, "different-test-master-secret")


def test_user_claude_model_is_stored_per_user(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(runtime_secrets, "encrypt_secret", lambda value: f"encrypted:{value}")
    monkeypatch.setattr(runtime_secrets, "decrypt_secret", lambda value: value.removeprefix("encrypted:"))
    with Session(engine) as session:
        status = runtime_secrets.save_user_claude_key(
            session, "user-1", "sk-ant-test-key-that-is-long-enough", "claude-sonnet-5", enabled=True,
        )
        row = session.get(UserClaudeSetting, "user-1")
        assert row is not None
        assert row.model == "claude-sonnet-5"
        assert status["model"] == "claude-sonnet-5"
        assert "api_key" not in status
        runtime_secrets.save_user_claude_key(
            session, "user-2", "sk-ant-second-user-key-long-enough", "claude-opus-4", enabled=False,
        )
        assert session.get(UserClaudeSetting, "user-2").fingerprint != row.fingerprint
        assert runtime_secrets.user_claude_configuration(session, "user-1")["api_key"] == "sk-ant-test-key-that-is-long-enough"
        assert runtime_secrets.user_claude_configuration(session, "user-2")["api_key"] == "sk-ant-second-user-key-long-enough"


def test_user_auth_resolves_identity_from_supabase(monkeypatch):
    monkeypatch.setattr(user_auth, "settings", SimpleNamespace(
        supabase_url="https://project.supabase.co", supabase_publishable_key="publishable-key",
    ))

    def fake_get(url, headers, timeout):
        assert url == "https://project.supabase.co/auth/v1/user"
        assert headers["Authorization"] == "Bearer access-token"
        assert headers["apikey"] == "publishable-key"
        assert timeout == 10.0
        return httpx.Response(200, json={"id": "user-1", "email": "one@example.com"})

    monkeypatch.setattr(user_auth.httpx, "get", fake_get)
    assert user_auth.require_user("Bearer access-token").id == "user-1"
    with pytest.raises(HTTPException) as exc_info:
        user_auth.require_user(None)
    assert exc_info.value.status_code == 401


def test_personal_claude_analysis_never_mutates_shared_prediction(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        game = Game(external_id="PRIVATE-1", league="KBO", game_date=date(2026, 8, 23),
                    away_team_id=away.id, home_team_id=home.id, status="SCHEDULED",
                    source="test", source_url="test", collected_at=datetime.now())
        model = ModelVersion(name="SHARED", algorithm="baseline", feature_schema={}, checksum="shared")
        session.add_all([game, model]); session.flush()
        prediction = Prediction(
            game_id=game.id, model_version_id=model.id, input_hash="shared-input", home_win_probability=.6,
            away_win_probability=.4, home_expected_runs=5.2, away_expected_runs=4.1, confidence=80,
            payload={"features": {"ops_diff": .05}, "league_average_runs": 4.8}, created_at=datetime.now(),
        )
        session.add(prediction); session.commit()

        monkeypatch.setattr(personal_claude, "user_claude_configuration", lambda _session, user_id: {
            "configured": True, "enabled": True, "source": "user", "fingerprint": user_id,
            "updated_at": None, "model": "claude-test", "api_key": f"key-{user_id}", "error": None,
        })
        monkeypatch.setattr(personal_claude, "claude_prediction_advice", lambda _key, _context, _config: ({
            "home_win_probability": .68, "home_expected_runs": 5.8, "away_expected_runs": 3.8,
            "confidence": 80, "reasons": ["개인 분석"], "caution": "표본 주의",
        }, {"model": "claude-test", "status": "applied", "usage": {"input_tokens": 10, "output_tokens": 5}}))

        result = personal_claude.analyze_game_for_user(session, "user-1", "PRIVATE-1")
        assert result["personalized"]["home_win_probability"] > prediction.home_win_probability
        assert result["reasons"] == ["개인 분석"]
        assert len(session.query(Prediction).all()) == 1
        assert prediction.home_win_probability == .6


def test_claude_advisor_uses_ui_runtime_key_without_exposing_it(monkeypatch):
    observed: dict[str, str] = {}

    def fake_request(_context, api_key, model):
        observed.update(api_key=api_key, model=model)
        return {
            "home_win_probability": .55, "home_expected_runs": 4.8, "away_expected_runs": 4.2,
            "confidence": 70, "reasons": ["test"], "caution": "test",
        }, {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(claude_advisor, "_request_advice", fake_request)
    claude_advisor.clear_claude_cache()
    advice, metadata = claude_advisor.claude_prediction_advice("ui-key-test", {}, {
        "configured": True, "enabled": True, "source": "user", "fingerprint": "abcdef123456",
        "updated_at": None, "model": "claude-sonnet-5", "api_key": "sk-ant-runtime-secret", "error": None,
    })
    assert advice is not None
    assert observed == {"api_key": "sk-ant-runtime-secret", "model": "claude-sonnet-5"}
    assert metadata["key_source"] == "user"
    assert "api_key" not in metadata


def test_prediction_stage_and_change_reason_are_explicit():
    start = datetime(2026, 8, 22, 18, 30, tzinfo=KST)
    game = SimpleNamespace(start_at=start)
    assert _prediction_stage(game, start - timedelta(hours=25)) == "T_MINUS_24H"
    assert _prediction_stage(game, start - timedelta(hours=3)) == "T_MINUS_3H"
    assert _prediction_stage(game, start - timedelta(minutes=55)) == "T_MINUS_60M"
    assert _prediction_stage(game, start - timedelta(minutes=10)) == "T_MINUS_15M"
    old = {"pitchers": [["p1"]], "lineups": [], "features": {"ops_diff": .01}}
    new = {"pitchers": [["p2"]], "lineups": [], "features": {"ops_diff": .02}}
    changes = _prediction_changes(old, new)
    assert {row["type"] for row in changes} == {"STARTER", "STATS"}


def test_walk_forward_backtest_excludes_post_start_prediction():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        start = datetime(2026, 8, 22, 18, 30)
        game = Game(external_id="TEST-GAME", league="KBO", game_date=date(2026, 8, 22), start_at=start,
                    away_team_id=away.id, home_team_id=home.id, status="FINAL", source="test",
                    source_url="test", collected_at=start)
        model = ModelVersion(name="TEST", algorithm="test", feature_schema={}, checksum="x")
        session.add_all([game, model]); session.flush()
        before = Prediction(game_id=game.id, model_version_id=model.id, input_hash="before", home_win_probability=.6,
                            away_win_probability=.4, home_expected_runs=5, away_expected_runs=4, confidence=80,
                            payload={"team_quantiles": {
                                "home": {"p10": 2, "p90": 7}, "away": {"p10": 1, "p90": 6},
                            }}, created_at=start - timedelta(minutes=15))
        after = Prediction(game_id=game.id, model_version_id=model.id, input_hash="after", home_win_probability=.99,
                           away_win_probability=.01, home_expected_runs=9, away_expected_runs=1, confidence=99,
                           payload={}, created_at=start + timedelta(minutes=5))
        session.add_all([before, after]); session.flush()
        session.add(PredictionSnapshot(game_id=game.id, prediction_id=before.id, stage="T_MINUS_15M",
                                       trigger="checkpoint_exact", minutes_to_start=15, input_hash="before",
                                       input_payload={}, changes=[], captured_at=start - timedelta(minutes=15)))
        session.add(GameResult(game_id=game.id, away_score=3, home_score=5, finalized_at=start + timedelta(hours=3), source_url="test"))
        session.commit()
        report = walk_forward_backtest(session, "KBO", "T_MINUS_15M")
        assert report["sample_size"] == 1
        assert report["metrics"]["brier_score"] == .16
        assert report["metrics"]["runs_mae"] == .5
        assert report["metrics"]["runs_rmse"] == .7071
        assert report["metrics"]["predicted_team_score_sd"] == .5
        assert report["metrics"]["actual_team_score_sd"] == 1.0
        assert report["metrics"]["runs_p10_p90_coverage"] == 1.0
        assert report["team_residual_walk_forward"]["official_live"]["sample_size"] == 1


def test_kbo_residual_calibration_separates_venue_shrinks_matchups_and_respects_cutoff():
    observations = []
    start = datetime(2026, 8, 1, 18, 30)
    for index in range(12):
        team_home = index % 2 == 0
        observations.append(ResidualObservation(
            game_id=index + 1, started_at=start + timedelta(days=index),
            # Deliberately later collection times emulate a historical bulk import.
            finalized_at=datetime(2026, 8, 23, 12, 0),
            home_team_id=1 if team_home else 2, away_team_id=2 if team_home else 1,
            home_expected=5.0, away_expected=5.0,
            home_actual=7 if team_home else 5, away_actual=5 if team_home else 4,
        ))
    context = residual_context(observations, 1, 3, date(2026, 8, 23), force_enabled=True)
    assert context["enabled"] is True
    assert context["home"]["venue_offense"] > 0
    assert context["home"]["venue_games"] == 6
    # No games were against team 3, so matchup carry-over is exactly neutral.
    assert context["home"]["matchup"] == 0
    adjusted = apply_residual_adjustment(5.0, 5.0, context)
    # Recent form is already in the base model, so the validated residual layer mean-reverts
    # unexplained over-performance instead of counting the same hot streak twice.
    assert adjusted[0] < 5.0
    assert abs(adjusted[0] - 5.0) <= .45
    assert available_before(observations[-1], datetime(2026, 8, 23, 18, 30)) is True

    same_day = ResidualObservation(
        game_id=99, started_at=datetime(2026, 8, 23, 14, 0),
        finalized_at=datetime(2026, 8, 23, 19, 0), home_team_id=1, away_team_id=2,
        home_expected=5, away_expected=5, home_actual=10, away_actual=0,
    )
    history = TeamResidualHistory(observations + [same_day])
    target = SimpleNamespace(league="KBO", game_date=date(2026, 8, 23),
                             start_at=datetime(2026, 8, 23, 18, 30), home_team_id=1, away_team_id=3)
    assert history.context_for(target)["source_game_count"] == 12


def test_mlb_residual_calibration_activates_from_replay_history():
    game = SimpleNamespace(league="MLB", game_date=date(2026, 8, 23),
                           start_at=datetime(2026, 8, 23, 10, 0), home_team_id=1, away_team_id=2)
    context = TeamResidualHistory([]).context_for(game)
    assert context["enabled"] is True
    assert context["league"] == "MLB"
    assert context["source_game_count"] == 0
    assert apply_residual_adjustment(4.5, 4.2, context) == (4.5, 4.2)


def test_mlb_opponent_residual_persists_after_strong_sample_shrinkage():
    start = datetime(2026, 4, 1, 18, 30)
    observations = [ResidualObservation(
        game_id=index + 1, started_at=start + timedelta(days=index),
        finalized_at=start + timedelta(days=index, hours=3),
        home_team_id=1, away_team_id=2, home_expected=4.5, away_expected=4.5,
        home_actual=7, away_actual=4,
    ) for index in range(20)]
    versus_repeat = residual_context(
        observations, 1, 2, date(2026, 8, 23), force_enabled=True, league="MLB",
    )
    assert versus_repeat["home"]["matchup_games"] == 20
    assert versus_repeat["home"]["matchup"] > 0
    assert versus_repeat["matchup_persistence_weight"] > 0
    general_only = versus_repeat["mean_reversion_weight"] * (
        .70 * versus_repeat["home"]["offense"] - .30 * versus_repeat["away"]["defense"]
    )
    with_matchup = general_only + (
        versus_repeat["matchup_persistence_weight"] * versus_repeat["home"]["matchup"]
    )
    assert with_matchup > general_only


def test_structural_residual_needs_twenty_matching_regime_games():
    start = datetime(2026, 4, 1, 18, 30)
    observations = []
    for index in range(40):
        plate = index < 20
        observations.append(ResidualObservation(
            game_id=index + 1, started_at=start + timedelta(days=index),
            finalized_at=start + timedelta(days=index, hours=3),
            home_team_id=1, away_team_id=2, home_expected=5, away_expected=5,
            home_actual=7 if plate else 3, away_actual=5,
            engine="PLATE_APPEARANCE" if plate else "INNING_RATE",
            confirmation="CONFIRMED" if plate else "PARTIAL", scoring_band="MID", season_phase="EARLY",
        ))
    regime = {"engine": "PLATE_APPEARANCE", "confirmation": "CONFIRMED",
              "scoring_band": "MID", "season_phase": "EARLY"}
    collecting = residual_context(observations[1:], 1, 2, date(2026, 5, 20),
                                  force_enabled=True, regime=regime)
    assert collecting["home"]["structure_games"] == 19
    assert collecting["home"]["structure"] == 0
    active = residual_context(observations, 1, 2, date(2026, 5, 20),
                              force_enabled=True, regime=regime)
    assert active["home"]["structure_games"] == 20
    assert active["home"]["structure"] > 0


def test_schedule_context_uses_only_prior_final_games_and_computes_travel():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        team = Team(league="MLB", code="1", name="One")
        opponents = [Team(league="MLB", code=str(index), name=str(index)) for index in (2, 3, 4)]
        session.add_all([team, *opponents]); session.flush()
        previous = Game(
            external_id="P", league="MLB", game_date=date(2026, 8, 22), start_at=datetime(2026, 8, 22, 10),
            away_team_id=team.id, home_team_id=opponents[0].id, status="FINAL", source="test", source_url="test",
            collected_at=datetime(2026, 8, 22, 14),
            pregame_context={"venue": {"latitude": 40.0, "longitude": -75.0}},
        )
        current = Game(
            external_id="C", league="MLB", game_date=date(2026, 8, 23), start_at=datetime(2026, 8, 23, 10),
            away_team_id=opponents[1].id, home_team_id=team.id, status="SCHEDULED", source="test", source_url="test",
            collected_at=datetime(2026, 8, 23, 1),
            pregame_context={"venue": {"latitude": 41.0, "longitude": -74.0}},
        )
        future = Game(
            external_id="F", league="MLB", game_date=date(2026, 8, 24), start_at=datetime(2026, 8, 24, 10),
            away_team_id=team.id, home_team_id=opponents[2].id, status="FINAL", source="test", source_url="test",
            collected_at=datetime(2026, 8, 24, 14),
        )
        session.add_all([previous, current, future]); session.flush()
        context = prediction_context(session, current)
        assert context["schedule"]["home"]["games_last_3d"] == 1
        assert context["schedule"]["home"]["travel_km"] > 100
        assert context["availability"]["schedule"] is True


def test_side_specific_residual_volatility_widens_the_inning_simulation():
    low = simulate_scores(5.0, 5.0, 30_000, 20260823, league="KBO",
                          home_team_variance=.04, away_team_variance=.04)
    high = simulate_scores(5.0, 5.0, 30_000, 20260823, league="KBO",
                           home_team_variance=.30, away_team_variance=.04)
    low_width = low["team_quantiles"]["home"]["p90"] - low["team_quantiles"]["home"]["p10"]
    high_width = high["team_quantiles"]["home"]["p90"] - high["team_quantiles"]["home"]["p10"]
    assert high_width > low_width

    table = build_batter_table(_batter_base_states(SITUATION_HTML))
    lineup = np.stack([table] * 9)
    plate_low = simulate_scores(5.0, 5.0, 8_000, 20260823, league="KBO",
                                home_lineup=lineup, away_lineup=lineup,
                                home_team_variance=.04, away_team_variance=.04)
    plate_high = simulate_scores(5.0, 5.0, 8_000, 20260823, league="KBO",
                                 home_lineup=lineup, away_lineup=lineup,
                                 home_team_variance=.30, away_team_variance=.04)
    def home_score_sd(result):
        counts = [(int(score.split(":")[1]), count)
                  for score, count in result["frequency_tables"]["scores"].items()]
        size = sum(count for _, count in counts)
        mean = sum(score * count for score, count in counts) / size
        return (sum((score - mean) ** 2 * count for score, count in counts) / size) ** .5

    assert home_score_sd(plate_high) > home_score_sd(plate_low)
    assert abs(plate_high["mean_runs"]["home"] / 5.0 - 1) < .03


def test_startup_bootstrap_only_requests_missing_leagues():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 8, 22)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home])
        session.flush()
        session.add(Game(external_id="KBO-BOOTSTRAP", league="KBO", game_date=target,
                         away_team_id=away.id, home_team_id=home.id, status="SCHEDULED",
                         source="test", source_url="test", collected_at=datetime.now()))
        session.commit()
        assert _missing_leagues_for_date(session, target) == {"MLB"}


def test_kbo_pitcher_opponent_split_parses_baseball_innings_and_whip():
    html = """
    <table summary="상대팀별 기록으로 경기,평균자책점"><tbody><tr>
      <td>키움</td><td>3</td><td>2.45</td><td>1</td><td>0</td><td>0</td><td>0</td><td>1.000</td>
      <td>50</td><td>14 2/3</td><td>12</td><td>1</td><td>4</td><td>0</td><td>13</td><td>4</td><td>4</td><td>.226</td>
    </tr></tbody></table>
    """
    split = _pitcher_opponent_split(html, "키움")
    assert split["opponent_games"] == 3
    assert round(split["opponent_innings"], 3) == 14.667
    assert split["opponent_whip"] == 1.091


def test_batter_vs_pitcher_ops_is_shrunk_by_plate_appearances():
    small = SimpleNamespace(value=.750, matchup_ops=1.300, matchup_plate_appearances=5)
    large = SimpleNamespace(value=.750, matchup_ops=1.300, matchup_plate_appearances=150)
    assert .750 < _effective_lineup_ops(small) < _effective_lineup_ops(large) < 1.300


def test_kbo_batter_pitcher_table_and_lineup_summary_are_sample_aware():
    html = """
    <table class="tData"><thead><tr><th>AVG</th><th>PA</th><th>AB</th><th>H</th>
      <th>2B</th><th>3B</th><th>HR</th><th>RBI</th><th>BB</th><th>HBP</th><th>SO</th>
      <th>SLG</th><th>OBP</th><th>OPS</th></tr></thead>
      <tbody><tr><td>.500</td><td>8</td><td>6</td><td>3</td><td>1</td><td>0</td><td>1</td>
      <td>2</td><td>2</td><td>0</td><td>1</td><td>1.167</td><td>.625</td><td>1.792</td></tr></tbody>
    </table>
    """
    split = _batter_pitcher_split(html)
    assert split["matchup_plate_appearances"] == 8
    assert split["matchup_home_runs"] == 1
    assert split["matchup_ops"] == 1.792
    adjustment, coverage, plate_appearances = _lineup_matchup_summary([
        SimpleNamespace(matchup_ops=split["matchup_ops"], matchup_plate_appearances=8),
        *[SimpleNamespace(matchup_ops=None, matchup_plate_appearances=None) for _ in range(8)],
    ])
    assert 0 < adjustment < .04
    assert coverage == 1
    assert plate_appearances == 8


def test_market_consensus_removes_two_way_margin_and_uses_median_lines():
    event = {
        "id": "event-1", "home_team": "KIA Tigers", "away_team": "Kiwoom Heroes",
        "bookmakers": [
            {"key": "a", "markets": [
                {"key": "h2h", "outcomes": [{"name": "KIA Tigers", "price": 1.8}, {"name": "Kiwoom Heroes", "price": 2.1}]},
                {"key": "totals", "outcomes": [{"name": "Over", "point": 8.5}, {"name": "Under", "point": 8.5}]},
                {"key": "spreads", "outcomes": [{"name": "KIA Tigers", "point": -1.5}, {"name": "Kiwoom Heroes", "point": 1.5}]},
            ]},
            {"key": "b", "markets": [
                {"key": "h2h", "outcomes": [{"name": "KIA Tigers", "price": 1.9}, {"name": "Kiwoom Heroes", "price": 2.0}]},
                {"key": "totals", "outcomes": [{"name": "Over", "point": 9.5}, {"name": "Under", "point": 9.5}]},
                {"key": "spreads", "outcomes": [{"name": "KIA Tigers", "point": -1.5}, {"name": "Kiwoom Heroes", "point": 1.5}]},
            ]},
        ],
    }
    row = _consensus_event(event)
    assert row["bookmaker_count"] == 2
    assert row["total_line"] == 9.0
    # The home team's median run-line point: negative means the market's -1.5 favorite is home.
    assert row["home_spread"] == -1.5
    assert abs(row["home_implied_probability"] + row["away_implied_probability"] - 1) < 1e-9
