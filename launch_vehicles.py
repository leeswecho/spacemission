"""Launch vehicle catalog and per-mission vehicle configuration.

Each vehicle has an optional set of strap-on boosters, a core (first) stage
and an upper stage.  Boosters burn alongside the core from liftoff and are
jettisoned together at burnout; the core then burns to depletion and the
upper stage finishes the job.  All figures are approximate public numbers;
engines run at constant mass flow (solid boosters included), with thrust
interpolated between sea level and vacuum by air density.

Units: kg, N, kg/s, degrees.  A vehicle's ``id`` is the spacecraft ID used in
the simulation; its upper stage keeps that ID through every separation.

The mission page sends edits as ``vehicle_config`` (same shape as a catalog
entry's ``boosters`` / ``core`` / ``upper`` blocks plus ``fairing`` and
``kick_angle``); ``configure()`` merges them over the catalog defaults and
validates the result.
"""

import copy
import math

G0 = 9.80665

# Drag coefficient against airspeed (m/s), shared by every vehicle.
CD_TABLE = [[0, 0.30], [200, 0.30], [270, 0.38], [320, 0.52], [380, 0.55], [450, 0.50],
            [600, 0.42], [1000, 0.33], [2000, 0.27], [4000, 0.23], [8000, 0.20]]

MAX_BOOSTERS = 8
MIN_LIFTOFF_TW = 1.05
BOOST_THROTTLE_AFTER = 50.0  # s after liftoff, when a core with boost_throttle < 1 throttles down


def _engine(thrust_sl, thrust_vac, isp_vac, **extra):
    """Stage figures with the mass flow derived from vacuum thrust and Isp."""
    return {"thrust_sl": thrust_sl, "thrust_vac": thrust_vac, "mdot": thrust_vac / (isp_vac * G0), **extra}


