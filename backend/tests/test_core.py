from __future__ import annotations

from datetime import date, datetime, timedelta
from contextlib import contextmanager
import json
import logging
from statistics import median
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
from backend.app.models import (Game, GameResult, GameStarter, LineupEntry, MarketSnapshot, ModelVersion, PitcherStat, Prediction, PredictionEvaluation, PredictionSnapshot, Team,
                                ModelLifecycleEvent, TeamBullpenEvent, UserClaudeSetting)
from backend.app.repositories.repository import (_market_snapshot_stage, _prediction_changes, game_cards,
                                                  game_dates, upsert_game, upsert_market_consensus)
from backend.app.services.archived_starters import _totals_before, starter_view
from backend.app.services.backtest import _walk_forward_probability, walk_forward_backtest
from backend.app.services.bullpen import apply_profile_update, derive_profile, load_profiles, seed_league
from backend.app.services import claude_advisor, personal_claude, runtime_secrets, user_auth
from backend.app.services.claude_advisor import blend_with_claude
from backend.app.collectors.kbo.client import (KBO_BASE_STATES, KboClient, _batter_base_states,
                                               _batter_pitcher_split, _data_id_table, _hitter_name,
                                               _pitcher_daily_log, _pitcher_log_summary,
                                               _pitcher_opponent_split,
                                               _rank_table, _record_rate,
                                               _scoreboard_innings, _flag)
from backend.app.collectors.kbo.client import SourcePayload
from backend.app.collectors.mlb.client import MLB_BASE_STATES, MlbClient, _linescore, _weather_context
from backend.app.collectors.odds import _consensus_event
from backend.app.services.feature_engineering import (HOME_FIELD_MULTIPLIERS, batted_ball_clumping,
                                                      expected_runs, _effective_lineup_ops,
                                                      _lineup_matchup_summary, _paired_pitcher_difference,
                                                      _platoon_feature, _recent_pitcher_deviation)
from backend.app.services.refresh import (SPLIT_FETCH_BUDGET, _collect_batter_splits, _market_event_date,
                                          _market_checkpoint_due, _market_refresh_due,
                                          _odds_request_credit_cost, _months_for_recent, _optional,
                                          _prediction_stage, _recent_by_team, _split_budget,
                                          _lineup_split_tables)
from backend.app.services.batting import SINGLE, STATE_INDEX, build_batter_table
from backend.app.services.batting import STRIKEOUT
from backend.app.services.plate_engine import _half_inning
from backend.app.services.simulation import simulate_scores
from backend.app.services.simulation import _draw_runs, evaluate_simulation_recipe
from backend.app.services.trajectory import (air_density, flight, park_home_run_index,
                                             park_weather_home_run_multiplier)
from backend.app.services.prediction import (SIMULATION_SUMMARY_SCHEMA_VERSION,
                                             _apply_daily_bullpen_workload,
                                             apply_market_consensus_anchor,
                                             blend_classifier_into_means, build_score_estimates,
                                             favorite_fragility_score,
                                             predict_game)
from backend.app.services.jobs import (REPLAY_END_DATE, REPLAY_START_DATE,
                                       _lineup_retry_needed, _missing_leagues_for_date,
                                       checkpoint_stage_for_minutes)
from backend.app.services import jobs as jobs_module
from backend.app.services import refresh as refresh_module
from pathlib import Path
from backend.app.services import prediction as prediction_module
from backend.app.services.model_lifecycle import (_coherent_run_means, _promotion_decision,
                                                  _operating_prediction, _validation_partition,
                                                  _restore_versioned_baseline,
                                                  predict_with_runtime)
from backend.app.services.historical_replay import run_historical_replay
from backend.app.services.runtime_secrets import decrypt_secret, encrypt_secret
from backend.app.services.team_residuals import (ResidualObservation, TeamResidualHistory,
                                                 apply_residual_adjustment, available_before,
                                                 residual_context)
from backend.app.services.residual_attribution import attribute_score_residual
from backend.app.services.market_offset import (MarketOffsetHistory, MarketOffsetObservation, fit_market_offset,
                                                 market_offset_shadow_probability,
                                                 walk_forward_offset_validation)
from backend.app.services.paired_ablation import paired_ablation_report
from backend.app.services.pregame_context import prediction_context
from backend.app.services.data_integrity import summarize_pitcher_rows
from backend.app.services.derived_market_calibration import (DerivedMarketHistory,
                                                              DerivedMarketObservation,
                                                              _underdog_metrics)
from backend.app.services.probability_calibration import (CALIBRATION_VALIDATION,
                                                          LeagueProbabilityCalibrationHistory,
                                                          walk_forward_win_validation,
                                                          walk_forward_segmented_validation,
                                                          DistributionCalibrationObservation,
                                                          ProbabilityObservation,
                                                          calibrated_probability,
                                                          distribution_calibration_validation)
from backend.app.services.prediction_history_cache import _deserialize, _serialize


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


def test_market_snapshots_retain_unchanged_checkpoint_and_executable_prices():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="MLB", code="AW", name="Away")
        home = Team(league="MLB", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        start = datetime(2026, 8, 23, 18, tzinfo=KST)
        game = Game(external_id="MARKET-1", league="MLB", game_date=start.date(), start_at=start,
                    away_team_id=away.id, home_team_id=home.id, status="SCHEDULED", source="test",
                    source_url="test", collected_at=start - timedelta(days=1))
        session.add(game); session.flush()
        quote = {
            "provider": "The Odds API", "bookmaker_count": 4, "total_line": 8.5,
            "home_spread": -1.5, "home_implied_probability": .58,
            "away_implied_probability": .42, "home_decimal_odds": 1.72,
            "away_decimal_odds": 2.20,
        }
        upsert_market_consensus(session, game, quote, "provider", start - timedelta(hours=20))
        session.flush()
        upsert_market_consensus(session, game, quote, "provider", start - timedelta(hours=3))
        session.flush()
        snapshots = session.scalars(select(MarketSnapshot).order_by(MarketSnapshot.collected_at)).all()
        assert [row.raw["snapshot_stage"] for row in snapshots] == ["T_MINUS_24H", "T_MINUS_3H"]
        assert snapshots[0].raw["observation_role"] == "OPENING"
        assert snapshots[-1].raw["home_decimal_odds"] == 1.72


def test_market_snapshot_stage_never_labels_post_start_as_closing():
    start = datetime(2026, 8, 23, 18, tzinfo=KST)
    game = SimpleNamespace(start_at=start)
    assert _market_snapshot_stage(game, start - timedelta(minutes=15))[0] == "T_MINUS_15M"
    assert _market_snapshot_stage(game, start + timedelta(seconds=1))[0] == "POST_START_REJECTED"


def test_dated_full_cron_refresh_uses_the_requested_slate(monkeypatch):
    target = date(2026, 9, 1)
    calls = []

    def fake_refresh(league, target_date):
        calls.append((league, target_date))
        return {"date": target_date.isoformat()}

    monkeypatch.setattr(jobs_module, "run_full_refresh", fake_refresh)
    result = jobs_module.run_cron_refresh("MLB", "full", target_date=target)
    assert calls == [("MLB", target)]
    assert result == {"date": "2026-09-01"}


def test_stored_prediction_scope_skips_provider_collection(monkeypatch):
    target = date(2026, 9, 1)
    calls = []

    def fake_predict(league, target_date, *, trigger, game_ids):
        calls.append((league, target_date, trigger, game_ids))
        return {"predictions": 12}

    monkeypatch.setattr(jobs_module, "predict_stored_games", fake_predict)
    result = jobs_module.run_cron_refresh("MLB", "predict", target_date=target)
    assert calls == [("MLB", target, "supabase_stored_prediction", None)]
    assert result == {"predictions": 12}


def test_manual_background_refresh_queues_baseline_then_bounded_chunks():
    from backend.app.main import ManualRefreshRequest, manual_refresh

    class Rows:
        def all(self):
            return ["MLB-1", "MLB-2", "MLB-3"]

    class FakeSession:
        committed = False

        def __init__(self):
            self.calls = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalars(self, _statement):
            return Rows()

        def scalar(self, statement, parameters):
            self.calls.append((str(statement), parameters))
            return len(self.calls)

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("queueing should not roll back")

    session = FakeSession()
    result = manual_refresh(ManualRefreshRequest(
        password=settings.manual_refresh_password,
        league="MLB",
        target_date=date(2026, 9, 1),
    ), session)
    assert result["status"] == "QUEUED"
    assert result["browser_independent"] is True
    assert result["leagues"]["MLB"] == {
        "mode": "STORED_BASELINE_THEN_BOUNDED_CHUNKS", "requests": 2, "chunks": 1, "games": 3,
    }
    assert "'predict'" in session.calls[0][0]
    assert "invoke_dugout_chunked_refresh" in session.calls[1][0]
    assert len(session.calls) == 2
    assert session.committed is True


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


def test_concurrent_mlb_workers_reuse_one_committed_slate_context(monkeypatch):
    games = {
        "MLB-10": SimpleNamespace(external_id="MLB-10", pregame_context={}, context_collected_at=None),
        "MLB-11": SimpleNamespace(external_id="MLB-11", pregame_context={}, context_collected_at=None),
    }

    class Result:
        def all(self):
            return [(key, game.context_collected_at) for key, game in games.items()]

    class Scalars:
        def all(self):
            return list(games.values())

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return Result()

        def scalars(self, _statement):
            return Scalars()

    @contextmanager
    def fake_scope():
        yield FakeSession()

    @contextmanager
    def fake_lock(_name, *, blocking=False):
        assert blocking is True
        yield

    class Client:
        calls = 0

        def slate_context(self, _target_date, scheduled):
            self.calls += 1
            now = datetime.now(KST)
            return SourcePayload(
                {row["external_id"]: {"snapshot": self.calls} for row in scheduled},
                "https://statsapi.mlb.com/context", now,
            )

    monkeypatch.setattr(refresh_module, "SessionLocal", FakeSession)
    monkeypatch.setattr(refresh_module, "session_scope", fake_scope)
    monkeypatch.setattr(refresh_module, "job_lock", fake_lock)
    monkeypatch.setattr(refresh_module, "_tracked", lambda _n, _u, operation, _e: operation())
    scheduled = [
        {"external_id": "MLB-10"}, {"external_id": "MLB-11"},
    ]
    client = Client()
    refresh_module._refresh_mlb_slate_context(client, date(2026, 9, 2), scheduled, [])
    refresh_module._refresh_mlb_slate_context(client, date(2026, 9, 2), scheduled, [])

    assert client.calls == 1
    assert games["MLB-10"].pregame_context == {"snapshot": 1}
    assert games["MLB-10"].context_collected_at == games["MLB-11"].context_collected_at


def test_prediction_history_cache_round_trip_preserves_context_inputs():
    residual = TeamResidualHistory([ResidualObservation(
        game_id=1, started_at=datetime(2026, 8, 1, 18, tzinfo=KST),
        finalized_at=datetime(2026, 8, 1, 22, tzinfo=KST),
        home_team_id=10, away_team_id=20, home_expected=4.2, away_expected=3.8,
        home_actual=5, away_actual=3,
    )])
    probability = LeagueProbabilityCalibrationHistory([
        ProbabilityObservation(1, 2026, datetime(2026, 8, 1, 22, tzinfo=KST), .57, 1.0),
    ], {"status": "HOLD", "sample_count": 1})
    market = MarketOffsetHistory([
        MarketOffsetObservation(1, 2026, datetime(2026, 8, 1, 22, tzinfo=KST), .55, .57, 1.0),
    ], {"status": "COLLECTING", "sample_count": 0})

    restored = _deserialize(_serialize(residual, probability, market))

    assert restored[0].observations == residual.observations
    assert restored[1].observations == probability.observations
    assert restored[1].validation == probability.validation
    assert restored[2].observations == market.observations
    assert restored[2].validation == market.validation


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


def test_score_residual_attribution_separates_starter_and_bullpen_phases():
    prediction = SimpleNamespace(
        home_expected_runs=5.0, away_expected_runs=4.0, home_win_probability=.62,
        payload={"residual_calibration": {
            "league_residual_sd": 2.5,
            "home": {"matchup_residual_flag": False},
            "away": {"matchup_residual_flag": True},
        }},
    )
    result = SimpleNamespace(
        home_score=2, away_score=7,
        innings={"home": [0, 1, 0, 0, 0, 0, 1, 0, 0],
                 "away": [1, 0, 1, 0, 1, 0, 2, 2, 0]},
    )
    attribution = attribute_score_residual(prediction, result)
    assert attribution["favorite_lost"] is True
    assert attribution["home"]["early_starter_phase"]["actual_runs"] == 1
    assert attribution["away"]["late_bullpen_phase"]["actual_runs"] == 4
    assert attribution["home"]["routing"] == "MEAN_REVERSION"
    assert attribution["inning_split_available"] is True


def test_favorite_fragility_scores_simulated_stall_and_bullpen_collapse_paths():
    features = {
        "home_starter_confirmed": True, "home_lineup_confirmed": True,
        "home_large_residual_flag": False,
    }
    simulation = {
        "home_two_way_probability": .64,
        "game_shape": {
            "favorite_side": "HOME", "favorite_scores_0_to_2_and_loses": .16,
            "favorite_leads_after_five_then_loses": .08,
            "underdog_leads_after_five_and_wins": .12,
        },
    }
    output = favorite_fragility_score(features, {
        "bullpen": {"home": {"available": True, "fatigue_index": .8}},
        "schedule": {"home": {"fatigue_index": .15}},
    }, {}, simulation, .08)
    assert output["favorite"] == "HOME"
    assert output["score"] >= 35
    assert output["upset_paths"]["favorite_leads_after_five_then_loses"] == .08
    assert output["directional_run_adjustment"] == 0


def test_market_offset_shadow_learns_only_the_model_market_disagreement():
    start = datetime(2026, 4, 1, 18)
    rows = []
    for index in range(520):
        market = .55
        model = .68
        # The truth is between market and model, so the fitted disagreement weight should be
        # positive but smaller than blindly replacing the market with the model.
        outcome = 1.0 if index % 100 < 60 else 0.0
        rows.append(MarketOffsetObservation(index, 2026, start + timedelta(hours=index),
                                            market, model, outcome))
    intercept, weight = fit_market_offset(rows[:300])
    shadow = market_offset_shadow_probability(.68, .55, {
        "enabled": True, "intercept": intercept, "disagreement_weight": weight,
        "sample_count": 300,
    })
    assert .55 < shadow["shadow_probability"] < .68
    assert shadow["production_enabled"] is False
    report = walk_forward_offset_validation(rows)
    assert report["sample_count"] >= 300
    assert report["shadow_brier"] < report["market_brier"]


def test_underdog_promotion_requires_price_roi_and_positive_closing_line_value():
    start = datetime(2026, 4, 1, 18)
    rows = [DerivedMarketObservation(
        game_id=index, league="MLB", season=2026, available_at=start + timedelta(hours=index),
        market="MONEYLINE", model_line=None, market_line=None,
        model_probability=.60, market_probability=.67,
        outcome=0.0 if index % 10 < 4 else 1.0,
        home_decimal_odds=1.5, away_decimal_odds=3.0,
    ) for index in range(320)]
    timelines = [{
        "game_id": index, "league": "MLB", "closing_home_probability": .64,
    } for index in range(320)]
    report = _underdog_metrics(rows, timelines)
    assert report["sample_size"] == 320
    assert report["edge_sample_size"] == 320
    assert report["unit_roi"] > 0
    assert report["mean_probability_clv"] > 0
    assert report["status"]["state"] == "READY"


def test_paired_ablation_uses_same_seed_and_reports_each_shadow_variant():
    recipe = {
        "home_expected": 5.0, "away_expected": 4.0, "simulations": 2000, "seed": 77,
        "environment_variance": 0.0, "team_variance": .10, "league": "MLB",
        "home_staff": None, "away_staff": None,
        "home_team_variance": .12, "away_team_variance": .11,
    }
    prediction = SimpleNamespace(payload={
        "simulation_recipe": recipe,
        "residual_calibration": {
            "baseline_home_expected_runs": 4.8, "baseline_away_expected_runs": 4.1,
            "home_variance_multiplier": 1.0, "away_variance_multiplier": 1.0,
        },
        "market_calibration": {"model_home_before": 5.0, "model_away_before": 4.0},
        "headline_market": {}, "upset_volatility": {"shared_volatility": .02},
    })
    game = SimpleNamespace()
    result = SimpleNamespace(home_score=6, away_score=3)
    report = paired_ablation_report([(prediction, game, result, None)])
    assert report["sample_size"] == 1
    assert report["common_random_numbers"] is True
    assert set(report["metrics"]) == {"production", "no_residual", "no_market", "no_upset_volatility"}


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
    mlb_latest = datetime(2026, 8, 21, 22, 0, tzinfo=KST)
    assert not _market_refresh_due("MLB", mlb_latest, datetime(2026, 8, 22, 21, 59, tzinfo=KST))
    assert _market_refresh_due("MLB", mlb_latest, datetime(2026, 8, 22, 22, 0, tzinfo=KST))
    assert _market_event_date("2026-08-22T15:30:00Z") == date(2026, 8, 23)


def test_odds_credit_cost_counts_markets_times_regions(monkeypatch):
    monkeypatch.setattr(refresh_module, "settings", SimpleNamespace(
        odds_api_regions="us", odds_api_regions_kbo="eu,us",
    ))
    assert _odds_request_credit_cost("MLB") == 3
    assert _odds_request_credit_cost("KBO") == 6


def test_successful_market_attempt_suppresses_repeat_when_provider_has_no_match():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 4, 12, 0)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        game = Game(
            external_id="KBO-NO-ODDS", league="KBO", game_date=now.date(),
            start_at=now + timedelta(hours=5), start_time=(now + timedelta(hours=5)).time(),
            away_team_id=away.id, home_team_id=home.id, status="SCHEDULED",
            source="test", source_url="test", collected_at=now,
        )
        session.add(game); session.flush()
        assert _market_checkpoint_due(session, "KBO", now, None)
        # No MarketSnapshot was written because the provider did not return/match this event.
        # The successful slate request still counts as this stage's attempt.
        assert not _market_checkpoint_due(session, "KBO", now, now)


