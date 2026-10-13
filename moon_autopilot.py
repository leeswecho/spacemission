"""Autopilot for the Moon mission page: four independent parts of a lunar mission.

1. launch   - a rocket stages payload and/or propellant in Earth orbit
              (the Earth-orbit autopilot in meo_autopilot.py, reported as delivered mass).
2. transfer - a vehicle assembled in Earth orbit flies to a lunar orbit:
              trans-lunar injection, coast with course corrections, lunar orbit insertion.
3. landing  - part of a vehicle in lunar orbit lands at a chosen site, leaves cargo,
              waits, and climbs back to the starting orbit.
4. return   - a vehicle in lunar orbit flies back to an Earth orbit.

Parts 2-4 fly a single-stage "stack": a dry mass (everything that is not
propellant: engines, tanks, structure and cargo), propellant, and a set of
identical engines (engines.py).  The autopilot chooses where the vehicle
starts in its orbit (and the orbit's orientation) so that the burns line up
with the Moon; trajectories are targeted with lunar_nav.py and then flown in
the simulator, which has the final word.
"""

import datetime
import math

import numpy as np

import engines as eng
import lunar_nav as nav
import meo_autopilot as ap
from meo_autopilot import MissionAborted, MissionFailed, SimFlight, unit

G0 = eng.G0
PARTS = {
    "launch": "Staging in Earth orbit",
    "transfer": "Transfer to lunar orbit",
    "landing": "Landing and return to lunar orbit",
    "return": "Return to Earth orbit",
}
# (powered-flight warp, coasting warp); None = as fast as possible.
PACES = {
    "watch": (5.0, 3_000.0),
    "fast": (50.0, 50_000.0),
    "max": (None, None),
}
TICK = 0.5                     # s, guidance interval in powered flight
COAST_WALL = 1.0               # s of wall time per coasting step when a warp is set
COAST_STEP = 600.0             # s, coasting step without a warp

EARTH_MIN_ALT = ap.ORBIT_MIN_ALT
EARTH_MAX_ALT = ap.ORBIT_MAX_ALT
LUNAR_MIN_ALT = 15e3           # m (no terrain in the sim; Apollo descent orbits reached ~15 km)
LUNAR_MAX_ALT = 10_000e3       # m (well inside the Moon's sphere of influence)
MAX_MASS = 5_000_000.0         # kg, sanity limit for payload and propellant
EARTH_TOL = (5e3, 0.01)        # altitude tolerance: max(5 km, 1 %)
LUNAR_TOL = (2e3, 0.01)        # max(2 km, 1 %)
INC_TOL = 0.5                  # deg
MCC_MIN = 0.3                  # m/s, smaller course corrections are skipped
MAX_BURN_FRACTION = 0.45       # departure burns longer than this share of the orbit period are refused

# Landing.
DESCENT_PERILUNE = 15e3        # m, low point of the descent orbit
HIGH_GATE = (1_000.0, -20.0)   # altitude m, vertical speed m/s at the end of braking
LOW_GATE = (30.0, -2.0)        # end of the approach phase
BRAKING_LOAD = 0.75            # ignite when braking needs this share of full thrust ...
APPROACH_LOAD = 0.6            # ... and plan the approach at this share
TOUCHDOWN_VZ = (-1.5, -0.75)   # m/s descent rate above / below 5 m
MAX_TOUCHDOWN = (3.0, 2.0)     # m/s vertical, horizontal for a safe landing
ASCENT_INSERT_ALT = 20e3       # m, insertion altitude after liftoff (or the target perilune if lower)
ASCENT_RISE = 8.0              # s of vertical rise before pitching over

LUNAR_SITES = {
    "apollo11": {"name": "Apollo 11 - Tranquility Base", "lat": 0.674, "lon": 23.473},
    "apollo15": {"name": "Apollo 15 - Hadley-Apennine", "lat": 26.132, "lon": 3.634},
    "apollo17": {"name": "Apollo 17 - Taurus-Littrow", "lat": 20.191, "lon": 30.772},
    "change4": {"name": "Chang'e 4 - Von Karman crater (far side)", "lat": -45.457, "lon": 177.588},
    "south_pole": {"name": "South pole - Shackleton crater rim", "lat": -89.45, "lon": -137.0},
}

IDS = {
    "transfer": ["lunar_transfer"],
    "landing": ["lunar_vehicle", "orbiter", "lander", "surface_cargo"],
    "return": ["earth_return"],
}


def tolerance(target_alt, tol):
    return max(tol[0], tol[1] * target_alt)


def phases_for(part, params=None):
    if part == "launch":
        return ap.phases_for(params["vehicle_config"])
    if part == "transfer":
        return [("prep", "Assemble the vehicle in Earth orbit; plan the trajectory"),
                ("tli", "Trans-lunar injection burn"),
                ("cruise", "Coast to the Moon, with course corrections"),
                ("loi", "Lunar orbit insertion burn"),
                ("trim", "Trim to the target lunar orbit"),
                ("demo", ap.DEMO_PHASE_TITLE)]
    if part == "landing":
        return [("prep", "Place the vehicle in lunar orbit over the site"),
                ("undock", "Lander separates"),
                ("doi", "Descent orbit insertion"),
                ("descent", "Powered descent and landing"),
                ("surface", "Surface stay: unload cargo, wait for the orbit plane"),
                ("ascent", "Ascent to orbit"),
                ("raise", "Raise to the starting orbit"),
                ("demo", ap.DEMO_PHASE_TITLE)]
    if part == "return":
        return [("prep", "Place the vehicle in lunar orbit; plan the trajectory"),
                ("tei", "Trans-Earth injection burn"),
                ("cruise", "Coast to Earth, with course corrections"),
                ("capture", "Earth orbit insertion burn at perigee"),
                ("trim", "Trim to the target Earth orbit"),
                ("demo", ap.DEMO_PHASE_TITLE)]
    raise ValueError(part)


# ---------------------------------------------------------------------------
# Validation and estimates
# ---------------------------------------------------------------------------

def _num(raw, key, label, errors, lo=None, hi=None, unit_text=""):
    try:
        x = float(raw.get(key))
        if not math.isfinite(x):
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number.")
        return None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        errors.append(f"{label} must be between {lo:,.6g} and {hi:,.6g}{unit_text}.")
        return None
    return x


def _orbit(raw, prefix, labels, lo, hi, errors, inc_range=(0.0, 180.0)):
    lo_km, hi_km = lo / 1e3, hi / 1e3
    p = _num(raw, f"{prefix}periapsis_km", labels[0], errors, lo_km, hi_km, " km")
    a = _num(raw, f"{prefix}apoapsis_km", labels[1], errors, lo_km, hi_km, " km")
    i = _num(raw, f"{prefix}inclination_deg", labels[2], errors, *inc_range, "°")
    if p is not None and a is not None and p > a:
        errors.append(f"{labels[0]} cannot be higher than {labels[1].lower()}.")
    return p, a, i


def _masses(raw, errors):
    pay = _num(raw, "payload_kg", "Payload mass", errors, 0.0, MAX_MASS, " kg")
    prop = _num(raw, "propellant_kg", "Propellant mass", errors, 0.0, MAX_MASS, " kg")
    return pay, prop


def _vis_viva(mu, r, r_other):
    """Speed at radius r on an orbit whose other apsis is r_other."""
    return math.sqrt(mu * 2 * r_other / (r * (r + r_other)))


def _burn(dv, m0, e):
    """(propellant used, burn time) for dv from mass m0 with engines e."""
    c = e["isp"] * G0
    used = m0 * (1 - math.exp(-dv / c))
    return used, used / e["mdot"]


def _budget(m0, e, burns):
    """Walk through [(label, dv, mass dropped before the burn)] and report propellant use."""
    m = m0
    rows = []
    for label, dv, drop in burns:
        m -= drop
        used, t = _burn(dv, m, e)
        rows.append({"label": label, "dv": dv, "propellant": used, "burn_time": t})
        m -= used
    return rows


EARTH_MU = ap.EARTH_MU
EARTH_RADIUS = ap.EARTH_RADIUS
MOON_MU = 4.9028e12
MOON_RADIUS = 1.7374e6
MOON_DISTANCE = 384_400e3
MOON_SPEED = math.sqrt(EARTH_MU / MOON_DISTANCE)


def _arrival_vinf(r_p_earth):
    """Approximate hyperbolic excess speed at the Moon for a minimum-energy transfer."""
    v_apo = _vis_viva(EARTH_MU, MOON_DISTANCE, r_p_earth)
    return MOON_SPEED - v_apo


