from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import Text, create_engine, event
from sqlalchemy.orm import Session
from backend.app.config import KST, database_url_from_environment
from backend.app.database.base import Base
from backend.app.models import Game, GameResult, ModelVersion, Prediction, PredictionSnapshot, Team, UserClaudeSetting
from backend.app.repositories.repository import _prediction_changes, game_cards, game_dates
from backend.app.services.backtest import walk_forward_backtest
from backend.app.services import claude_advisor, personal_claude, runtime_secrets, user_auth
from backend.app.services.claude_advisor import blend_with_claude
from backend.app.collectors.kbo.client import (KboClient, _batter_pitcher_split, _data_id_table,
                                               _pitcher_opponent_split, _rank_table, _record_rate)
from backend.app.collectors.mlb.client import MlbClient
from backend.app.collectors.odds import _consensus_event
from backend.app.services.feature_engineering import _effective_lineup_ops, _lineup_matchup_summary
from backend.app.services.refresh import (_market_event_date, _market_refresh_due, _months_for_recent,
                                          _prediction_stage, _recent_by_team)
from backend.app.services.simulation import simulate_scores
from backend.app.services.prediction import predict_game, select_primary_score
from backend.app.services.jobs import _missing_leagues_for_date, checkpoint_stage_for_minutes
from backend.app.services.model_lifecycle import _promotion_decision, predict_with_runtime
from backend.app.services.runtime_secrets import decrypt_secret, encrypt_secret


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
    assert home_runs == 10.0
    assert away_runs == .6


def test_supabase_database_url_selects_psycopg_driver(monkeypatch):
    monkeypatch.setenv("BASEBALL_DATABASE_URL", "postgresql://user:pass@example.com:6543/postgres?sslmode=require")
    assert database_url_from_environment() == (
        "postgresql+psycopg://user:pass@example.com:6543/postgres?sslmode=require"
    )


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
    assert len(representative["inning_line"]) == 9
    assert sum(item["away"] for item in representative["inning_line"]) == representative["away"]
    assert sum(item["home"] for item in representative["inning_line"]) == representative["home"]
    assert 0 < representative["trajectory_probability_given_score"] <= 1
    assert first["team_quantiles"]["away"]["p10"] < first["team_quantiles"]["away"]["p90"]
    assert first["total_quantiles"]["p10"] < first["total_quantiles"]["p90"]
    assert 0 < first["game_shape"]["blowout_probability"] < 1


def test_representative_score_prioritizes_winner_and_expected_total():
    scores = [
        {"away": 3, "home": 4, "probability": .08},
        {"away": 4, "home": 6, "probability": .06},
        {"away": 5, "home": 3, "probability": .07},
    ]
    selected = select_primary_score(scores, home_expected=5.6, away_expected=4.2, home_win_probability=.61)
    assert selected == scores[1]
    assert selected["away"] + selected["home"] == round(5.6 + 4.2)


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
    assert after["payload"]["summary_schema_version"] == 2
    assert after["payload"]["coherence_valid"] is True
    assert after["home_win_probability"] == after["payload"]["simulation_home_probability"]
    assert after["away_win_probability"] == round(1 - after["home_win_probability"], 4)


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
            ]},
            {"key": "b", "markets": [
                {"key": "h2h", "outcomes": [{"name": "KIA Tigers", "price": 1.9}, {"name": "Kiwoom Heroes", "price": 2.0}]},
                {"key": "totals", "outcomes": [{"name": "Over", "point": 9.5}, {"name": "Under", "point": 9.5}]},
            ]},
        ],
    }
    row = _consensus_event(event)
    assert row["bookmaker_count"] == 2
    assert row["total_line"] == 9.0
    assert abs(row["home_implied_probability"] + row["away_implied_probability"] - 1) < 1e-9
