"""Autopilot for the "rocket to Earth orbit" mission.

Launches a vehicle from launch_vehicles.py (Falcon 9, Atlas V 551, Vulcan,
Delta IV Heavy, New Glenn, SLS, with any edited masses, thrusts and flows)
from LC-39A into a medium Earth orbit with a requested perigee, apogee and
inclination, by sending ordinary commands (add_spacecraft, set_thrust, stage,
step, get_spacecraft, ...) to a running nbody_sim over a SimClient.

Profile
-------
1. Configure: optional sim reset; clear the pad and place the configured
   vehicle on it (its own ID, masses, drag area).
2. Liftoff and vertical rise to 60 m/s.
3. Pitch kick and gravity turn along the launch azimuth that gives the target
   inclination (corrected for Earth's rotation).  Vehicles without boosters
   burn the first stage to depletion here.
4. Boosters only: booster separation at burnout; the first stage continues
   under the closed-loop guidance of step 6.
5. First-stage cutoff and separation.
6. Closed-loop ascent to a 200 km parking altitude in the target orbital
   plane, across any remaining stages; fairing jettison above 110 km.
7. Without shutting down, the burn continues into a transfer orbit whose
   apogee is the target apogee (perigee ~200 km).  A first stage that is
   still attached is then jettisoned.
8. Coast to apogee.
9. Apogee burn: raises perigee to the target and trims the inclination.
10. Payload separation and evaluation against the targets.

Liquid stages throttle to stay under 4 g; solid boosters cannot throttle.
Guidance after the gravity turn uses Earth-centred inertial vectors
(position/velocity relative to Earth, non-rotating) and Earth's spin axis.
"""

import datetime
import inspect
import math
import threading
import time

import numpy as np

import launch_vehicles as lv

G0 = lv.G0

FAIRING_ID = "fairing"
PAYLOAD_ID = "payload"

LAUNCH_LATITUDE = 28.60822   # LC-39A, as in solar_system.yaml
LAUNCH_LONGITUDE = -80.60409
PARKING_ALT = 200e3          # m
FAIRING_ALT = 110e3          # m
ORBIT_MIN_ALT = PARKING_ALT  # m, nothing below the parking orbit
ORBIT_MAX_ALT = 40_000e3     # m, room for GTO and Molniya apogees
MAX_PAYLOAD = 150_000.0      # kg
G_LIMIT = 4.0 * G0           # liquid stages throttle to stay under 4 g
TICK = 0.5                   # s, guidance update interval in powered flight
PACE_CHUNK = 0.25            # s of wall time per sim step when a time warp is set
KICK_SPEED = 60.0            # m/s, start of pitch-over
KICK_DURATION = 10.0         # s
CLOSED_LOOP_ALT = 45e3      # m, end of the open-loop gravity turn ...
MIN_TURN_ANGLE = 15.0        # deg, ... or earlier if the flight path gets this shallow
SOLVE_EVERY = 4.0            # s between steering solutions (1 s in the last minute)
PREDICT_DT = 2.0             # s, step of the guidance predictor
STAGING_COAST = 3.0          # s between first-stage separation and upper-stage ignition
ASCENT_LOSSES = 1_700.0      # m/s, gravity/drag/steering losses to the parking orbit (for estimates)
EARTH_OMEGA = 7.2921159e-5   # rad/s

# Pacing presets: (powered-flight warp, coasting warp); None = as fast as possible.
PACES = {
    "watch": (5.0, 200.0),
    "x5": (5.0, 5.0),          # constant x5 throughout, coasts included
    "fast": (50.0, 2_000.0),
    "max": (None, None),
}

# Success tolerances.
ALT_TOLERANCE = (5e3, 0.01)   # max(5 km, 1 %)
INC_TOLERANCE = 0.5           # deg

EARTH_MU = 398600.435507e9
EARTH_RADIUS = 6.378137e6


def alt_tolerance(target_alt):
    """Allowed altitude error (m) for a target altitude (m)."""
    return max(ALT_TOLERANCE[0], ALT_TOLERANCE[1] * target_alt)


def phases_for(cfg):
    """Checklist phases for a vehicle configuration: [(key, title)]."""
    out = [("prep", f"Configure the {cfg['name']} and payload"),
           ("liftoff", "Liftoff and vertical rise"),
           ("gravity_turn", "Pitch-over and gravity turn")]
    if lv.boosters(cfg):
        out.append(("booster_sep", "Booster separation; first stage on closed-loop guidance"))
    out += [("meco", "First-stage cutoff and separation"),
            ("s2_ascent", "Upper-stage ascent" + (" and fairing jettison" if cfg["fairing"] > 0 else "")),
            ("transfer", "Orbit insertion and transfer burn"),
            ("coast", "Coast to apogee"),
            ("apogee_burn", "Apogee burn (raise perigee, trim inclination)"),
            ("separation", "Payload separation")]
    return out


class MissionAborted(Exception):
    pass


class MissionFailed(Exception):
    pass


# ---------------------------------------------------------------------------
# Questionnaire validation and performance estimate
# ---------------------------------------------------------------------------

def _num(raw, key, label, errors):
    try:
        x = float(raw.get(key))
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number.")
        return None
    if not math.isfinite(x):
        errors.append(f"{label} must be a finite number.")
        return None
    return x


def stage_delta_v(cfg, payload):
    """Ideal (vacuum) delta-v of each burn phase: [(label, m/s)]."""
    out = []
    core = cfg["core"]
    m = lv.liftoff_mass(cfg, payload)
    core_left = core["prop"]
    b = lv.boosters(cfg)
    if b:
        t_b = b["prop"] / b["mdot"]
        thr = core.get("boost_throttle", 1.0)
        t_full = min(t_b, lv.BOOST_THROTTLE_AFTER)
        core_used = core["mdot"] * (t_full + thr * (t_b - t_full))
        burned = b["count"] * b["prop"] + core_used
        impulse = b["count"] * b["thrust_vac"] * t_b + core["thrust_vac"] * core_used / core["mdot"]
        out.append(("Boosters + first stage", impulse / burned * math.log(m / (m - burned))))
        m -= burned + b["count"] * b["dry"]
        core_left -= core_used
    c = core["thrust_vac"] / core["mdot"]
    out.append(("First stage" + (" alone" if b else ""), c * math.log(m / (m - core_left))))
    u = cfg["upper"]
    m = lv.upper_stack(cfg, payload, fairing=False)
    out.append(("Upper stage", u["thrust"] / u["mdot"] * math.log(m / (m - u["prop"]))))
    return out


def _dv_needed(perigee_alt, apogee_alt, inc_deg):
    """(ascent into the transfer orbit, apogee burn), m/s."""
    r_park = EARTH_RADIUS + PARKING_ALT
    r_p = EARTH_RADIUS + perigee_alt
    r_a = EARTH_RADIUS + apogee_alt
    mu = EARTH_MU
    v_tp = math.sqrt(mu * (2 / r_park - 2 / (r_park + r_a)))
    v_ta = math.sqrt(mu * (2 / r_a - 2 / (r_park + r_a)))
    v_final = math.sqrt(mu * 2 * r_p / (r_a * (r_a + r_p)))
    lat = math.radians(LAUNCH_LATITUDE)
    sin_az = max(-1.0, min(1.0, math.cos(math.radians(inc_deg)) / math.cos(lat)))
    rotation = EARTH_OMEGA * EARTH_RADIUS * math.cos(lat) * sin_az
    return v_tp + ASCENT_LOSSES - rotation, v_final - v_ta