def validate(part, raw):
    """Return (params, errors, estimate, phases) for one tab of the page."""
    raw = raw if isinstance(raw, dict) else {}
    if part not in PARTS:
        return None, ["Unknown mission part."], None, None
    if part == "launch":
        params, errors, est = ap.validate(raw)
        params["part"] = part
        phases = ap.phases_for(params["vehicle_config"]) if params["vehicle_config"] else None
        if est:
            est = dict(est)
            est["note"] = ("Delivered propellant is whatever the upper stage still holds once the payload "
                           "is in orbit.")
        return params, errors, est, phases
    errors = []
    pace = raw.get("pace", "watch")
    if pace not in PACES:
        errors.append("Unknown pacing option.")
    params = {"part": part, "pace": pace, "reset": bool(raw.get("reset", True))}
    payload, prop = _masses(raw, errors)
    e, e_err = eng.configure(raw.get("engine"), "Engine")
    errors += e_err
    params.update({"payload_kg": payload, "propellant_kg": prop, "engine": e})

    if part == "transfer":
        ep, ea, ei = _orbit(raw, "start_", ("Starting perigee", "Starting apogee", "Starting inclination"),
                            EARTH_MIN_ALT, EARTH_MAX_ALT, errors)
        lp, la, li = _orbit(raw, "target_", ("Target perilune", "Target apolune", "Target lunar inclination"),
                            LUNAR_MIN_ALT, LUNAR_MAX_ALT, errors)
        params.update({"start_perigee_km": ep, "start_apogee_km": ea, "start_inclination_deg": ei,
                       "target_perilune_km": lp, "target_apolune_km": la, "target_inclination_deg": li})
    elif part == "landing":
        lp, la, li = _orbit(raw, "start_", ("Perilune", "Apolune", "Lunar inclination"),
                            LUNAR_MIN_ALT, LUNAR_MAX_ALT, errors)
        lpay = _num(raw, "lander_payload_pct", "Lander share of the payload", errors, 0.0, 100.0, " %")
        lprop = _num(raw, "lander_propellant_pct", "Lander share of the propellant", errors, 0.0, 100.0, " %")
        lat = _num(raw, "site_lat", "Site latitude", errors, -90.0, 90.0, "°")
        lon = _num(raw, "site_lon", "Site longitude", errors, -180.0, 180.0, "°")
        s_pay = _num(raw, "surface_payload_kg", "Payload left on the surface", errors, 0.0, MAX_MASS, " kg")
        s_prop = _num(raw, "surface_propellant_kg", "Propellant left on the surface", errors, 0.0, MAX_MASS, " kg")
        stay = _num(raw, "stay_hours", "Minimum surface stay", errors, 0.0, 24.0 * 30, " h")
        site_name = str(raw.get("site_name") or "Custom site")[:80]
        liftoff = raw.get("liftoff", "plane")
        if liftoff not in ("plane", "asap"):
            errors.append("Unknown liftoff option.")
        params["liftoff"] = liftoff
        params.update({"start_perilune_km": lp, "start_apolune_km": la, "start_inclination_deg": li,
                       "lander_payload_pct": lpay, "lander_propellant_pct": lprop,
                       "site_lat": lat, "site_lon": lon, "site_name": site_name,
                       "surface_payload_kg": s_pay, "surface_propellant_kg": s_prop, "stay_hours": stay})
        if lat is not None and li is not None:
            reach = min(li, 180.0 - li)
            if abs(lat) > reach + 1e-9:
                errors.append(f"Lunar inclination: an orbit inclined {li:g}° never passes over latitude "
                              f"{lat:g}°; use an inclination between {abs(lat):g}° and {180 - abs(lat):g}°.")
        if None not in (payload, prop, lpay, lprop) and e:
            l_dry = payload * lpay / 100.0
            l_prop = prop * lprop / 100.0
            params["lander_dry_kg"] = l_dry
            params["lander_prop_kg"] = l_prop
            if lpay <= 0:
                errors.append("Lander share of the payload must be more than 0 % (the lander needs its engines).")
            elif e["total_mass"] > l_dry + 1e-6:
                errors.append(f"Engine mass ({e['total_mass']:,.0f} kg) is more than the lander's share of the "
                              f"payload ({l_dry:,.0f} kg).")
            if s_pay is not None and e["total_mass"] + s_pay > l_dry + 1e-6:
                errors.append(f"Payload left on the surface: the lander carries {l_dry:,.0f} kg of payload, "
                              f"{e['total_mass']:,.0f} kg of it engines, so at most "
                              f"{max(0.0, l_dry - e['total_mass']):,.0f} kg can stay on the surface.")
            if s_prop is not None and s_prop > l_prop + 1e-6:
                errors.append(f"Propellant left on the surface: the lander only carries {l_prop:,.0f} kg.")
    else:  # return
        lp, la, li = _orbit(raw, "start_", ("Starting perilune", "Starting apolune", "Starting lunar inclination"),
                            LUNAR_MIN_ALT, LUNAR_MAX_ALT, errors)
        ep, ea, ei = _orbit(raw, "target_", ("Target perigee", "Target apogee", "Target inclination"),
                            EARTH_MIN_ALT, EARTH_MAX_ALT, errors)
        params.update({"start_perilune_km": lp, "start_apolune_km": la, "start_inclination_deg": li,
                       "target_perigee_km": ep, "target_apogee_km": ea, "target_inclination_deg": ei})

    if part in ("transfer", "return") and e and payload is not None and e["total_mass"] > payload + 1e-6:
        errors.append(f"Engine mass ({e['total_mass']:,.0f} kg) is more than the payload ({payload:,.0f} kg); "
                      "the payload includes the engines.")
    if payload is not None and prop is not None and payload + prop <= 0:
        errors.append("Payload mass: the vehicle needs some mass.")
    est = None
    if not errors:
        est = estimate(part, params)
        if est.get("error"):
            errors.append(est["error"])
    return params, errors, est, phases_for(part)


def estimate(part, p):
    """Rough propellant budget for parts 2-4 (impulsive burns plus allowances)."""
    e = p["engine"]
    dry, prop = p["payload_kg"], p["propellant_kg"]
    m0 = dry + prop
    out = {"mass": m0, "engine_mass": e["total_mass"], "thrust": e["total_thrust"],
           "isp": e["isp"], "burns": []}
    if part == "transfer":
        r_pe = EARTH_RADIUS + p["start_perigee_km"] * 1e3
        r_ae = EARTH_RADIUS + p["start_apogee_km"] * 1e3
        tli = _vis_viva(EARTH_MU, r_pe, MOON_DISTANCE) - _vis_viva(EARTH_MU, r_pe, r_ae)
        r_pl = MOON_RADIUS + p["target_perilune_km"] * 1e3
        r_al = MOON_RADIUS + p["target_apolune_km"] * 1e3
        vinf = _arrival_vinf(r_pe)
        loi = math.sqrt(vinf**2 + 2 * MOON_MU / r_pl) - _vis_viva(MOON_MU, r_pl, r_al)
        burns = [("Trans-lunar injection", tli, 0.0), ("Course corrections (allowance)", 30.0, 0.0),
                 ("Lunar orbit insertion", loi, 0.0), ("Trim (allowance)", 20.0, 0.0)]
        out["accel"] = e["total_thrust"] / m0
        period = 2 * math.pi * math.sqrt(((r_pe + r_ae) / 2) ** 3 / EARTH_MU)
        _, t_tli = _burn(tli, m0, e)
        if t_tli > MAX_BURN_FRACTION * period and prop > 0:
            out["error"] = (f"Engine thrust: the trans-lunar injection burn would take about {t_tli / 60:,.0f} min, "
                            f"too long for a {period / 60:,.0f}-minute orbit. Add engines or pick bigger ones.")
    elif part == "return":
        r_pl = MOON_RADIUS + p["start_perilune_km"] * 1e3
        r_al = MOON_RADIUS + p["start_apolune_km"] * 1e3
        r_pe = EARTH_RADIUS + p["target_perigee_km"] * 1e3
        r_ae = EARTH_RADIUS + p["target_apogee_km"] * 1e3
        vinf = _arrival_vinf(r_pe)
        tei = math.sqrt(vinf**2 + 2 * MOON_MU / r_pl) - _vis_viva(MOON_MU, r_pl, r_al)
        cap = _vis_viva(EARTH_MU, r_pe, MOON_DISTANCE) - _vis_viva(EARTH_MU, r_pe, r_ae)
        burns = [("Trans-Earth injection", tei, 0.0), ("Course corrections (allowance)", 30.0, 0.0),
                 ("Earth orbit insertion", cap, 0.0), ("Trim (allowance)", 20.0, 0.0)]
        out["accel"] = e["total_thrust"] / m0
        period = 2 * math.pi * math.sqrt(((r_pl + r_al) / 2) ** 3 / MOON_MU)
        _, t_tei = _burn(tei, m0, e)
        if t_tei > MAX_BURN_FRACTION * period and prop > 0:
            out["error"] = (f"Engine thrust: the trans-Earth injection burn would take about {t_tei / 60:,.0f} min, "
                            f"too long for a {period / 60:,.0f}-minute lunar orbit. Add engines or pick bigger ones.")
    else:  # landing
        l_dry, l_prop = p["lander_dry_kg"], p["lander_prop_kg"]
        m0 = l_dry + l_prop
        out["mass"] = m0
        out["orbiter_mass"] = dry + prop - m0
        r_p0 = MOON_RADIUS + p["start_perilune_km"] * 1e3
        r_a0 = MOON_RADIUS + p["start_apolune_km"] * 1e3
        r_d = MOON_RADIUS + DESCENT_PERILUNE
        doi = _vis_viva(MOON_MU, r_a0, r_p0) - _vis_viva(MOON_MU, r_a0, r_d)
        v_pdi = _vis_viva(MOON_MU, r_d, r_a0)
        g_m = MOON_MU / MOON_RADIUS**2
        tw = e["total_thrust"] / (m0 * g_m)
        loss = 250.0 + 400.0 / max(tw, 1.0)   # gravity losses and hover margin, larger for weak engines
        h_ins = min(ASCENT_INSERT_ALT, p["start_perilune_km"] * 1e3)
        r_ins = MOON_RADIUS + h_ins
        ascent = _vis_viva(MOON_MU, r_ins, r_a0) + 100.0
        raise_dv = _vis_viva(MOON_MU, r_a0, r_p0) - _vis_viva(MOON_MU, r_a0, r_ins)
        drop = p["surface_payload_kg"] + p["surface_propellant_kg"]
        burns = [("Descent orbit insertion", doi, 0.0), ("Powered descent and landing", v_pdi + loss, 0.0),
                 ("Ascent to orbit", ascent, drop), ("Raise to the starting orbit", raise_dv + 10.0, 0.0)]
        out["accel"] = e["total_thrust"] / m0
        out["lunar_tw"] = tw
        if e["total_thrust"] <= m0 * g_m:
            out["error"] = (f"Engine thrust: the lander's thrust-to-weight on the Moon is only {tw:.2f}; "
                            "it must be above 1 to land. Add engines or pick bigger ones.")
        if e["min_thrust"] >= (l_dry - drop) * g_m * 0.98 and not out.get("error"):
            out["error"] = (f"Engine minimum throttle: one engine at its lowest setting ({e['min_thrust'] / 1e3:,.1f} kN) "
                            "is more than the lander's weight on the Moon, so it cannot hover or touch down "
                            "gently. Pick an engine that throttles deeper, or a smaller one.")
    rows = _budget(m0, e, burns)
    need = sum(r["propellant"] for r in rows)
    have = prop if part != "landing" else p["lander_prop_kg"]
    if part == "landing":
        need_on_board = need + p["surface_propellant_kg"]
        out["propellant_left"] = have - need_on_board
    else:
        out["propellant_left"] = have - need
    out["burns"] = rows
    out["dv_total"] = sum(r["dv"] for r in rows)
    out["propellant_needed"] = need
    out["margin_fraction"] = out["propellant_left"] / have if have > 0 else -1.0
    return out


# ---------------------------------------------------------------------------
# Mission status
# ---------------------------------------------------------------------------

class MoonStatus(ap.MissionStatus):
    def __init__(self):
        super().__init__()
        self.part = None

    def begin_part(self, part, params, est, phases, vehicle):
        with self.lock:
            self.abort_requested = False
            self.state = "running"
            self.part = part
            self.params = params
            self.estimate = est
            self.phases = [{"key": k, "title": t, "state": "pending"} for k, t in phases]
            self.telemetry = {}
            self.log = []
            self.result = None
            self.message = None
            self.vehicle = vehicle
            self.commands = []
            self.started = datetime.datetime.now(datetime.timezone.utc)

    def snapshot(self):
        snap = super().snapshot()
        with self.lock:
            snap["part"] = self.part
        return snap

    def command_report(self):
        if self.part == "launch":
            return super().command_report()
        with self.lock:
            p, est, result = self.params, self.estimate, self.result
            log, commands = list(self.log), list(self.commands)
            state, message, started = self.state, self.message, self.started
        t0 = log[0]["t"] if log else None
        title = f"MOON MISSION COMMAND LOG - {PARTS[self.part]}"
        out = [title, "=" * len(title), ""]
        if started:
            out.append(f"Mission started: {started:%Y-%m-%d %H:%M:%S} UTC (wall clock)")
        out.append(f"Outcome:         {state} - {message}")
        out += ["", "Setup", "-" * 5] + describe_setup(p)
        if est:
            out += ["", "Pre-flight estimate", "-" * 19]
            for b in est["burns"]:
                out.append(f"  {b['label']}: {b['dv']:,.0f} m/s, {b['propellant']:,.0f} kg of propellant, "
                           f"about {b['burn_time']:,.0f} s")
            out.append(f"  Propellant left (estimate): {est['propellant_left']:,.0f} kg")
        if result and result.get("report"):
            out += ["", "Result", "-" * 6] + [f"  {line}" for line in result["report"]]
        out += ["", "Times: T is mission time from the start (T+hh:mm:ss.s); t is simulation time in seconds.",
                "Inertial directions are unit vectors in the simulation frame (J2000 ecliptic: x toward the",
                "vernal equinox, z toward ecliptic north).", "", "Flight log", "-" * 10]
        for e in log:
            out.append(f"{ap._mission_clock(e['t'], t0):>16}  {e['text']}")
        out += ["", f"Commands sent to the simulation ({len(commands)})", "-" * 40,
                "Control commands only; the autopilot also steps the simulation and reads craft states.", ""]
        for t, text in commands:
            out.append(f"{ap._mission_clock(t, t0):>16}  t={t:>13,.1f}  {text}")
        return "\n".join(out) + "\n"