def test_exact_checkpoint_windows_do_not_use_broad_stage_buckets():
    assert checkpoint_stage_for_minutes(40) == "T_MINUS_40M"
    assert checkpoint_stage_for_minutes(37.5) == "T_MINUS_40M"
    assert checkpoint_stage_for_minutes(42.5) == "T_MINUS_40M"
    assert checkpoint_stage_for_minutes(43) is None
    assert checkpoint_stage_for_minutes(60) is None


def test_lineup_retry_is_near_first_pitch_and_rate_limited():
    now = datetime(2026, 9, 1, 8, 0, tzinfo=KST)
    assert _lineup_retry_needed(90, 0, None, now)
    assert _lineup_retry_needed(-5, 17, None, now)
    assert not _lineup_retry_needed(91, 0, None, now)
    assert not _lineup_retry_needed(-6, 0, None, now)
    assert not _lineup_retry_needed(30, 18, None, now)
    assert not _lineup_retry_needed(30, 0, now - timedelta(minutes=9), now)
    assert _lineup_retry_needed(30, 0, now - timedelta(minutes=10), now)


def test_candidate_promotion_requires_improvement_and_non_regression_guards():
    comparator = {"brier": .240, "log_loss": .690, "run_mae": 2.40, "margin_mae": 3.20}
    assert _promotion_decision(
        {"brier": .230, "log_loss": .680, "run_mae": 2.39, "margin_mae": 3.18}, comparator,
    )[0]
    assert not _promotion_decision(
        {"brier": .230, "log_loss": .680, "run_mae": 2.60, "margin_mae": 3.40}, comparator,
    )[0]
    promoted, reason = _promotion_decision(
        {"brier": .230, "log_loss": .680, "run_mae": 2.39, "margin_mae": 3.18,
         "predicted_margin_sd": .75},
        {**comparator, "predicted_margin_sd": 1.0},
    )
    assert promoted is False
    assert "마진 분산" in reason


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


def test_trained_runtime_uses_independent_margin_target_without_changing_total():
    runtime = {
        "feature_names": [], "feature_means": [], "feature_scales": [],
        "win_intercept": 0.0, "win_coefficients": [],
        "home_run_intercept": 5.0, "home_run_coefficients": [],
        "away_run_intercept": 5.0, "away_run_coefficients": [],
        "margin_intercept": 3.0, "margin_coefficients": [],
        "residual_stack": False,
    }
    _probability, home_runs, away_runs = predict_with_runtime(runtime, {}, 5.0, 5.0)
    assert home_runs > away_runs
    assert round(home_runs + away_runs, 6) == 10.0
    # Legacy artifacts without the new target remain readable and use their team-score gap.
    legacy = {key: value for key, value in runtime.items() if not key.startswith("margin_")}
    _probability, legacy_home, legacy_away = predict_with_runtime(legacy, {}, 5.0, 5.0)
    assert round(legacy_home - legacy_away, 6) == 0.0


def test_residual_runtime_corrects_baseline_instead_of_replacing_it():
    runtime = {
        "feature_names": [], "feature_means": [], "feature_scales": [],
        "win_intercept": 0.0, "win_coefficients": [],
        "home_run_intercept": .2, "home_run_coefficients": [],
        "away_run_intercept": -.1, "away_run_coefficients": [],
        "margin_intercept": .3, "margin_coefficients": [],
        "residual_stack": True,
    }
    probability, home_runs, away_runs = predict_with_runtime(runtime, {}, 5.0, 4.0)
    assert probability > .5
    assert home_runs > away_runs
    assert home_runs + away_runs > 9.0


def test_zero_residual_runtime_reproduces_the_baseline_means():
    from collections import defaultdict
    from backend.app.services.feature_engineering import logistic_probability
    from backend.app.services.prediction import blend_classifier_into_means

    runtime = {
        "feature_names": [], "feature_means": [], "feature_scales": [],
        "win_intercept": 0.0, "win_coefficients": [],
        "home_run_intercept": 0.0, "home_run_coefficients": [],
        "away_run_intercept": 0.0, "away_run_coefficients": [],
        "margin_intercept": 0.0, "margin_coefficients": [],
        "residual_stack": True,
    }
    features = defaultdict(float)
    baseline_probability = logistic_probability(features)
    baseline_home, baseline_away = blend_classifier_into_means(baseline_probability, 5.0, 4.0)
    _probability, home_runs, away_runs = predict_with_runtime(runtime, features, 5.0, 4.0)
    assert abs(home_runs - baseline_home) < 1e-9
    assert abs(away_runs - baseline_away) < 1e-9


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
        # One more than before: the archived-starter fallback adds exactly one bulk query,
        # not one per game, so the bound grows by 1 regardless of how many cards render.
        assert queries <= 9