VEHICLES = {
    "falcon9": {
        "id": "falcon9",
        "name": "Falcon 9 Block 5",
        "maker": "SpaceX",
        "summary": "Two stages, nine Merlin 1D engines on the first stage (expended here).",
        "boosters": None,
        "core": {"dry": 25_600.0, "prop": 395_700.0, **_engine(7.607e6, 8.227e6, 311.0)},
        "upper": {"dry": 3_900.0, "prop": 92_670.0, "thrust": 981e3, "mdot": 981e3 / (348.0 * G0)},
        "fairing": 1_900.0,
        "kick_angle": 15.0,
        "diameter": 3.66,
    },
    "atlas_v_551": {
        "id": "atlas_v_551",
        "name": "Atlas V 551",
        "maker": "ULA",
        "summary": "RD-180 core, five AJ-60A solid boosters, single-engine Centaur, 5 m fairing.",
        "boosters": {"count": 5, "max_count": 5, "dry": 4_070.0, "prop": 42_630.0, "diameter": 1.58,
                     **_engine(1.112e6, 1.241e6, 279.0)},
        "core": {"dry": 21_054.0, "prop": 284_089.0, **_engine(3.827e6, 4.152e6, 338.4)},
        "upper": {"dry": 2_316.0, "prop": 20_830.0, "thrust": 101.8e3, "mdot": 101.8e3 / (450.5 * G0)},
        "fairing": 4_000.0,
        "kick_angle": 15.0,
        "diameter": 5.4,
    },
    "vulcan": {
        "id": "vulcan",
        "name": "Vulcan Centaur VC4",
        "maker": "ULA",
        "summary": "Two BE-4 engines, four GEM 63XL solid boosters (0, 2, 4 or 6 fly), Centaur V.",
        "boosters": {"count": 4, "max_count": 6, "dry": 6_000.0, "prop": 47_500.0, "diameter": 1.6,
                     **_engine(1.292e6, 1.447e6, 280.0)},
        "core": {"dry": 35_000.0, "prop": 420_000.0, **_engine(4.90e6, 5.374e6, 340.0)},
        "upper": {"dry": 5_000.0, "prop": 54_000.0, "thrust": 212e3, "mdot": 212e3 / (453.8 * G0)},
        "fairing": 4_500.0,
        "kick_angle": 10.0,
        "diameter": 5.4,
    },
    "delta_iv_heavy": {
        "id": "delta_iv_heavy",
        "name": "Delta IV Heavy",
        "maker": "ULA",
        "summary": "Three RS-68A common booster cores; the centre core throttles down while the "
                   "two side cores burn. 5 m Delta Cryogenic Second Stage.",
        "boosters": {"count": 2, "max_count": 2, "dry": 26_760.0, "prop": 202_380.0, "diameter": 5.1,
                     **_engine(3.137e6, 3.560e6, 414.0)},
        "core": {"dry": 26_760.0, "prop": 202_380.0, "boost_throttle": 0.57, **_engine(3.137e6, 3.560e6, 414.0)},
        "upper": {"dry": 3_490.0, "prop": 27_220.0, "thrust": 110.1e3, "mdot": 110.1e3 / (465.5 * G0)},
        "fairing": 3_800.0,
        "kick_angle": 4.0,
        "diameter": 5.1,
    },
    "new_glenn": {
        "id": "new_glenn",
        "name": "New Glenn",
        "maker": "Blue Origin",
        "summary": "Seven BE-4 engines on the first stage (expended here), two BE-3U on the "
                   "upper stage, 7 m fairing.",
        "boosters": None,
        "core": {"dry": 150_000.0, "prop": 1_000_000.0, **_engine(17.15e6, 18.81e6, 340.0)},
        "upper": {"dry": 20_000.0, "prop": 150_000.0, "thrust": 1.42e6, "mdot": 1.42e6 / (445.0 * G0)},
        "fairing": 7_500.0,
        "kick_angle": 8.0,
        "diameter": 7.0,
    },
    "sls": {
        "id": "sls",
        "name": "SLS Block 1",
        "maker": "NASA",
        "summary": "Four RS-25 core engines, two five-segment solid boosters and the Interim "
                   "Cryogenic Propulsion Stage. No fairing (Orion configuration).",
        "boosters": {"count": 2, "max_count": 2, "dry": 99_000.0, "prop": 628_000.0, "diameter": 3.71,
                     **_engine(12.0e6, 13.10e6, 268.0)},
        "core": {"dry": 85_270.0, "prop": 979_452.0, **_engine(7.44e6, 9.116e6, 452.3)},
        "upper": {"dry": 3_490.0, "prop": 28_800.0, "thrust": 110.1e3, "mdot": 110.1e3 / (465.5 * G0)},
        "fairing": 0.0,
        "kick_angle": 4.0,
        "diameter": 8.4,
    },
}

DEFAULT_VEHICLE = "falcon9"

# Editable fields per block: (key, label, minimum, maximum).
_STAGE_FIELDS = {
    "boosters": [("dry", "dry mass", 1.0, 2e6), ("prop", "propellant", 1.0, 5e6),
                 ("thrust_sl", "sea-level thrust", 1.0, 1e8), ("thrust_vac", "vacuum thrust", 1.0, 1e8),
                 ("mdot", "propellant flow", 0.01, 1e5)],
    "core": [("dry", "dry mass", 1.0, 2e6), ("prop", "propellant", 1.0, 5e6),
             ("thrust_sl", "sea-level thrust", 1.0, 1e8), ("thrust_vac", "vacuum thrust", 1.0, 1e8),
             ("mdot", "propellant flow", 0.01, 1e5)],
    "upper": [("dry", "dry mass", 1.0, 1e6), ("prop", "propellant", 1.0, 2e6),
              ("thrust", "thrust", 1.0, 2e7), ("mdot", "propellant flow", 0.01, 1e4)],
}
_STAGE_NAMES = {"boosters": "Booster", "core": "First stage", "upper": "Upper stage"}
ISP_RANGE = (150.0, 500.0)  # s, sanity bounds on vacuum Isp


def area(diameter):
    return math.pi * diameter**2 / 4.0


def isp(stage):
    return stage.get("thrust_vac", stage.get("thrust")) / (stage["mdot"] * G0)


def catalog():
    """A copy of the catalog (for the page)."""
    return copy.deepcopy(VEHICLES)