def describe_setup(p):
    e = p["engine"]
    lines = [f"  Part: {PARTS[p['part']]}",
             f"  Payload (dry mass, engines included): {p['payload_kg']:,.0f} kg; propellant {p['propellant_kg']:,.0f} kg",
             f"  Engines: {e['count']} x {e['name']} ({e['thrust'] / 1e3:,.1f} kN, Isp {e['isp']:.0f} s, "
             f"{e['mass']:,.0f} kg each, throttle down to {e['min_throttle'] * 100:.0f} %)"]
    if p["part"] == "transfer":
        lines.append(f"  Start: Earth orbit {p['start_perigee_km']:,.0f} x {p['start_apogee_km']:,.0f} km at "
                     f"{p['start_inclination_deg']:.2f} deg")
        lines.append(f"  Target: lunar orbit {p['target_perilune_km']:,.0f} x {p['target_apolune_km']:,.0f} km at "
                     f"{p['target_inclination_deg']:.2f} deg to the lunar equator")
    elif p["part"] == "landing":
        lines.append(f"  Start: lunar orbit {p['start_perilune_km']:,.0f} x {p['start_apolune_km']:,.0f} km at "
                     f"{p['start_inclination_deg']:.2f} deg")
        lines.append(f"  Lander: {p['lander_payload_pct']:g} % of the payload ({p['lander_dry_kg']:,.0f} kg), "
                     f"{p['lander_propellant_pct']:g} % of the propellant ({p['lander_prop_kg']:,.0f} kg)")
        lines.append(f"  Site: {p['site_name']} (latitude {p['site_lat']:.3f}, longitude {p['site_lon']:.3f})")
        lines.append(f"  Left on the surface: {p['surface_payload_kg']:,.0f} kg payload, "
                     f"{p['surface_propellant_kg']:,.0f} kg propellant; minimum stay {p['stay_hours']:g} h")
        lines.append("  Liftoff: " + ("when the site next passes under the orbit's plane" if p["liftoff"] == "plane"
                                      else "right after the minimum stay (same altitude and inclination, new plane)"))
    else:
        lines.append(f"  Start: lunar orbit {p['start_perilune_km']:,.0f} x {p['start_apolune_km']:,.0f} km at "
                     f"{p['start_inclination_deg']:.2f} deg")
        lines.append(f"  Target: Earth orbit {p['target_perigee_km']:,.0f} x {p['target_apogee_km']:,.0f} km at "
                     f"{p['target_inclination_deg']:.2f} deg")
    lines.append(f"  Pace: {p['pace']}" + ("   (simulation reset first)" if p["reset"] else ""))
    return lines


# ---------------------------------------------------------------------------
# Part 1: staging in Earth orbit
# ---------------------------------------------------------------------------

class LaunchMission(ap.MeoMission):
    """The Earth-orbit mission, reporting what it delivered: payload and leftover propellant."""

    def _separate_and_evaluate(self):
        result = super()._separate_and_evaluate()
        left = result.get("propellant_left_kg") or 0.0
        result["delivered"] = {"payload_kg": self.payload if result["success"] else 0.0,
                               "propellant_kg": left if result["success"] else 0.0,
                               "perigee_km": result["perigee_km"], "apogee_km": result["apogee_km"],
                               "inclination_deg": result["inclination_deg"]}
        return result


# ---------------------------------------------------------------------------
# Parts 2-4: shared flight machinery
# ---------------------------------------------------------------------------

def rotate(v, axis, angle):
    k = unit(axis)
    return (v * math.cos(angle) + np.cross(k, v) * math.sin(angle)
            + k * float(k @ v) * (1 - math.cos(angle)))


