"""Batted-ball flight, and whether a given ball clears a given wall.

The physics is Alan Nathan's fly-ball model (Analysis of Baseball Trajectories, University of
Illinois, 2017: https://baseball.physics.illinois.edu/TrajectoryAnalysis.pdf), integrated with
RK4 exactly as the paper describes. Drag and lift use his fitted coefficients; spin decay and
precession are ignored, as they are in the paper.

Why this exists at all: a season-long park factor says a ballpark suppressed home runs on
average last year. It cannot say that tonight is 4C and the wind is blowing in, which changes
the carry on every ball struck. Air density is the single largest environmental lever on fly
ball distance, and it is knowable before first pitch from data already collected.

Distances are in feet and speeds in mph at the boundary; the integration works in feet and
feet per second.
"""

from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


# Nathan, Eq. 11: fitted to 2016 Trackman fly balls at Tropicana Field.
DRAG_INTERCEPT = .297
DRAG_SPIN_SLOPE = .0292
LIFT_NUMERATOR = 1.120
LIFT_INTERCEPT = .583
LIFT_SLOPE = 2.333
# Nathan, Eq. 9, at the nominal ball: K = 5.509e-3 /ft scaled by air density.
DRAG_CONSTANT = 5.509e-3
REFERENCE_AIR_DENSITY = 1.225
GRAVITY = 32.174
BALL_RADIUS_FT = (9.125 / math.pi) / 2 / 12
# Home plate to the front of the plate area; batted balls are launched from about here.
LAUNCH_HEIGHT_FT = 3.0
INTEGRATION_STEP = .01
MAX_FLIGHT_SECONDS = 9.0

# Backspin is not measured by anything we collect and it changes carry substantially, so it is
# modelled from launch angle: a ball driven at a home-run angle carries more backspin than a
# liner. Set so that 30 degrees gives about 2,400 rpm, near the 2,500 Nathan uses as a reference
# batted ball, which reproduces his published carry (100 mph at 30 degrees, sea level) to within
# about 2%. That residual is a uniform bias, and every number this module is actually used for
# is a ratio against a league-average park under identical conditions, where it cancels.
BACKSPIN_BASE_RPM = 0.0
BACKSPIN_PER_DEGREE = 80.0
BACKSPIN_MAX_RPM = 3000.0

_GEOMETRY_PATH = Path(__file__).resolve().parent.parent / "data" / "mlb_park_geometry.json"


@lru_cache(maxsize=1)
def park_geometry() -> dict[str, dict[str, Any]]:
    """Wall distance and height in feet at each degree from the left-field foul pole."""
    with _GEOMETRY_PATH.open(encoding="utf-8") as handle:
        return json.load(handle).get("parks") or {}


def air_density(temperature_c: float = 22.0, elevation_m: float = 0.0,
                relative_humidity: float = .5, pressure_mmhg: float = 760.0) -> float:
    """Air density in kg/m^3 from the conditions a forecast can actually know beforehand.

    Cold, dense air at sea level kills carry; thin air at altitude adds it. This is the term
    that makes Denver play differently from Miami before anyone swings.
    """
    temperature = max(-30.0, min(50.0, float(temperature_c)))
    saturated = 4.5841 * math.exp((18.687 - temperature / 234.5) * temperature / (257.14 + temperature))
    # Barometric reduction with height, then the partial pressure water vapour displaces.
    station = pressure_mmhg * math.exp(-1.217e-4 * max(0.0, float(elevation_m)))
    vapour = .3783 * max(0.0, min(1.0, relative_humidity)) * saturated
    return 1.2929 * (273.0 / (temperature + 273.0)) * ((station - vapour) / 760.0)


def _spin_rpm(launch_angle_deg: float) -> float:
    angle = max(0.0, float(launch_angle_deg))
    return min(BACKSPIN_MAX_RPM, BACKSPIN_BASE_RPM + BACKSPIN_PER_DEGREE * angle)