def test_card_falls_back_to_the_archived_starter_when_no_live_one_was_ever_collected():
    """A finished game from before the service ran has no live PitcherStat at all. Without a
    fallback its card shows no starter even after the replay archive has the pre-game identity,
    which is exactly the 미정 gap the archived-starter backfill was supposed to close."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="MLB", code="AW", name="Away")
        home = Team(league="MLB", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        game = Game(external_id="ARCHIVE-1", league="MLB", game_date=date(2026, 4, 1),
                    away_team_id=away.id, home_team_id=home.id, status="FINAL",
                    source="test", source_url="test", collected_at=datetime(2026, 4, 1, 12))
        session.add(game); session.flush()
        # Only the home side has a live PitcherStat; away has only the archived record.
        session.add(PitcherStat(game_id=game.id, side="home", player_id="10", name="Live Starter",
                                era=3.10, whip=1.10, source="test", source_url="test",
                                collected_at=datetime(2026, 4, 1, 11)))
        session.add(GameStarter(game_id=game.id, side="away", player_id="20", name="Archived Starter",
                                prior_games=15, prior_starts=15, prior_innings=90.0,
                                prior_earned_runs=30, prior_hits=80, prior_walks=25,
                                prior_strikeouts=70, prior_home_runs=8, prior_quality_starts=6,
                                source="test", source_url="test",
                                collected_at=datetime(2026, 4, 1, 11)))
        session.commit()
        card = game_cards(session, date(2026, 4, 1), "MLB")[0]
        assert card["home"]["starter"]["name"] == "Live Starter"
        assert card["away"]["starter"]["name"] == "Archived Starter"
        assert abs(card["away"]["starter"]["era"] - 30 * 9 / 90.0) < 1e-9


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
    # A club's season run rate already counts its extra-inning runs, so it is the full-game mean
    # that has to land on target - the nine innings deliberately come in under it.
    for result in (neutral, with_ace, with_opener):
        assert abs(result["mean_runs"]["home"] / 4.6 - 1) < .03
        assert abs(result["mean_runs"]["away"] / 4.2 - 1) < .03
        assert result["regulation_mean_runs"]["home"] < result["mean_runs"]["home"]
        assert result["regulation_mean_runs"]["away"] < result["mean_runs"]["away"]
    # A starter who works deep leaves fewer innings for the bullpen.
    assert with_ace["bullpen_usage"]["away"]["starter_share"] > neutral["bullpen_usage"]["away"]["starter_share"]
    assert with_opener["bullpen_usage"]["away"]["starter_share"] < neutral["bullpen_usage"]["away"]["starter_share"]
    usage = neutral["bullpen_usage"]["away"]
    # Each share is stored rounded to four decimals, so the five can only sum to 1 that closely.
    assert abs(sum(usage[key] for key in ("starter_share", "high_leverage_share", "middle_share",
                                          "chase_share", "mop_up_share")) - 1) < 5e-4
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


def test_plate_engine_places_mlb_automatic_runner_in_the_base_state():
    tables = np.zeros((9, 8, 7), dtype=float)
    tables[:, :, STRIKEOUT] = .55
    tables[:, :, SINGLE] = .45
    simulations = 12_000
    active = np.ones(simulations, dtype=bool)
    multiplier = np.ones(simulations)
    empty, _ = _half_inning(
        np.random.default_rng(123), tables, np.zeros(simulations, dtype=np.int64), 1.0,
        multiplier, active, None,
    )
    automatic_runner, _ = _half_inning(
        np.random.default_rng(123), tables, np.zeros(simulations, dtype=np.int64), 1.0,
        multiplier, active, None, runner_on_second=True,
    )
    assert automatic_runner.mean() > empty.mean() + .10


def test_projected_lineups_cannot_activate_the_plate_appearance_engine():
    rows = [SimpleNamespace(side=side, player_id=f"{side}-{order}", confirmed=True)
            for side in ("home", "away") for order in range(1, 10)]
    rows[-1].confirmed = False
    # The function must stop before touching split storage when even one batting slot is projected.
    assert _lineup_split_tables(None, SimpleNamespace(), "MLB", 2026, rows) == {}


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


def test_distribution_collapse_rollback_disables_all_learned_champions():
    registry = SimpleNamespace(champion_model_version_id=22, previous_model_version_id=11)
    _restore_versioned_baseline(registry, failed_id=22)
    assert registry.champion_model_version_id is None
    assert registry.previous_model_version_id == 22


def test_lifecycle_scores_candidates_through_the_operating_simulation_recipe():
    runtime = {
        "feature_names": [], "feature_means": [], "feature_scales": [],
        "win_intercept": 0.0, "win_coefficients": [],
        "home_run_intercept": 5.0, "home_run_coefficients": [],
        "away_run_intercept": 4.0, "away_run_coefficients": [],
    }
    row = {
        "features": {}, "base_home_runs": 5.0, "base_away_runs": 4.0,
        "residual_context": {}, "league": "MLB",
        "simulation_recipe": {
            "seed": 77, "league": "MLB", "environment_variance": .08,
            "team_variance": .26, "home_team_variance": .26, "away_team_variance": .26,
        },
    }
    probability, home_runs, away_runs, method = _operating_prediction(runtime, row)
    assert method == "OPERATING_MONTE_CARLO_RECIPE"
    assert 0 < probability < 1
    assert home_runs > away_runs


def test_lifecycle_evaluates_legacy_feature_snapshots_with_neutral_missing_values():
    row = {
        "features": {}, "base_home_runs": 4.5, "base_away_runs": 4.4,
        "residual_context": {}, "league": "MLB", "simulation_recipe": None,
    }
    probability, home_runs, away_runs, method = _operating_prediction(None, row)
    assert method == "POISSON_FALLBACK"
    assert 0 < probability < 1
    assert home_runs > 0 and away_runs > 0


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


def test_trained_runtime_does_not_apply_the_classifier_twice():
    """predict_with_runtime already folds the classifier into its run means. Blending again in
    predict_game would price the same signal twice and overstate every confident call."""
    runtime_home, runtime_away = _coherent_run_means(.75, 4.6, 4.4)
    blended_home, blended_away = blend_classifier_into_means(.75, runtime_home, runtime_away)
    # The second pass demonstrably moves the margin further toward the classifier.
    assert (blended_home - blended_away) > (runtime_home - runtime_away) + .1
    # predict_game must therefore run the blend only on the baseline path.
    source = Path(prediction_module.__file__).read_text(encoding="utf-8")
    blend_line = next(line for line in source.splitlines()
                      if "blend_classifier_into_means(logistic" in line)
    guard = source.splitlines()[source.splitlines().index(blend_line) - 1]
    assert "if not model_runtime" in guard


def test_batting_factor_does_not_reach_across_to_the_opponent():
    """The slash-line factor was anchored to the mean of the two clubs playing, so raising one
    club's OBP shrank the OTHER club's projected runs. A club's hitting must not suppress its
    opponent's offense."""
    def club(**overrides):
        base = dict(runs_per_game=4.47, runs_allowed_per_game=4.47, avg=.2436, obp=.3182,
                    slg=.4004, era=4.10, whip=1.30, games=130, ops=.7186)
        return SimpleNamespace(team=SimpleNamespace(league="MLB"), **{**base, **overrides})

    def starter():
        return SimpleNamespace(era=4.10, whip=1.30, avg_start_innings=5.3, fip=4.10, games=25,
                               confirmed=True, opponent_innings=0, opponent_era=None, opponent_whip=None)

    environment = {"league_average_runs": 4.47}
    base_home, base_away, _ = expected_runs(club(), club(), starter(), starter(), 1.0, 0.0, environment)
    for field, better in (("obp", .3782), ("slg", .4704), ("avg", .2836)):
        home, away, _ = expected_runs(club(**{field: better}), club(), starter(), starter(),
                                      1.0, 0.0, environment)
        assert home > base_home + .02, f"better {field} must raise that club's own runs"
        assert abs(away - base_away) < .005, f"better home {field} must not move the opponent"


def test_archived_starter_totals_stop_at_the_game_being_replayed():
    """The whole point of the archive is pre-game information. A pitcher's line must accumulate
    only appearances strictly before the target game, or the replay is training on hindsight."""
    appearances = [
        {"date": "2026-04-01", "innings": 6.0, "earned_runs": 2, "hits": 5, "walks": 1,
         "strikeouts": 7, "home_runs": 1, "started": True},
        {"date": "2026-04-08", "innings": 5.0, "earned_runs": 4, "hits": 8, "walks": 3,
         "strikeouts": 4, "home_runs": 2, "started": True},
        # The game being replayed, plus a later one. Neither may be counted.
        {"date": "2026-04-15", "innings": 7.0, "earned_runs": 0, "hits": 2, "walks": 0,
         "strikeouts": 11, "home_runs": 0, "started": True},
        {"date": "2026-04-22", "innings": 6.0, "earned_runs": 3, "hits": 6, "walks": 2,
         "strikeouts": 5, "home_runs": 1, "started": True},
    ]
    totals = _totals_before(appearances, "2026-04-15")
    assert totals["prior_starts"] == 2
    assert totals["prior_innings"] == 11.0
    assert totals["prior_earned_runs"] == 6
    # One of the two prior starts was six innings with two earned runs.
    assert totals["prior_quality_starts"] == 1

    record = SimpleNamespace(player_id="1", name="Test", prior_innings=11.0, prior_earned_runs=6,
                             prior_hits=13, prior_walks=4, prior_strikeouts=11, prior_home_runs=3,
                             prior_starts=2, prior_games=2, prior_quality_starts=1)
    view = starter_view(record, 4.10)
    assert abs(view.era - 6 * 9 / 11) < 1e-9
    assert abs(view.whip - (13 + 4) / 11) < 1e-9
    assert view.confirmed is True

    # A pitcher with no prior work carries no rate at all rather than a fabricated one.
    empty = starter_view(SimpleNamespace(
        player_id="2", name="Debut", prior_innings=0.0, prior_earned_runs=0, prior_hits=0,
        prior_walks=0, prior_strikeouts=0, prior_home_runs=0, prior_starts=0, prior_games=0,
        prior_quality_starts=0), 4.10)
    assert empty.era is None and empty.whip is None and empty.fip is None


def test_starter_skill_units_and_recent_form_are_independent_signals():
    home = SimpleNamespace(fip=3.5, era=4.0, k_bb_rate=.20,
                           recent={"available": True, "era": 5.0, "k_bb_rate": .15})
    away = SimpleNamespace(fip=4.5, era=4.0, k_bb_rate=.10,
                           recent={"available": True, "era": 3.0, "k_bb_rate": .18})
    assert _paired_pitcher_difference(away, home, "fip") == 1.0
    assert _paired_pitcher_difference(home, away, "k_bb_rate") == pytest.approx(.10)
    assert _recent_pitcher_deviation(away, "era") - _recent_pitcher_deviation(home, "era") == -2.0
    assert (_recent_pitcher_deviation(home, "k_bb_rate")
            - _recent_pitcher_deviation(away, "k_bb_rate")) == pytest.approx(-.13)
    unavailable = SimpleNamespace(era=2.0, k_bb_rate=.30, recent={"available": False})
    assert _recent_pitcher_deviation(unavailable, "era") == 0.0
    assert _paired_pitcher_difference(home, SimpleNamespace(fip=None), "fip") == 0.0


def test_kbo_official_daily_log_supplies_recent_starter_form_without_fake_pitch_counts():
    rows = [
        {"date": "2026-04-01", "innings": 6.0, "earned_runs": 2, "hits": 5, "walks": 1,
         "hit_batters": 1, "batters_faced": 25, "strikeouts": 7, "home_runs": 1,
         "pitches": None, "started": True},
        {"date": "2026-04-07", "innings": 1.0, "earned_runs": 0, "hits": 0, "walks": 0,
         "hit_batters": 0, "batters_faced": 3, "strikeouts": 2, "home_runs": 0,
         "pitches": None, "started": False},
        {"date": "2026-04-12", "innings": 5.0, "earned_runs": 3, "hits": 6, "walks": 2,
         "hit_batters": 0, "batters_faced": 23, "strikeouts": 5, "home_runs": 1,
         "pitches": None, "started": True},
    ]
    summary = _pitcher_log_summary(rows, date(2026, 4, 18))
    assert summary["starts"] == 2
    assert summary["rest_days"] == 6
    assert summary["k_bb_rate"] == round((14 - 3) / 51, 4)
    assert summary["recent"]["available"] is True
    assert summary["recent"]["starts"] == 2
    assert summary["recent_pitches"] is None
    assert summary["recent"]["avg_pitches"] is None


def test_kbo_daily_log_reproduces_the_official_season_line():
    """KBO publishes no per-game pitching feed, so the archive rebuilds it from the 일자별 기록
    page. Parsing it wrongly would quietly feed the model bad starter rates, so the parsed rows
    are checked against the season totals the same site reports."""
    html = """
    <table><thead><tr><th>4월</th><th>상대</th><th>구분</th><th>결과</th><th>ERA1</th>
      <th>TBF</th><th>IP</th><th>H</th><th>HR</th><th>BB</th><th>HBP</th><th>SO</th>
      <th>R</th><th>ER</th><th>ERA2</th></tr></thead>
    <tbody>
      <tr><td>04.01</td><td>한화</td><td>선발</td><td></td><td>5.40</td><td>26</td><td>5</td>
          <td>7</td><td>1</td><td>1</td><td>1</td><td>7</td><td>4</td><td>3</td><td>5.40</td></tr>
      <tr><td>04.07</td><td>롯데</td><td>구원</td><td>승</td><td>1.80</td><td>22</td><td>5 1/3</td>
          <td>6</td><td>0</td><td>2</td><td>0</td><td>9</td><td>1</td><td>1</td><td>3.60</td></tr>
      <tr><td>합계</td><td></td><td></td><td></td><td></td><td>48</td><td>10 1/3</td>
          <td>13</td><td>1</td><td>3</td><td>1</td><td>16</td><td>5</td><td>4</td><td>3.60</td></tr>
    </tbody></table>
    """
    rows = _pitcher_daily_log(html, 2026)
    # The 합계 row has no date and must not be counted as a third appearance.
    assert [row["date"] for row in rows] == ["2026-04-01", "2026-04-07"]
    assert [row["started"] for row in rows] == [True, False]
    assert abs(sum(row["innings"] for row in rows) - (5 + 5 + 1 / 3)) < 1e-9
    assert sum(row["earned_runs"] for row in rows) == 4

    # Same accumulation rule as MLB: the target game's own date is excluded.
    totals = _totals_before(rows, "2026-04-07")
    assert totals["prior_games"] == 1 and totals["prior_starts"] == 1