class LunarFlight(SimFlight):
    part = None

    def __init__(self, client, params, status):
        super().__init__(client, status, PACES[params["pace"]])
        self.p = params
        self.e = params["engine"]
        self.phase_keys = [k for k, _ in phases_for(self.part)]
        self.burn_log = []        # (label, dv m/s, propellant kg)
        self.prop_full = None

    # -- setup --------------------------------------------------------------------

    def setup(self):
        self.phase("prep")
        if self.p["reset"]:
            self.sim.reset()
            self.log("Simulation reset to its initial state.")
            # The scenario starts with a rocket on the pad; this part of the mission doesn't use it.
            for s in self.sim.get_state(bodies=False)["spacecraft"]:
                self.sim.remove_spacecraft(s["id"])
                self.log(f"Cleared '{s['id']}' from the scenario (not used by this part).")
        info = self.sim.info()
        self.t = info["time"]
        self.G = info["gravitational_constant"]
        self.body = {}
        for b in ("earth", "luna"):
            d = self.sim.get_body(b)
            self.body[b] = {
                "mu": self.G * d["mass"], "radius": d["radius"], "pole": np.array(d["pole"]),
                "spin": 2 * math.pi / d["rotation_period"] if d["rotation_period"] else 0.0,
                "pm": math.radians(d["prime_meridian"]), "pm_t": self.t,
            }
        existing = {s["id"] for s in self.sim.get_state(bodies=False)["spacecraft"]}
        clash = [i for i in IDS[self.part] if i in existing]
        if clash:
            raise MissionFailed(f"A craft named '{clash[0]}' is already in the simulation. "
                                "Enable 'reset the simulation first' and retry.")

    def mu(self, b):
        return self.body[b]["mu"]

    def radius(self, b):
        return self.body[b]["radius"]

    def pole(self, b):
        return self.body[b]["pole"]

    def snapshot(self, days):
        """Ephemeris from the sim's current state, plus that state."""
        st = self.sim.get_state()
        return nav.Ephemeris(st, self.G, days * 86400.0), st

    def craft_abs(self, st, sc_id=None):
        s = next(x for x in st["spacecraft"] if x["id"] == (sc_id or self.vehicle))
        return np.array(s["position"]), np.array(s["velocity"])

    def craft(self, sc_id=None, rel="luna"):
        sc = self.sim.get_spacecraft(sc_id or self.vehicle, relative_to=rel)
        return sc, np.array(sc["position"]), np.array(sc["velocity"])

    def add_vehicle(self, sc_id, dry, prop, body, r, v, parts, extra=None):
        model = {"vehicle": "lunar", "parts": parts, "engines": self.e["count"] if "engines" in parts else 0,
                 "engine": self.e["key"], "engine_thrust": self.e["thrust"], "dry": round(dry), "prop": round(prop)}
        if extra:
            model.update(extra)
        self.sim.add_spacecraft({
            "id": sc_id, "mass": dry + prop, "dry_mass": dry, "reference_area": 10.0,
            "model": model,
            "initial_state": {"frame": "relative", "body": body, "position": [float(x) for x in r],
                              "velocity": [float(x) for x in v]},
        })

    # -- telemetry ------------------------------------------------------------------

    def report(self, sc, r, v, body, note=""):
        el = nav.elements(r, v, self.mu(body), self.pole(body))
        R = self.radius(body)
        nb = sc["nearest_body"]
        prop = max(0.0, sc["mass"] - sc["dry_mass"])
        tel = {
            "t": self.t, "vehicle": sc["id"], "note": note,
            "body": "Moon" if body == "luna" else "Earth",
            "altitude_km": (np.linalg.norm(r) - R) / 1e3,
            "speed": float(np.linalg.norm(v)),
            "vertical_speed": float(v @ unit(r)),
            "periapsis_km": (el["r_p"] - R) / 1e3,
            "apoapsis_km": (el["r_a"] - R) / 1e3 if math.isfinite(el["r_a"]) else None,
            "inclination": el["inc"],
            "mass": sc["mass"], "propellant": prop,
            "propellant_full": self.prop_full if sc["id"] == self.vehicle else None,
            "thrust_kn": float(np.linalg.norm(sc["thrust"]["force"])) / 1e3,
            "accel_g": float(np.linalg.norm(sc["thrust"]["force"])) / sc["mass"] / G0,  # from thrust alone
            "surface_speed": nb["airspeed"],
            "nearest": nb["id"],
        }
        self.status.set_telemetry(tel, sc["id"])
        return el

    # -- engines ------------------------------------------------------------------------

    def command_accel(self, a_vec, mass, note):
        """Thrust for a desired thrust acceleration, within the engines' range."""
        f = float(np.linalg.norm(a_vec)) * mass
        e = self.e
        if f < e["min_thrust"]:
            f = e["min_thrust"] if f >= 0.5 * e["min_thrust"] else 0.0
        f = min(f, e["total_thrust"])
        if f <= 0:
            self.thrust([0.0, 0.0, 0.0], 0.0, note=note)
            return 0.0
        self.thrust(unit(a_vec) * f, -f / (e["isp"] * G0), note=note)
        return f

    def full_thrust(self, direction, note):
        e = self.e
        self.thrust(unit(direction) * e["total_thrust"], -e["mdot"], note=note)

    def check_events(self, what="vehicle"):
        if self.take_events({"contact_start"}, self.vehicle):
            raise MissionFailed(f"The {what} hit the surface.")
        if self.take_events({"engine_cutoff"}, self.vehicle):
            raise MissionFailed(f"The {what} ran out of propellant.")

    def burn_time(self, dv, mass):
        return _burn(dv, mass, self.e)[1]

    def _record_burn(self, label, m0, m1):
        dv = self.e["isp"] * G0 * math.log(m0 / m1) if m1 > 0 else 0.0
        self.burn_log.append((label, dv, m0 - m1))
        return dv

    # -- manoeuvres --------------------------------------------------------------------------

    def coast(self, remaining, body, note):
        """Coast until remaining(sc, r, v) <= 0.5 s (re-evaluated after every step)."""
        max_step = COAST_STEP if not self.warp[1] else max(1.0, self.warp[1] * COAST_WALL)
        while True:
            sc, r, v = self.craft(rel=body)
            self.report(sc, r, v, body, note)
            rem = remaining(sc, r, v)
            if rem <= 0.5:
                return sc, r, v
            # The last step goes straight to the target time: re-evaluating just past an
            # apsis would otherwise read "a whole orbit to go".
            self.step(min(rem, max_step), powered=False)
            self.check_events()
            if rem <= max_step:
                sc, r, v = self.craft(rel=body)
                self.report(sc, r, v, body, note)
                return sc, r, v

    def coast_to_time(self, t_target, body, note):
        return self.coast(lambda sc, r, v: t_target - self.t, body, note)

    def burn_to(self, desired, label, body, note=None, tol=0.3):
        """Full-thrust burn along the velocity still to be gained until it is below tol."""
        sc, r, v = self.craft(rel=body)
        m0 = sc["mass"]
        best = math.inf
        rising = 0
        while True:
            v_go = desired(r, v) - v
            n = float(np.linalg.norm(v_go))
            if n < tol:
                break
            best, rising = (n, 0) if n < best else (best, rising + 1)
            if rising >= 6 and best < 5.0:  # can't do better (steering limit): stop here
                break
            a = self.e["total_thrust"] / sc["mass"]
            f = min(self.e["total_thrust"], max(self.e["min_thrust"], sc["mass"] * n / TICK))
            self.thrust(unit(v_go) * f, -f / (self.e["isp"] * G0),
                        note=f"{note or label}: along the velocity still to be gained ({n:,.1f} m/s)")
            self.step(max(0.002, min(TICK, n * sc["mass"] / f)))
            self.check_events()
            sc, r, v = self.craft(rel=body)
            self.report(sc, r, v, body, label.lower())
        self.cut()
        dv = self._record_burn(label, m0, sc["mass"])
        return sc, r, v, dv

    def impulse(self, dv_vec, label, body):
        """Burn a fixed inertial velocity change (mid-course corrections)."""
        need = float(np.linalg.norm(dv_vec))
        d = unit(dv_vec)
        c = self.e["isp"] * G0
        sc, r, v = self.craft(rel=body)
        m0 = sc["mass"]
        while True:
            done = c * math.log(m0 / sc["mass"])
            rem = need - done
            if rem <= 0.01:
                break
            f = min(self.e["total_thrust"], max(self.e["min_thrust"], sc["mass"] * rem / TICK))
            self.thrust(d * f, -f / c, note=f"{label}: {need:,.2f} m/s in a fixed direction")
            self.step(max(0.001, min(TICK, rem * sc["mass"] / f)))
            self.check_events()
            sc, r, v = self.craft(rel=body)
            self.report(sc, r, v, body, label.lower())
        self.cut()
        self._record_burn(label, m0, sc["mass"])
        return sc, r, v

    def horizontal_burn(self, body, other_apsis, normal, label):
        """Burn at an apsis to a horizontal velocity that puts the other apsis at other_apsis."""
        mu = self.mu(body)

        def desired(r, v):
            rn = float(np.linalg.norm(r))
            return _vis_viva(mu, rn, other_apsis) * unit(np.cross(normal, r))

        return self.burn_to(desired, label, body)

    def trim_orbit(self, body, r_p_t, r_a_t, tol):
        """Two-burn correction of periapsis/apoapsis if outside half the tolerance."""
        R = self.radius(body)
        mu = self.mu(body)
        for _ in range(2):
            sc, r, v = self.craft(rel=body)
            el = nav.elements(r, v, mu, self.pole(body))
            err_p = abs(el["r_p"] - r_p_t)
            err_a = abs(el["r_a"] - r_a_t) if math.isfinite(el["r_a"]) else math.inf
            if err_p <= 0.5 * tolerance(r_p_t - R, tol) and err_a <= 0.5 * tolerance(r_a_t - R, tol):
                return
            normal = unit(el["h"])
            if err_a > 0.5 * tolerance(r_a_t - R, tol):
                lead = 0.5 * self.burn_time(abs(_vis_viva(mu, el["r_p"], r_a_t) - _vis_viva(mu, el["r_p"], el["r_a"])),
                                            sc["mass"])
                self.coast(lambda sc, r, v: nav.time_to_periapsis(r, v, mu) - lead, body, "coasting to periapsis")
                sc, r, v, dv = self.horizontal_burn(body, r_a_t, normal, "Trim burn at periapsis")
                self.log(f"Trim burn at periapsis: {dv:,.1f} m/s to set the apoapsis.")
            sc, r, v = self.craft(rel=body)
            el = nav.elements(r, v, mu, self.pole(body))
            if abs(el["r_p"] - r_p_t) > 0.5 * tolerance(r_p_t - R, tol):
                lead = 0.5 * self.burn_time(abs(_vis_viva(mu, el["r_a"], r_p_t) - _vis_viva(mu, el["r_a"], el["r_p"])),
                                            sc["mass"])
                self.coast(lambda sc, r, v: nav.time_to_apoapsis(r, v, mu) - lead, body, "coasting to apoapsis")
                sc, r, v, dv = self.horizontal_burn(body, r_p_t, normal, "Trim burn at apoapsis")
                self.log(f"Trim burn at apoapsis: {dv:,.1f} m/s to set the periapsis.")

    # -- targeting -------------------------------------------------------------------------

    def conic_desired(self, mu, r_dep, v_after):
        """Desired-velocity function for a long departure burn: same energy, plane and
        periapsis direction as the impulsive solution (r_dep, v_after)."""
        h = np.cross(r_dep, v_after)
        hn = float(np.linalg.norm(h))
        h_hat = h / hn
        e_vec = np.cross(v_after, h) / mu - r_dep / np.linalg.norm(r_dep)
        energy = 0.5 * float(v_after @ v_after) - mu / float(np.linalg.norm(r_dep))

        def desired(r, v):
            rn = float(np.linalg.norm(r))
            r_in = unit(r - float(r @ h_hat) * h_hat)
            direction = unit(np.cross(h_hat, r_in + e_vec))
            return math.sqrt(max(0.0, 2 * (energy + mu / rn))) * direction

        return desired

    # -- running -----------------------------------------------------------------------------

    def run(self):
        try:
            result = self.fly()
            ok = result["success"]
            if ok:
                b = result["orbit"]["body"]
                self.demo_body = b
                self.demo_orbit(self.mu(b), self.radius(b), self.pole(b), b)
            self.status.finish("succeeded" if ok else "failed", result.pop("message"), result)
        except MissionAborted:
            self._safe_cut()
            self.log("Mission aborted by the operator; engines shut down.", "error")
            self.status.finish("aborted", "Mission aborted.", self._partial())
        except MissionFailed as e:
            self._safe_cut()
            self.log(str(e), "error")
            self.status.finish("failed", str(e), self._partial())
        except Exception as e:  # sim unreachable, unexpected state, ...
            self._safe_cut()
            self.log(f"Autopilot error: {type(e).__name__}: {e}", "error")
            self.status.finish("failed", f"Autopilot error: {e}", self._partial())

    def demo_report(self):
        sc, r, v = self.craft(rel=self.demo_body)
        self.report(sc, r, v, self.demo_body, "demonstration orbit")

    def _partial(self):
        try:
            sc, _, _ = self.craft(rel="luna")
            return {"craft": sc["id"], "mass_kg": sc["mass"],
                    "propellant_kg": max(0.0, sc["mass"] - sc["dry_mass"]), "burns": self._burns()}
        except Exception:
            return None

    def _burns(self):
        return [{"label": b[0], "dv": b[1], "propellant": b[2]} for b in self.burn_log]

    def orbit_result(self, body, sc, r, v, targets, tol):
        """Achieved vs target orbit; targets = (periapsis alt m, apoapsis alt m, inclination deg)."""
        el = nav.elements(r, v, self.mu(body), self.pole(body))
        R = self.radius(body)
        got = ((el["r_p"] - R), (el["r_a"] - R) if math.isfinite(el["r_a"]) else math.inf, el["inc"])
        ok = (abs(got[0] - targets[0]) <= tolerance(targets[0], tol)
              and abs(got[1] - targets[1]) <= tolerance(targets[1], tol)
              and abs(got[2] - targets[2]) <= INC_TOL)
        return ok, {
            "body": body,
            "target": {"periapsis_km": targets[0] / 1e3, "apoapsis_km": targets[1] / 1e3,
                       "inclination_deg": targets[2]},
            "periapsis_km": got[0] / 1e3,
            "apoapsis_km": got[1] / 1e3 if math.isfinite(got[1]) else None,
            "inclination_deg": got[2],
        }


# ---------------------------------------------------------------------------
# Part 2: Earth orbit -> lunar orbit
# ---------------------------------------------------------------------------