def flight(exit_velocity_mph: float, launch_angle_deg: float, spray_angle_deg: float,
           density: float = REFERENCE_AIR_DENSITY, wind_mph: float = 0.0,
           wind_from_deg: float = 0.0) -> dict[str, float]:
    """Integrate one batted ball, returning where it is when it reaches the fence.

    `spray_angle_deg` is 0 up the middle, negative to left field, positive to right - the same
    convention the wall table uses once shifted. `wind_from_deg` is the direction the wind blows
    toward, in the same frame, so a positive `wind_mph` straight out is 0.
    """
    speed = max(1.0, float(exit_velocity_mph)) * 5280 / 3600
    theta = math.radians(float(launch_angle_deg))
    phi = math.radians(float(spray_angle_deg))
    velocity = np.array([speed * math.cos(theta) * math.sin(phi),
                         speed * math.cos(theta) * math.cos(phi),
                         speed * math.sin(theta)], dtype=float)
    position = np.array([0.0, 0.0, LAUNCH_HEIGHT_FT], dtype=float)

    omega = _spin_rpm(launch_angle_deg) * 2 * math.pi / 60
    # Pure backspin: the spin axis is horizontal and perpendicular to the direction of travel,
    # which is Nathan's assumption once gyrospin is dropped.
    spin = omega * np.array([math.cos(phi), -math.sin(phi), 0.0], dtype=float)
    spin_magnitude = float(np.linalg.norm(spin)) or 1e-9

    wind_speed = float(wind_mph) * 5280 / 3600
    wind_direction = math.radians(float(wind_from_deg))
    wind = np.array([wind_speed * math.sin(wind_direction),
                     wind_speed * math.cos(wind_direction), 0.0], dtype=float)

    constant = DRAG_CONSTANT * (density / REFERENCE_AIR_DENSITY)
    drag_coefficient = DRAG_INTERCEPT + DRAG_SPIN_SLOPE * (_spin_rpm(launch_angle_deg) / 1000.0)

    def acceleration(state_velocity: np.ndarray) -> np.ndarray:
        # Drag and lift act on the velocity relative to the air, not to the ground.
        relative = state_velocity - wind
        speed_now = float(np.linalg.norm(relative))
        if speed_now < 1e-6:
            return np.array([0.0, 0.0, -GRAVITY])
        spin_factor = BALL_RADIUS_FT * spin_magnitude / speed_now
        lift_coefficient = LIFT_NUMERATOR * spin_factor / (LIFT_INTERCEPT + LIFT_SLOPE * spin_factor)
        magnus = np.cross(spin, relative) / spin_magnitude
        return (-constant * drag_coefficient * speed_now * relative
                + constant * lift_coefficient * speed_now * magnus
                - np.array([0.0, 0.0, GRAVITY]))

    apex = position[2]
    elapsed = 0.0
    while elapsed < MAX_FLIGHT_SECONDS and position[2] > 0:
        k1v = acceleration(velocity)
        k2v = acceleration(velocity + INTEGRATION_STEP / 2 * k1v)
        k3v = acceleration(velocity + INTEGRATION_STEP / 2 * k2v)
        k4v = acceleration(velocity + INTEGRATION_STEP * k3v)
        previous = position.copy()
        position = position + INTEGRATION_STEP * (
            velocity + INTEGRATION_STEP / 6 * (k1v + k2v + k3v))
        velocity = velocity + INTEGRATION_STEP / 6 * (k1v + 2 * k2v + 2 * k3v + k4v)
        apex = max(apex, position[2])
        elapsed += INTEGRATION_STEP
        if position[2] <= 0 < previous[2]:
            # Land on the plane of the field rather than a step past it.
            share = previous[2] / max(previous[2] - position[2], 1e-9)
            position = previous + share * (position - previous)
            break
    horizontal = float(math.hypot(position[0], position[1]))
    return {"distance_ft": horizontal, "hang_time_s": elapsed, "apex_ft": float(apex),
            "spray_angle_deg": float(spray_angle_deg)}