def test_home_field_edge_is_not_counted_twice():
    """The simulation gives the home club the batting-last advantage on its own: it skips the
    ninth while ahead and walk-offs cap the inning. Layering a large run multiplier on top of
    that pushed the model to a 55% home win rate against a real 52.8%."""
    league_average = 4.47
    home_multiplier, away_multiplier = HOME_FIELD_MULTIPLIERS["MLB"]
    fitted = simulate_scores(league_average * home_multiplier, league_average * away_multiplier,
                             40_000, 11, league="MLB")
    # Real MLB home clubs win 52.8% of decided games and score about 1.4% more runs.
    assert .515 < fitted["home_two_way_probability"] < .545
    means = fitted["mean_runs"]
    assert .99 < means["home"] / means["away"] < 1.05
    # The old 1.035/0.985 edge is what over-counted; it must stay clearly worse than the fit.
    legacy = simulate_scores(league_average * 1.035, league_average * .985, 40_000, 11, league="MLB")
    assert legacy["home_two_way_probability"] > fitted["home_two_way_probability"]
    assert abs(legacy["home_two_way_probability"] - .528) > abs(fitted["home_two_way_probability"] - .528)


def test_simulated_run_distribution_tracks_real_results():
    """The fitted dispersion must keep the run histogram close to real team-game outcomes."""
    # Shape of ~3,900 real MLB team-games: mode at 2-3 runs with a long right tail.
    observed = [.064, .110, .140, .135, .120, .106, .094, .065, .053, .039, .026]
    result = simulate_scores(4.47, 4.47, 40_000, 11, league="MLB", team_variance=.06)
    simulated = result["team_run_distribution"]["away"][:len(observed)]
    total_variation = sum(abs(a - b) for a, b in zip(simulated, observed, strict=True)) / 2
    assert total_variation < .03
    # Most of this spread now comes from run clumping and the matchup tilt, so the per-club shock
    # that used to carry it alone is materially worse against the same data.
    legacy = simulate_scores(4.47, 4.47, 40_000, 11, league="MLB", team_variance=.26)
    legacy_variation = sum(abs(a - b) for a, b in
                           zip(legacy["team_run_distribution"]["away"][:len(observed)], observed, strict=True)) / 2
    assert total_variation < legacy_variation


def test_extra_innings_are_not_added_on_top_of_a_full_game_mean():
    """A season run rate already counts extra-inning runs, so the tiebreaker cannot add more."""
    for league, expected in (("MLB", (4.6, 4.4)), ("KBO", (5.2, 5.0))):
        result = simulate_scores(expected[0], expected[1], 40_000, 20260825, league=league)
        full = result["mean_runs"]["home"] + result["mean_runs"]["away"]
        regulation = result["regulation_mean_runs"]["home"] + result["regulation_mean_runs"]["away"]
        assert full == pytest.approx(expected[0] + expected[1], abs=.15)
        # The nine innings are handed the target less whatever the tiebreaker is expected to
        # contribute, so they land under it and the two together land on it.
        assert regulation < full
        assert result["extra_innings"]["probability"] > 0
        # The gap is the tiebreaker's contribution, which is a fraction of a run - a whole extra
        # inning's worth spread over the games that actually need one.
        assert .1 < full - regulation < .6


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
    assert estimates["full_distribution"] == estimates["mode"]
    assert estimates["representative"]["population_coverage"] == 1.0
    assert estimates["representative"]["projects_favorite_cover"] is True
    assert estimates["winner_conditional"] == estimates["representative"]


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
    unchanged = predict_game(
        game, home, away, pitcher("home-p"), pitcher("away-p"), base,
        known_input_hash=before["input_hash"],
    )
    assert unchanged == {
        "input_hash": before["input_hash"],
        "input_payload": before["input_payload"],
        "unchanged": True,
    }
    after = predict_game(game, home, away, pitcher("home-p"), pitcher("away-p"), changed)
    assert before["input_hash"] != after["input_hash"]
    assert before["input_payload"]["features"]["home_lineup_index"] != (
        after["input_payload"]["features"]["home_lineup_index"]
    )
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
    assert after["payload"]["primary_score"]["selection_method"] == "COHERENT_BAYES_MEDIAN_V3"
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
    assert after["payload"]["raw_simulation_home_probability"] == (
        after["payload"]["probability_calibration"]["raw_home_two_way_probability"]
    )
    assert after["payload"]["full_distribution_score"] == after["payload"]["top_scores"][0]
    assert after["payload"]["winner_conditional_score"] == after["payload"]["primary_score"]

    close = predict_game(
        game, home, away, pitcher("home-p"), pitcher("away-p"), base,
        game_context={"probability_calibration": {
            "enabled": True, "method": "TEST", "sample_count": 100,
            "slope": 0.0, "intercept": 0.0,
        }},
    )
    assert close["home_win_probability"] == .5
    # A dead heat still names one winner and publishes one representative score for it, rather
    # than two branch scores that disagree with each other and with the selected candidate.
    assert "close_game_scenarios" not in close["payload"]
    close_primary = close["payload"]["primary_score"]
    assert close_primary == close["payload"]["winner_conditional_score"]
    close_favors_home = close["home_win_probability"] >= close["away_win_probability"]
    assert (close_primary["home"] > close_primary["away"]) is close_favors_home
    # The representative is the first of the candidates the detail list shows, so the headline
    # score and the candidate chips can never name different scores.
    candidates = close["payload"]["projected_score_candidates"]
    assert (candidates[0]["home"], candidates[0]["away"]) == (close_primary["home"], close_primary["away"])


def test_integer_projection_uses_full_distribution_and_respects_run_line_majority():
    strong = simulate_scores(6.8, 3.1, 20_000, 20260824, league="MLB")
    primary = strong["projected_score"]
    assert primary["population_coverage"] == 1.0
    assert primary["projects_favorite_cover"] is True
    assert primary["favorite_cover_probability"] >= 0.5
    assert primary["run_line_conditioning"] == "WINNER_CONDITIONAL_COVER_MAJORITY"
    assert primary["home"] - primary["away"] >= 2
    assert len(strong["projected_score_candidates"]) >= 3
    assert all(score["selection_method"] == "COHERENT_BAYES_MEDIAN_V3"
               for score in strong["projected_score_candidates"])

    close = simulate_scores(4.5, 4.4, 20_000, 20260824, league="MLB")
    close_primary = close["projected_score"]
    favorite_margin = (close_primary["home"] - close_primary["away"]
                       if close["home_two_way_probability"] >= .5
                       else close_primary["away"] - close_primary["home"])
    if close_primary["projects_favorite_cover"]:
        assert favorite_margin >= close_primary["minimum_favorite_margin"]
    else:
        assert 1 <= favorite_margin < close_primary["minimum_favorite_margin"]
    # The headline score follows whatever the run-line decision was, so the score and the pick
    # on the card can never disagree.
    conditional = close["winner_conditional_market"]
    assert close_primary["projects_favorite_cover"] is conditional["projects_favorite_cover"]
    assert conditional["headline_follows_pick"] is True
    assert close_primary["projects_favorite_cover"] is (conditional["handicap"]["pick"] == "MINUS")


def test_second_stage_markets_are_priced_only_inside_the_winning_branch():
    result = simulate_scores(5.6, 3.9, 20_000, 20260824, league="MLB",
                             headline_total_line=8.5, headline_home_spread=-1.5)
    conditional = result["winner_conditional_market"]
    assert conditional["winner"] == "HOME"
    assert conditional["conditioning"] == "WINNER_WINS_OUTRIGHT"

    # The branch is exactly the simulations the forecast winner won, and nothing else.
    margins = result["frequency_tables"]["margins"]
    home_wins = sum(count for margin, count in margins.items() if int(margin) > 0)
    assert conditional["sample_size"] == home_wins
    assert conditional["scenario_probability"] == pytest.approx(home_wins / 20_000, abs=1e-4)

    # The narrative the branch supplies: given the favourite wins, it usually clears the line.
    # True in essentially every matchup, which is exactly why it is not the decision.
    handicap = conditional["handicap"]
    assert handicap["winner_cover_probability"] > handicap["winner_short_probability"]
    assert (handicap["winner_cover_probability"] + handicap["winner_short_probability"]
            + handicap["winner_push_probability"]) == pytest.approx(1)
    # A conditional percentage is not the chance it lands; the joint figure is.
    assert handicap["joint_winner_cover_probability"] == pytest.approx(
        handicap["winner_cover_probability"] * conditional["scenario_probability"], abs=1e-3)
    assert handicap["joint_winner_cover_probability"] == pytest.approx(
        result["market_handicap"]["minus_probability"], abs=1e-3)
    # With no collected run-line price there is no market statement to disagree with, so the
    # handicap is information rather than a recommendation.
    assert handicap["pick_basis"] == "NO_MARKET_PRICE"
    assert handicap["edge"] is None
    assert handicap["comparable"] is False

    # Conditioning on the winner lifts the total, which is what stops every game reading under.
    total = conditional["headline_total"]
    assert total["line"] == 8.5
    assert total["line_source"] == "MARKET"
    assert total["over_probability"] > result["totals"]["8.5"]["over"]
    assert conditional["mean_runs"]["home"] > result["mean_runs"]["home"]

    # The headline score is chosen under these same two decisions.
    primary = result["projected_score"]
    assert primary["total_conditioning"] == "EXPECTED_TOTAL_VS_LINE"
    assert primary["headline_total_pick"] == total["pick"]
    assert primary["scenario_over_probability"] == total["over_probability"]
    assert (primary["home"] + primary["away"] > total["line"]) is (total["pick"] == "OVER")
    assert primary["home"] - primary["away"] >= handicap["minimum_margin"]


def test_second_stage_always_prices_against_the_posted_run_line():
    # A run line is one two-sided quote: home -2.5 is away +2.5. Its magnitude is the market's
    # number for both clubs, so it stays the reference even when the book made the other club
    # favourite - only the side is read from the club this branch assumes wins.
    for spread in (-2.5, 2.5):
        result = simulate_scores(5.2, 4.0, 20_000, 20260824, league="MLB", headline_home_spread=spread)
        handicap = result["winner_conditional_market"]["handicap"]
        assert handicap["run_line_source"] == "MARKET"
        assert handicap["run_line"] == 2.5
        assert handicap["minimum_margin"] == 3
        assert handicap["market_home_spread"] == spread
        assert handicap["market_agrees_with_model"] is (spread < 0)
        # The priced side follows the posted sign, and the model prices the same event.
        assert handicap["minus_side"] == ("HOME" if spread < 0 else "AWAY")
        assert 0 < handicap["model_minus_probability"] < 1
        assert result["projected_score"]["favorite_run_line"] == 2.5

    without_market = simulate_scores(4.9, 4.3, 20_000, 20260824, league="MLB")
    assert without_market["winner_conditional_market"]["handicap"]["run_line_source"] == "MODEL_FALLBACK"
    assert without_market["winner_conditional_market"]["handicap"]["run_line"] == 1.5
    assert without_market["winner_conditional_market"]["handicap"]["market_home_spread"] is None