class TransferMission(LunarFlight):
    part = "transfer"
    VID = "lunar_transfer"

    def fly(self):
        p = self.p
        self.setup()
        self.r_pl_t = self.radius("luna") + p["target_perilune_km"] * 1e3
        self.r_al_t = self.radius("luna") + p["target_apolune_km"] * 1e3
        self.inc_l = p["target_inclination_deg"]
        self.plan_and_place()
        self.tli()
        self.cruise()
        self.loi()
        self.phase("trim")
        self.trim_orbit("luna", self.r_pl_t, self.r_al_t, LUNAR_TOL)
        return self.finish()

    def residual(self, eph, t_dep, r_abs, v_abs, t_max):
        muM, poleM = self.mu("luna"), self.pole("luna")

        def f(dv):
            t, r, v = nav.closest_approach(eph, t_dep, r_abs, v_abs + dv, "luna", t_max, near=150e6)
            bp = nav.bplane(r, v, muM, poleM)
            if bp is None:
                el = nav.elements(r, v, muM, poleM)
                return np.array([(el["r_p"] - self.r_pl_t) / 1e3, 0.0]) + 1e4
            bt, br, S, T, R, vinf = bp
            tgt = nav.bplane_target(S, T, R, vinf, muM, self.r_pl_t, self.inc_l, poleM, (bt, br))
            return np.array([bt - tgt[0], br - tgt[1]]) / 1e3

        return f

    def plan_and_place(self):
        p = self.p
        muE, RE, poleE = self.mu("earth"), self.radius("earth"), self.pole("earth")
        r_pe = RE + p["start_perigee_km"] * 1e3
        r_ae = RE + p["start_apogee_km"] * 1e3
        m0 = p["payload_kg"] + p["propellant_kg"]
        self.prop_full = p["propellant_kg"]
        dv_est = _vis_viva(muE, r_pe, MOON_DISTANCE) - _vis_viva(muE, r_pe, r_ae)
        tau = self.burn_time(dv_est, m0)
        lead = tau / 2 + 900.0
        eph, st = self.snapshot(14)
        t0 = st["time"]
        t_dep = t0 + lead
        pe0, ve0 = eph.body("earth", t0)
        pm_dep, vm_dep = eph.body("luna", t_dep)
        pe_dep, ve_dep = eph.body("earth", t_dep)
        h_moon = unit(np.cross(pm_dep - pe_dep, vm_dep - ve_dep))
        self.log(f"Planning the trans-lunar trajectory (TLI about {dv_est:,.0f} m/s, burn about {tau:,.0f} s).")
        best = None
        for days in (3.0, 3.5, 4.0, 4.5, 5.0):
            pm, _ = eph.body("luna", t_dep + days * 86400)
            pe_a, _ = eph.body("earth", t_dep + days * 86400)
            m_rel = pm - pe_a
            n = nav.plane_through(m_rel, poleE, p["start_inclination_deg"], prefer=h_moon)
            d = -unit(m_rel)
            r0, v0 = nav.conic_state(muE, r_pe, r_ae, n, d, lead)
            # Where the placed craft really is at departure (the Moon and Sun pull on it too).
            _, r_dep, v_dep = nav.propagate(eph, t0, pe0 + r0, ve0 + v0, t_dep)
            pe_d, ve_d = eph.body("earth", t_dep)
            rel_r, rel_v = r_dep - pe_d, v_dep - ve_d
            v_need = _vis_viva(muE, float(np.linalg.norm(rel_r)), float(np.linalg.norm(m_rel)))
            dv0 = (v_need - float(np.linalg.norm(rel_v))) * unit(rel_v)
            f = self.residual(eph, t_dep, r_dep, v_dep, t_dep + 9 * 86400)
            x, res, ok = nav.solve(f, dv0)
            if ok and (best is None or np.linalg.norm(x) < np.linalg.norm(best["dv"])):
                best = {"dv": x, "r0": r0, "v0": v0, "rel_r": rel_r, "rel_v": rel_v, "days": days}
        if best is None:
            raise MissionFailed("Could not find a trajectory to the requested lunar orbit.")
        self.add_vehicle(self.VID, p["payload_kg"], p["propellant_kg"], "earth", best["r0"], best["v0"],
                         ["tanks", "engines"])
        self.vehicle = self.VID
        t_ca, _, _ = nav.closest_approach(eph, t_dep, best["rel_r"] + eph.body("earth", t_dep)[0],
                                          best["rel_v"] + best["dv"] + eph.body("earth", t_dep)[1],
                                          "luna", t_dep + 9 * 86400, near=150e6)
        self.t_dep = t_dep
        self.t_arrive = t_ca
        self.tli_plan = (best["rel_r"], best["rel_v"] + best["dv"])
        self.tau = tau
        self.log(f"Vehicle assembled in a {p['start_perigee_km']:,.0f} x {p['start_apogee_km']:,.0f} km Earth orbit "
                 f"at {p['start_inclination_deg']:.1f}°: {m0:,.0f} kg ({p['propellant_kg']:,.0f} kg propellant), "
                 f"{self.e['count']} x {self.e['name']}.")
        self.log(f"Trajectory: trans-lunar injection of {np.linalg.norm(best['dv']):,.0f} m/s in "
                 f"{(t_dep - self.t) / 60:,.0f} min; arrival at the Moon about {(t_ca - t_dep) / 86400:.1f} days later.")
        sc, r, v = self.craft(rel="earth")
        self.report(sc, r, v, "earth", "in Earth orbit")

    def tli(self):
        self.phase("tli")
        self.coast_to_time(self.t_dep - self.tau / 2, "earth", "coasting to the injection point")
        desired = self.conic_desired(self.mu("earth"), *self.tli_plan)
        self.log("Trans-lunar injection burn.")
        sc, r, v, dv = self.burn_to(desired, "Trans-lunar injection", "earth")
        el = nav.elements(r, v, self.mu("earth"), self.pole("earth"))
        apo = (el["r_a"] - self.radius("earth")) / 1e3 if math.isfinite(el["r_a"]) else None
        self.log(f"Injection complete: {dv:,.0f} m/s; Earth orbit apogee now "
                 + (f"{apo:,.0f} km." if apo is not None else "beyond escape."))
        self.t_burn_end = self.t

    def correct(self, n):
        """Mid-course correction: re-target the lunar arrival from the current state."""
        eph, st = self.snapshot(10)
        r, v = self.craft_abs(st)
        t = st["time"]
        f = self.residual(eph, t, r, v, t + 9 * 86400)
        before = f(np.zeros(3))
        x, res, ok = nav.solve(f, np.zeros(3), step=0.01, max_move=50.0)
        t_ca, rr, vv = nav.closest_approach(eph, t, r, v + x, "luna", t + 9 * 86400, near=150e6)
        self.t_arrive = t_ca
        if not ok:
            self.log(f"Course correction {n}: no exact solution found; best effort.", "warn")
        miss = float(np.linalg.norm(before))
        if np.linalg.norm(x) < MCC_MIN:
            self.log(f"Course correction {n} not needed (arrival off by about {miss:,.0f} km in the B-plane).")
            return
        self.impulse(x, f"Course correction {n}", "earth")
        self.log(f"Course correction {n}: {np.linalg.norm(x):,.2f} m/s (arrival was off by about "
                 f"{miss:,.0f} km in the B-plane).")

    def cruise(self):
        self.phase("cruise")
        plan = [self.t_burn_end + 6 * 3600, None, None]
        for i, frac in enumerate((None, 20 * 3600, 5 * 3600)):
            t_mcc = plan[0] if i == 0 else self.t_arrive - frac
            if t_mcc <= self.t + 60:
                continue
            self.coast_to_time(t_mcc, "earth" if i == 0 else "luna", "coasting to the Moon")
            self.correct(i + 1)

    def loi(self):
        self.phase("loi")
        muM = self.mu("luna")
        sc, r, v = self.craft(rel="luna")
        vinf2 = max(0.0, float(v @ v) - 2 * muM / float(np.linalg.norm(r)))
        v_peri = math.sqrt(vinf2 + 2 * muM / self.r_pl_t)
        dv = v_peri - _vis_viva(muM, self.r_pl_t, self.r_al_t)
        tau = self.burn_time(dv, sc["mass"])

        def remaining(sc, r, v):
            if np.linalg.norm(r) > 40e6:
                return self.t_arrive - tau / 2 - self.t
            return nav.time_to_periapsis(r, v, muM) - tau / 2

        sc, r, v = self.coast(remaining, "luna", "approaching the Moon")
        normal = unit(np.cross(r, v))
        self.log(f"Lunar orbit insertion burn (about {dv:,.0f} m/s, {tau:,.0f} s).")
        sc, r, v, dv = self.horizontal_burn("luna", self.r_al_t, normal, "Lunar orbit insertion")
        el = nav.elements(r, v, muM, self.pole("luna"))
        R = self.radius("luna")
        if el["e"] >= 1:
            raise MissionFailed("Lunar orbit insertion failed: the vehicle is still on an escape trajectory.")
        self.log(f"In lunar orbit: {(el['r_p'] - R) / 1e3:,.0f} x {(el['r_a'] - R) / 1e3:,.0f} km at "
                 f"{el['inc']:.2f}° ({dv:,.0f} m/s).")

    def finish(self):
        sc, r, v = self.craft(rel="luna")
        self.step(10.0, powered=False)
        sc, r, v = self.craft(rel="luna")
        self.report(sc, r, v, "luna", "in lunar orbit")
        p = self.p
        ok, orbit = self.orbit_result("luna", sc, r, v, (p["target_perilune_km"] * 1e3,
                                                         p["target_apolune_km"] * 1e3, self.inc_l), LUNAR_TOL)
        prop = max(0.0, sc["mass"] - sc["dry_mass"])
        delivered = {"payload_kg": sc["dry_mass"], "propellant_kg": prop,
                     "perilune_km": orbit["periapsis_km"], "apolune_km": orbit["apoapsis_km"],
                     "inclination_deg": orbit["inclination_deg"]}
        report = [f"In lunar orbit {orbit['periapsis_km']:,.1f} x {orbit['apoapsis_km']:,.1f} km at "
                  f"{orbit['inclination_deg']:.2f} deg (target {p['target_perilune_km']:,.0f} x "
                  f"{p['target_apolune_km']:,.0f} km at {self.inc_l:.2f} deg)",
                  f"Payload {sc['dry_mass']:,.0f} kg, propellant remaining {prop:,.0f} kg",
                  f"Flight time {(self.t - self.t_dep) / 86400:.2f} days from injection"]
        report += [f"{b[0]}: {b[1]:,.1f} m/s, {b[2]:,.0f} kg" for b in self.burn_log]
        msg = ("Vehicle in the target lunar orbit." if ok
               else "Vehicle in lunar orbit, but outside the target tolerance.")
        self.log(msg, "info" if ok else "warn")
        return {"success": ok, "message": msg, "craft": sc["id"], "orbit": orbit, "delivered": delivered,
                "burns": self._burns(), "report": report}


# ---------------------------------------------------------------------------
# Part 3: landing and return to lunar orbit
# ---------------------------------------------------------------------------

def _eguidance(r, v, r_t, v_t, T):
    """Constant-jerk (E-guidance) total acceleration now and its rate, to reach r_t, v_t in T."""
    dr = r_t - r - v * T
    dv = v_t - v
    c1 = (6 * dv * T - 12 * dr) / T**3
    c0 = (dv - c1 * T * T / 2) / T
    return c0, c1