def height_at_distance(exit_velocity_mph: float, launch_angle_deg: float, spray_angle_deg: float,
                       fence_distance_ft: float, density: float = REFERENCE_AIR_DENSITY,
                       wind_mph: float = 0.0, wind_from_deg: float = 0.0) -> float:
    """How high the ball is when it first reaches the fence, or -1 if it never gets there."""
    speed = max(1.0, float(exit_velocity_mph)) * 5280 / 3600
    theta = math.radians(float(launch_angle_deg))
    phi = math.radians(float(spray_angle_deg))
    velocity = np.array([speed * math.cos(theta) * math.sin(phi),
                         speed * math.cos(theta) * math.cos(phi),
                         speed * math.sin(theta)], dtype=float)
    position = np.array([0.0, 0.0, LAUNCH_HEIGHT_FT], dtype=float)
    omega = _spin_rpm(launch_angle_deg) * 2 * math.pi / 60
    spin = omega * np.array([math.cos(phi), -math.sin(phi), 0.0], dtype=float)
    spin_magnitude = float(np.linalg.norm(spin)) or 1e-9
    wind_speed = float(wind_mph) * 5280 / 3600
    wind_direction = math.radians(float(wind_from_deg))
    wind = np.array([wind_speed * math.sin(wind_direction),
                     wind_speed * math.cos(wind_direction), 0.0], dtype=float)
    constant = DRAG_CONSTANT * (density / REFERENCE_AIR_DENSITY)
    drag_coefficient = DRAG_INTERCEPT + DRAG_SPIN_SLOPE * (_spin_rpm(launch_angle_deg) / 1000.0)

    def acceleration(state_velocity: np.ndarray) -> np.ndarray:
        relative = state_velocity - wind
        speed_now = float(np.linalg.norm(relative))
        if speed_now < 1e-6:
            return np.array([0.0, 0.0, -GRAVITY])
        spin_factor = BALL_RADIUS_FT * spin_magnitude / speed_now
        lift_coefficient = LIFT_NUMERATOR * spin_factor / (LIFT_INTERCEPT + LIFT_SLOPE * spin_factor)
        magnus = np.cross(spin, relative) / spin_magnitude
        return (-constant * drag_coefficient * speed_now * relative
                + constant * lift_coefficient * speed_now * magnus
                - np.array([0.0, 0.0, GRAVITY]))

    elapsed = 0.0
    while elapsed < MAX_FLIGHT_SECONDS and position[2] > 0:
        k1v = acceleration(velocity)
        k2v = acceleration(velocity + INTEGRATION_STEP / 2 * k1v)
        k3v = acceleration(velocity + INTEGRATION_STEP / 2 * k2v)
        k4v = acceleration(velocity + INTEGRATION_STEP * k3v)
        previous = position.copy()
        position = position + INTEGRATION_STEP * (
            velocity + INTEGRATION_STEP / 6 * (k1v + k2v + k3v))
        velocity = velocity + INTEGRATION_STEP / 6 * (k1v + 2 * k2v + 2 * k3v + k4v)
        elapsed += INTEGRATION_STEP
        before = math.hypot(previous[0], previous[1])
        after = math.hypot(position[0], position[1])
        if before <= fence_distance_ft <= after:
            share = (fence_distance_ft - before) / max(after - before, 1e-9)
            return float(previous[2] + share * (position[2] - previous[2]))
    return -1.0