def test_run_line_pick_is_decided_against_the_market_price_not_a_flat_half():
    # Given a club wins a baseball game it clears a 1.5 line in every matchup, so a 50% bar names
    # the same side on every card. The book already priced that event; the pick is the
    # disagreement with that price, which is what differs from game to game.
    generous = simulate_scores(5.2, 4.0, 20_000, 20260824, league="MLB",
                               headline_home_spread=-1.5, headline_spread_probability=.35)
    stingy = simulate_scores(5.2, 4.0, 20_000, 20260824, league="MLB",
                             headline_home_spread=-1.5, headline_spread_probability=.60)
    generous_handicap = generous["winner_conditional_market"]["handicap"]
    stingy_handicap = stingy["winner_conditional_market"]["handicap"]

    # Same matchup, same simulations, same line - only the price differs, and it flips the side.
    assert generous["frequency_tables"] == stingy["frequency_tables"]
    assert generous_handicap["model_minus_probability"] == stingy_handicap["model_minus_probability"]
    assert generous_handicap["pick_basis"] == "EDGE_VS_MARKET"
    assert generous_handicap["comparable"] is True
    assert generous_handicap["pick"] == "MINUS"
    assert generous_handicap["edge"] > 0
    assert stingy_handicap["pick"] == "PLUS"
    assert stingy_handicap["edge"] < 0
    assert stingy_handicap["pick_edge"] == pytest.approx(-stingy_handicap["edge"])

    # Handicap is no longer a published prediction and must not reshape the representative score.
    # Changing only the handicap price therefore leaves the winner-scenario score unchanged.
    assert (
        generous["projected_score"]["home"],
        generous["projected_score"]["away"],
    ) == (
        stingy["projected_score"]["home"],
        stingy["projected_score"]["away"],
    )
    assert generous["projected_score"]["scenario_conditioning"] == "FAVORITE_WIN+HEADLINE_TOTAL"

    # The collected price always describes the home club, so an away run line inverts it.
    away_line = simulate_scores(4.0, 5.2, 20_000, 20260824, league="MLB",
                                headline_home_spread=1.5, headline_spread_probability=.35)
    away_handicap = away_line["winner_conditional_market"]["handicap"]
    assert away_handicap["minus_side"] == "AWAY"
    assert away_handicap["market_minus_probability"] == pytest.approx(.65)


def test_total_pick_follows_expected_total_and_keeps_market_edge_as_context():
    # The price can change the measured edge, but it must not flip the direction away from the
    # expected total printed on the same card.
    common = dict(league="MLB", headline_total_line=8.5, headline_home_spread=-1.5)
    cheap = simulate_scores(5.4, 4.0, 20_000, 20260824, headline_total_over_probability=.40, **common)
    dear = simulate_scores(5.4, 4.0, 20_000, 20260824, headline_total_over_probability=.60, **common)
    cheap_total = cheap["winner_conditional_market"]["headline_total"]
    dear_total = dear["winner_conditional_market"]["headline_total"]

    assert cheap["frequency_tables"] == dear["frequency_tables"]
    assert cheap_total["model_over_probability"] == dear_total["model_over_probability"]
    assert cheap_total["pick_basis"] == "EXPECTED_TOTAL_VS_LINE"
    assert cheap_total["comparable"] is True
    assert cheap_total["pick"] == dear_total["pick"] == "OVER"
    assert cheap_total["edge"] > 0 and dear_total["edge"] < 0
    assert cheap_total["expected_total"] > cheap_total["line"]
    # The total read is priced from an unchanged score population; its market price must not
    # choose a different representative score.
    assert (cheap["projected_score"]["home"], cheap["projected_score"]["away"]) == (
        dear["projected_score"]["home"], dear["projected_score"]["away"]
    )
    assert cheap["projected_score"]["total_conditioning"] == "EXPECTED_TOTAL_VS_LINE"

    # Both sides lean over inside the winning branch, while the price only changes value context.
    assert cheap_total["over_probability"] == dear_total["over_probability"] > .5

    without_price = simulate_scores(5.4, 4.0, 20_000, 20260824, **common)
    without_total = without_price["winner_conditional_market"]["headline_total"]
    assert without_total["pick_basis"] == "EXPECTED_TOTAL_VS_LINE"
    assert without_total["comparable"] is False
    assert without_total["edge"] is None
    assert without_price["projected_score"]["total_conditioning"] == "EXPECTED_TOTAL_VS_LINE"

    # A price only means anything at the line it was quoted for, so a model-derived line
    # (no market total collected) never borrows it.
    model_line = simulate_scores(5.4, 4.0, 20_000, 20260824, league="MLB",
                                 headline_total_over_probability=.40)
    model_total = model_line["winner_conditional_market"]["headline_total"]
    assert model_total["line_source"] == "MODEL_FAIR"
    assert model_total["market_over_probability"] is None
    assert model_total["pick_basis"] == "EXPECTED_TOTAL_VS_LINE"


def test_total_pick_cannot_say_over_when_expected_total_is_below_line():
    result = simulate_scores(
        6.2, 6.0, 20_000, 20260904, league="MLB",
        headline_total_line=12.5, headline_total_over_probability=.10,
    )
    total = result["winner_conditional_market"]["headline_total"]
    # The very cheap over price creates a positive over edge. Previously that edge incorrectly
    # changed the card to over even though the displayed mean was below 12.5.
    assert total["edge"] > 0
    assert total["expected_total"] < total["line"]
    assert total["pick"] == "UNDER"
    assert total["pick_edge"] < 0


def test_headline_score_is_centred_on_the_branch_it_is_selected_from():
    # Full-population medians describe a population that includes every game the forecast winner
    # loses. For a mild favourite they point at a level score the winning branch cannot contain,
    # which is what used to drag every headline onto the smallest admissible margin.
    result = simulate_scores(4.6, 4.4, 20_000, 20260824, league="MLB",
                             headline_total_line=8.5, headline_home_spread=-1.5)
    primary = result["projected_score"]
    conditional = result["winner_conditional_market"]
    assert primary["target_population"] == "WINNER_BRANCH"
    assert primary["target_home_median"] == conditional["median_runs"]["home"]
    assert primary["target_away_median"] == conditional["median_runs"]["away"]
    assert primary["target_favorite_margin_median"] == conditional["median_margin"]
    # The branch median is a winning score, unlike the full-population median it replaced.
    assert conditional["median_runs"]["home"] > conditional["median_runs"]["away"]
    assert primary["home"] > primary["away"]


def test_second_stage_branch_excludes_ties_and_matches_the_forecast_winner():
    result = simulate_scores(5.5, 4.6, 20_000, 20260824, league="KBO",
                             headline_total_line=9.5, headline_home_spread=-1.5)
    conditional = result["winner_conditional_market"]
    assert conditional["winner"] == "HOME"
    # KBO ties belong to neither branch, so the branch share sits below the two-way probability.
    assert result["tie_probability"] > 0
    assert conditional["scenario_probability"] < conditional["winner_probability"]
    assert conditional["scenario_probability"] == pytest.approx(
        result["home_win_probability"], abs=1e-4)

    away_favorite = simulate_scores(3.4, 5.9, 20_000, 20260825, league="MLB")
    assert away_favorite["winner_conditional_market"]["winner"] == "AWAY"
    assert away_favorite["winner_conditional_market"]["handicap"]["minus_side"] == "AWAY"
    assert away_favorite["winner_conditional_market"]["handicap"]["plus_side"] == "HOME"


def test_market_run_line_sets_dynamic_cover_population_for_headline_score():
    result = simulate_scores(
        8.0, 2.5, 20_000, 20260824, league="MLB", headline_home_spread=-2.5,
    )
    primary = result["projected_score"]
    market = result["market_handicap"]
    assert market["run_line"] == 2.5
    assert market["minimum_margin"] == 3
    assert market["minus_side"] == "HOME"
    assert market["minus_probability"] > market["plus_probability"]
    assert primary["run_line_source"] == "MARKET"
    assert primary["favorite_run_line"] == 2.5
    assert primary["minimum_favorite_margin"] == 3
    assert primary["run_line_conditioning"] == "WINNER_CONDITIONAL_COVER_MAJORITY"
    assert primary["home"] - primary["away"] >= 3

    unchanged_population = simulate_scores(8.0, 2.5, 20_000, 20260824, league="MLB")
    assert result["frequency_tables"] == unchanged_population["frequency_tables"]

    away_favorite = simulate_scores(
        2.5, 8.0, 20_000, 20260825, league="MLB", headline_home_spread=2.5,
    )
    away_primary = away_favorite["projected_score"]
    away_market = away_favorite["market_handicap"]
    assert away_market["minus_side"] == "AWAY"
    assert away_market["minus_probability"] > away_market["plus_probability"]
    assert away_primary["run_line_source"] == "MARKET"
    assert away_primary["away"] - away_primary["home"] >= 3

    margins = away_favorite["frequency_tables"]["margins"]
    home_plus_from_wins_and_close_losses = sum(
        count for margin, count in margins.items() if int(margin) > -2.5
    ) / 20_000
    assert away_market["plus_probability"] == pytest.approx(home_plus_from_wins_and_close_losses)


@pytest.mark.parametrize("home_expected,away_expected,total_line", [
    (4.8, 4.1, 8.5),
    (6.8, 3.1, 9.5),
    (3.5, 5.5, 8.5),
])
def test_coherent_headline_score_is_not_selected_to_match_total_direction(
    home_expected, away_expected, total_line,
):
    result = simulate_scores(
        home_expected, away_expected, 20_000, 20260824,
        league="MLB", headline_total_line=total_line,
    )
    result_at_other_line = simulate_scores(
        home_expected, away_expected, 20_000, 20260824,
        league="MLB", headline_total_line=total_line + 2,
    )
    # Total directions remain reportable, but neither the book's threshold nor the resulting
    # over/under side is allowed to force the representative winner scenario into a score.
    assert result["projected_score"]["home"] == result_at_other_line["projected_score"]["home"]
    assert result["projected_score"]["away"] == result_at_other_line["projected_score"]["away"]
    assert result["projected_score"]["headline_total_line"] == total_line
    assert result["projected_score"]["scenario_probability"] > 0


def test_verified_market_consensus_conservatively_changes_simulation_population():
    home_team, away_team = SimpleNamespace(name="Home"), SimpleNamespace(name="Away")
    recent = {"10": {"games": 10, "win_rate": .5}}
    home = SimpleNamespace(team=home_team, recent=recent, win_rate=.55, home_win_rate=.58, runs_per_game=4.8,
                           runs_allowed_per_game=4.2, ops=.750, era=4.10)
    away = SimpleNamespace(team=away_team, recent=recent, win_rate=.50, away_win_rate=.48, runs_per_game=4.4,
                           runs_allowed_per_game=4.5, ops=.720, era=4.40)
    pitcher = lambda player_id: SimpleNamespace(player_id=player_id, name=player_id, confirmed=True,
                                                era=4.0, whip=1.3, war=1.0)
    game = SimpleNamespace(external_id="MLB-market-line", league="MLB", stadium="Yankee Stadium")
    lower = predict_game(
        game, home, away, pitcher("home-p"), pitcher("away-p"),
        game_context={"market": {"provider": "TEST_BOOKS", "bookmaker_count": 4,
                                         "collected_at": "2026-08-24T10:00:00+09:00",
                                         "total_line": 7.5, "home_implied_probability": .60,
                                         "away_implied_probability": .40}},
    )
    upper = predict_game(
        game, home, away, pitcher("home-p"), pitcher("away-p"),
        game_context={"market": {"provider": "TEST_BOOKS", "bookmaker_count": 4,
                                         "collected_at": "2026-08-24T10:00:00+09:00",
                                         "total_line": 10.5, "home_implied_probability": .60,
                                         "away_implied_probability": .40}},
    )
    spread = predict_game(
        game, home, away, pitcher("home-p"), pitcher("away-p"),
        game_context={"market": {"provider": "TEST_BOOKS", "bookmaker_count": 4,
                                         "collected_at": "2026-08-24T10:00:00+09:00",
                                         "total_line": 7.5, "home_spread": -2.5,
                                         "home_implied_probability": .60,
                                         "away_implied_probability": .40}},
    )
    assert lower["input_hash"] != upper["input_hash"]
    assert lower["payload"]["frequency_tables"] == upper["payload"]["frequency_tables"]
    assert lower["home_expected_runs"] + lower["away_expected_runs"] == (
        upper["home_expected_runs"] + upper["away_expected_runs"]
    )
    assert lower["payload"]["market_calibration"]["enabled"] is True
    assert lower["payload"]["market_calibration"]["total_weight"] == 0
    assert lower["payload"]["market_calibration"]["total_role"] == "REFERENCE_ONLY_DERIVED_TOTAL_COMPARISON"
    assert lower["payload"]["primary_score"]["headline_total_line"] == 7.5
    assert upper["payload"]["primary_score"]["headline_total_line"] == 10.5
    assert lower["input_hash"] != spread["input_hash"]
    assert spread["payload"]["market_handicap"]["run_line"] == 2.5