def estimate(cfg, perigee_alt, apogee_alt, inc_deg, payload):
    """Rough whole-vehicle delta-v budget for the profile above."""
    ascent, apogee = _dv_needed(perigee_alt, apogee_alt, inc_deg)
    need = ascent + apogee

    def available(p):
        return sum(dv for _, dv in stage_delta_v(cfg, p))

    have = available(payload)
    lo, hi = 0.0, MAX_PAYLOAD
    if available(lo) < need:
        max_payload = 0.0
    else:
        while hi - lo > 10.0:
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if available(mid) >= need else (lo, mid)
        max_payload = lo
    mass = lv.liftoff_mass(cfg, payload)
    return {
        "dv_ascent": ascent,
        "dv_orbit": apogee,
        "dv_needed": need,
        "dv_available": have,
        "stages": [{"label": k, "dv": dv} for k, dv in stage_delta_v(cfg, payload)],
        "margin": have - need,
        "max_payload": max_payload,
        "liftoff_mass": mass,
        "liftoff_tw": lv.liftoff_thrust(cfg) / (mass * G0),
    }


def validate(raw):
    """Return (params, errors, estimate) for questionnaire input."""
    raw = raw if isinstance(raw, dict) else {}
    errors = []
    key = raw.get("vehicle", lv.DEFAULT_VEHICLE)
    cfg, vehicle_errors = lv.configure(key, raw.get("vehicle_config"))
    perigee = _num(raw, "perigee_km", "Perigee altitude", errors)
    apogee = _num(raw, "apogee_km", "Apogee altitude", errors)
    inc = _num(raw, "inclination_deg", "Inclination", errors)
    payload = _num(raw, "payload_kg", "Payload mass", errors)
    pace = raw.get("pace", "watch")
    if pace not in PACES:
        errors.append("Unknown pacing option.")
    lo, hi = ORBIT_MIN_ALT / 1e3, ORBIT_MAX_ALT / 1e3
    for value, label in ((perigee, "Perigee"), (apogee, "Apogee")):
        if value is not None and not lo <= value <= hi:
            errors.append(f"{label} altitude must be between {lo:,.0f} and {hi:,.0f} km.")
    if perigee is not None and apogee is not None and perigee > apogee:
        errors.append("Perigee altitude cannot be higher than apogee altitude.")
    min_inc = math.ceil(LAUNCH_LATITUDE * 10) / 10
    if inc is not None and not min_inc <= inc <= 180 - min_inc:
        errors.append(f"From Cape Canaveral (latitude {LAUNCH_LATITUDE:.1f}°) the inclination must be "
                      f"between {min_inc}° and {180 - min_inc:.1f}° without a plane change.")
    if payload is not None and not 0 <= payload <= MAX_PAYLOAD:
        errors.append(f"Payload mass must be between 0 and {MAX_PAYLOAD:,.0f} kg.")
        payload = None
    errors += vehicle_errors
    if cfg is not None and payload is not None and not vehicle_errors:
        tw = lv.liftoff_thrust(cfg) / (lv.liftoff_mass(cfg, payload) * G0)
        if tw < lv.MIN_LIFTOFF_TW:
            errors.append(f"Liftoff thrust-to-weight is only {tw:.2f}; it must be at least "
                          f"{lv.MIN_LIFTOFF_TW:.2f}. Add thrust or remove mass.")
    params = {
        "perigee_km": perigee, "apogee_km": apogee, "inclination_deg": inc,
        "payload_kg": payload, "pace": pace, "reset": bool(raw.get("reset", True)),
        "vehicle": key, "vehicle_config": cfg,
    }
    est = None
    if not errors:
        est = estimate(cfg, perigee * 1e3, apogee * 1e3, inc, payload)
    return params, errors, est


# ---------------------------------------------------------------------------
# Orbital mechanics helpers (Earth-centred inertial vectors)
# ---------------------------------------------------------------------------

def unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def elements(r, v, mu, pole):
    """Osculating elements: semi-major axis, eccentricity, periapsis/apoapsis radius, inclination."""
    rn = np.linalg.norm(r)
    h = np.cross(r, v)
    hn = np.linalg.norm(h)
    energy = 0.5 * float(v @ v) - mu / rn
    e_vec = np.cross(v, h) / mu - r / rn
    e = float(np.linalg.norm(e_vec))
    a = -mu / (2 * energy) if energy < 0 else math.inf
    r_p = hn * hn / mu / (1 + e)
    r_a = a * (1 + e) if energy < 0 and e < 1 else math.inf
    inc = math.degrees(math.acos(max(-1.0, min(1.0, float(h @ pole) / hn)))) if hn > 0 else 0.0
    return {"a": a, "e": e, "e_vec": e_vec, "r_p": r_p, "r_a": r_a, "inc": inc, "h": h, "energy": energy}


def time_to_apoapsis(r, v, mu):
    """Seconds until the next apoapsis of an elliptical orbit."""
    el = elements(r, v, mu, np.array([0.0, 0.0, 1.0]))
    a, e = el["a"], el["e"]
    if not math.isfinite(a) or e < 1e-6:
        return 0.0
    rn = np.linalg.norm(r)
    cos_e_anom = (1 - rn / a) / e
    E = math.acos(max(-1.0, min(1.0, cos_e_anom)))
    if float(r @ v) < 0:  # heading back down: past apoapsis
        E = 2 * math.pi - E
    M = E - e * math.sin(E)
    n = math.sqrt(mu / a**3)
    return ((math.pi - M) % (2 * math.pi)) / n


def target_plane_normal(r, v, pole, inc_deg):
    """Unit normal of the orbital plane through r with the given inclination, closest to the current plane."""
    up = unit(r)
    east = unit(np.cross(pole, up))
    north = np.cross(up, east)
    cos_lat = float(north @ pole)
    c = max(-1.0, min(1.0, math.cos(math.radians(inc_deg)) / max(cos_lat, 1e-9)))
    alpha = math.acos(c)
    h_now = unit(np.cross(r, v))
    candidates = [math.cos(a) * north + math.sin(a) * east for a in (alpha, -alpha)]
    return max(candidates, key=lambda n: float(n @ h_now))


# ---------------------------------------------------------------------------
# Command log: every control command sent to the simulation, in plain words
# ---------------------------------------------------------------------------

CONTROL_COMMANDS = ("reset", "add_spacecraft", "remove_spacecraft", "set_mass", "set_thrust", "stage")


class RecordingClient:
    """Wraps a SimClient; control commands are passed on, then reported to record(cmd, args)."""

    def __init__(self, client, record):
        self._client = client
        self._record = record

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in CONTROL_COMMANDS:
            return attr

        def call(*args, **kwargs):
            result = attr(*args, **kwargs)
            bound = inspect.signature(attr).bind(*args, **kwargs)
            bound.apply_defaults()
            self._record(name, dict(bound.arguments))
            return result

        return call


def _parts(model):
    if not isinstance(model, dict) or not model.get("parts"):
        return ""
    parts = list(model["parts"])
    if "boosters" in parts and model.get("boosters"):
        parts[parts.index("boosters")] = f"{model['boosters']} boosters"
    return " [" + ", ".join(parts) + "]"