def _number(raw, label, lo, hi, errors, integer=False):
    try:
        x = float(raw)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number.")
        return None
    if not math.isfinite(x):
        errors.append(f"{label} must be a finite number.")
        return None
    if integer and x != int(x):
        errors.append(f"{label} must be a whole number.")
        return None
    if not lo <= x <= hi:
        errors.append(f"{label} must be between {lo:g} and {hi:g}.")
        return None
    return int(x) if integer else x


def configure(key, overrides=None):
    """Merge page edits over the catalog entry.  Returns (config, errors)."""
    errors = []
    if key not in VEHICLES:
        return None, ["Unknown launch vehicle."]
    cfg = copy.deepcopy(VEHICLES[key])
    cfg["key"] = key
    ov = overrides if isinstance(overrides, dict) else {}

    for block, fields in _STAGE_FIELDS.items():
        if cfg.get(block) is None:
            continue
        given = ov.get(block) if isinstance(ov.get(block), dict) else {}
        name = _STAGE_NAMES[block]
        for field, label, lo, hi in fields:
            if field in given:
                x = _number(given[field], f"{name} {label}", lo, hi, errors)
                if x is not None:
                    cfg[block][field] = x
        st = cfg[block]
        if "thrust_sl" in st and st["thrust_sl"] > st["thrust_vac"]:
            errors.append(f"{name} sea-level thrust cannot exceed its vacuum thrust.")
        lo, hi = ISP_RANGE
        if not lo <= isp(st) <= hi:
            errors.append(f"{name} thrust and propellant flow give an Isp of {isp(st):.0f} s; "
                          f"it must be between {lo:.0f} and {hi:.0f} s.")

    if cfg["boosters"] is not None:
        b = cfg["boosters"]
        given = ov.get("boosters") if isinstance(ov.get("boosters"), dict) else {}
        if "count" in given:
            n = _number(given["count"], "Number of boosters", 0, b["max_count"], errors, integer=True)
            if n is not None:
                b["count"] = n
        if "boost_throttle" in cfg["core"]:
            given = ov.get("core") if isinstance(ov.get("core"), dict) else {}
            if "boost_throttle" in given:
                x = _number(given["boost_throttle"], "Core throttle while boosters burn", 0.3, 1.0, errors)
                if x is not None:
                    cfg["core"]["boost_throttle"] = x
    if "fairing" in ov:
        x = _number(ov["fairing"], "Fairing mass", 0.0, 50_000.0, errors)
        if x is not None:
            cfg["fairing"] = x
    if "kick_angle" in ov:
        x = _number(ov["kick_angle"], "Pitch kick", 0.5, 45.0, errors)
        if x is not None:
            cfg["kick_angle"] = x

    b = boosters(cfg)
    if b and not errors:
        # The core must not run dry before the boosters burn out.
        t_b = b["prop"] / b["mdot"]
        thr = cfg["core"].get("boost_throttle", 1.0)
        used = cfg["core"]["mdot"] * (min(t_b, BOOST_THROTTLE_AFTER) + thr * max(0.0, t_b - BOOST_THROTTLE_AFTER))
        if used >= cfg["core"]["prop"]:
            errors.append("The first stage would run out of propellant before the boosters burn out.")
    return cfg, errors


def boosters(cfg):
    """The booster block if any boosters fly, else None."""
    b = cfg.get("boosters")
    return b if b and b.get("count", 0) > 0 else None


def liftoff_mass(cfg, payload):
    b = boosters(cfg)
    m = cfg["core"]["dry"] + cfg["core"]["prop"] + upper_stack(cfg, payload, fairing=True)
    if b:
        m += b["count"] * (b["dry"] + b["prop"])
    return m


def upper_stack(cfg, payload, fairing=True):
    """Mass above the core stage (fully fuelled upper stage + payload, + fairing if still on)."""
    u = cfg["upper"]
    return u["dry"] + u["prop"] + payload + (cfg["fairing"] if fairing else 0.0)


def liftoff_thrust(cfg):
    b = boosters(cfg)
    t = cfg["core"]["thrust_sl"]
    if b:
        t += b["count"] * b["thrust_sl"]
    return t


def stack_area(cfg):
    b = boosters(cfg)
    a = area(cfg["diameter"])
    if b:
        a += b["count"] * area(b["diameter"])
    return a
