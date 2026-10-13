"""Rocket engines for the Moon mission page (in-space and landing engines).

Approximate public figures, vacuum performance.  Engines still in
development (BE-7, Raptor) have no settled published figures; their values
here are rough estimates.  Every figure can be overridden on the page with a
custom engine.

thrust:       vacuum thrust of one engine, N
isp:          vacuum specific impulse, s
mass:         mass of one engine, kg
min_throttle: lowest throttle setting as a fraction of full thrust (1.0 = no throttling)
"""

G0 = 9.80665

ENGINES = {
    "apollo_dps": {
        "name": "Apollo LM descent engine (DPS)", "use": "Apollo Lunar Module descent stage, 1969-72",
        "propellants": "Aerozine 50 / N2O4", "thrust": 45_040.0, "isp": 311.0, "mass": 179.0,
        "min_throttle": 0.10,
    },
    "apollo_aps": {
        "name": "Apollo LM ascent engine (APS)", "use": "Apollo Lunar Module ascent stage, 1969-72",
        "propellants": "Aerozine 50 / N2O4", "thrust": 15_570.0, "isp": 311.0, "mass": 82.0,
        "min_throttle": 1.0,
    },
    "apollo_sps": {
        "name": "Apollo service module engine (AJ10-137)", "use": "Apollo CSM: lunar orbit insertion and return",
        "propellants": "Aerozine 50 / N2O4", "thrust": 91_190.0, "isp": 314.0, "mass": 290.0,
        "min_throttle": 1.0,
    },
    "aj10_190": {
        "name": "AJ10-190 (OMS-E)", "use": "Space Shuttle OMS; Orion service module",
        "propellants": "MMH / N2O4", "thrust": 26_700.0, "isp": 316.0, "mass": 118.0,
        "min_throttle": 1.0,
    },
    "rl10b2": {
        "name": "RL10B-2", "use": "Delta Cryogenic Second Stage, ICPS",
        "propellants": "LH2 / LOX", "thrust": 110_100.0, "isp": 465.5, "mass": 301.0,
        "min_throttle": 1.0,
    },
    "rl10c1": {
        "name": "RL10C-1", "use": "Centaur (Atlas V)",
        "propellants": "LH2 / LOX", "thrust": 101_800.0, "isp": 449.7, "mass": 190.0,
        "min_throttle": 1.0,
    },
    "rl10_cece": {
        "name": "RL10 deep-throttle (CECE demonstrator)", "use": "NASA lunar lander technology demonstrator",
        "propellants": "LH2 / LOX", "thrust": 66_700.0, "isp": 445.0, "mass": 200.0,
        "min_throttle": 0.10,
    },
    "be7": {
        "name": "BE-7 (estimate)", "use": "Blue Origin Blue Moon lander (in development)",
        "propellants": "LH2 / LOX", "thrust": 44_500.0, "isp": 450.0, "mass": 200.0,
        "min_throttle": 0.20,
    },
    "j2": {
        "name": "J-2", "use": "Saturn V S-II and S-IVB stages",
        "propellants": "LH2 / LOX", "thrust": 1_033_100.0, "isp": 421.0, "mass": 1_788.0,
        "min_throttle": 1.0,
    },
    "merlin_vac": {
        "name": "Merlin 1D Vacuum", "use": "Falcon 9 second stage",
        "propellants": "RP-1 / LOX", "thrust": 981_000.0, "isp": 348.0, "mass": 490.0,
        "min_throttle": 0.39,
    },
    "raptor_vac": {
        "name": "Raptor Vacuum (estimate)", "use": "Starship upper stage (in development)",
        "propellants": "CH4 / LOX", "thrust": 2_500_000.0, "isp": 380.0, "mass": 1_700.0,
        "min_throttle": 0.40,
    },
    "raptor": {
        "name": "Raptor 2, sea-level nozzle (estimate)", "use": "Starship; Starship HLS landings (in development)",
        "propellants": "CH4 / LOX", "thrust": 2_400_000.0, "isp": 350.0, "mass": 1_630.0,
        "min_throttle": 0.40,
    },
}

DEFAULT_ENGINE = "rl10b2"
FIELDS = ("thrust", "isp", "mass", "min_throttle")
MAX_COUNT = 30


def catalog():
    return {k: dict(v) for k, v in ENGINES.items()}


def configure(raw, label="Engine"):
    """Engine choice from page input: {"key", "count", "custom": {thrust, isp, mass, min_throttle}}.

    Returns (config, errors).  The config carries the per-engine figures plus
    count, total thrust, total mass and the total propellant flow at full thrust.
    """
    raw = raw if isinstance(raw, dict) else {}
    errors = []
    key = raw.get("key", DEFAULT_ENGINE)
    if key != "custom" and key not in ENGINES:
        return None, [f"{label}: unknown engine '{key}'."]
    base = dict(ENGINES.get(key, {"name": "Custom engine", "use": "", "propellants": "",
                                  "thrust": None, "isp": None, "mass": None, "min_throttle": None}))
    overrides = raw.get("custom") if isinstance(raw.get("custom"), dict) else {}
    for f in FIELDS:
        if f in overrides:
            base[f] = overrides[f]
    cfg = {"key": key, "name": base["name"]}
    names = {"thrust": "thrust", "isp": "Isp", "mass": "mass", "min_throttle": "minimum throttle"}
    for f in FIELDS:
        try:
            x = float(base[f])
            if x != x or x in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label} {names[f]} must be a number.")
            continue
        cfg[f] = x
    try:
        count = int(raw.get("count", 1))
        if count != float(raw.get("count", 1)):
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{label} count must be a whole number.")
        count = None
    if count is not None and not 1 <= count <= MAX_COUNT:
        errors.append(f"{label} count must be between 1 and {MAX_COUNT}.")
    if errors:
        return None, errors
    if cfg["thrust"] <= 0:
        errors.append(f"{label} thrust must be positive.")
    if not 150.0 <= cfg["isp"] <= 500.0:
        errors.append(f"{label} Isp must be between 150 and 500 s (chemical rockets).")
    if cfg["mass"] < 0:
        errors.append(f"{label} mass cannot be negative.")
    if not 0.05 <= cfg["min_throttle"] <= 1.0:
        errors.append(f"{label} minimum throttle must be between 5 % and 100 %.")
    if errors:
        return None, errors
    cfg["count"] = count
    cfg["total_thrust"] = count * cfg["thrust"]
    cfg["total_mass"] = count * cfg["mass"]
    cfg["mdot"] = cfg["total_thrust"] / (cfg["isp"] * G0)
    # One engine at its lowest setting (the others shut down).
    cfg["min_thrust"] = cfg["min_throttle"] * cfg["thrust"]
    return cfg, []