def describe_command(cmd, a, note=None):
    """One human-readable line for a control command and its arguments."""
    if cmd == "reset":
        return "reset: the simulation is reloaded from its scenario file."
    if cmd == "remove_spacecraft":
        return f"remove_spacecraft '{a['sc_id']}': the craft is taken out of the simulation."
    if cmd == "set_mass":
        dry = "" if a.get("dry_mass") is None else f", {a['dry_mass']:,.0f} kg without propellant"
        return f"set_mass '{a['sc_id']}': {a['mass']:,.0f} kg{dry}."
    if cmd == "add_spacecraft":
        spec = a["spec"]
        init = spec.get("initial_state", {})
        where = ""
        if init.get("frame") == "surface":
            where = (f" on the surface of {init.get('body')} at latitude {init.get('latitude'):.5f}°, "
                     f"longitude {init.get('longitude'):.5f}°")
        dry = spec.get("dry_mass", 0.0)
        return (f"add_spacecraft '{spec['id']}'{_parts(spec.get('model'))}{where}: {spec['mass']:,.0f} kg "
                f"({spec['mass'] - dry:,.0f} kg of it propellant), drag reference area "
                f"{spec.get('reference_area', 0.0):.1f} m².")
    if cmd == "set_thrust":
        f = np.array(a["force"], dtype=float)
        mag = float(np.linalg.norm(f))
        flow = -float(a.get("mass_rate") or 0.0)
        if mag == 0.0 and flow == 0.0:
            return f"set_thrust '{a['sc_id']}': engines off."
        if a.get("frame") == "enu":
            e, n, u = f
            pitch = math.degrees(math.atan2(u, math.hypot(e, n)))
            heading = math.degrees(math.atan2(e, n)) % 360.0
            direction = (f"pitch {pitch:.1f}° above the horizon, heading {heading:.1f}° "
                         f"(local east-north-up frame of {a.get('body')})")
        else:
            d = f / mag if mag else f
            direction = f"inertial direction ({d[0]:+.4f}, {d[1]:+.4f}, {d[2]:+.4f})"
        text = f"set_thrust '{a['sc_id']}': {mag / 1e3:,.1f} kN, {direction}; propellant flow {flow:,.1f} kg/s"
        return text + (f"; {note}." if note else ".")
    if cmd == "stage":
        kids = []
        for c in a["children"]:
            dv = float(np.linalg.norm(c.get("delta_v", [0.0, 0.0, 0.0])))
            push = f", pushed {dv:.1f} m/s" if dv else ""
            kids.append(f"'{c['id']}'{_parts(c.get('model'))} {c['mass']:,.0f} kg{push}")
        return f"stage '{a['sc_id']}' into " + "; ".join(kids) + "."
    return f"{cmd} {a}"


def _mission_clock(t, t0):
    if t0 is None:
        return f"t={t:,.1f} s"
    d = t - t0
    sign = "-" if d < -1e-9 else "+"
    s = abs(d)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"T{sign}{int(h):02d}:{int(m):02d}:{sec:04.1f}"


# ---------------------------------------------------------------------------
# Shared mission status (read by the web server, written by the autopilot)
# ---------------------------------------------------------------------------

class MissionStatus:
    MAX_LOG = 300

    def __init__(self):
        self.lock = threading.Lock()
        self.abort_requested = False
        self.state = "idle"
        self.params = None
        self.estimate = None
        self.phases = []
        self.telemetry = {}
        self.log = []
        self.result = None
        self.message = None
        self.vehicle = None
        self.commands = []        # (sim time, text)
        self.started = None       # wall-clock start (UTC datetime)

    def begin(self, params, est):
        cfg = params["vehicle_config"]
        with self.lock:
            self.abort_requested = False
            self.state = "running"
            self.params = params
            self.estimate = est
            self.phases = [{"key": k, "title": t, "state": "pending"} for k, t in phases_for(cfg)]
            self.telemetry = {}
            self.log = []
            self.result = None
            self.message = None
            self.vehicle = cfg["id"]
            self.commands = []
            self.started = datetime.datetime.now(datetime.timezone.utc)

    def phase(self, key):
        with self.lock:
            seen = False
            for p in self.phases:
                if p["key"] == key:
                    p["state"] = "active"
                    seen = True
                elif not seen and p["state"] in ("pending", "active"):
                    p["state"] = "done"

    def add_log(self, t, text, kind="info"):
        with self.lock:
            self.log.append({"t": t, "text": text, "kind": kind, "wall": time.time()})
            del self.log[:-self.MAX_LOG]

    def add_command(self, t, text):
        with self.lock:
            self.commands.append((t, text))

    def set_telemetry(self, tel, vehicle=None):
        with self.lock:
            self.telemetry = tel
            if vehicle:
                self.vehicle = vehicle

    def finish(self, state, message, result=None):
        with self.lock:
            self.state = state
            self.message = message
            self.result = result
            for p in self.phases:
                if p["state"] == "active":
                    p["state"] = "done" if state == "succeeded" else "failed"
                elif p["state"] == "pending" and state == "succeeded":
                    p["state"] = "done"

    def request_abort(self):
        with self.lock:
            if self.state == "running":
                self.abort_requested = True
                return True
            return False

    def command_report(self):
        """Plain-text report of a finished mission: setup, outcome, flight log, every command."""
        with self.lock:
            p, est, result = self.params, self.estimate, self.result
            log, commands = list(self.log), list(self.commands)
            state, message, started = self.state, self.message, self.started
        cfg = p["vehicle_config"]
        t0 = next((e["t"] for e in log if e["text"].startswith("Ignition and liftoff")), None)
        out = ["MISSION COMMAND LOG", "=" * 19, ""]
        out.append(f"Mission started: {started:%Y-%m-%d %H:%M:%S} UTC (wall clock)" if started else "")
        out.append(f"Outcome:         {state} - {message}")
        out.append(f"Rocket:          {cfg['name']} (craft ID '{cfg['id']}')")
        out.append(f"Target orbit:    {p['perigee_km']:,.0f} x {p['apogee_km']:,.0f} km at "
                   f"{p['inclination_deg']:.2f} deg inclination")
        out.append(f"Payload:         {p['payload_kg']:,.0f} kg")
        out.append(f"Pace:            {p['pace']}" + ("   (simulation reset before launch)" if p["reset"] else ""))
        if result:
            ach = result.get("apogee_km")
            out.append(f"Achieved orbit:  {result['perigee_km']:,.1f} x "
                       + (f"{ach:,.1f} km" if ach is not None else "escape")
                       + f" at {result['inclination_deg']:.3f} deg (craft '{result['craft']}')")
            if result.get("propellant_left_kg") is not None:
                out.append(f"Upper-stage propellant left: {result['propellant_left_kg']:,.0f} kg")
        if est:
            out.append(f"Pre-launch estimate: liftoff mass {est['liftoff_mass']:,.0f} kg, "
                       f"thrust-to-weight {est['liftoff_tw']:.2f}, delta-v margin {est['margin']:+,.0f} m/s")

        out += ["", "Vehicle configuration", "-" * 21]
        names = {"boosters": "Boosters (each)", "core": "First stage", "upper": "Upper stage"}
        for block in ("boosters", "core", "upper"):
            st = cfg.get(block)
            if not st or (block == "boosters" and not st.get("count")):
                continue
            thrust = (f"thrust {st['thrust_sl'] / 1e3:,.0f} kN sea level / {st['thrust_vac'] / 1e3:,.0f} kN vacuum"
                      if "thrust_sl" in st else f"thrust {st['thrust'] / 1e3:,.1f} kN")
            count = f" x{st['count']}" if block == "boosters" else ""
            out.append(f"{names[block]}{count}: dry {st['dry']:,.0f} kg, propellant {st['prop']:,.0f} kg, "
                       f"{thrust}, propellant flow {st['mdot']:,.1f} kg/s")
        out.append(f"Fairing {cfg['fairing']:,.0f} kg; pitch kick {cfg['kick_angle']:g} deg")

        out += ["", "Times: T is mission time from ignition (T+hh:mm:ss.s); t is simulation time in seconds.",
                "Inertial directions are unit vectors in the simulation frame (J2000 ecliptic: x toward the",
                "vernal equinox, z toward ecliptic north); pitch and heading are relative to the local horizon.",
                "", "Flight log", "-" * 10]
        for e in log:
            out.append(f"{_mission_clock(e['t'], t0):>14}  {e['text']}")

        out += ["", f"Commands sent to the simulation ({len(commands)})", "-" * 40,
                "Control commands only; the autopilot also steps the simulation and reads craft states.",
                "Thrust is re-commanded every guidance update (every 0.5 s of powered flight).", ""]
        for t, text in commands:
            out.append(f"{_mission_clock(t, t0):>14}  t={t:>12,.1f}  {text}")
        return "\n".join(out) + "\n"

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "params": self.params,
                "estimate": self.estimate,
                "phases": [dict(p) for p in self.phases],
                "telemetry": dict(self.telemetry),
                "log": list(self.log),
                "result": self.result,
                "message": self.message,
                "vehicle": self.vehicle,
                "abort_requested": self.abort_requested,
            }