def test_unverified_bare_market_number_cannot_move_model_means():
    home, away, audit = apply_market_consensus_anchor(
        5.2, 4.3, {"total_line": 14.5, "home_spread": -2.5}, "MLB",
    )
    assert (home, away) == (5.2, 4.3)
    assert audit["enabled"] is False
    assert audit["reason"] == "NO_VERIFIED_PREGAME_MARKET"


def test_verified_run_line_without_moneyline_is_reference_only_for_scores():
    """A handicap direction is not score input; retain it only for later comparison."""
    baseline = {
        "provider": "TEST_BOOKS", "bookmaker_count": 4,
        "collected_at": "2026-08-24T10:00:00+09:00", "total_line": 8.5,
    }
    home_a, away_a, audit_a = apply_market_consensus_anchor(5.2, 4.3, baseline, "MLB")
    home_b, away_b, audit_b = apply_market_consensus_anchor(
        5.2, 4.3, {**baseline, "home_spread": -2.5}, "MLB")
    assert (home_b, away_b) == (home_a, away_a)
    assert audit_b["spread_role"] == "REFERENCE_ONLY_DERIVED_HANDICAP_COMPARISON"
    assert audit_a["market_home_probability"] is None
    assert audit_b["market_home_probability"] is None


def test_market_moneyline_can_temper_margin_but_total_is_reference_only():
    home, away, audit = apply_market_consensus_anchor(7.0, 3.0, {
        "provider": "TEST_BOOKS", "bookmaker_count": 8, "total_line": 8.0,
        "home_implied_probability": .45, "away_implied_probability": .55,
    }, "MLB")
    assert audit["enabled"] is True
    assert audit["total_weight"] == 0
    assert home + away == 10.0
    # A contradictory market tempers a large model edge but cannot blindly reverse it.
    assert home > away
    assert audit["anchored_home_probability"] < audit["model_home_probability_before"]


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


def test_residual_calibration_flags_large_team_errors_and_widens_variance():
    start = datetime(2026, 4, 1, 18, 30)
    observations = [ResidualObservation(
        game_id=index + 1, started_at=start + timedelta(days=index),
        finalized_at=start + timedelta(days=index, hours=3),
        home_team_id=1, away_team_id=2, home_expected=4.5, away_expected=4.5,
        home_actual=10 if index % 2 == 0 else 0, away_actual=4,
    ) for index in range(16)]
    context = residual_context(
        observations, 1, 3, date(2026, 8, 23), force_enabled=True, league="MLB",
    )
    assert context["home"]["offense_large_residual_team"] is True
    assert context["home"]["offense_large_residual_games"] >= 8
    assert context["home"]["offense_outlier_index"] > 1
    assert context["outlier_analysis"]["home_scoring"]["large_residual_flag"] is True
    assert context["home_variance_multiplier"] >= 1


def test_matchup_residual_requires_sample_and_direction_consistency():
    start = datetime(2026, 4, 1, 18, 30)
    observations = [ResidualObservation(
        game_id=index + 1, started_at=start + timedelta(days=index),
        finalized_at=start + timedelta(days=index, hours=3),
        home_team_id=1, away_team_id=2, home_expected=4.5, away_expected=4.5,
        home_actual=7, away_actual=4,
    ) for index in range(8)]
    repeated = residual_context(
        observations, 1, 2, date(2026, 8, 23), force_enabled=True, league="MLB",
    )
    assert repeated["home"]["matchup_residual_flag"] is True
    assert repeated["home"]["matchup_residual_direction"] == "OVER_EXPECTED"
    assert repeated["home"]["matchup_direction_consistency"] == 1

    unseen = residual_context(
        observations, 1, 3, date(2026, 8, 23), force_enabled=True, league="MLB",
    )
    assert unseen["home"]["matchup_residual_flag"] is False
    assert unseen["home"]["matchup_residual_direction"] == "NO_SAMPLE"


def test_league_probability_calibration_uses_only_results_final_before_first_pitch():
    target_start = datetime(2026, 8, 24, 18, 30)
    observations = [ProbabilityObservation(
        game_id=index + 1, season=2026,
        available_at=target_start - timedelta(days=35 - index),
        probability=.64, outcome=float(index % 2),
    ) for index in range(35)]
    observations.extend([
        ProbabilityObservation(game_id=100, season=2025, available_at=target_start - timedelta(days=1),
                               probability=.99, outcome=1.0),
        ProbabilityObservation(game_id=101, season=2026, available_at=target_start + timedelta(hours=1),
                               probability=.99, outcome=1.0),
    ])
    game = SimpleNamespace(id=999, league="KBO", external_id="KBO-20260824-TEST", game_date=date(2026, 8, 24),
                           start_at=target_start)
    # This test is about which results the fitting window may see, so the gate is handed in.
    passed = {"status": "PASS", "validation_scope": "WIN_WALK_FORWARD", "sample_count": 400}
    context = LeagueProbabilityCalibrationHistory(observations, passed).context_for(game)
    assert context["enabled"] is True
    assert context["sample_count"] == 35
    assert context["future_results_used"] == 0
    # Repeated 64% forecasts that actually won only about half the time must be pulled inward.
    assert calibrated_probability(.64, context) < .55

    # With no measured verdict for the league, the map stays off: absent evidence the safe
    # action is to leave the probabilities exactly as the simulation produced them.
    unmeasured = LeagueProbabilityCalibrationHistory(observations).context_for(game)
    assert unmeasured["enabled"] is False
    assert unmeasured["reason"] == "WALK_FORWARD_VALIDATION_HOLD"
    assert unmeasured["validation"]["status"] == "HOLD"


def test_trajectory_model_reproduces_published_carry():
    """The physics has to land on numbers the sport already knows before it is worth trusting."""
    sea_level = air_density(22, 0)
    assert sea_level == pytest.approx(1.19, abs=.02)
    # Cold air is denser and altitude thins it, which is the whole mechanism.
    assert air_density(4, 0) > sea_level > air_density(22, 1580)

    reference = flight(103, 28, 0, sea_level)["distance_ft"]
    # A well struck ball at a home-run angle carries a little over 400 feet.
    assert 405 < reference < 445

    # Published rules of thumb: about 10 ft of carry lost going from 22C to 4C, and Coors adds
    # roughly 25-30 ft at a mile of elevation.
    cold = flight(103, 28, 0, air_density(4, 0))["distance_ft"]
    assert -16 < cold - reference < -6
    altitude = flight(103, 28, 0, air_density(22, 1580))["distance_ft"]
    assert 20 < altitude - reference < 38

    # And roughly five feet per five miles per hour of wind, which is what the effective-wind
    # fraction exists to reproduce - the raw reported speed gives three times too much.
    from backend.app.services.trajectory import WIND_EFFECTIVE_FRACTION
    tail = flight(103, 28, 0, sea_level, wind_mph=10 * WIND_EFFECTIVE_FRACTION)["distance_ft"]
    assert 6 < tail - reference < 18

    # Harder and higher both carry further, up to the angle where lift stops paying.
    assert flight(108, 28, 0, sea_level)["distance_ft"] > reference
    assert flight(103, 45, 0, sea_level)["distance_ft"] < flight(103, 30, 0, sea_level)["distance_ft"]


def test_park_home_run_index_ranks_parks_the_way_the_sport_does():
    indices = {code: park_home_run_index(code, elevation_m=1580 if code == "COL" else 10)["index"]
               for code in ("COL", "NYY", "CIN", "KC", "DET", "SFG")}
    # Coors and the short porch at Yankee Stadium are the friendliest yards in the game; Kauffman
    # and Comerica are among the least. Geometry alone should already say so.
    assert indices["COL"] > 1.05
    assert indices["NYY"] > 1.05
    assert indices["KC"] < .95
    assert indices["KC"] < indices["CIN"]
    assert indices["DET"] < indices["NYY"]

    missing = park_home_run_index("KBO-JAMSIL")
    assert missing["available"] is False
    assert missing["index"] == 1.0


def test_weather_multiplier_is_tonight_against_this_park_not_against_sea_level():
    def multiplier(stadium, temperature_f, wind, roofed=False):
        return park_weather_home_run_multiplier(stadium, {
            "available": True, "temperature_f": temperature_f, "wind": wind,
            "controlled_roof": roofed})

    warm = multiplier("Wrigley Field", 80, "15 mph, Out To CF")
    cold = multiplier("Wrigley Field", 48, "15 mph, In From CF")
    assert warm["multiplier"] > 1.05 > .95 > cold["multiplier"]

    # Coors is a mile up every night of the season, and the season park factor already knows it.
    # Charging the altitude again here would count it twice, so an ordinary evening at Coors has
    # to come out neutral.
    ordinary = multiplier("Coors Field", 72, "0 mph")
    assert ordinary["multiplier"] == pytest.approx(1.0, abs=.06)

    # A closed roof means the weather outside is not the weather the ball flies through.
    assert multiplier("Minute Maid Park", 95, "20 mph, Out To CF", roofed=True)["multiplier"] == pytest.approx(1.0, abs=.02)

    # No geometry and no weather both fall back to neutral rather than guessing. KBO parks are
    # absent from the table by design, so a KBO game is left exactly as it was.
    assert park_weather_home_run_multiplier("Jamsil Baseball Stadium", {"available": True}
                                            )["multiplier"] == 1.0
    assert park_weather_home_run_multiplier("Wrigley Field", {"available": False})["multiplier"] == 1.0
    assert park_weather_home_run_multiplier(None, None)["multiplier"] == 1.0

    # The weather rides on top of the season park factor rather than replacing it.
    park = {"home_run": 1.10, "double": 1.0, "triple": 1.0}
    assert batted_ball_clumping(park, .385, 1.0) < batted_ball_clumping(park, .385, 1.25)
    assert batted_ball_clumping(park, .385, .75) < batted_ball_clumping(park, .385, 1.0)


def test_park_and_contact_reshape_innings_without_rescaling_them():
    """A ballpark is already in the run means, so here it may only change the shape."""
    neutral = batted_ball_clumping({"home_run": 1.0, "double": 1.0, "triple": 1.0}, .385)
    assert neutral == pytest.approx(0.0, abs=1e-9)

    # A park where balls leave the yard makes the same expected total arrive in lumps.
    launching = batted_ball_clumping({"home_run": 1.35, "double": 1.25, "triple": 1.6}, .385)
    suppressing = batted_ball_clumping({"home_run": .75, "double": .9, "triple": .9}, .385)
    assert launching > 0 > suppressing

    # A park can only turn contact into home runs that were struck hard enough to leave, so the
    # same park does more for a lineup that squares the ball up.
    assert batted_ball_clumping({"home_run": 1.35, "double": 1.25, "triple": 1.6}, .44) > launching
    assert batted_ball_clumping({"home_run": 1.35, "double": 1.25, "triple": 1.6}, .33) < launching

    # However extreme the inputs, one game cannot be reshaped without limit.
    assert abs(batted_ball_clumping({"home_run": 3.0, "double": 3.0, "triple": 3.0}, .60)) <= .35
    assert abs(batted_ball_clumping({"home_run": .1, "double": .1, "triple": .1}, .10)) <= .35

    # Missing Statcast leaves the shape exactly where the league fit put it.
    assert batted_ball_clumping(None, None) == pytest.approx(0.0, abs=1e-9)

    # And in the simulation it is a shape change, not a scoring change.
    flat = simulate_scores(4.6, 4.4, 30_000, 20260825, league="MLB",
                           home_inning_variance_ratio=1.6, away_inning_variance_ratio=1.6)
    lumpy = simulate_scores(4.6, 4.4, 30_000, 20260825, league="MLB",
                            home_inning_variance_ratio=2.1, away_inning_variance_ratio=2.1)
    assert lumpy["mean_runs"]["home"] == pytest.approx(flat["mean_runs"]["home"], abs=.12)
    assert lumpy["game_shape"]["blowout_probability"] > flat["game_shape"]["blowout_probability"]
    assert lumpy["game_shape"]["either_shutout_probability"] > flat["game_shape"]["either_shutout_probability"]

    # Each club carries its own value, because the park is shared but the contact is not.
    one_sided = simulate_scores(4.6, 4.4, 30_000, 20260825, league="MLB",
                                home_inning_variance_ratio=2.1, away_inning_variance_ratio=1.2)
    home_runs = np.array([int(k.split(":")[1]) for k, c in one_sided["frequency_tables"]["scores"].items()
                          for _ in range(c)])
    away_runs = np.array([int(k.split(":")[0]) for k, c in one_sided["frequency_tables"]["scores"].items()
                          for _ in range(c)])
    assert home_runs.std() > away_runs.std()