# Only these balls can leave the yard, so the grid stops where home runs stop. Clearing a fence
# is a threshold event, so the grid is fine enough that a few feet of carry moves a few cells
# rather than a whole block of them.
GRID_EXIT_VELOCITY = tuple(float(value) for value in range(88, 117, 3))
GRID_LAUNCH_ANGLE = tuple(float(value) for value in range(16, 45, 2))
# Roughly the joint frequency of those cells among balls hit in the air. Exact weights matter
# little: the output is a ratio between parks on the identical grid, so any consistent weighting
# gives the same ordering.
GRID_EXIT_CENTRE, GRID_EXIT_SPREAD = 95.0, 9.0
GRID_ANGLE_CENTRE, GRID_ANGLE_SPREAD = 25.0, 10.0
# Hitters pull fly balls; the corners see more of them than the gaps do.
SPRAY_PULL_WEIGHT = .55
# Reported wind is measured above the stands, and a ball in play sees far less of it than that:
# the stadium shields the field and the gusts are not steady. Left unscaled this model gives a
# 10 mph tailwind 35 ft of carry, where the measured effect is nearer 10. Scaling the reported
# speed to this fraction reproduces the published rule of roughly five feet per five miles per
# hour, with temperature and altitude already landing on their own published values unaided.
WIND_EFFECTIVE_FRACTION = .30


def _wind_component(spray_angle_deg: float, wind_mph: float, wind_from_deg: float) -> float:
    """The part of the wind that pushes along this ball's flight, in mph."""
    if not wind_mph:
        return 0.0
    return (WIND_EFFECTIVE_FRACTION * float(wind_mph)
            * math.cos(math.radians(float(spray_angle_deg) - float(wind_from_deg))))


# Fence distances the lookup table covers, in feet, at one-foot resolution.
FENCE_MIN_FT, FENCE_MAX_FT = 280, 460
# Along-flight wind is quantised so the table is reused across spray angles instead of being
# rebuilt for each of the ninety-one of them.
WIND_BUCKET_MPH = 1.0


@lru_cache(maxsize=1)
def _grid_cells() -> tuple[tuple[float, float, float], ...]:
    cells = []
    for exit_velocity in GRID_EXIT_VELOCITY:
        exit_weight = math.exp(-.5 * ((exit_velocity - GRID_EXIT_CENTRE) / GRID_EXIT_SPREAD) ** 2)
        for launch_angle in GRID_LAUNCH_ANGLE:
            angle_weight = math.exp(-.5 * ((launch_angle - GRID_ANGLE_CENTRE) / GRID_ANGLE_SPREAD) ** 2)
            cells.append((exit_velocity, launch_angle, exit_weight * angle_weight))
    total = sum(cell[2] for cell in cells)
    return tuple((cell[0], cell[1], cell[2] / total) for cell in cells)