# ---------------------------------------------------------------------------
# The autopilot
# ---------------------------------------------------------------------------

class SimFlight:
    """Simulator plumbing shared by the autopilots: pacing, events, thrust, logging.

    Subclasses set self.vehicle (the craft being flown) and self.phase_keys.
    """

    def __init__(self, client, status, warp):
        self.sim = RecordingClient(client, self._record_command)
        self.thrust_note = None
        self.status = status
        self.warp = warp
        self.vehicle = None
        self.last_thrust = None
        self.t = 0.0
        self._pace_anchor = None
        self.pending_events = []
        self.phase_keys = []
        self.phase_index = -1

    def log(self, text, kind="info"):
        self.status.add_log(self.t, text, kind)

    def phase(self, key):
        """Advance the checklist (never backwards: stages can finish out of the usual order)."""
        i = self.phase_keys.index(key)
        if i > self.phase_index:
            self.phase_index = i
            self.status.phase(key)

    def check_abort(self):
        if self.status.abort_requested:
            raise MissionAborted()

    def _record_command(self, cmd, args):
        note = self.thrust_note if cmd == "set_thrust" else None
        self.status.add_command(self.t, describe_command(cmd, args, note))

    def thrust(self, force, mass_rate, frame="inertial", body=None, note=None):
        self.last_thrust = ([float(x) for x in force], mass_rate, frame, body)
        self.thrust_note = note
        self.sim.set_thrust(self.vehicle, *self.last_thrust)

    def cut(self, vehicle=None):
        self.last_thrust = None
        self.thrust_note = None
        self.sim.set_thrust(vehicle or self.vehicle, [0.0, 0.0, 0.0], 0.0)

    def step(self, dt, powered=True):
        """Advance the sim by dt, honouring the pacing preset and collecting events.

        With a time warp, a long step is split into chunks of at most
        PACE_CHUNK seconds of wall time, and the runner sleeps until wall time
        catches up with warp-scaled sim time, so long coasts play at the chosen
        warp too.  If the sim falls behind, the pacing clock is re-anchored
        rather than racing ahead later to catch up.
        """
        warp = self.warp[0] if powered else self.warp[1]
        chunk = dt if not warp else max(warp * PACE_CHUNK, 1e-3)
        remaining = dt
        reply = None
        while remaining > 1e-9:
            self.check_abort()
            d = min(chunk, remaining)
            reply = self.sim.step(d)
            remaining -= d
            self.t = reply["time"]
            self.pending_events.extend(reply.get("events", []))
            if warp:
                self._pace(warp, d)
        return reply

    def _pace(self, warp, d):
        now = time.monotonic()
        if self._pace_anchor is None or self._pace_anchor[2] != warp:
            self._pace_anchor = (now, self.t - d, warp)
        wall0, sim0, _ = self._pace_anchor
        ahead = (self.t - sim0) / warp - (now - wall0)
        if ahead < -1.0:  # the sim can't keep up: don't bank the deficit
            self._pace_anchor = (now, self.t, warp)
            return
        while ahead > 0:  # sleep in short slices so an abort is noticed quickly
            self.check_abort()
            time.sleep(min(ahead, PACE_CHUNK))
            ahead = (self.t - sim0) / warp - (time.monotonic() - wall0)

    def take_events(self, kinds=None, craft=None):
        out = [e for e in self.pending_events
               if (kinds is None or e["event"] in kinds) and (craft is None or e["spacecraft"] == craft)]
        self.pending_events = [e for e in self.pending_events if e not in out]
        return out

    def _safe_cut(self):
        try:
            self.cut()
        except Exception:
            pass


