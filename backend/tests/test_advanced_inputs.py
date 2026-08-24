from datetime import date, datetime
from types import SimpleNamespace

import numpy as np

from backend.app.collectors.kbo.client import _hitter_pitcher_type_split, _pitcher_hand
from backend.app.collectors.mlb.client import _pitch_summary
from backend.app.services.feature_engineering import _dynamic_park_factors
from backend.app.services.plate_engine import _adjustment
from backend.app.services.team_strength import TeamStrengthHistory, _Result


def test_team_strength_uses_only_games_before_target_and_shrinks_ratings():
    rows = [
        _Result(1, 2026, datetime(2026, 4, 1, 18), date(2026, 4, 1), 1, 2, 8, 2),
        _Result(2, 2026, datetime(2026, 4, 2, 18), date(2026, 4, 2), 1, 3, 6, 1),
        # This future result must not enter the April 3 forecast.
        _Result(3, 2026, datetime(2026, 4, 4, 18), date(2026, 4, 4), 2, 1, 20, 0),
    ]
    target = SimpleNamespace(id=99, game_date=date(2026, 4, 3), start_at=datetime(2026, 4, 3, 18),
                             home_team_id=1, away_team_id=2)
    context = TeamStrengthHistory(rows, "MLB").context_for(target)
    assert context["home"]["games"] == 2
    assert context["away"]["games"] == 1
    assert context["elo_diff"] > 0
    assert abs(context["home"]["elo"] - 1500) < 100  # small samples are shrunk


def test_kbo_official_handedness_and_pitcher_type_split_parsers():
    pitcher = '<span id="x_playerProfile_lblPosition">투수(좌투좌타)</span>'
    hitter = """
    <table summary="투수유형별 기록"><tbody>
      <tr><td>좌투수</td><td>.300</td><td>100</td><td>30</td><td>5</td><td>1</td>
          <td>4</td><td>20</td><td>10</td><td>2</td><td>20</td><td>1</td></tr>
    </tbody></table>
    """
    assert _pitcher_hand(pitcher) == "L"
    split = _hitter_pitcher_type_split(hitter, "L")
    assert split["platoon_plate_appearances"] == 112
    assert split["platoon_ops"] > .7


def test_pitch_summary_detects_velocity_and_arsenal_mix():
    rows = [
        {"pitch_type": "FF", "release_speed": "96", "release_spin_rate": "2400", "pfx_x": ".2", "pfx_z": "1.3"},
        {"pitch_type": "FF", "release_speed": "94", "release_spin_rate": "2300", "pfx_x": ".1", "pfx_z": "1.2"},
        {"pitch_type": "SL", "release_speed": "86", "release_spin_rate": "2500", "pfx_x": ".8", "pfx_z": ".2"},
    ]
    summary = _pitch_summary(rows)
    assert summary["fastball_velocity"] == 95
    assert summary["pitches"]["FF"]["usage"] == .6667
    assert summary["spin_rate"] > 2300


def test_handed_park_factor_changes_extra_base_hit_weights():
    pregame = {"park_factors": {"available": True, "by_batter_hand": {
        "ALL": {"runs": 1, "woba": 1, "double": 1, "triple": 1, "home_run": 1},
        "L": {"runs": 1.02, "woba": 1.03, "double": .95, "triple": .90, "home_run": 1.15},
        "R": {"runs": .98, "woba": .99, "double": 1.05, "triple": 1.10, "home_run": .90},
    }}}
    lineups = [SimpleNamespace(side="home", batting_side="L"), SimpleNamespace(side="away", batting_side="R")]
    home, away, events = _dynamic_park_factors(pregame, lineups)
    assert home > away
    weights = _adjustment(np.array([1.0]), 1.0, events["home"])
    assert weights[0, 6] == 1.15  # HOMER column