def test_run_dispersion_is_built_from_three_separable_terms():
    """Each term does one job, and doing it must not undo another's."""
    def population(**kwargs):
        result = simulate_scores(4.6, 4.4, 30_000, 20260825, league="MLB",
                                 environment_variance=0.0, team_variance=.06, **kwargs)
        home, away = [], []
        for key, count in result["frequency_tables"]["scores"].items():
            a, h = (int(v) for v in key.split(":"))
            home.extend([h] * count); away.extend([a] * count)
        home, away = np.array(home), np.array(away)
        return result, home, away

    flat, flat_home, flat_away = population(inning_variance_ratio=1.0, matchup_variance=0.0)

    # Runs clumping inside an inning: more quiet innings and more crooked ones, same mean.
    clumped, clumped_home, clumped_away = population(inning_variance_ratio=1.9, matchup_variance=0.0)
    assert clumped["mean_runs"]["home"] == pytest.approx(flat["mean_runs"]["home"], abs=.12)
    rng = np.random.default_rng(3)
    poisson = _draw_runs(rng, np.full(200_000, .5), 1.0)
    overdispersed = _draw_runs(rng, np.full(200_000, .5), 1.9)
    assert overdispersed.mean() == pytest.approx(poisson.mean(), abs=.02)
    # Real half-innings are mostly quiet and occasionally crooked; a Poisson draw at the same
    # mean gives neither, which is what made runs dribble out one at a time.
    assert np.mean(overdispersed == 0) > np.mean(poisson == 0)
    assert np.mean(overdispersed >= 3) > 2 * np.mean(poisson >= 3)

    # The matchup tilt widens the margin while leaving the total alone, which is the whole point
    # of making it oppose itself rather than adding another independent per-club shock.
    tilted, tilted_home, tilted_away = population(inning_variance_ratio=1.0, matchup_variance=.20)
    assert tilted["mean_runs"]["home"] == pytest.approx(flat["mean_runs"]["home"], abs=.12)
    assert (tilted_home - tilted_away).std() > (flat_home - flat_away).std()
    assert (tilted_home + tilted_away).std() == pytest.approx((flat_home + flat_away).std(), rel=.04)
    # Real clubs' scores are very slightly negatively correlated; an opposing tilt is what
    # produces that, where an independent or shared shock cannot.
    assert np.corrcoef(tilted_home, tilted_away)[0, 1] < np.corrcoef(flat_home, flat_away)[0, 1]

    # The smooth per-club shock still widens both, so the three are genuinely separable.
    wide, wide_home, wide_away = population(inning_variance_ratio=1.0, matchup_variance=0.0,
                                            home_team_variance=.24, away_team_variance=.24)
    assert wide_home.std() > flat_home.std()
    assert (wide_home + wide_away).std() > (flat_home + flat_away).std()


def test_upset_factors_widen_the_distribution_instead_of_moving_the_means():
    """An upset is an uncertain game, not a better underdog, so these must move the spread."""
    def forecast(pregame, confirmed=True):
        home = SimpleNamespace(team=SimpleNamespace(name="Fav"), recent={"10": {"games": 10, "win_rate": .5}},
                               win_rate=.62, home_win_rate=.64, runs_per_game=5.4,
                               runs_allowed_per_game=3.9, ops=.790, era=3.4)
        away = SimpleNamespace(team=SimpleNamespace(name="Dog"), recent={"10": {"games": 10, "win_rate": .5}},
                               win_rate=.42, away_win_rate=.40, runs_per_game=3.9,
                               runs_allowed_per_game=5.4, ops=.690, era=4.8)
        pitcher = lambda pid, era, whip, ok: SimpleNamespace(player_id=pid, name=pid, confirmed=ok,
                                                             era=era, whip=whip, war=2.0)
        return predict_game(SimpleNamespace(external_id="UPSET", league="MLB", stadium="Yankee Stadium"),
                            home, away, pitcher("h", 3.4, 1.15, confirmed), pitcher("a", 4.8, 1.42, True),
                            [], game_context={"pregame": pregame})

    # The channel itself, isolated: nothing but the spread changes, and the underdog gains.
    steady = simulate_scores(5.4, 3.9, 20_000, 20260825, league="MLB", team_variance=.20)
    volatile = simulate_scores(5.4, 3.9, 20_000, 20260825, league="MLB", team_variance=.30)
    assert volatile["mean_runs"]["away"] == pytest.approx(steady["mean_runs"]["away"], abs=.15)
    assert volatile["away_two_way_probability"] > steady["away_two_way_probability"]

    base = forecast({})
    tired = forecast({"bullpen": {"home": {"available": True, "fatigue_index": 1.0}}})
    assert tired["payload"]["simulation_home_team_variance"] > base["payload"]["simulation_home_team_variance"]
    assert tired["payload"]["upset_volatility"]["home_volatility"] > 0
    assert tired["away_win_probability"] > base["away_win_probability"]

    # Weather is a condition of the ballpark, so it widens both clubs alike.
    windy = forecast({"weather": {"available": True, "run_multiplier": 1.08}})
    assert windy["payload"]["upset_volatility"]["shared_volatility"] > (
        base["payload"]["upset_volatility"]["shared_volatility"])
    assert windy["payload"]["simulation_away_team_variance"] > base["payload"]["simulation_away_team_variance"]

    # However many factors line up, one game cannot be turned into noise.
    everything = forecast({"bullpen": {"home": {"available": True, "fatigue_index": 1.0}},
                           "schedule": {"home": {"fatigue_index": .5}},
                           "weather": {"available": True, "run_multiplier": 1.20}}, confirmed=False)
    assert everything["payload"]["simulation_home_team_variance"] <= .32
    assert everything["away_win_probability"] > tired["away_win_probability"]


def test_upset_watch_names_a_side_only_when_it_beats_the_posted_price():
    def forecast(market):
        home = SimpleNamespace(team=SimpleNamespace(name="Fav"), recent={"10": {"games": 10, "win_rate": .5}},
                               win_rate=.62, home_win_rate=.64, runs_per_game=5.4,
                               runs_allowed_per_game=3.9, ops=.790, era=3.4)
        away = SimpleNamespace(team=SimpleNamespace(name="Dog"), recent={"10": {"games": 10, "win_rate": .5}},
                               win_rate=.42, away_win_rate=.40, runs_per_game=3.9,
                               runs_allowed_per_game=5.4, ops=.690, era=4.8)
        pitcher = lambda pid, era, whip: SimpleNamespace(player_id=pid, name=pid, confirmed=True,
                                                         era=era, whip=whip, war=2.0)
        return predict_game(SimpleNamespace(external_id="UW", league="MLB", stadium="Yankee Stadium"),
                            home, away, pitcher("h", 3.4, 1.15), pitcher("a", 4.8, 1.42), [],
                            game_context={"market": market})["payload"]["upset_watch"]

    base = {"provider": "t", "collected_at": "2026-08-25T00:00:00", "bookmaker_count": 5}
    generous = forecast({**base, "home_implied_probability": .70, "away_implied_probability": .30})
    assert generous["underdog"] == "AWAY"
    assert generous["underdog_source"] == "MARKET"
    assert generous["edge"] > generous["edge_threshold"]
    assert generous["flagged"] is True

    # The same forecast at a fair price is not an opportunity.
    fair = forecast({**base, "home_implied_probability": .64, "away_implied_probability": .36})
    assert fair["model_probability"] == pytest.approx(generous["model_probability"], abs=.03)
    assert fair["flagged"] is False

    # Model confidence alone never flags one: with no price there is nothing to disagree with.
    unpriced = forecast({})
    assert unpriced["comparable"] is False
    assert unpriced["edge"] is None
    assert unpriced["flagged"] is False
    assert unpriced["underdog_source"] == "MODEL"


def test_win_calibration_gate_is_measured_walk_forward_not_hardcoded():
    """The gate must open on this league's own out-of-sample evidence, and only on that."""
    start = datetime(2026, 4, 1, 18, 30)

    def history(probability, win_rate, count=600):
        # Forecasts that repeatedly claim `probability` while winning only `win_rate` of the
        # time are exactly what a shrinking map is supposed to fix.
        return [ProbabilityObservation(
            game_id=index + 1, season=2026, available_at=start + timedelta(hours=index),
            probability=probability, outcome=1.0 if index % 100 < win_rate * 100 else 0.0,
        ) for index in range(count)]

    overconfident = walk_forward_win_validation(history(.75, .55), "MLB")
    assert overconfident["status"] == "PASS"
    assert overconfident["validation_scope"] == "WIN_WALK_FORWARD"
    assert overconfident["brier_delta"] < 0
    assert overconfident["log_loss_delta"] < 0
    assert overconfident["calibrated_brier"] < overconfident["raw_brier"]

    # Already-honest forecasts have nothing to gain, so the map stays off rather than being
    # applied because it happens to be available.
    honest = walk_forward_win_validation(history(.55, .55), "MLB")
    assert honest["status"] == "HOLD"
    assert honest["reason"] == "NO_OUT_OF_SAMPLE_IMPROVEMENT"

    # Too few finals is a hold, never a borrowed verdict from another league or another season.
    sparse = walk_forward_win_validation(history(.75, .55, count=40), "MLB")
    assert sparse["status"] == "HOLD"
    assert sparse["reason"] == "INSUFFICIENT_FINALS"
    assert CALIBRATION_VALIDATION["MLB"]["status"] == "HOLD"


def test_segmented_calibration_challenger_shrinks_overconfident_strong_favorites():
    start = datetime(2026, 4, 1, 18, 30)
    rows = []
    for index in range(720):
        strong = index % 2 == 0
        probability = .76 if strong else .56
        realized_rate = .56 if strong else .56
        rows.append(ProbabilityObservation(
            game_id=index + 1, season=2026, available_at=start + timedelta(hours=index),
            probability=probability,
            outcome=1.0 if index % 100 < realized_rate * 100 else 0.0,
            stage="T_MINUS_15M" if index % 3 else "T_MINUS_24H",
        ))
    report = walk_forward_segmented_validation(rows, "MLB")
    assert report["sample_count"] >= 150
    assert report["segmented_predictions"] >= 60
    assert report["candidate_brier"] < report["raw_brier"]
    assert report["candidate_log_loss"] < report["raw_log_loss"]


def test_backtest_probability_calibration_ignores_results_not_final_at_cutoff():
    cutoff = datetime(2026, 8, 24, 18, 30)
    prior = [
        (cutoff - timedelta(days=30 - index), .64, float(index % 2))
        for index in range(29)
    ]
    future = [
        (cutoff + timedelta(minutes=index + 1), .99, 1.0)
        for index in range(100)
    ]
    # The 100 future finals cannot push a 29-game history over the 30-game activation line.
    assert _walk_forward_probability(.64, "MLB", cutoff, {"MLB": prior + future}) == .64
    prior.append((cutoff - timedelta(minutes=1), .64, 1.0))
    assert _walk_forward_probability(.64, "MLB", cutoff, {"MLB": prior + future}) < .56


def test_calibrated_winner_branch_reweighting_recomputes_one_coherent_population():
    baseline = simulate_scores(4.9, 4.2, 20_000, 20260824, league="MLB",
                               headline_total_line=8.5, headline_home_spread=-1.5)
    context = {
        "enabled": True, "method": "TEST_PLATT", "sample_count": 400,
        "slope": .58, "intercept": -.12, "future_results_used": 0,
    }
    calibrated = simulate_scores(4.9, 4.2, 20_000, 20260824, league="MLB",
                                 headline_total_line=8.5, headline_home_spread=-1.5,
                                 probability_calibration=context)
    metadata = calibrated["probability_calibration"]
    target = calibrated_probability(metadata["raw_home_two_way_probability"], context)
    assert metadata["raw_home_two_way_probability"] == pytest.approx(
        baseline["probability_calibration"]["raw_home_two_way_probability"])
    assert calibrated["home_two_way_probability"] == pytest.approx(target, abs=1 / 20_000)
    assert sum(calibrated["frequency_tables"]["outcomes"].values()) == 20_000
    assert sum(calibrated["frequency_tables"]["scores"].values()) == 20_000
    assert calibrated["handicap"]["home_minus_1_5"] + calibrated["handicap"]["away_plus_1_5"] == pytest.approx(1)
    assert calibrated["market_handicap"]["minus_probability"] + calibrated["market_handicap"]["plus_probability"] == pytest.approx(1)
    assert calibrated["totals"]["8.5"]["over"] + calibrated["totals"]["8.5"]["under"] == pytest.approx(1)
    assert calibrated["full_distribution_score"] == calibrated["top_scores"][0]
    assert calibrated["winner_conditional_score"] == calibrated["projected_score"]