class LandingMission(LunarFlight):
    part = "landing"
    STACK, ORBITER, LANDER, CARGO = IDS["landing"]

    def fly(self):
        self.setup()
        self.plan_and_place()
        self.undock()
        self.doi()
        self.descend()
        self.surface()
        self.ascend()
        self.phase("raise")
        self.trim_orbit("luna", self.r_p0, self.r_a0, LUNAR_TOL)
        return self.finish()

    # -- the Moon's surface -------------------------------------------------------------

    def site_dir(self, t):
        b = self.body["luna"]
        pole = b["pole"]
        ref = np.cross(pole, [0.0, 0.0, 1.0])
        if np.linalg.norm(ref) < 1e-12:
            ref = np.cross(pole, [0.0, 1.0, 0.0])
        x = unit(ref)
        y = np.cross(pole, x)
        w = b["pm"] + b["spin"] * (t - b["pm_t"]) + math.radians(self.p["site_lon"])
        lat = math.radians(self.p["site_lat"])
        return math.cos(lat) * (math.cos(w) * x + math.sin(w) * y) + math.sin(lat) * pole

    def surface_velocity(self, rel):
        b = self.body["luna"]
        return np.cross(b["spin"] * b["pole"], rel)

    def gravity(self, r):
        return -self.mu("luna") * r / float(np.linalg.norm(r)) ** 3

    # -- plan ---------------------------------------------------------------------------------

    def plan_and_place(self):
        p = self.p
        R, muM, pole = self.radius("luna"), self.mu("luna"), self.pole("luna")
        self.r_p0 = R + p["start_perilune_km"] * 1e3
        self.r_a0 = R + p["start_apolune_km"] * 1e3
        self.l_dry, self.l_prop = p["lander_dry_kg"], p["lander_prop_kg"]
        self.o_dry = p["payload_kg"] - self.l_dry
        self.o_prop = p["propellant_kg"] - self.l_prop
        self.prop_full = self.l_prop
        m_l = self.l_dry + self.l_prop
        r_d = R + DESCENT_PERILUNE
        # Braking distance at a typical 60 % of full thrust: the descent orbit's low point
        # is put that far before the site.
        v_pdi = _vis_viva(muM, r_d, self.r_a0)
        a_avg = 0.6 * self.e["total_thrust"] / (m_l * 0.85)
        brake = min(v_pdi**2 / (2 * a_avg), 0.25 * 2 * math.pi * R)
        phi = brake / R
        self.phi = phi
        doi_dv = _vis_viva(muM, self.r_a0, self.r_p0) - _vis_viva(muM, self.r_a0, r_d)
        tau_doi = self.burn_time(abs(doi_dv), m_l)
        lead = 900.0 + tau_doi / 2
        a_d = 0.5 * (r_d + self.r_a0)
        p_d = 2 * math.pi * math.sqrt(a_d**3 / muM)
        t_land = self.t + lead + p_d / 2 + 2 * brake / v_pdi
        s_hat = self.site_dir(t_land)
        self.normal = nav.plane_through(s_hat, pole, p["start_inclination_deg"], prefer=pole)
        p_hat = rotate(s_hat - float(s_hat @ self.normal) * self.normal, self.normal, -phi)
        a0 = 0.5 * (self.r_p0 + self.r_a0)
        p0 = 2 * math.pi * math.sqrt(a0**3 / muM)
        r, v = nav.conic_state(muM, self.r_p0, self.r_a0, self.normal, p_hat, lead + p0 / 2)
        self.t_doi = self.t + lead
        self.has_orbiter = self.o_dry + self.o_prop > 0
        if self.has_orbiter:
            self.add_vehicle(self.STACK, p["payload_kg"], p["propellant_kg"], "luna", r, v,
                             ["tanks", "lander", "engines"],
                             {"lander_dry": round(self.l_dry), "lander_prop": round(self.l_prop)})
            self.vehicle = self.STACK
        else:  # the whole vehicle is the lander
            self.add_vehicle(self.LANDER, self.l_dry, self.l_prop, "luna", r, v, ["lander", "engines"])
            self.vehicle = self.LANDER
        self.log(f"Vehicle placed in a {p['start_perilune_km']:,.0f} x {p['start_apolune_km']:,.0f} km lunar orbit "
                 f"at {p['start_inclination_deg']:.1f}°, its plane passing over {p['site_name']}.")
        sc, r, v = self.craft()
        self.report(sc, r, v, "luna", "in lunar orbit")

    def undock(self):
        self.phase("undock")
        self.step(30.0, powered=False)
        if not self.has_orbiter:
            self.log(f"The whole vehicle is the lander: {self.l_dry + self.l_prop:,.0f} kg "
                     f"({self.l_prop:,.0f} kg propellant); nothing stays in orbit.")
            return
        sc, r, v = self.craft()
        self.sim.stage(self.STACK, [
            {"id": self.ORBITER, "mass": self.o_dry + self.o_prop, "dry_mass": self.o_dry,
             "delta_v": [0.0, 0.0, 0.0],
             "model": {"vehicle": "lunar", "parts": ["tanks"], "engines": 0,
                       "dry": round(self.o_dry), "prop": round(self.o_prop)}},
            {"id": self.LANDER, "mass": self.l_dry + self.l_prop, "dry_mass": self.l_dry,
             "delta_v": list(-0.2 * unit(v)),
             "model": {"vehicle": "lunar", "parts": ["lander", "engines"],
                       "engines": self.e["count"], "engine": self.e["key"], "engine_thrust": self.e["thrust"],
                       "dry": round(self.l_dry), "prop": round(self.l_prop)}},
        ], frame="inertial")
        self.vehicle = self.LANDER
        self.log(f"Lander separated: {self.l_dry + self.l_prop:,.0f} kg ({self.l_prop:,.0f} kg propellant); "
                 f"{self.o_dry + self.o_prop:,.0f} kg stays in orbit as '{self.ORBITER}'.")

    def doi(self):
        self.phase("doi")
        R, muM = self.radius("luna"), self.mu("luna")
        r_d = R + DESCENT_PERILUNE
        dv = _vis_viva(muM, self.r_a0, self.r_p0) - _vis_viva(muM, self.r_a0, r_d)
        tau = self.burn_time(abs(dv), self.l_dry + self.l_prop)
        self.coast_to_time(self.t_doi - tau / 2, "luna", "coasting to descent orbit insertion")
        sc, r, v, dv = self.horizontal_burn("luna", r_d, self.normal, "Descent orbit insertion")
        el = nav.elements(r, v, muM, self.pole("luna"))
        self.log(f"Descent orbit insertion: {dv:,.1f} m/s; low point now {(el['r_p'] - R) / 1e3:,.1f} km.")

    # -- descent ---------------------------------------------------------------------------

    def gate(self, t_arrive, alt, vz):
        s = self.site_dir(t_arrive)
        pos = (self.radius("luna") + alt) * s
        return pos, self.surface_velocity(pos) + vz * s

    def guide(self, r, v, T, gate):
        """E-guidance toward a gate above the site, T seconds from now.

        Works in curvilinear coordinates along the great circle to the gate
        (downrange arc length, crossrange, altitude), with gravity reduced by
        the centrifugal effect of horizontal speed, so a long braking arc
        around the curved Moon is planned sensibly.  Returns the thrust
        acceleration needed now (inertial) and at the end (magnitude).
        """
        muM = self.mu("luna")
        r_t, v_t = self.gate(self.t + T, *gate)
        up = unit(r)
        up_t = unit(r_t)
        horiz = r_t - float(r_t @ up) * up
        if np.linalg.norm(horiz) < 1.0:
            horiz = v - float(v @ up) * up
        w = unit(horiz)
        n = np.cross(up, w)
        w_t = np.cross(n, up_t)
        rn, rt = float(np.linalg.norm(r)), float(np.linalg.norm(r_t))
        theta = math.atan2(float(np.linalg.norm(np.cross(up, up_t))), float(up @ up_t))
        pos = np.array([0.0, 0.0, rn])
        vel = np.array([float(v @ w), float(v @ n), float(v @ up)])
        pos_t = np.array([theta * rt, 0.0, rt])
        vel_t = np.array([float(v_t @ w_t), float(v_t @ n), float(v_t @ up_t)])
        c0, c1 = _eguidance(pos, vel, pos_t, vel_t, T)
        g_now = np.array([-vel[0] * vel[2] / rn, 0.0, -muM / rn**2 + vel[0] ** 2 / rn])
        g_end = np.array([0.0, 0.0, -muM / rt**2 + vel_t[0] ** 2 / rt])
        a = c0 - g_now
        a_end = c0 + c1 * T - g_end
        return a[0] * w + a[1] * n + a[2] * up, float(np.linalg.norm(a_end))

    def peak_load(self, r, v, m, T, alt, vz):
        a, a_end = self.guide(r, v, T, (alt, vz))
        a_max = self.e["total_thrust"] / m
        return max(float(np.linalg.norm(a)), a_end) / a_max

    def plan_phase(self, r, v, m, alt, vz, load, t_max=1800.0):
        """Shortest time to a gate whose peak thrust need is within `load` of full thrust;
        returns (T, peak) - the least-loaded T if none qualifies."""
        best = None
        for T in np.arange(10.0, t_max, 5.0):
            pk = self.peak_load(r, v, m, T, alt, vz)
            if pk <= load:
                return T, pk
            if best is None or pk < best[1]:
                best = (T, pk)
        return best

    def descend(self):
        self.phase("descent")
        R = self.radius("luna")
        # Coast toward the low point.  Once the site is within reach ahead (the braking
        # guidance assumes a short arc), ignite when braking needs BRAKING_LOAD of full thrust.
        window = min(math.radians(60.0), 2.0 * self.phi + math.radians(10.0))
        while True:
            sc, r, v = self.craft()
            self.report(sc, r, v, "luna", "coasting to powered descent")
            s = self.site_dir(self.t)
            ahead = math.acos(max(-1.0, min(1.0, float(unit(r) @ s))))
            approaching = float(v @ (s * R - r)) > 0
            alt = np.linalg.norm(r) - R
            if not (approaching and ahead < window):
                self.step(min(60.0, max(1.0, (ahead - window) * np.linalg.norm(r) / np.linalg.norm(v)))
                          if approaching else 60.0, powered=False)
                self.check_events("lander")
                continue
            # Constant-deceleration braking over the remaining arc, with on average half
            # of lunar gravity still to be held up (none at orbital speed, all of it at the end).
            v_rel = v - self.surface_velocity(r)
            v_h = float(np.linalg.norm(v_rel - float(v_rel @ unit(r)) * unit(r)))
            dist = max(ahead * R, 1.0)
            g_m = self.mu("luna") / R**2
            load = math.hypot(v_h**2 / (2 * dist), 0.5 * g_m) / (self.e["total_thrust"] / sc["mass"])
            if load >= BRAKING_LOAD or alt < 3e3:
                T = 2 * dist / max(v_h, 1.0)
                break
            self.step(1.0 if load > 0.6 * BRAKING_LOAD else 5.0, powered=load > 0.6 * BRAKING_LOAD)
            self.check_events("lander")
        m_pdi = sc["mass"]
        self.log(f"Powered descent initiation at {alt / 1e3:,.1f} km, {np.linalg.norm(v):,.0f} m/s; "
                 f"braking planned for {T:,.0f} s.")
        self.fly_gate(T, HIGH_GATE, "braking", BRAKING_LOAD)
        sc, r, v = self.craft()
        self.log(f"High gate: {(np.linalg.norm(r) - R):,.0f} m above the surface.")
        T, pk = self.plan_phase(r, v, sc["mass"], *LOW_GATE, load=APPROACH_LOAD, t_max=300.0)
        self.fly_gate(T, LOW_GATE, "approach", APPROACH_LOAD)
        self.touchdown()
        sc, r, v = self.craft()
        self._record_burn("Powered descent and landing", m_pdi, sc["mass"])

    def fly_gate(self, T, gate, label, load):
        t_end = self.t + T
        last_replan = self.t
        while True:
            sc, r, v = self.craft()
            self.check_events("lander")
            t_go = t_end - self.t
            if t_go < 2.0:
                return
            a, _ = self.guide(r, v, t_go, gate)
            a_max = self.e["total_thrust"] / sc["mass"]
            if np.linalg.norm(a) > 0.98 * a_max and self.t - last_replan > 5.0:
                T2, pk = self.plan_phase(r, v, sc["mass"], *gate, load=0.95, t_max=max(60.0, 3 * t_go))
                t_end = self.t + T2
                last_replan = self.t
                continue
            self.command_accel(a, sc["mass"], note=f"{label} guidance, {t_go:,.0f} s to go")
            self.report(sc, r, v, "luna", label)
            self.step(min(TICK, max(0.1, t_go - 2.0)))

    def touchdown(self):
        R = self.radius("luna")
        last = None
        t_start = self.t
        while True:
            sc, r, v = self.craft()
            if self.take_events({"contact_start"}, self.vehicle):
                break
            if self.take_events({"engine_cutoff"}, self.vehicle):
                raise MissionFailed("The lander ran out of propellant just before touchdown.")
            alt = np.linalg.norm(r) - R
            up = unit(r)
            v_rel = v - self.surface_velocity(r)
            last = (float(v_rel @ up), float(np.linalg.norm(v_rel - (v_rel @ up) * up)))
            vz_t = TOUCHDOWN_VZ[0] if alt > 5.0 else TOUCHDOWN_VZ[1]
            a = (vz_t * up - v_rel) / 1.5 - self.gravity(r)
            self.command_accel(a, sc["mass"], note="terminal descent")
            self.report(sc, r, v, "luna", "terminal descent")
            if self.t - t_start > 600:
                raise MissionFailed("Terminal descent took too long (the lander could not get down).")
            self.step(0.25)
        self.cut()
        self.step(10.0)
        sc, r, v = self.craft()
        nb = sc["nearest_body"]
        vz, vh = last
        self.touch = {"vertical": -vz, "horizontal": vh}
        lat1, lon1 = math.radians(nb["latitude"]), math.radians(nb["longitude"])
        lat2, lon2 = math.radians(self.p["site_lat"]), math.radians(self.p["site_lon"])
        c = (math.sin(lat1) * math.sin(lat2) + math.cos(lat1) * math.cos(lat2) * math.cos(lon1 - lon2))
        self.site_error = R * math.acos(max(-1.0, min(1.0, c)))
        if -vz > MAX_TOUCHDOWN[0] or vh > MAX_TOUCHDOWN[1]:
            raise MissionFailed(f"Hard landing: touched down at {-vz:.1f} m/s down, {vh:.1f} m/s sideways "
                                f"(limits {MAX_TOUCHDOWN[0]:g} and {MAX_TOUCHDOWN[1]:g} m/s).")
        self.t_land = self.t
        self.log(f"Touchdown at {-vz:.2f} m/s down, {vh:.2f} m/s sideways, {self.site_error / 1e3:,.2f} km from "
                 f"the target; {sc['mass'] - sc['dry_mass']:,.0f} kg propellant left.")

    # -- surface ----------------------------------------------------------------------------

    def surface(self):
        self.phase("surface")
        p = self.p
        sc, r, v = self.craft()
        drop = p["surface_payload_kg"] + p["surface_propellant_kg"]
        if drop > 0:
            prop_now = sc["mass"] - sc["dry_mass"]
            if p["surface_propellant_kg"] > prop_now + 1e-6:
                raise MissionFailed(f"Only {prop_now:,.0f} kg of propellant is left, less than the "
                                    f"{p['surface_propellant_kg']:,.0f} kg meant for the surface.")
            self.sim.stage(self.LANDER, [
                {"id": self.CARGO, "mass": drop, "dry_mass": p["surface_payload_kg"], "delta_v": [0.0, 0.0, 0.0],
                 "model": {"vehicle": "lunar", "parts": ["cargo"], "dry": round(p["surface_payload_kg"]),
                           "prop": round(p["surface_propellant_kg"])}},
                {"id": self.LANDER, "mass": sc["mass"] - drop, "dry_mass": sc["dry_mass"] - p["surface_payload_kg"],
                 "delta_v": [0.0, 0.0, 0.0]},
            ], frame="inertial")
            self.log(f"Unloaded {p['surface_payload_kg']:,.0f} kg of payload and {p['surface_propellant_kg']:,.0f} kg "
                     f"of propellant on the surface as '{self.CARGO}'.")
        if self.has_orbiter:
            _, ro, vo = self.craft(self.ORBITER)
            self.normal = unit(np.cross(ro, vo))
        t_min = max(self.t_land + p["stay_hours"] * 3600.0, self.t + 60.0)
        if p["liftoff"] == "plane":
            # Lift off when the site next passes under the orbit's plane (after the minimum stay).
            t_go = self.next_plane_crossing(t_min)
            self.log(f"Surface stay: liftoff planned {((t_go - self.t_land) / 3600):,.1f} h after landing, when "
                     "the site passes under the orbit's plane.")
        else:
            # Lift off after the minimum stay into an orbit of the same altitude and
            # inclination whose plane passes over the site then.
            t_go = t_min
            old = self.normal
            self.normal = nav.plane_through(self.site_dir(t_go), self.pole("luna"), p["start_inclination_deg"],
                                            prefer=old)
            turn = math.degrees(math.acos(max(-1.0, min(1.0, float(old @ self.normal)))))
            self.log(f"Surface stay: liftoff planned {((t_go - self.t_land) / 3600):,.1f} h after landing, into an "
                     f"orbit of the same altitude and inclination; its plane is turned {turn:.1f}° from the "
                     "starting orbit's.")
        self.coast_to_time(t_go, "luna", "on the surface")
        self.t_liftoff = self.t

    def next_plane_crossing(self, t_from):
        f = lambda t: float(self.site_dir(t) @ self.normal)
        step = 1800.0
        t = t_from
        f0 = f(t)
        if abs(f0) < 1e-4:
            return t
        for _ in range(int(40 * 86400 / step)):
            t1 = t + step
            f1 = f(t1)
            if f0 * f1 <= 0:
                lo, hi = t, t1
                for _ in range(40):
                    mid = 0.5 * (lo + hi)
                    if f(lo) * f(mid) <= 0:
                        hi = mid
                    else:
                        lo = mid
                return hi
            t, f0 = t1, f1
        # Tangent plane (site latitude equals the inclination): closest approach instead.
        ts = np.arange(t_from, t_from + 30 * 86400, 600.0)
        return float(ts[int(np.argmin([abs(f(x)) for x in ts]))])

    # -- ascent -----------------------------------------------------------------------------

    def ascend(self):
        self.phase("ascent")
        R, muM = self.radius("luna"), self.mu("luna")
        sc, r, v = self.craft()
        m_lift = sc["mass"]
        h_ins = min(ASCENT_INSERT_ALT, self.p["start_perilune_km"] * 1e3)
        r_ins = R + h_ins
        v_h_t = _vis_viva(muM, r_ins, self.r_a0)
        c = self.e["isp"] * G0
        self.log(f"Liftoff with {sc['mass'] - sc['dry_mass']:,.0f} kg of propellant.")
        t0 = self.t
        while True:
            sc, r, v = self.craft()
            self.check_events("lander")
            up = unit(r)
            n = self.normal
            w = unit(np.cross(n, up))
            vr, vh, vn = float(v @ up), float(v @ w), float(v @ n)
            rn = float(np.linalg.norm(r))
            a_T = self.e["total_thrust"] / sc["mass"]
            if self.t - t0 < ASCENT_RISE:
                self.full_thrust(up, "vertical rise")
                self.report(sc, r, v, "luna", "vertical rise")
                self.step(TICK)
                continue
            dv_need = math.sqrt((v_h_t - vh) ** 2 + vr**2 + vn**2)
            el = nav.elements(r, v, muM, self.pole("luna"))
            if vh >= v_h_t - 0.3 or dv_need < 0.5 or (math.isfinite(el["r_a"]) and el["r_a"] >= self.r_a0
                                                       and rn > r_ins - 2e3):
                break
            t_go = sc["mass"] / self.e["mdot"] * (1 - math.exp(-dv_need / c))
            T = max(t_go, 8.0)
            dr = r_ins - rn - vr * T
            dvr = -vr
            c1 = (6 * dvr * T - 12 * dr) / T**3
            c0 = (dvr - c1 * T * T / 2) / T
            a_r = c0 + muM / rn**2 - vh**2 / rn
            a_n = -vn / max(T, 5.0)
            a_h = math.sqrt(max(a_T**2 - a_r**2 - a_n**2, 0.0))
            direction = a_r * up + a_n * n + a_h * w
            f = self.e["total_thrust"]
            if t_go < TICK:  # last fraction of a second: throttle to land on the target speed
                f = max(self.e["min_thrust"], min(f, sc["mass"] * dv_need / TICK))
            self.thrust(unit(direction) * f, -f / c,
                        note=f"ascent guidance: {math.degrees(math.atan2(a_r, a_h)):.1f}° above the horizon, "
                             f"{t_go:,.0f} s to go")
            self.report(sc, r, v, "luna", "ascent")
            self.step(max(0.02, min(TICK, t_go)))
        self.cut()
        sc, r, v = self.craft()
        self._record_burn("Ascent to orbit", m_lift, sc["mass"])
        el = nav.elements(r, v, muM, self.pole("luna"))
        self.log(f"Orbit insertion: {(el['r_p'] - R) / 1e3:,.1f} x {(el['r_a'] - R) / 1e3:,.1f} km, "
                 f"{sc['mass'] - sc['dry_mass']:,.0f} kg propellant left.")

    def finish(self):
        p = self.p
        self.step(10.0, powered=False)
        sc, r, v = self.craft()
        self.report(sc, r, v, "luna", "back in lunar orbit")
        ok, orbit = self.orbit_result("luna", sc, r, v, (p["start_perilune_km"] * 1e3, p["start_apolune_km"] * 1e3,
                                                         p["start_inclination_deg"]), LUNAR_TOL)
        l_prop = max(0.0, sc["mass"] - sc["dry_mass"])
        o_pay, o_prop = (self.o_dry, self.o_prop) if self.has_orbiter else (0.0, 0.0)
        surface = {"payload_kg": p["surface_payload_kg"], "propellant_kg": p["surface_propellant_kg"]}
        delivered = {"payload_kg": o_pay + sc["dry_mass"], "propellant_kg": o_prop + l_prop,
                     "lander_payload_kg": sc["dry_mass"], "lander_propellant_kg": l_prop,
                     "orbiter_payload_kg": o_pay, "orbiter_propellant_kg": o_prop,
                     "perilune_km": p["start_perilune_km"], "apolune_km": p["start_apolune_km"],
                     "inclination_deg": p["start_inclination_deg"]}
        report = [f"Landed {self.site_error / 1e3:,.2f} km from {p['site_name']}, touchdown "
                  f"{self.touch['vertical']:.2f} m/s down / {self.touch['horizontal']:.2f} m/s sideways",
                  f"Left on the surface: {surface['payload_kg']:,.0f} kg payload, "
                  f"{surface['propellant_kg']:,.0f} kg propellant",
                  f"Lander back in orbit {orbit['periapsis_km']:,.1f} x {orbit['apoapsis_km']:,.1f} km at "
                  f"{orbit['inclination_deg']:.2f} deg: {sc['dry_mass']:,.0f} kg payload, {l_prop:,.0f} kg propellant",
                  f"Orbiter: {o_pay:,.0f} kg payload, {o_prop:,.0f} kg propellant",
                  f"In lunar orbit in total: {delivered['payload_kg']:,.0f} kg payload, "
                  f"{delivered['propellant_kg']:,.0f} kg propellant"]
        report += [f"{b[0]}: {b[1]:,.1f} m/s, {b[2]:,.0f} kg" for b in self.burn_log]
        msg = ("Landed and back in the starting orbit." if ok
               else "Landed and back in lunar orbit, but outside the target tolerance.")
        self.log(msg, "info" if ok else "warn")
        return {"success": ok, "message": msg, "craft": sc["id"], "orbit": orbit, "delivered": delivered,
                "surface": surface, "landing": {"site_error_km": self.site_error / 1e3, **self.touch,
                                                "surface_hours": (self.t_liftoff - self.t_land) / 3600},
                "burns": self._burns(), "report": report}