@lru_cache(maxsize=256)
def _clearance_table(density: float, along_wind: float) -> np.ndarray:
    """Height of every reference batted ball as it passes each fence distance.

    One vectorised integration for the whole grid rather than one per ball, because the park
    index needs this at ninety-one spray angles and would otherwise re-derive the same flights
    thousands of times.
    """
    cells = _grid_cells()
    count = len(cells)
    speeds = np.array([cell[0] for cell in cells]) * 5280 / 3600
    angles = np.radians([cell[1] for cell in cells])
    rpm = np.array([_spin_rpm(cell[1]) for cell in cells])

    velocity = np.stack([np.zeros(count), speeds * np.cos(angles), speeds * np.sin(angles)], axis=1)
    position = np.zeros((count, 3))
    position[:, 2] = LAUNCH_HEIGHT_FT
    spin_x = rpm * 2 * math.pi / 60
    wind = np.zeros((count, 3))
    wind[:, 1] = along_wind * 5280 / 3600
    constant = DRAG_CONSTANT * (density / REFERENCE_AIR_DENSITY)
    drag = DRAG_INTERCEPT + DRAG_SPIN_SLOPE * (rpm / 1000.0)

    def acceleration(state: np.ndarray) -> np.ndarray:
        relative = state - wind
        speed = np.linalg.norm(relative, axis=1)
        safe = np.maximum(speed, 1e-6)
        spin_factor = BALL_RADIUS_FT * spin_x / safe
        lift = LIFT_NUMERATOR * spin_factor / (LIFT_INTERCEPT + LIFT_SLOPE * spin_factor)
        # Spin is purely about +x, so the Magnus term reduces to a rotation in the y-z plane.
        magnus = np.stack([np.zeros(count), -relative[:, 2], relative[:, 1]], axis=1)
        result = (-constant * (drag * safe)[:, None] * relative
                  + constant * (lift * safe)[:, None] * magnus)
        result[:, 2] -= GRAVITY
        return result

    fences = np.arange(FENCE_MIN_FT, FENCE_MAX_FT + 1, dtype=float)
    # Below the launch point means the ball never got there; the comparison then always fails.
    table = np.full((count, fences.size), -1.0)
    previous_distance = np.zeros(count)
    live = np.ones(count, dtype=bool)
    elapsed = 0.0
    while elapsed < MAX_FLIGHT_SECONDS and live.any():
        k1 = acceleration(velocity)
        k2 = acceleration(velocity + INTEGRATION_STEP / 2 * k1)
        k3 = acceleration(velocity + INTEGRATION_STEP / 2 * k2)
        k4 = acceleration(velocity + INTEGRATION_STEP * k3)
        previous_height = position[:, 2].copy()
        position = position + INTEGRATION_STEP * (
            velocity + INTEGRATION_STEP / 6 * (k1 + k2 + k3))
        velocity = velocity + INTEGRATION_STEP / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        distance = np.hypot(position[:, 0], position[:, 1])
        for index in np.flatnonzero(live):
            crossed = (fences > previous_distance[index]) & (fences <= distance[index])
            if crossed.any():
                span = max(distance[index] - previous_distance[index], 1e-9)
                share = (fences[crossed] - previous_distance[index]) / span
                table[index, crossed] = previous_height[index] + share * (
                    position[index, 2] - previous_height[index])
        previous_distance = distance
        live &= position[:, 2] > 0
        elapsed += INTEGRATION_STEP
    return table


def clears_fence(exit_velocity: float, launch_angle: float, fence_distance_ft: float,
                 fence_height_ft: float, density: float, along_wind: float = 0.0) -> bool:
    """Is this reference ball still above the wall when it gets there?"""
    cells = _grid_cells()
    try:
        index = next(i for i, cell in enumerate(cells)
                     if cell[0] == exit_velocity and cell[1] == launch_angle)
    except StopIteration:
        return False
    table = _clearance_table(round(density, 4), _wind_bucket(along_wind))
    position = int(round(fence_distance_ft)) - FENCE_MIN_FT
    if not 0 <= position < table.shape[1]:
        return fence_distance_ft < FENCE_MIN_FT
    return bool(table[index, position] > fence_height_ft)


def _wind_bucket(along_wind: float) -> float:
    return round(round(float(along_wind) / WIND_BUCKET_MPH) * WIND_BUCKET_MPH, 1)