class MeoMission(SimFlight):
    def __init__(self, client, params, status):
        super().__init__(client, status, PACES[params["pace"]])
        self.p = params
        self.cfg = params["vehicle_config"]
        self.vid = self.cfg["id"]
        self.s1_id = f"{self.vid}_s1"
        self.boosters_id = f"{self.vid}_boosters"
        self.b = lv.boosters(self.cfg)
        self.vehicle = self.vid
        self.stage = "boost" if self.b else "core"   # which engines are attached: boost, core, upper
        self.fairing_on = self.cfg["fairing"] > 0
        self.liftoff_t = None
        self.boost_end = None
        self.core_depleted = False
        self.payload = params["payload_kg"]
        self.r_p_target = EARTH_RADIUS + params["perigee_km"] * 1e3
        self.r_a_target = EARTH_RADIUS + params["apogee_km"] * 1e3
        self.inc_target = params["inclination_deg"]
        self.phase_keys = [k for k, _ in phases_for(self.cfg)]
        # What the vehicle craft is made of, for the viewer's model tag.
        self.parts = ((["boosters"] if self.b else []) + ["core", "upper"]
                      + (["fairing"] if self.fairing_on else []) + ["payload"])

    # -- sim plumbing ---------------------------------------------------------

    def craft(self, sc_id=None):
        sc = self.sim.get_spacecraft(sc_id or self.vehicle, relative_to="earth")
        return sc, np.array(sc["position"]), np.array(sc["velocity"])

    def report(self, sc, r, v, phase_note=""):
        el = elements(r, v, self.mu, self.pole)
        nb = sc["nearest_body"]
        tel = {
            "t": self.t,
            "vehicle": sc["id"],
            "note": phase_note,
            "altitude_km": (np.linalg.norm(r) - self.radius) / 1e3,
            "speed": float(np.linalg.norm(v)),
            "surface_speed": nb["airspeed"],
            "vertical_speed": float(v @ unit(r)),
            "perigee_km": (el["r_p"] - self.radius) / 1e3,
            "apogee_km": (el["r_a"] - self.radius) / 1e3 if math.isfinite(el["r_a"]) else None,
            "inclination": el["inc"],
            "mass": sc["mass"],
            "propellant": max(0.0, sc["mass"] - sc["dry_mass"]),
            "propellant_full": self._full_load() if sc["id"] == self.vid else None,
            "thrust_kn": float(np.linalg.norm(sc["thrust"]["force"])) / 1e3,
            "accel_g": float(np.linalg.norm(sc["acceleration"]["total"])) / G0,
            "dynamic_pressure": nb["dynamic_pressure"],
        }
        self.status.set_telemetry(tel, sc["id"])
        return el

    # -- vehicle model ------------------------------------------------------------

    def _engine(self, sc):
        """Thrust (N) and mass rate (kg/s, negative) of the engines now attached.

        Thrust is interpolated between sea level and vacuum by air density.
        Liquid stages throttle to keep the thrust acceleration under G_LIMIT;
        solid boosters run at full flow until burnout.
        """
        m = sc["mass"]
        if self.stage == "upper":
            u = self.cfg["upper"]
            thr = min(1.0, G_LIMIT * m / u["thrust"])
            return u["thrust"] * thr, -u["mdot"] * thr
        p_ratio = min(1.0, sc["nearest_body"]["density"] / 1.225)

        def lerp(st):
            return st["thrust_vac"] - (st["thrust_vac"] - st["thrust_sl"]) * p_ratio

        core = self.cfg["core"]
        t_core = lerp(core)
        t_b = mdot_b = 0.0
        thr = 1.0
        if self.stage == "boost":
            t_b = self.b["count"] * lerp(self.b)
            mdot_b = self.b["count"] * self.b["mdot"]
            if self.t - self.liftoff_t > lv.BOOST_THROTTLE_AFTER:
                thr = core.get("boost_throttle", 1.0)
        thr = max(0.0, min(thr, (G_LIMIT * m - t_b) / t_core))
        return t_b + thr * t_core, -(mdot_b + thr * core["mdot"])

    def _full_load(self):
        """Propellant (kg) of the engines now attached when fully loaded."""
        if self.stage == "upper":
            return self.cfg["upper"]["prop"]
        full = self.cfg["core"]["prop"]
        if self.stage == "boost":
            full += self.b["count"] * self.b["prop"]
        return full

    def _model(self, parts):
        """Model tag for a craft made of these parts (see viz/models.js)."""
        tag = {"vehicle": self.cfg["key"], "parts": list(parts)}
        if "boosters" in parts:
            tag["boosters"] = self.b["count"]
        return tag

    def _drop_parts(self, *gone):
        """Remove parts from the vehicle; returns the model tag for what remains."""
        self.parts = [p for p in self.parts if p not in gone]
        return self._model(self.parts)

    def _reapply_thrust(self):
        if self.last_thrust:
            self.thrust_note = "re-applied to the craft that continues after the separation"
            self.sim.set_thrust(self.vehicle, *self.last_thrust)

    def _maybe_jettison_fairing(self, sc, r, v):
        if not self.fairing_on or np.linalg.norm(r) - self.radius <= FAIRING_ALT:
            return
        f = self.cfg["fairing"]
        side = unit(np.cross(unit(v), unit(r)))
        self.sim.stage(self.vehicle, [
            {"id": FAIRING_ID, "mass": f, "dry_mass": f, "delta_v": list(2.0 * side),
             "model": self._model(["fairing"])},
            {"id": self.vehicle, "mass": sc["mass"] - f, "dry_mass": max(0.0, sc["dry_mass"] - f),
             "delta_v": [0.0, 0.0, 0.0], "model": self._drop_parts("fairing")},
        ], frame="inertial")
        self.fairing_on = False
        self._reapply_thrust()  # staging resets thrust on the new craft
        self.log(f"Fairing jettisoned at {(np.linalg.norm(r) - self.radius) / 1e3:.0f} km.")

    def _check_events(self):
        """Handle engine cutoff / impact.  Returns True if the vehicle was restaged."""
        if any(e["time"] - self.liftoff_t > 5 for e in self.take_events({"contact_start"}, self.vehicle)):
            raise MissionFailed("The vehicle impacted the surface.")
        if not self.take_events({"engine_cutoff"}, self.vehicle):
            return False
        if self.stage == "upper":
            raise MissionFailed("Upper-stage propellant depleted before reaching the target orbit "
                                "(payload too heavy for this orbit?).")
        if self.stage == "boost":
            raise MissionFailed("The first stage ran out of propellant before the boosters burned out.")
        self._stage_core()
        return True

    # -- mission ----------------------------------------------------------------

    def run(self):
        try:
            self._prepare()
            self._first_stage()
            if self.core_depleted:
                self._stage_core()
            self._closed_loop_ascent()
            if self._transfer_burn():
                self._coast_to_apogee()
                self._apogee_burn()
            result = self._separate_and_evaluate()
            ok = result["success"]
            self.status.finish("succeeded" if ok else "failed",
                               "Payload delivered to the target orbit." if ok
                               else "Payload separated, but the orbit is outside tolerance.", result)
        except MissionAborted:
            self._safe_cut()
            self.log("Mission aborted by the operator; engines shut down.", "error")
            self.status.finish("aborted", "Mission aborted.", self._partial_result())
        except MissionFailed as e:
            self._safe_cut()
            self.log(str(e), "error")
            self.status.finish("failed", str(e), self._partial_result())
        except Exception as e:  # sim unreachable, unexpected state, ...
            self._safe_cut()
            self.log(f"Autopilot error: {type(e).__name__}: {e}", "error")
            self.status.finish("failed", f"Autopilot error: {e}", self._partial_result())

    def _partial_result(self):
        try:
            sc, r, v = self.craft()
            el = elements(r, v, self.mu, self.pole)
            return self._orbit_summary(sc["id"], el)
        except Exception:
            return None

    def _orbit_summary(self, craft_id, el):
        return {
            "craft": craft_id,
            "perigee_km": (el["r_p"] - self.radius) / 1e3,
            "apogee_km": (el["r_a"] - self.radius) / 1e3 if math.isfinite(el["r_a"]) else None,
            "inclination_deg": el["inc"],
            "target": {"perigee_km": self.p["perigee_km"], "apogee_km": self.p["apogee_km"],
                       "inclination_deg": self.inc_target},
        }

    # 1. Configure
    def _prepare(self):
        self.phase("prep")
        if self.p["reset"]:
            self.sim.reset()
            self.log("Simulation reset to its initial state.")
        info = self.sim.info()
        self.t = info["time"]
        earth = self.sim.get_body("earth")
        self.mu = info["gravitational_constant"] * earth["mass"]
        self.radius = earth["radius"]
        self.pole = np.array(earth["pole"])
        self.omega = 2 * math.pi / earth["rotation_period"] if earth["rotation_period"] else 0.0

        # Clear the pad, and make sure this flight's craft IDs are free.
        planned = {self.vid, self.s1_id, PAYLOAD_ID}
        if self.b:
            planned.add(self.boosters_id)
        if self.fairing_on:
            planned.add(FAIRING_ID)
        for s in self.sim.get_state(bodies=False)["spacecraft"]:
            nb = s["nearest_body"]
            if (nb["id"] == "earth" and nb["altitude"] < 100.0 and abs(nb["latitude"] - LAUNCH_LATITUDE) < 0.01
                    and abs((nb["longitude"] - LAUNCH_LONGITUDE + 180.0) % 360.0 - 180.0) < 0.01):
                self.sim.remove_spacecraft(s["id"])
                self.log(f"Cleared '{s['id']}' from the pad.")
            elif s["id"] in planned:
                raise MissionFailed(f"A craft named '{s['id']}' is already in the simulation. "
                                    "Enable 'reset before launch' and retry.")

        cfg = self.cfg
        mass = lv.liftoff_mass(cfg, self.payload)
        prop = cfg["core"]["prop"] + (self.b["count"] * self.b["prop"] if self.b else 0.0)
        self.sim.add_spacecraft({
            "id": self.vid, "mass": mass, "dry_mass": mass - prop,
            "reference_area": lv.stack_area(cfg), "drag_coefficient": lv.CD_TABLE,
            "model": self._model(self.parts),
            "initial_state": {"frame": "surface", "body": "earth", "latitude": LAUNCH_LATITUDE,
                              "longitude": LAUNCH_LONGITUDE, "altitude": 0.0},
        })
        boosters = f", {self.b['count']} boosters" if self.b else ""
        self.log(f"{cfg['name']} on the pad{boosters}: liftoff mass {mass:,.0f} kg including a "
                 f"{self.payload:,.0f} kg payload.")
        sc, r, v = self.craft()
        nb = sc["nearest_body"]

        # Launch azimuth for the target inclination, corrected for Earth's rotation.
        lat = math.radians(nb["latitude"])
        beta_i = math.asin(max(-1.0, min(1.0, math.cos(math.radians(self.inc_target)) / math.cos(lat))))
        v_orb = 7_800.0
        v_rot = self.omega * self.radius * math.cos(lat)
        self.azimuth = math.atan2(v_orb * math.sin(beta_i) - v_rot, v_orb * math.cos(beta_i))
        self.log(f"Launch azimuth {math.degrees(self.azimuth):.1f}° for a {self.inc_target:.1f}° inclination.")
        self.report(sc, r, v, "on the pad")

    # 2-4. Liftoff, gravity turn, booster separation
    def _s1_thrust(self, sc, pitch):
        thrust, mdot = self._engine(sc)
        cp = math.cos(pitch)
        h = self.azimuth
        self.thrust([thrust * cp * math.sin(h), thrust * cp * math.cos(h), thrust * math.sin(pitch)],
                    mdot, "enu", "earth")

    def _first_stage(self):
        """Open-loop gravity turn through the dense atmosphere (to CLOSED_LOOP_ALT,
        or until the flight path gets shallower than MIN_TURN_ANGLE).

        Boosters that burn out on the way are separated; a first stage that
        runs dry first (no boosters) sets core_depleted.
        """
        self.phase("liftoff")
        sc, r, v = self.craft()
        self.liftoff_t = self.t
        self.boost_end = self.t + self.b["prop"] / self.b["mdot"] if self.b else None
        self._s1_thrust(sc, math.pi / 2)
        self.log("Ignition and liftoff.")
        kick = self.cfg["kick_angle"]
        kick_start = None
        while True:
            self.step(self._tick())
            sc, r, v = self.craft()
            nb = sc["nearest_body"]
            if self._check_boosters(sc, v):
                sc, r, v = self.craft()
                nb = sc["nearest_body"]
            if self.take_events({"engine_cutoff"}, self.vehicle):
                if self.stage == "boost":
                    raise MissionFailed("The first stage ran out of propellant before the boosters burned out.")
                self.core_depleted = True
                return
            if nb["altitude"] >= CLOSED_LOOP_ALT:
                return
            enu = nb["surface_relative_velocity_enu"]
            if kick_start is None:
                pitch = math.pi / 2
                if nb["airspeed"] >= KICK_SPEED:
                    kick_start = self.t
                    self.phase("gravity_turn")
                    self.log(f"Pitch-over at {nb['airspeed']:.0f} m/s, altitude {nb['altitude']:.0f} m.")
            elif self.t - kick_start < KICK_DURATION:
                pitch = math.radians(90 - kick * (self.t - kick_start) / KICK_DURATION)
            else:
                # Gravity turn: thrust along the surface-relative velocity.
                pitch = math.atan2(enu[2], math.hypot(enu[0], enu[1]))
                if pitch < math.radians(MIN_TURN_ANGLE):
                    # Too flat for this thrust-to-weight: let closed-loop guidance pull up now.
                    self.log(f"Flight path down to {math.degrees(pitch):.0f}° at "
                             f"{nb['altitude'] / 1e3:.1f} km; switching to closed-loop guidance early.")
                    return
            self._s1_thrust(sc, pitch)
            self._maybe_jettison_fairing(sc, r, v)
            self.report(sc, r, v, "boosters and first stage" if self.stage == "boost" else "first stage")
            if nb["altitude"] < -1.0 and self.t - self.liftoff_t > 5:
                raise MissionFailed("The vehicle hit the ground during first-stage flight.")

    def _tick(self):
        """Guidance interval, shortened so booster burnout lands on a step boundary."""
        if self.stage == "boost":
            return max(1e-3, min(TICK, self.boost_end - self.t))
        return TICK

    def _check_boosters(self, sc, v):
        if self.stage == "boost" and self.t >= self.boost_end - 1e-6:
            self._separate_boosters(sc, v)
            return True
        return False

    def _separate_boosters(self, sc, v):
        self.phase("booster_sep")
        b = self.b
        n = b["count"]
        left = n * max(0.0, b["prop"] - b["mdot"] * (self.t - self.liftoff_t))
        mass = n * b["dry"] + left
        vhat = unit(v)
        self.sim.stage(self.vehicle, [
            {"id": self.boosters_id, "mass": mass, "dry_mass": mass,
             "reference_area": n * lv.area(b["diameter"]), "delta_v": list(-1.0 * vhat),
             "model": self._model(["boosters"])},
            {"id": self.vid, "mass": sc["mass"] - mass, "dry_mass": sc["dry_mass"] - n * b["dry"],
             "reference_area": lv.area(self.cfg["diameter"]), "delta_v": [0.0, 0.0, 0.0],
             "model": self._drop_parts("boosters")},
        ], frame="inertial")
        self.stage = "core"
        self._reapply_thrust()
        alt = (sc["nearest_body"]["altitude"]) / 1e3
        self.log(f"Booster burnout and separation at {alt:.1f} km, {np.linalg.norm(v):,.0f} m/s inertial.")

    # 5. First-stage separation
    def _stage_core(self):
        self.phase("meco")
        sc, r, v = self.craft()
        nb = sc["nearest_body"]
        upper = lv.upper_stack(self.cfg, self.payload, self.fairing_on)
        s1_mass = max(self.cfg["core"]["dry"], sc["mass"] - upper)
        left = s1_mass - self.cfg["core"]["dry"]
        self.cut()
        self.log(f"First-stage cutoff at {nb['altitude'] / 1e3:.1f} km, {np.linalg.norm(v):,.0f} m/s inertial"
                 + (f" ({left:,.0f} kg propellant left)." if left > 1.0 else "."))
        vhat = unit(v)
        u = self.cfg["upper"]
        self.sim.stage(self.vehicle, [
            {"id": self.s1_id, "mass": s1_mass, "dry_mass": min(self.cfg["core"]["dry"], s1_mass),
             "delta_v": list(-1.0 * vhat), "model": self._model(["core"])},
            {"id": self.vid, "mass": upper,
             "dry_mass": u["dry"] + self.payload + (self.cfg["fairing"] if self.fairing_on else 0.0),
             "delta_v": list(0.5 * vhat), "model": self._drop_parts("core", "boosters")},
        ], frame="inertial")
        self.stage = "upper"
        self.log("Stage separation: first stage released.")
        for _ in range(int(STAGING_COAST)):
            self.step(1.0)
        sc, r, v = self.craft()
        self.report(sc, r, v, "coasting after separation")
        self.phase("s2_ascent")
        self.log("Upper-stage ignition.")

    # 6. Closed-loop ascent to the parking orbit
    def _burn_schedule(self, sc):
        """Remaining powered flight as segments for the predictor (vacuum figures).

        Each segment: thrust (N), mdot (kg/s), and either a duration (s) or a
        propellant load (kg); 'drop' is the mass released at its end and
        'coast' an unpowered interval after it.
        """
        core, u = self.cfg["core"], self.cfg["upper"]
        prop = max(0.0, sc["mass"] - sc["dry_mass"])
        segs = []
        if self.stage == "boost":
            b, n = self.b, self.b["count"]
            thr = core.get("boost_throttle", 1.0) if self.t - self.liftoff_t > lv.BOOST_THROTTLE_AFTER else 1.0
            duration = max(0.0, self.boost_end - self.t)
            mdot = n * b["mdot"] + thr * core["mdot"]
            segs.append({"thrust": n * b["thrust_vac"] + thr * core["thrust_vac"], "mdot": mdot,
                         "duration": duration, "limit": False, "drop": n * b["dry"], "coast": 0.0})
            prop -= mdot * duration
        if self.stage in ("boost", "core"):
            segs.append({"thrust": core["thrust_vac"], "mdot": core["mdot"], "prop": max(0.0, prop),
                         "limit": True, "drop": core["dry"], "coast": STAGING_COAST})
            prop = u["prop"]
        segs.append({"thrust": u["thrust"], "mdot": u["mdot"], "prop": prop, "limit": True,
                     "drop": 0.0, "coast": 0.0})
        return segs

    def _predict(self, x0, A, B, segs, vt_target):
        """Fly the remaining burn in 2D with tan(pitch) = A + B*tau.

        x0 = (r, v_r, v_t, mass).  Returns (r, v_r, tau) when v_t reaches
        vt_target, and whether it got there before the propellant ran out.
        """
        mu = self.mu
        r, vr, vt, m = x0
        fairing = self.cfg["fairing"] if self.fairing_on else 0.0
        r_fairing = self.radius + FAIRING_ALT
        tau = 0.0

        def accel(r, vr, vt, a, tau):
            phi = math.atan(A + B * tau)
            return vt * vt / r - mu / (r * r) + a * math.sin(phi), -vr * vt / r + a * math.cos(phi)

        for seg in segs:
            t_left = seg.get("duration")
            p_left = seg.get("prop")
            while True:
                thrust, mdot = seg["thrust"], seg["mdot"]
                if seg["limit"]:
                    thr = min(1.0, G_LIMIT * m / thrust)
                    thrust, mdot = thrust * thr, mdot * thr
                h = PREDICT_DT
                if t_left is not None:
                    h = min(h, t_left)
                if p_left is not None:
                    h = min(h, p_left / mdot)
                if h <= 1e-6:
                    break
                # Midpoint (RK2) step.
                ar, at = accel(r, vr, vt, thrust / m, tau)
                hm = 0.5 * h
                rm, vrm, vtm = r + hm * vr, vr + hm * ar, vt + hm * at
                ar, at = accel(rm, vrm, vtm, thrust / (m - mdot * hm), tau + hm)
                r1, vr1, vt1 = r + h * vrm, vr + h * ar, vt + h * at
                if vt1 >= vt_target:
                    f = (vt_target - vt) / (vt1 - vt) if vt1 > vt else 1.0
                    return r + f * (r1 - r), vr + f * (vr1 - vr), tau + f * h, True
                r, vr, vt, m, tau = r1, vr1, vt1, m - mdot * h, tau + h
                if t_left is not None:
                    t_left -= h
                if p_left is not None:
                    p_left -= mdot * h
                if fairing and r > r_fairing:
                    m -= fairing
                    fairing = 0.0
            m -= seg["drop"]
            if seg["coast"]:
                ar, at = accel(r, vr, vt, 0.0, tau)
                h = seg["coast"]
                r, vr, vt, tau = r + h * vr, vr + h * ar, vt + h * at, tau + h
        return r, vr, tau, False

    def _solve_steering(self, x0, segs, vt_target, guess):
        """Newton iterations on (A, B) so the burn ends at the parking radius with v_r = 0."""
        r_park = self.radius + PARKING_ALT

        def residual(A, B):
            r, vr, tau, ok = self._predict(x0, A, B, segs, vt_target)
            return np.array([(r - r_park) / 1e3, vr / 10.0]), tau, ok

        A, B = guess
        f, tau, ok = residual(A, B)
        for _ in range(8):
            if abs(f[0]) < 0.5 and abs(f[1]) < 0.5:
                break
            dA, dB = 1e-4, 1e-6
            fA, _, _ = residual(A + dA, B)
            fB, _, _ = residual(A, B + dB)
            J = np.column_stack(((fA - f) / dA, (fB - f) / dB))
            try:
                d = np.linalg.solve(J, -f)
            except np.linalg.LinAlgError:
                break
            scale = min(1.0, 0.5 / max(abs(d[0]), 1e-12), 0.005 / max(abs(d[1]), 1e-12))
            A, B = A + scale * d[0], B + scale * d[1]
            f, tau, ok = residual(A, B)
        return A, B, tau, ok

    def _closed_loop_ascent(self):
        """Predictive closed-loop ascent straight into the transfer orbit's perigee (200 km).

        Every few seconds the remaining burn (booster burnout, first stage, staging
        coast, upper stage, fairing jettison, 4 g throttling) is flown in a 2D
        predictor, and the linear-tangent pitch program tan(pitch) = A + B*t is
        solved so the vehicle reaches the 200 km parking radius with zero vertical
        speed just as its horizontal speed reaches the transfer orbit's perigee
        speed.  Out of plane, the velocity normal to the target plane is nulled.
        """
        sc, r, v = self.craft()
        self.plane = target_plane_normal(r, v, self.pole, self.inc_target)
        r_park = self.radius + PARKING_ALT
        v_target = math.sqrt(self.mu * (2 / r_park - 2 / (r_park + self.r_a_target)))
        steering = None  # (A, B, t_solved, t_go, ok)
        warned = False
        orbit_logged = False
        while True:
            sc, r, v = self.craft()
            if self._check_events() or self._check_boosters(sc, v):
                steering = (steering[0], steering[1], steering[2], 0.0, steering[4]) if steering else None
                continue
            rn = np.linalg.norm(r)
            up = r / rn
            n = self.plane
            t_hat = unit(np.cross(n, up))
            v_r, v_t, v_n = float(v @ up), float(v @ t_hat), float(v @ n)
            thrust, mdot = self._engine(sc)
            a_T = thrust / sc["mass"]
            el = self.report(sc, r, v, "upper-stage ascent" if self.stage == "upper" else "first-stage ascent")
            if el["r_p"] > self.radius + 150e3 and not orbit_logged:
                orbit_logged = True
                self.phase("transfer")
                self.log(f"Orbit achieved (perigee {(el['r_p'] - self.radius) / 1e3:.0f} km); "
                         "continuing the burn into the transfer orbit.")

            t_go = steering[3] - (self.t - steering[2]) if steering else None
            if steering is None or self.t - steering[2] >= (SOLVE_EVERY if t_go > 60 else 1.0) or t_go <= 0:
                guess = ((steering[0] + steering[1] * (self.t - steering[2]), steering[1]) if steering
                         else (v_r / max(v_t, 1.0), 0.0))
                A, B, tau, ok = self._solve_steering((rn, v_r, v_t, sc["mass"]), self._burn_schedule(sc),
                                                     v_target, guess)
                steering = (A, B, self.t, tau, ok)
                t_go = tau
                if not ok and not warned:
                    warned = True
                    self.log("Guidance predicts the propellant will run out before the transfer orbit "
                             "is reached; flying the best available trajectory.", "error")
            if t_go < 8.0:
                return  # hand over to the precise terminal burn

            A, B, t0 = steering[0], steering[1], steering[2]
            phi = math.atan(A + B * (self.t - t0))
            a_n = max(-0.3 * a_T, min(0.3 * a_T, -v_n / 20.0))
            a_in = math.sqrt(max(a_T**2 - a_n**2, 0.0))
            direction = (a_in * (math.sin(phi) * up + math.cos(phi) * t_hat) + a_n * n) / a_T
            self.thrust(direction * thrust, mdot,
                        note=f"closed-loop guidance: pitch {math.degrees(phi):.1f}° above the local "
                             f"horizontal in the target plane, about {max(t_go, 0.0):,.0f} s of burn to go")
            self._maybe_jettison_fairing(sc, r, v)
            self.step(self._tick())

    def _burn_to(self, desired_velocity, done, note):
        """Thrust along velocity-to-be-gained until done(el, v_go) is true."""
        while True:
            sc, r, v = self.craft()
            if self._check_events():
                continue
            el = self.report(sc, r, v, note)
            v_go = desired_velocity(r, v) - v
            v_go_n = float(np.linalg.norm(v_go))
            if done(el, v_go_n):
                self.cut()
                return el
            thrust, mdot = self._engine(sc)
            a_T = thrust / sc["mass"]
            self.thrust(v_go / v_go_n * thrust, mdot,
                        note=f"{note}: along the velocity still to be gained ({v_go_n:,.1f} m/s)")
            self._maybe_jettison_fairing(sc, r, v)
            # Shorten the last steps so the cutoff lands close to the target.
            self.step(max(0.02, min(TICK, v_go_n / a_T)))

    def _horizontal_in_plane(self, r):
        return unit(np.cross(self.plane, unit(r)))

    # 7. Transfer burn: perigee here, apogee at the target apogee
    def _transfer_burn(self):
        self.phase("transfer")
        r_a_t = self.r_a_target
        self.log("Terminal guidance for the end of the transfer burn.")

        def desired(r, v):
            rn = np.linalg.norm(r)
            speed = math.sqrt(self.mu * 2 * r_a_t / (rn * (rn + r_a_t)))
            return speed * self._horizontal_in_plane(r)

        # Stop early only once the orbit is safe as well: for a target apogee at the parking
        # altitude, the apogee is reached long before the perigee is out of the atmosphere.
        r_p_min = self.radius + PARKING_ALT - 2e3
        el = self._burn_to(desired, lambda el, vg: vg < 0.5 or (el["r_a"] >= r_a_t and el["r_p"] >= r_p_min),
                           "transfer burn")
        self.log(f"Transfer orbit {(el['r_p'] - self.radius) / 1e3:,.0f} × "
                 f"{(el['r_a'] - self.radius) / 1e3:,.0f} km, inclination {el['inc']:.2f}°.")
        if self.stage != "upper":
            # The first stage did the whole transfer burn: drop it before the coast.
            self._stage_core()
        # A target perigee at the parking altitude: the transfer orbit is already the target.
        if (abs(el["r_p"] - self.r_p_target) <= 0.25 * alt_tolerance(self.r_p_target - self.radius)
                and abs(el["inc"] - self.inc_target) <= 0.1 * INC_TOLERANCE):
            self.log("Already in the target orbit; no apogee burn needed.")
            return False
        return True

    # 8. Coast
    def _coast_to_apogee(self):
        self.phase("coast")
        sc, r, v = self.craft()
        r_a = elements(r, v, self.mu, self.pole)["r_a"]
        v_a = math.sqrt(self.mu * 2 * self.r_p_target / (r_a * (r_a + self.r_p_target)))
        v_now_a = np.linalg.norm(np.cross(r, v)) / r_a
        thrust, _ = self._engine(sc)
        lead = 0.5 * abs(v_a - v_now_a) / (thrust / sc["mass"])  # half the burn before apogee
        self.log(f"Coasting to apogee (burn starts {lead:.0f} s early).")
        while True:
            sc, r, v = self.craft()
            self.report(sc, r, v, "coasting")
            remaining = time_to_apoapsis(r, v, self.mu) - lead
            if remaining <= 0.5:
                return
            # With a time warp, keep each step to about a second of wall time so the
            # page's telemetry keeps updating during long coasts.
            max_step = 600.0 if not self.warp[1] else max(1.0, self.warp[1] * 1.0)
            self.step(min(remaining, max_step), powered=False)

    # 9. Apogee burn
    def _apogee_burn(self):
        self.phase("apogee_burn")
        r_p_t = self.r_p_target

        def desired(r, v):
            rn = np.linalg.norm(r)
            speed = math.sqrt(self.mu * 2 * r_p_t / (rn * (rn + r_p_t)))
            return speed * self._horizontal_in_plane(r)

        self.log("Apogee burn.")
        el = self._burn_to(desired, lambda el, vg: vg < 0.5, "apogee burn")
        self.log(f"Burn complete: orbit {(el['r_p'] - self.radius) / 1e3:,.0f} × "
                 f"{(el['r_a'] - self.radius) / 1e3:,.0f} km, inclination {el['inc']:.2f}°.")

    # 10. Separation and evaluation
    def _separate_and_evaluate(self):
        self.phase("separation")
        sc, r, v = self.craft()
        vhat = unit(v)
        u_dry = self.cfg["upper"]["dry"]
        self.sim.stage(self.vehicle, [
            {"id": PAYLOAD_ID, "mass": self.payload or 1.0, "dry_mass": self.payload or 1.0,
             "delta_v": [0.0, 0.0, 0.0], "drag_coefficient": None, "model": self._model(["payload"])},
            # The spent stage backs away so the payload keeps the orbit it was given.
            {"id": self.vid, "mass": max(sc["mass"] - self.payload, u_dry),
             "dry_mass": u_dry, "delta_v": list(-0.5 * vhat), "model": self._drop_parts("payload")},
        ], frame="inertial")
        self.vehicle = PAYLOAD_ID
        self.log("Payload separated.")
        self.step(10.0, powered=False)
        sc, r, v = self.craft()
        el = self.report(sc, r, v, "payload in orbit")
        result = self._orbit_summary(PAYLOAD_ID, el)
        tol_alt = lambda target_km: alt_tolerance(target_km * 1e3) / 1e3
        errs = {
            "perigee_km": result["perigee_km"] - self.p["perigee_km"],
            "apogee_km": (result["apogee_km"] or math.inf) - self.p["apogee_km"],
            "inclination_deg": result["inclination_deg"] - self.inc_target,
        }
        result["errors"] = errs
        result["success"] = (abs(errs["perigee_km"]) <= tol_alt(self.p["perigee_km"])
                             and abs(errs["apogee_km"]) <= tol_alt(self.p["apogee_km"])
                             and abs(errs["inclination_deg"]) <= INC_TOLERANCE)
        upper = self.sim.get_spacecraft(self.vid)
        result["propellant_left_kg"] = max(0.0, upper["mass"] - upper["dry_mass"])
        self.log(f"Final orbit {result['perigee_km']:,.0f} × {result['apogee_km']:,.0f} km at "
                 f"{result['inclination_deg']:.2f}° (target {self.p['perigee_km']:,.0f} × "
                 f"{self.p['apogee_km']:,.0f} km at {self.inc_target:.2f}°).",
                 "success" if result["success"] else "error")
        return result