# ---------------------------------------------------------------------------
# Part 4: lunar orbit -> Earth orbit
# ---------------------------------------------------------------------------

class ReturnMission(LunarFlight):
    part = "return"
    VID = "earth_return"

    def fly(self):
        p = self.p
        self.setup()
        self.r_pe_t = self.radius("earth") + p["target_perigee_km"] * 1e3
        self.r_ae_t = self.radius("earth") + p["target_apogee_km"] * 1e3
        self.inc_t = p["target_inclination_deg"]
        self.plan_and_place()
        self.tei()
        self.cruise()
        self.capture()
        self.phase("trim")
        self.trim_orbit("earth", self.r_pe_t, self.r_ae_t, EARTH_TOL)
        return self.finish()

    def residual(self, eph, t_dep, r_abs, v_abs, t_max):
        muE, poleE = self.mu("earth"), self.pole("earth")

        def f(dv):
            t, r, v = nav.closest_approach(eph, t_dep, r_abs, v_abs + dv, "earth", t_max, near=150e6)
            el = nav.elements(r, v, muE, poleE)
            return np.array([(el["r_p"] - self.r_pe_t) / 1e3, (el["inc"] - self.inc_t) * 20.0])

        return f

    def plan_and_place(self):
        p = self.p
        muM, R, poleM = self.mu("luna"), self.radius("luna"), self.pole("luna")
        muE, poleE = self.mu("earth"), self.pole("earth")
        r_p0 = R + p["start_perilune_km"] * 1e3
        r_a0 = R + p["start_apolune_km"] * 1e3
        m0 = p["payload_kg"] + p["propellant_kg"]
        self.prop_full = p["propellant_kg"]
        vinf_est = _arrival_vinf(self.r_pe_t)
        dv_est = math.sqrt(vinf_est**2 + 2 * muM / r_p0) - _vis_viva(muM, r_p0, r_a0)
        tau = self.burn_time(dv_est, m0)
        lead = tau / 2 + 900.0
        eph, st = self.snapshot(14)
        t0 = st["time"]
        t_dep = t0 + lead
        pm, vm = eph.body("luna", t_dep)
        pe, ve = eph.body("earth", t_dep)
        r_m, v_m = pm - pe, vm - ve
        h_moon = unit(np.cross(r_m, v_m))
        pm0, vm0 = eph.body("luna", t0)
        self.log(f"Planning the trip home (trans-Earth injection about {dv_est:,.0f} m/s, burn about {tau:,.0f} s).")
        best = None
        for prefer in (h_moon, -h_moon):
            n_ret = nav.plane_through(r_m, poleE, self.inc_t, prefer=prefer)
            rm = float(np.linalg.norm(r_m))
            v_apo = _vis_viva(muE, rm, self.r_pe_t) * unit(np.cross(n_ret, r_m))
            vinf_vec = v_apo - v_m
            S = unit(vinf_vec)
            vinf = float(np.linalg.norm(vinf_vec))
            for pref_l in (poleM, -poleM):
                n_l = nav.plane_through(S, poleM, p["start_inclination_deg"], prefer=pref_l)
                S_in = unit(S - float(S @ n_l) * n_l)
                ecc = 1 + r_p0 * vinf**2 / muM
                w = np.cross(n_l, S_in)
                p_hat = -(1 / ecc) * S_in - math.sqrt(1 - 1 / ecc**2) * w
                r0, v0 = nav.conic_state(muM, r_p0, r_a0, n_l, p_hat, lead)
                _, r_dep, v_dep = nav.propagate(eph, t0, pm0 + r0, vm0 + v0, t_dep)
                pm_d, vm_d = eph.body("luna", t_dep)
                rel_r, rel_v = r_dep - pm_d, v_dep - vm_d
                v_need = math.sqrt(vinf**2 + 2 * muM / float(np.linalg.norm(rel_r)))
                dv0 = (v_need - float(np.linalg.norm(rel_v))) * unit(rel_v)
                f = self.residual(eph, t_dep, r_dep, v_dep, t_dep + 9 * 86400)
                x, res, ok = nav.solve(f, dv0)
                if ok and (best is None or np.linalg.norm(x) < np.linalg.norm(best["dv"])):
                    best = {"dv": x, "r0": r0, "v0": v0, "rel_r": rel_r, "rel_v": rel_v}
        if best is None:
            raise MissionFailed("Could not find a trajectory back to the requested Earth orbit.")
        self.add_vehicle(self.VID, p["payload_kg"], p["propellant_kg"], "luna", best["r0"], best["v0"],
                         ["tanks", "engines"])
        self.vehicle = self.VID
        pm_d, vm_d = eph.body("luna", t_dep)
        t_pe, _, _ = nav.closest_approach(eph, t_dep, best["rel_r"] + pm_d, best["rel_v"] + best["dv"] + vm_d,
                                          "earth", t_dep + 9 * 86400, near=150e6)
        self.t_dep = t_dep
        self.t_arrive = t_pe
        self.tei_plan = (best["rel_r"], best["rel_v"] + best["dv"])
        self.tau = tau
        self.log(f"Vehicle placed in a {p['start_perilune_km']:,.0f} x {p['start_apolune_km']:,.0f} km lunar orbit "
                 f"at {p['start_inclination_deg']:.1f}°: {m0:,.0f} kg ({p['propellant_kg']:,.0f} kg propellant), "
                 f"{self.e['count']} x {self.e['name']}.")
        self.log(f"Trajectory: trans-Earth injection of {np.linalg.norm(best['dv']):,.0f} m/s in "
                 f"{(t_dep - self.t) / 60:,.0f} min; perigee about {(t_pe - t_dep) / 86400:.1f} days later.")
        sc, r, v = self.craft()
        self.report(sc, r, v, "luna", "in lunar orbit")

    def tei(self):
        self.phase("tei")
        self.coast_to_time(self.t_dep - self.tau / 2, "luna", "coasting to the injection point")
        desired = self.conic_desired(self.mu("luna"), *self.tei_plan)
        self.log("Trans-Earth injection burn.")
        sc, r, v, dv = self.burn_to(desired, "Trans-Earth injection", "luna")
        self.log(f"Injection complete: {dv:,.0f} m/s.")
        self.t_burn_end = self.t

    def correct(self, n):
        eph, st = self.snapshot(10)
        r, v = self.craft_abs(st)
        t = st["time"]
        f = self.residual(eph, t, r, v, t + 9 * 86400)
        before = f(np.zeros(3))
        x, res, ok = nav.solve(f, np.zeros(3), step=0.01, max_move=50.0)
        t_pe, _, _ = nav.closest_approach(eph, t, r, v + x, "earth", t + 9 * 86400, near=150e6)
        self.t_arrive = t_pe
        if not ok:
            self.log(f"Course correction {n}: no exact solution found; best effort.", "warn")
        if np.linalg.norm(x) < MCC_MIN:
            self.log(f"Course correction {n} not needed (perigee off by {before[0]:+,.0f} km).")
            return
        self.impulse(x, f"Course correction {n}", "earth")
        self.log(f"Course correction {n}: {np.linalg.norm(x):,.2f} m/s (perigee was off by {before[0]:+,.0f} km, "
                 f"inclination by {before[1] / 20:+.2f}°).")

    def cruise(self):
        self.phase("cruise")
        for i, before in enumerate((None, 20 * 3600, 5 * 3600)):
            t_mcc = self.t_burn_end + 6 * 3600 if before is None else self.t_arrive - before
            if t_mcc <= self.t + 60:
                continue
            self.coast_to_time(t_mcc, "earth", "coasting to Earth")
            self.correct(i + 1)

    def capture(self):
        self.phase("capture")
        muE = self.mu("earth")
        sc, r, v = self.craft(rel="earth")
        v_arr = math.sqrt(max(0.0, float(v @ v) - 2 * muE / float(np.linalg.norm(r)) + 2 * muE / self.r_pe_t))
        dv = v_arr - _vis_viva(muE, self.r_pe_t, self.r_ae_t)
        tau = self.burn_time(max(dv, 0.0), sc["mass"])

        def remaining(sc, r, v):
            if np.linalg.norm(r) > 60e6:
                return self.t_arrive - tau / 2 - self.t
            return nav.time_to_periapsis(r, v, muE) - tau / 2

        sc, r, v = self.coast(remaining, "earth", "approaching Earth")
        normal = unit(np.cross(r, v))
        self.log(f"Earth orbit insertion burn (about {dv:,.0f} m/s, {tau:,.0f} s).")
        sc, r, v, dv = self.horizontal_burn("earth", self.r_ae_t, normal, "Earth orbit insertion")
        el = nav.elements(r, v, muE, self.pole("earth"))
        RE = self.radius("earth")
        if el["e"] >= 1:
            raise MissionFailed("Earth orbit insertion failed: the vehicle is still on an escape trajectory.")
        self.log(f"In Earth orbit: {(el['r_p'] - RE) / 1e3:,.0f} x {(el['r_a'] - RE) / 1e3:,.0f} km at "
                 f"{el['inc']:.2f}° ({dv:,.0f} m/s).")

    def finish(self):
        p = self.p
        self.step(10.0, powered=False)
        sc, r, v = self.craft(rel="earth")
        self.report(sc, r, v, "earth", "in Earth orbit")
        ok, orbit = self.orbit_result("earth", sc, r, v, (p["target_perigee_km"] * 1e3, p["target_apogee_km"] * 1e3,
                                                          self.inc_t), EARTH_TOL)
        prop = max(0.0, sc["mass"] - sc["dry_mass"])
        delivered = {"payload_kg": sc["dry_mass"], "propellant_kg": prop,
                     "perigee_km": orbit["periapsis_km"], "apogee_km": orbit["apoapsis_km"],
                     "inclination_deg": orbit["inclination_deg"]}
        report = [f"In Earth orbit {orbit['periapsis_km']:,.1f} x {orbit['apoapsis_km']:,.1f} km at "
                  f"{orbit['inclination_deg']:.2f} deg (target {p['target_perigee_km']:,.0f} x "
                  f"{p['target_apogee_km']:,.0f} km at {self.inc_t:.2f} deg)",
                  f"Payload {sc['dry_mass']:,.0f} kg, propellant remaining {prop:,.0f} kg",
                  f"Flight time {(self.t - self.t_dep) / 86400:.2f} days from injection"]
        report += [f"{b[0]}: {b[1]:,.1f} m/s, {b[2]:,.0f} kg" for b in self.burn_log]
        msg = ("Vehicle in the target Earth orbit." if ok
               else "Vehicle in Earth orbit, but outside the target tolerance.")
        self.log(msg, "info" if ok else "warn")
        return {"success": ok, "message": msg, "craft": sc["id"], "orbit": orbit, "delivered": delivered,
                "burns": self._burns(), "report": report}


MISSIONS = {"transfer": TransferMission, "landing": LandingMission, "return": ReturnMission}


def make_mission(client, params, status):
    if params["part"] == "launch":
        return LaunchMission(client, params, status)
    return MISSIONS[params["part"]](client, params, status)