@lru_cache(maxsize=1)
def league_average_walls() -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The wall every park is measured against: the mean of the parks we have geometry for."""
    parks = park_geometry()
    distances = [sum(park["distance_ft"][angle] for park in parks.values()) / len(parks)
                 for angle in range(91)]
    heights = [sum(park["height_ft"][angle] for park in parks.values()) / len(parks)
               for angle in range(91)]
    return tuple(distances), tuple(heights)


def _spray_weights() -> tuple[float, ...]:
    """Fly balls are not spread evenly across the outfield; the corners get more of them."""
    weights = []
    for angle in range(91):
        pulled = abs(angle - 45) / 45
        weights.append(1.0 + SPRAY_PULL_WEIGHT * pulled)
    total = sum(weights)
    return tuple(value / total for value in weights)


def home_run_rate(distances: tuple[float, ...], heights: tuple[float, ...], density: float,
                  wind_mph: float = 0.0, wind_from_deg: float = 0.0) -> float:
    """Share of the reference batted-ball grid that leaves this yard in these conditions."""
    spray_weights = _spray_weights()
    weights = np.array([cell[2] for cell in _grid_cells()])
    density_key = round(density, 4)
    total = 0.0
    for angle in range(91):
        # Angle 0 is the left-field pole and 90 the right-field pole, so the spray angle used by
        # the flight model - 0 up the middle - is the wall angle shifted by 45 degrees.
        bucket = _wind_bucket(_wind_component(angle - 45, wind_mph, wind_from_deg))
        table = _clearance_table(density_key, bucket)
        position = int(round(distances[angle])) - FENCE_MIN_FT
        if position < 0:
            total += spray_weights[angle] * float(weights.sum())
            continue
        if position >= table.shape[1]:
            continue
        cleared = table[:, position] > heights[angle]
        total += spray_weights[angle] * float(weights[cleared].sum())
    return total


def park_home_run_index(park_code: str, temperature_c: float = 22.0, elevation_m: float = 0.0,
                        wind_mph: float = 0.0, wind_from_deg: float = 0.0,
                        relative_humidity: float = .5) -> dict[str, Any]:
    """How this park plays for home runs tonight, relative to an average park on a neutral day.

    Above 1 means more balls leave the yard here than at the league-average wall in reference
    conditions. Unlike a season-long park factor this moves with the weather, which is the whole
    point: the same fence plays differently at 4C with the wind in than at 30C with it out.
    """
    parks = park_geometry()
    park = parks.get(str(park_code).upper())
    if not park:
        return {"available": False, "reason": "NO_GEOMETRY", "index": 1.0}
    density = air_density(temperature_c, elevation_m, relative_humidity)
    reference_density = air_density(22.0, 0.0, .5)
    reference = home_run_rate(*league_average_walls(), reference_density)
    if reference <= 0:
        return {"available": False, "reason": "DEGENERATE_REFERENCE", "index": 1.0}
    actual = home_run_rate(tuple(park["distance_ft"]), tuple(park["height_ft"]),
                           density, wind_mph, wind_from_deg)
    # This park on an ordinary still evening, at its own altitude. Altitude and the shape of the
    # fence are permanent properties that the season home-run park factor has already measured,
    # so the weather multiplier must be tonight against that, not against sea level - otherwise
    # Coors would be credited with a mile of elevation twice.
    neutral_weather = home_run_rate(tuple(park["distance_ft"]), tuple(park["height_ft"]),
                                    air_density(22.0, elevation_m, .5))
    multiplier = actual / neutral_weather if neutral_weather > 0 else 1.0
    return {
        "available": True,
        "park": park_code,
        "stadium": park.get("stadium"),
        "index": round(actual / reference, 4),
        # Split so the card can say how much is the yard and how much is tonight.
        "geometry_index": round(neutral_weather / reference, 4),
        # Bounded: a single evening's forecast is not precise enough to justify more, and the
        # reported wind is a rough proxy for what the ball actually flies through.
        "weather_multiplier": round(max(.65, min(1.5, multiplier)), 4),
        "air_density": round(density, 4),
        "reference_air_density": round(reference_density, 4),
    }


# Stadium name to the park code the geometry table uses, plus field elevation in metres. The
# geometry is a 2021 snapshot, so current names are mapped onto the codes it was compiled with;
# Truist Park and Globe Life Field are absent from it and fall back to the season park factor.
STADIUM_PARKS = {
    "Chase Field": ("ARZ", 331), "Oriole Park at Camden Yards": ("BAL", 10),
    "Camden Yards": ("BAL", 10), "Fenway Park": ("BOS", 6), "Wrigley Field": ("CHC", 182),
    "Great American Ball Park": ("CIN", 149), "Great American Ballpark": ("CIN", 149),
    "Progressive Field": ("CLE", 200), "Coors Field": ("COL", 1580),
    "Rate Field": ("CWS", 181), "Guaranteed Rate Field": ("CWS", 181),
    "Comerica Park": ("DET", 180), "Minute Maid Park": ("HOU", 22), "Daikin Park": ("HOU", 22),
    "Kauffman Stadium": ("KC", 229), "Angel Stadium": ("LAA", 48),
    "Dodger Stadium": ("LAD", 82), "loanDepot park": ("MIA", 2), "LoanDepot Park": ("MIA", 2),
    "Marlins Park": ("MIA", 2), "American Family Field": ("MIL", 187), "Miller Park": ("MIL", 187),
    "Target Field": ("MIN", 251), "Citi Field": ("NYM", 6), "Yankee Stadium": ("NYY", 16),
    "RingCentral Coliseum": ("OAK", 13), "Oakland Coliseum": ("OAK", 13),
    "Citizens Bank Park": ("PHI", 12), "PNC Park": ("PIT", 223), "Petco Park": ("SDP", 19),
    "Oracle Park": ("SFG", 4), "T-Mobile Park": ("SEA", 17), "Safeco Field": ("SEA", 17),
    "Busch Stadium": ("STL", 141), "Tropicana Field": ("TB", 3), "Rogers Centre": ("TOR", 91),
    "Nationals Park": ("WSN", 7),
}
# A closed roof means the weather outside is not the weather the ball flies through.
ROOFED_TEMPERATURE_C = 22.0


def _parse_wind(text: str) -> tuple[float, float | None]:
    """Speed in mph and the direction it blows toward, in the flight model's spray frame.

    MLB reports wind as free text such as "12 mph, Out To CF". Only the components that help or
    hurt carry are recoverable from it, which is all the model needs.
    """
    lowered = str(text or "").lower()
    speed = 0.0
    for token in lowered.replace(",", " ").split():
        try:
            speed = float(token)
            break
        except ValueError:
            continue
    if "out to" in lowered or "out " in lowered:
        direction = -30.0 if "lf" in lowered else 30.0 if "rf" in lowered else 0.0
    elif "in from" in lowered or "in " in lowered:
        direction = 150.0 if "lf" in lowered else -150.0 if "rf" in lowered else 180.0
    elif "l to r" in lowered:
        direction = 90.0
    elif "r to l" in lowered:
        direction = -90.0
    else:
        return speed, None
    return speed, direction


def park_weather_home_run_multiplier(stadium: str | None, weather: dict[str, Any] | None) -> dict[str, Any]:
    """How much tonight's air changes home runs at this park, against its own typical night.

    Deliberately only the weather delta. The park's own baseline is already measured empirically
    by the season home-run park factor, which also captures things geometry cannot know - a
    marine layer, prevailing wind - so replacing that with a fence model would throw information
    away. What the season factor cannot do is tell you about tonight.
    """
    entry = STADIUM_PARKS.get(str(stadium or "").strip())
    if not entry:
        return {"available": False, "reason": "NO_GEOMETRY", "multiplier": 1.0}
    park_code, elevation = entry
    conditions = weather or {}
    if not conditions.get("available"):
        return {"available": False, "reason": "NO_WEATHER", "multiplier": 1.0, "park": park_code}
    roofed = bool(conditions.get("controlled_roof"))
    temperature_f = conditions.get("temperature_f")
    temperature_c = (ROOFED_TEMPERATURE_C if roofed or temperature_f is None
                     else (float(temperature_f) - 32) * 5 / 9)
    wind_speed, wind_direction = (0.0, 0.0) if roofed else _parse_wind(conditions.get("wind"))
    if wind_direction is None:
        wind_speed, wind_direction = 0.0, 0.0
    result = park_home_run_index(park_code, temperature_c=temperature_c, elevation_m=elevation,
                                 wind_mph=wind_speed, wind_from_deg=wind_direction)
    if not result.get("available"):
        return {"available": False, "reason": result.get("reason"), "multiplier": 1.0,
                "park": park_code}
    return {
        "available": True,
        "park": park_code,
        "multiplier": result["weather_multiplier"],
        "geometry_index": result["geometry_index"],
        "temperature_c": round(temperature_c, 1),
        "wind_mph": round(wind_speed, 1),
        "wind_toward_deg": wind_direction,
        "roofed": roofed,
        "air_density": result["air_density"],
        "method": "NATHAN_TRAJECTORY_RK4",
    }