def test_calibration_gate_checks_runs_margin_run_line_and_totals_together():
    observations = []
    for index in range(200):
        home_score, away_score = ((6, 3) if index % 2 == 0 else (3, 6))
        home_win = home_score > away_score
        raw = {
            "home_two_way_probability": .5, "mean_runs": {"home": 4.5, "away": 4.5},
            "handicap": {"home_minus_1_5": .3, "away_minus_1_5": .3},
            "totals": {line: {"over": .5} for line in ("7.5", "8.5", "9.5")},
        }
        calibrated = {
            "home_two_way_probability": .8 if home_win else .2,
            "mean_runs": {"home": float(home_score), "away": float(away_score)},
            "handicap": {
                "home_minus_1_5": .8 if home_win else .05,
                "away_minus_1_5": .05 if home_win else .8,
            },
            "totals": {"7.5": {"over": .9}, "8.5": {"over": .9}, "9.5": {"over": .1}},
        }
        observations.append(DistributionCalibrationObservation(
            game_id=index, raw=raw, calibrated=calibrated,
            home_score=home_score, away_score=away_score,
        ))
    validation = distribution_calibration_validation(observations, "MLB")
    assert validation["status"] == "PASS"
    assert validation["validation_scope"] == "FULL_SCORE_DISTRIBUTION"
    assert not validation["failed_metrics"]
    assert validation["deltas"]["run_mae"] < 0
    assert validation["deltas"]["handicap_brier"] < 0
    assert validation["deltas"]["total_brier"] < 0


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
    assert row["home_decimal_odds"] == 1.85
    assert row["away_decimal_odds"] == 2.05
    # The run line's point barely moves, so its price is where the market states how likely the
    # favourite is to clear it. Without a price there is nothing to compare a model against.
    assert row["home_spread_probability"] is None

    priced = {**event, "bookmakers": [{"key": "a", "markets": [{"key": "spreads", "outcomes": [
        {"name": "KIA Tigers", "point": -1.5, "price": 2.30},
        {"name": "Kiwoom Heroes", "point": 1.5, "price": 1.62}]}]}]}
    priced_row = _consensus_event(priced)
    assert priced_row["home_spread_probability"] == pytest.approx(
        (1 / 2.30) / ((1 / 2.30) + (1 / 1.62)), abs=1e-6)
    # De-vigged, so the two sides are complements rather than summing above one.
    assert priced_row["home_spread_probability"] < 1 / 2.30


def test_market_prices_are_only_devigged_at_the_line_they_were_quoted_for():
    def event(first_point, second_point):
        return {"id": "e", "home_team": "KIA Tigers", "away_team": "Kiwoom Heroes", "bookmakers": [
            {"key": "a", "markets": [{"key": "totals", "outcomes": [
                {"name": "Over", "point": first_point, "price": 1.95},
                {"name": "Under", "point": first_point, "price": 1.87}]}]},
            {"key": "b", "markets": [{"key": "totals", "outcomes": [
                {"name": "Over", "point": second_point, "price": 2.40},
                {"name": "Under", "point": second_point, "price": 1.58}]}]},
        ]}

    agreed = _consensus_event(event(8.5, 8.5))
    assert agreed["total_line"] == 8.5
    assert agreed["total_over_probability"] == pytest.approx(
        median([(1 / 1.95) / ((1 / 1.95) + (1 / 1.87)),
                (1 / 2.40) / ((1 / 2.40) + (1 / 1.58))]), abs=1e-6)

    # The books straddle the line, so the median total is a number neither of them quoted. A
    # price for a bet nobody offered would be invented, so none is reported.
    split = _consensus_event(event(8.5, 9.5))
    assert split["total_line"] == 9.0
    assert split["total_over_probability"] is None

    # A book quoting its two halves at different numbers is quoting two different bets.
    mismatched = _consensus_event({"id": "e", "home_team": "H", "away_team": "A", "bookmakers": [
        {"key": "a", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "point": 8.5, "price": 1.95},
            {"name": "Under", "point": 9.5, "price": 1.87}]}]},
    ]})
    assert mismatched["total_over_probability"] is None


def test_derived_market_comparison_accumulates_from_finished_games_only():
    """Each finished game becomes one labelled row per derived market, market price included."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="MLB", code="AW", name="Away")
        home = Team(league="MLB", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        model = ModelVersion(name="MLB_TEST", algorithm="test", feature_schema={}, checksum="c")
        session.add(model); session.flush()
        base = datetime(2026, 4, 1, 18, 30)

        def add(index, home_score, away_score, *, leaked=False, legacy=False):
            start = base + timedelta(days=index)
            game = Game(external_id=f"MC-{index}", league="MLB", game_date=start.date(),
                        start_at=start, start_time=start.time(), away_team_id=away.id,
                        home_team_id=home.id, status="FINAL", source="t", source_url="t",
                        collected_at=start)
            session.add(game); session.flush()
            session.add(GameResult(game_id=game.id, away_score=away_score, home_score=home_score,
                                   finalized_at=start + timedelta(hours=3), source_url="t"))
            payload = {"summary_schema_version": SIMULATION_SUMMARY_SCHEMA_VERSION}
            if not legacy:
                payload |= {
                    # The model reached 9.5 and -2.5 on its own; the book posted 8.5 and -1.5.
                    "model_fair_lines": {"total_line": 9.5, "home_spread": -2.5,
                                         "market_total_line": 8.5, "market_home_spread": -1.5},
                    "winner_conditional_market": {
                        "headline_total": {"line": 8.5, "line_source": "MARKET",
                                           "model_over_probability": .60,
                                           "market_over_probability": .50},
                        "handicap": {"run_line": 1.5, "minus_side": "HOME",
                                     "model_minus_probability": .55,
                                     "market_minus_probability": .45},
                    },
                }
            session.add(Prediction(
                game_id=game.id, model_version_id=model.id, input_hash=f"h{index}",
                origin="HISTORICAL_REPLAY" if leaked else "LIVE_PREGAME",
                data_cutoff=start - timedelta(hours=1),
                home_win_probability=.6, away_win_probability=.4,
                home_expected_runs=5.0, away_expected_runs=3.0, confidence=.5,
                payload=payload, created_at=start - timedelta(hours=1),
                training_eligible=not leaked,
                leakage_audit={"passed": False} if leaked else {"passed": True},
            ))
            return game

        add(0, 7, 3)   # total 10 over 8.5, home by 4 covers -1.5
        add(1, 6, 4)   # total 10 over, home by 2 covers
        add(2, 2, 1)   # total 3 under, home by 1 does not cover
        add(3, 5, 5)   # total 10 over, level margin does not cover
        add(4, 4, 4)   # total 8 under, level margin does not cover
        # An unaudited replay could have seen its own result, so it must never be scored.
        add(5, 9, 0, leaked=True)
        # A forecast saved before the model recorded its own reference points has nothing to compare.
        add(6, 9, 0, legacy=True)
        session.flush()

        report = DerivedMarketHistory.from_session(session, "MLB").report()
        assert report["sample_size"] == 15  # five usable games x three markets
        assert set(report["markets"]) == {"TOTAL", "RUN_LINE", "MONEYLINE"}
        total = report["markets"]["TOTAL"]
        run_line = report["markets"]["RUN_LINE"]

        # Our own line sat a run above the posted total in every game.
        assert total["line_comparison"]["sample_size"] == 5
        assert total["line_comparison"]["mean_difference"] == pytest.approx(1.0)
        assert total["line_comparison"]["agreement_rate"] == 0.0
        assert run_line["line_comparison"]["mean_difference"] == pytest.approx(-1.0)

        # Three of five went over, and we said 60% every time.
        assert total["realized"]["priced_side_win_rate"] == pytest.approx(.6)
        assert total["probability_comparison"]["mean_difference"] == pytest.approx(.10)
        assert total["probability_comparison"]["model_brier"] == pytest.approx(
            (3 * .4 ** 2 + 2 * .6 ** 2) / 5)
        assert total["probability_comparison"]["market_brier"] == pytest.approx(.25)
        assert total["probability_comparison"]["brier_improvement"] > 0

        # Two of five covered the run line while we said 55%, so we were worse than the book here.
        assert run_line["realized"]["priced_side_win_rate"] == pytest.approx(.4)
        assert run_line["probability_comparison"]["brier_improvement"] < 0

        # Measured, but nowhere near enough games to fit a correction from.
        assert total["status"]["state"] == "COLLECTING"
        assert run_line["status"]["state"] == "COLLECTING"
        assert report["leagues"]["MLB"]["TOTAL"]["sample_size"] == 5
        # An upset call is a claim about the moneyline, so that market is accumulated too. No
        # `upset_watch` was stored on these fixtures, so it has our probability but no price.
        moneyline = report["markets"]["MONEYLINE"]
        # Two of the five fixtures ended level, which the moneyline cannot judge.
        assert moneyline["sample_size"] == 5
        assert moneyline["realized"]["sample_size"] == 3
        assert moneyline["probability_comparison"]["sample_size"] == 0
        assert moneyline["line_comparison"]["sample_size"] == 0


def test_derived_market_comparison_skips_pushes_and_unpriced_games():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        away = Team(league="KBO", code="AW", name="Away")
        home = Team(league="KBO", code="HM", name="Home")
        session.add_all([away, home]); session.flush()
        model = ModelVersion(name="KBO_TEST", algorithm="test", feature_schema={}, checksum="c")
        session.add(model); session.flush()
        start = datetime(2026, 4, 1, 18, 30)
        game = Game(external_id="MC-PUSH", league="KBO", game_date=start.date(), start_at=start,
                    start_time=start.time(), away_team_id=away.id, home_team_id=home.id,
                    status="FINAL", source="t", source_url="t", collected_at=start)
        session.add(game); session.flush()
        # Total lands exactly on an integer line and the margin lands exactly on the run line.
        session.add(GameResult(game_id=game.id, away_score=4, home_score=5,
                               finalized_at=start + timedelta(hours=3), source_url="t"))
        session.add(Prediction(
            game_id=game.id, model_version_id=model.id, input_hash="push",
            origin="LIVE_PREGAME", data_cutoff=start - timedelta(hours=1),
            home_win_probability=.6, away_win_probability=.4,
            home_expected_runs=5.0, away_expected_runs=4.0, confidence=.5,
            created_at=start - timedelta(hours=1), training_eligible=True, leakage_audit={},
            payload={"summary_schema_version": SIMULATION_SUMMARY_SCHEMA_VERSION,
                     "model_fair_lines": {"total_line": 9.5, "home_spread": -1.5,
                                          "market_total_line": 9.0, "market_home_spread": None},
                     "winner_conditional_market": {
                         "headline_total": {"line": 9.0, "line_source": "MARKET",
                                            "model_over_probability": .55,
                                            "market_over_probability": None},
                         "handicap": {"run_line": 1.0, "minus_side": "HOME",
                                      "model_minus_probability": .52,
                                      "market_minus_probability": None}}},
        ))
        session.flush()
        report = DerivedMarketHistory.from_session(session, "KBO").report()
        # Both rows exist, but a push is not a labelled outcome and an unpriced game is not a
        # comparison, so neither market can be scored from this game.
        assert report["sample_size"] == 3
        assert report["markets"]["TOTAL"]["realized"]["sample_size"] == 0
        assert report["markets"]["TOTAL"]["probability_comparison"]["sample_size"] == 0
        assert report["markets"]["TOTAL"]["probability_comparison"]["brier_improvement"] is None
        assert report["markets"]["RUN_LINE"]["realized"]["sample_size"] == 0
        # The line comparison still works for the market that published a number.
        assert report["markets"]["TOTAL"]["line_comparison"]["mean_difference"] == pytest.approx(.5)
        assert report["markets"]["RUN_LINE"]["line_comparison"]["sample_size"] == 0
