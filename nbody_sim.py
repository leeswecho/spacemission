#!/usr/bin/env python3
"""N-body gravitational simulation with spacecraft, atmospheric drag and surface contact.

The simulation is passive: on startup it loads a YAML scenario, initialises all
objects, and then waits for JSON commands over a ZeroMQ REP socket.  Time only
advances when a client sends a ``step`` command.  See README.md for the scenario
schema and the full command protocol.

Physics summary
---------------
* Celestial bodies: mutual Newtonian gravity, RK4 with fixed substeps.  Bodies
  are rigid spheres rotating at a constant rate about a fixed pole.
* Spacecraft: massless test particles for the n-body problem (they feel gravity
  but do not exert it).  Integrated with RK4 using adaptive substeps chosen from
  the local environment (orbital time scale, atmosphere, thrust, surface
  proximity).  Body positions inside a step are cubic-Hermite interpolated from
  the n-body trajectory.
* Drag: F = -1/2 rho Cd A |v_rel| v_rel, where v_rel is relative to the
  co-rotating atmosphere.  Only computed when both the body has an atmosphere
  table and the spacecraft has a drag-coefficient table.
* Contact: damped spring along the local vertical, k = m w^2, c = 2 zeta m w,
  never pulling the craft down, plus viscous tangential friction relative to the
  moving surface, capped at mu * normal force.
* Thrust: constant force (inertial or local East-North-Up frame) and constant
  mass rate; persists until changed.  Cut off automatically at dry mass.
* Trails: each craft records a thinned trajectory history during integration,
  relative to the body whose sphere of influence it is in (non-rotating axes).
  Nothing is recorded while a craft rests on a surface.

Run:  python nbody_sim.py solar_system.yaml [--endpoint tcp://127.0.0.1:5555]
"""

import argparse
import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import yaml

DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"

DEFAULT_SETTINGS = {
    "gravitational_constant": 6.67430e-11,
    "body_max_substep": 600.0,
    "spacecraft": {
        "max_substep": 10.0,
        "min_substep": 1.0e-4,
        "orbit_step_fraction": 0.005,
        "atmosphere_max_substep": 1.0,
        "thrust_max_substep": 0.5,
        "contact_steps_per_period": 25,
    },
    "trails": {
        "max_points": 5000,         # per craft; older history is thinned beyond this
        "min_spacing": 20.0,        # m
        "altitude_fraction": 0.1,   # spacing <= this * altitude (finer near surfaces)...
        "radius_fraction": 0.02,    # ...and <= this * distance from the reference body
    },
}

DEFAULT_CONTACT = {
    "natural_frequency": 2.0,
    "damping_ratio": 1.0,
    "tangential_damping": 5.0,
    "friction_coefficient": 1.0,
}

# Spacecraft within this distance above a surface use contact-sized substeps.
CONTACT_ZONE = 1.0
# Engines cut off at max(dry_mass, MIN_MASS) so mass never reaches zero.
MIN_MASS = 1.0e-6
# A craft in contact moving slower than this relative to the surface is "resting".
REST_SPEED = 1.0

# Trail versions are unique for the life of the process (also across resets), so
# clients holding an old copy always see a version change.
_TRAIL_VERSIONS = itertools.count(1)


class SimError(Exception):
    """An error caused by bad input (scenario file or command)."""


# ----------------------------------------------------------------------------
# Input helpers
# ----------------------------------------------------------------------------

def _vec3(value, what):
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        raise SimError(f"{what} must be a list of 3 numbers")
    if arr.shape != (3,) or not np.all(np.isfinite(arr)):
        raise SimError(f"{what} must be a list of 3 finite numbers")
    return arr


def _number(value, what, positive=False, nonnegative=False):
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise SimError(f"{what} must be a number")
    if not math.isfinite(x):
        raise SimError(f"{what} must be finite")
    if positive and x <= 0:
        raise SimError(f"{what} must be > 0")
    if nonnegative and x < 0:
        raise SimError(f"{what} must be >= 0")
    return x


def _require(mapping, key, what):
    if not isinstance(mapping, dict) or key not in mapping:
        raise SimError(f"{what}: missing required field '{key}'")
    return mapping[key]


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _list(v):
    return [float(x) for x in v]


class LookupTable:
    """Piecewise-linear table, clamped at both ends.

    With ``log_y`` the interpolation is linear in log(y), which suits
    exponentially varying quantities such as atmospheric density.
    """

    def __init__(self, rows, what, log_y=False):
        try:
            arr = np.asarray(rows, dtype=float)
        except (TypeError, ValueError):
            raise SimError(f"{what} must be a list of [x, y] pairs")
        if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] < 1:
            raise SimError(f"{what} must be a non-empty list of [x, y] pairs")
        if not np.all(np.isfinite(arr)):
            raise SimError(f"{what} contains non-finite values")
        if np.any(np.diff(arr[:, 0]) <= 0):
            raise SimError(f"{what}: x values must be strictly increasing")
        if log_y and np.any(arr[:, 1] <= 0):
            raise SimError(f"{what}: y values must be > 0")
        self.x = arr[:, 0]
        self.y = np.log(arr[:, 1]) if log_y else arr[:, 1]
        self.log_y = log_y
        self.rows = arr.tolist()

    @property
    def x_max(self):
        return float(self.x[-1])

    def __call__(self, x):
        y = float(np.interp(x, self.x, self.y))
        return math.exp(y) if self.log_y else y


# ----------------------------------------------------------------------------
# Model objects
# ----------------------------------------------------------------------------

class Body:
    """A celestial body: constant-rate rotating sphere with optional atmosphere."""

    def __init__(self, spec, index, G):
        what = f"body '{spec.get('id', index)}'" if isinstance(spec, dict) else f"body #{index}"
        self.id = str(_require(spec, "id", what))
        self.index = index
        self.mass = _number(_require(spec, "mass", what), f"{what} mass", positive=True)
        self.gm = G * self.mass
        self.radius = _number(_require(spec, "radius", what), f"{what} radius", nonnegative=True)
        self.initial_position = _vec3(_require(spec, "position", what), f"{what} position")
        self.initial_velocity = _vec3(_require(spec, "velocity", what), f"{what} velocity")

        rot = spec.get("rotation") or {}
        pole = _vec3(rot.get("pole", [0.0, 0.0, 1.0]), f"{what} rotation.pole")
        if np.linalg.norm(pole) == 0:
            raise SimError(f"{what} rotation.pole must be non-zero")
        self.pole = _unit(pole)
        period = rot.get("period")
        if period is None or float(period) == 0.0:
            self.spin_rate = 0.0
        else:
            self.spin_rate = 2.0 * math.pi / _number(period, f"{what} rotation.period")
        self.prime_meridian0 = math.radians(_number(rot.get("prime_meridian", 0.0), f"{what} rotation.prime_meridian"))
        # Reference direction in the equatorial plane from which the prime
        # meridian angle is measured: pole x Z (the vernal equinox for Earth).
        ref = np.cross(self.pole, [0.0, 0.0, 1.0])
        if np.linalg.norm(ref) < 1e-12:
            ref = np.cross(self.pole, [0.0, 1.0, 0.0])
        self.ref_x = _unit(ref)
        self.ref_y = np.cross(self.pole, self.ref_x)
        self.omega_vec = self.spin_rate * self.pole

        contact = dict(DEFAULT_CONTACT)
        contact.update(spec.get("contact") or {})
        self.contact = _parse_contact(contact, what)

        self.atmosphere = None
        if spec.get("atmosphere") is not None:
            self.atmosphere = LookupTable(spec["atmosphere"], f"{what} atmosphere", log_y=True)

    def density(self, altitude):
        """Atmospheric density at a given altitude (0 above the table)."""
        if self.atmosphere is None or altitude > self.atmosphere.x_max:
            return 0.0
        return self.atmosphere(altitude)

    def prime_meridian(self, t):
        return self.prime_meridian0 + self.spin_rate * t

    def surface_direction(self, lat, lon, t):
        """Inertial unit vector from the centre to geographic lat/lon (radians)."""
        w = self.prime_meridian(t) + lon
        eq = math.cos(w) * self.ref_x + math.sin(w) * self.ref_y
        return math.cos(lat) * eq + math.sin(lat) * self.pole

    def lat_lon(self, rel_pos, t):
        """Geographic latitude/longitude (degrees, east positive) of a relative position."""
        u = _unit(rel_pos)
        lat = math.asin(max(-1.0, min(1.0, float(np.dot(u, self.pole)))))
        ang = math.atan2(float(np.dot(u, self.ref_y)), float(np.dot(u, self.ref_x)))
        lon = (ang - self.prime_meridian(t) + math.pi) % (2.0 * math.pi) - math.pi
        return math.degrees(lat), math.degrees(lon)

    def enu_basis(self, rel_pos):
        """East, North, Up unit vectors at a position relative to the body centre."""
        up = _unit(rel_pos)
        east = np.cross(self.pole, up)
        if np.linalg.norm(east) < 1e-12:  # over a pole: pick any horizontal axis
            east = np.cross(self.ref_y, up)
        east = _unit(east)
        north = np.cross(up, east)
        return east, north, up


def _parse_contact(c, what):
    return {
        "natural_frequency": _number(c["natural_frequency"], f"{what} contact.natural_frequency", positive=True),
        "damping_ratio": _number(c["damping_ratio"], f"{what} contact.damping_ratio", nonnegative=True),
        "tangential_damping": _number(c["tangential_damping"], f"{what} contact.tangential_damping", nonnegative=True),
        "friction_coefficient": _number(c["friction_coefficient"], f"{what} contact.friction_coefficient", nonnegative=True),
    }


class Spacecraft:
    def __init__(self, sc_id, position, velocity, mass, dry_mass=0.0, reference_area=0.0,
                 cd_table=None, contact=None):
        self.id = sc_id
        self.pos = np.array(position, dtype=float)
        self.vel = np.array(velocity, dtype=float)
        self.mass = mass
        self.dry_mass = dry_mass
        self.reference_area = reference_area
        self.cd_table = cd_table
        self.contact = contact  # optional per-craft override (dict of partial values)
        self.in_contact = set()
        self.trail = Trail()
        self.clear_thrust()

    def clear_thrust(self):
        self.thrust = np.zeros(3)
        self.thrust_frame = "inertial"
        self.thrust_body = None  # Body for the ENU frame
        self.mass_rate = 0.0

    @property
    def thrust_active(self):
        return bool(np.any(self.thrust != 0.0) or self.mass_rate != 0.0)

    def contact_params(self, body):
        if not self.contact:
            return body.contact
        p = dict(body.contact)
        p.update(self.contact)
        return p


class Trail:
    """Thinned trajectory history of one spacecraft.

    Points are tuples (seq, t, body_index, x, y, z, brk): position relative to
    the reference body's centre (inertial axes), with ``brk`` marking the start
    of a new segment (e.g. liftoff after resting).  ``seq`` increases
    monotonically so clients can fetch only new points; ``version`` changes
    whenever existing points are removed, telling clients to refetch.
    """

    def __init__(self, points=(), resting=False):
        self.points = list(points)
        self.version = next(_TRAIL_VERSIONS)
        self.next_seq = self.points[-1][0] + 1 if self.points else 0
        self.resting = resting

    def copy(self):
        return Trail(self.points, self.resting)

    def add(self, t, body_index, rel, brk, max_points):
        self.points.append((self.next_seq, float(t), int(body_index),
                            float(rel[0]), float(rel[1]), float(rel[2]), bool(brk)))
        self.next_seq += 1
        if len(self.points) > max_points:
            self._thin(max_points)

    def _thin(self, max_points):
        """Keep the newest half intact; drop every other older point except segment ends."""
        keep_recent = max(1, max_points // 2)
        old, recent = self.points[:-keep_recent], self.points[-keep_recent:]
        thinned = []
        for k, p in enumerate(old):
            prev = old[k - 1] if k > 0 else None
            nxt = old[k + 1] if k + 1 < len(old) else recent[0]
            boundary = p[6] or nxt[6] or nxt[2] != p[2] or (prev is not None and prev[2] != p[2])
            if k % 2 == 0 or boundary:
                thinned.append(p)
        self.points = (thinned + recent)[-max_points:]
        self.version = next(_TRAIL_VERSIONS)


def _parse_spacecraft_props(spec, what, inherit=None):
    """Mass, dry mass, area, Cd table and contact override for a spacecraft spec."""
    inherit = inherit or {}
    mass = _number(_require(spec, "mass", what), f"{what} mass", positive=True)
    dry_mass = _number(spec.get("dry_mass", inherit.get("dry_mass", 0.0)), f"{what} dry_mass", nonnegative=True)
    if dry_mass > mass:
        raise SimError(f"{what}: dry_mass ({dry_mass}) exceeds mass ({mass})")
    area = _number(spec.get("reference_area", inherit.get("reference_area", 0.0)),
                   f"{what} reference_area", nonnegative=True)
    if "drag_coefficient" in spec:
        cd = spec["drag_coefficient"]
        cd_table = None if cd is None else LookupTable(cd, f"{what} drag_coefficient")
    else:
        cd_table = inherit.get("cd_table")
    if cd_table is not None and area <= 0:
        raise SimError(f"{what}: reference_area must be > 0 when drag_coefficient is given")
    contact = inherit.get("contact")
    if spec.get("contact") is not None:
        c = dict(DEFAULT_CONTACT)
        c.update(spec["contact"])
        _parse_contact(c, what)  # validation only
        contact = {k: float(v) for k, v in spec["contact"].items() if k in DEFAULT_CONTACT}
    return dict(mass=mass, dry_mass=dry_mass, reference_area=area, cd_table=cd_table, contact=contact)


# ----------------------------------------------------------------------------
# Body trajectory over one step (for interpolation by the spacecraft integrator)
# ----------------------------------------------------------------------------

class BodyTrajectory:
    """Body states at substep boundaries with cubic-Hermite interpolation."""

    def __init__(self, t0, h, positions, velocities):
        self.t0 = t0
        self.h = h
        self.P = positions   # (n+1, N, 3)
        self.V = velocities  # (n+1, N, 3)
        self.n = positions.shape[0] - 1

    def at(self, t):
        if self.n == 0 or self.h == 0:
            return self.P[0], self.V[0]
        x = (t - self.t0) / self.h
        k = min(max(int(math.floor(x)), 0), self.n - 1)
        s = x - k
        if s <= 0.0:
            return self.P[k], self.V[k]
        if s >= 1.0:
            return self.P[k + 1], self.V[k + 1]
        h = self.h
        p0, p1, v0, v1 = self.P[k], self.P[k + 1], self.V[k], self.V[k + 1]
        s2, s3 = s * s, s * s * s
        pos = ((2 * s3 - 3 * s2 + 1) * p0 + (s3 - 2 * s2 + s) * h * v0
               + (-2 * s3 + 3 * s2) * p1 + (s3 - s2) * h * v1)
        vel = ((6 * s2 - 6 * s) * (p0 - p1) / h + (3 * s2 - 4 * s + 1) * v0 + (3 * s2 - 2 * s) * v1)
        return pos, vel


# ----------------------------------------------------------------------------
# Simulation
# ----------------------------------------------------------------------------

class Simulation:
    def __init__(self, scenario_path):
        self.scenario_path = Path(scenario_path)
        self.load()

    # -- setup ---------------------------------------------------------------

    def load(self):
        try:
            with open(self.scenario_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except OSError as e:
            raise SimError(f"cannot read scenario file: {e}")
        except yaml.YAMLError as e:
            raise SimError(f"invalid YAML: {e}")
        if not isinstance(cfg, dict):
            raise SimError("scenario file must be a mapping")

        sim_cfg = cfg.get("simulation") or {}
        sc_cfg = dict(DEFAULT_SETTINGS["spacecraft"])
        sc_cfg.update(sim_cfg.get("spacecraft") or {})
        self.G = _number(sim_cfg.get("gravitational_constant", DEFAULT_SETTINGS["gravitational_constant"]),
                         "gravitational_constant", positive=True)
        self.endpoint = sim_cfg.get("endpoint", DEFAULT_ENDPOINT)
        self.body_max_substep = _number(sim_cfg.get("body_max_substep", DEFAULT_SETTINGS["body_max_substep"]),
                                        "body_max_substep", positive=True)
        self.sc_settings = {k: _number(v, f"simulation.spacecraft.{k}", positive=True) for k, v in sc_cfg.items()
                            if k in DEFAULT_SETTINGS["spacecraft"]}
        trail_cfg = dict(DEFAULT_SETTINGS["trails"])
        trail_cfg.update(sim_cfg.get("trails") or {})
        self.trail_settings = {k: _number(v, f"simulation.trails.{k}", positive=True) for k, v in trail_cfg.items()
                               if k in DEFAULT_SETTINGS["trails"]}
        self.trail_settings["max_points"] = max(10, int(self.trail_settings["max_points"]))

        body_specs = cfg.get("bodies")
        if not isinstance(body_specs, list) or not body_specs:
            raise SimError("scenario must define a non-empty 'bodies' list")
        self.bodies = [Body(spec, i, self.G) for i, spec in enumerate(body_specs)]
        self.body_by_id = {}
        for b in self.bodies:
            if b.id in self.body_by_id:
                raise SimError(f"duplicate body id '{b.id}'")
            self.body_by_id[b.id] = b
        self.gm = np.array([b.gm for b in self.bodies])
        self.radii = np.array([b.radius for b in self.bodies])
        self.bpos = np.array([b.initial_position for b in self.bodies])
        self.bvel = np.array([b.initial_velocity for b in self.bodies])
        self.time = 0.0
        self.step_count = 0

        self.spacecraft = {}
        for i, spec in enumerate(cfg.get("spacecraft") or []):
            self._add_initial_spacecraft(spec, i)
        self._compute_spheres_of_influence()
        for sc in self.spacecraft.values():
            self._update_contact(sc, self.time, self.bpos, [])
            self._record_trail(sc, self.time, self.bpos, self.bvel, force=True)

    def _add_initial_spacecraft(self, spec, index):
        what = f"spacecraft '{spec.get('id', index)}'" if isinstance(spec, dict) else f"spacecraft #{index}"
        sc_id = str(_require(spec, "id", what))
        if sc_id in self.spacecraft or sc_id in self.body_by_id:
            raise SimError(f"duplicate id '{sc_id}'")
        props = _parse_spacecraft_props(spec, what)
        init = _require(spec, "initial_state", what)
        frame = init.get("frame", "inertial")
        if frame == "inertial":
            pos = _vec3(_require(init, "position", what), f"{what} position")
            vel = _vec3(_require(init, "velocity", what), f"{what} velocity")
        elif frame == "relative":
            body = self._body(_require(init, "body", what))
            pos = self.bpos[body.index] + _vec3(_require(init, "position", what), f"{what} position")
            vel = self.bvel[body.index] + _vec3(_require(init, "velocity", what), f"{what} velocity")
        elif frame == "surface":
            body = self._body(_require(init, "body", what))
            lat = math.radians(_number(_require(init, "latitude", what), f"{what} latitude"))
            lon = math.radians(_number(_require(init, "longitude", what), f"{what} longitude"))
            alt = _number(init.get("altitude", 0.0), f"{what} altitude")
            u = body.surface_direction(lat, lon, self.time)
            rel = (body.radius + alt) * u
            pos = self.bpos[body.index] + rel
            east, north, up = body.enu_basis(rel)
            v_enu = _vec3(init.get("velocity_enu", [0.0, 0.0, 0.0]), f"{what} velocity_enu")
            vel = (self.bvel[body.index] + np.cross(body.omega_vec, rel)
                   + v_enu[0] * east + v_enu[1] * north + v_enu[2] * up)
        else:
            raise SimError(f"{what}: initial_state.frame must be 'inertial', 'relative' or 'surface'")
        self.spacecraft[sc_id] = Spacecraft(sc_id, pos, vel, **props)

    def _body(self, body_id):
        try:
            return self.body_by_id[str(body_id)]
        except KeyError:
            raise SimError(f"unknown body '{body_id}'")

    def _craft(self, sc_id):
        try:
            return self.spacecraft[str(sc_id)]
        except KeyError:
            raise SimError(f"unknown spacecraft '{sc_id}'")

    # -- n-body integration --------------------------------------------------

    def _body_accel(self, pos):
        d = pos[None, :, :] - pos[:, None, :]            # d[i, j] = r_j - r_i
        r2 = np.einsum("ijk,ijk->ij", d, d)
        np.fill_diagonal(r2, 1.0)
        inv_r3 = r2 ** -1.5
        np.fill_diagonal(inv_r3, 0.0)
        return np.einsum("ij,ijk->ik", inv_r3 * self.gm[None, :], d)

    def _advance_bodies(self, dt):
        n = max(1, int(math.ceil(dt / self.body_max_substep - 1e-12)))
        h = dt / n
        P = np.empty((n + 1,) + self.bpos.shape)
        V = np.empty_like(P)
        p, v = self.bpos.copy(), self.bvel.copy()
        P[0], V[0] = p, v
        for k in range(n):
            a1 = self._body_accel(p)
            p2, v2 = p + 0.5 * h * v, v + 0.5 * h * a1
            a2 = self._body_accel(p2)
            p3, v3 = p + 0.5 * h * v2, v + 0.5 * h * a2
            a3 = self._body_accel(p3)
            p4, v4 = p + h * v3, v + h * a3
            a4 = self._body_accel(p4)
            p = p + h / 6.0 * (v + 2 * v2 + 2 * v3 + v4)
            v = v + h / 6.0 * (a1 + 2 * a2 + 2 * a3 + a4)
            P[k + 1], V[k + 1] = p, v
        return BodyTrajectory(self.time, h, P, V)

    # -- spacecraft dynamics -------------------------------------------------

    def _sc_forces(self, sc, t, r, v, m, P, V, diagnostics=False):
        """Acceleration of a spacecraft and optional per-term breakdown."""
        d = P - r
        dist = np.sqrt(np.einsum("ij,ij->i", d, d))
        a_grav = np.einsum("i,ij->j", self.gm / dist**3, d)
        f_drag = np.zeros(3)
        f_contact = np.zeros(3)
        f_thrust = np.zeros(3)

        alt = dist - self.radii
        has_cd = sc.cd_table is not None
        for i in np.flatnonzero(alt < 0.0) if not has_cd else range(len(self.bodies)):
            body = self.bodies[i]
            in_atm = has_cd and body.atmosphere is not None and alt[i] < body.atmosphere.x_max
            if alt[i] >= 0.0 and not in_atm:
                continue
            rel = -d[i]
            v_rel = v - (V[i] + np.cross(body.omega_vec, rel))
            if in_atm:
                speed = float(np.linalg.norm(v_rel))
                if speed > 0.0:
                    rho = body.density(alt[i])
                    f_drag -= 0.5 * rho * sc.cd_table(speed) * sc.reference_area * speed * v_rel
            if alt[i] < 0.0:
                c = sc.contact_params(body)
                w = 2.0 * math.pi * c["natural_frequency"]
                n_hat = rel / dist[i]
                vn = float(np.dot(v_rel, n_hat))
                fn = m * (w * w * (-alt[i]) - 2.0 * c["damping_ratio"] * w * vn)
                if fn > 0.0:
                    f_contact += fn * n_hat
                    vt = v_rel - vn * n_hat
                    ft = m * c["tangential_damping"] * vt
                    ft_mag = float(np.linalg.norm(ft))
                    limit = c["friction_coefficient"] * fn
                    if ft_mag > limit:
                        ft *= limit / ft_mag
                    f_contact -= ft

        if sc.thrust_active:
            if sc.thrust_frame == "enu":
                east, north, up = sc.thrust_body.enu_basis(r - P[sc.thrust_body.index])
                f_thrust = sc.thrust[0] * east + sc.thrust[1] * north + sc.thrust[2] * up
            else:
                f_thrust = sc.thrust

        accel = a_grav + (f_drag + f_contact + f_thrust) / m
        if not diagnostics:
            return accel, sc.mass_rate
        return accel, {
            "gravity": a_grav, "drag": f_drag / m, "contact": f_contact / m,
            "thrust": f_thrust / m, "total": accel,
        }

    def _choose_substep(self, sc, t, P, V, remaining):
        s = self.sc_settings
        r, v = sc.pos, sc.vel
        d = r - P
        dist = np.sqrt(np.einsum("ij,ij->i", d, d))
        h = min(s["max_substep"], s["orbit_step_fraction"] * float(np.min(np.sqrt(dist**3 / self.gm))))
        alt = dist - self.radii
        for i, body in enumerate(self.bodies):
            in_atm = sc.cd_table is not None and body.atmosphere is not None and alt[i] < body.atmosphere.x_max
            if alt[i] > CONTACT_ZONE and not in_atm and alt[i] > 0.05 * dist[i]:
                continue  # far from this body's surface and atmosphere
            v_rel = v - (V[i] + np.cross(body.omega_vec, d[i]))
            c = sc.contact_params(body)
            h_contact = 1.0 / (c["natural_frequency"] * s["contact_steps_per_period"])
            vr = float(np.dot(v_rel, d[i])) / dist[i]
            if alt[i] < CONTACT_ZONE:
                h = min(h, h_contact)
            elif vr < 0.0:
                h = min(h, max(h_contact, 0.2 * alt[i] / -vr))
            if in_atm:
                h = min(h, s["atmosphere_max_substep"])
                speed = float(np.linalg.norm(v_rel))
                rho = body.density(alt[i])
                if speed > 0.0 and rho > 0.0:
                    k = 0.5 * rho * sc.cd_table(speed) * sc.reference_area * speed / sc.mass
                    if k > 0.0:
                        h = min(h, 0.1 / k)  # 10% of the drag velocity time constant
        if sc.thrust_active:
            h = min(h, s["thrust_max_substep"])
        h = max(h, s["min_substep"])
        return min(h, remaining)

    def _advance_spacecraft(self, sc, traj, t0, dt, events):
        t = t0
        t_end = t0 + dt
        while t_end - t > 1e-12 * max(1.0, abs(t_end)):
            P0, V0 = traj.at(t)
            h = self._choose_substep(sc, t, P0, V0, t_end - t)

            # Stop exactly at propellant depletion.
            cutoff = False
            floor = max(sc.dry_mass, MIN_MASS)
            if sc.mass_rate < 0.0:
                t_dry = (sc.mass - floor) / -sc.mass_rate
                if t_dry <= 1e-9:
                    self._engine_cutoff(sc, t, events)
                    continue
                if t_dry <= h:
                    h, cutoff = t_dry, True

            Ph, Vh = traj.at(t + 0.5 * h)
            P1, V1 = traj.at(t + h)
            r, v, m = sc.pos, sc.vel, sc.mass
            a1, md1 = self._sc_forces(sc, t, r, v, m, P0, V0)
            r2, v2, m2 = r + 0.5 * h * v, v + 0.5 * h * a1, m + 0.5 * h * md1
            a2, md2 = self._sc_forces(sc, t + 0.5 * h, r2, v2, m2, Ph, Vh)
            r3, v3, m3 = r + 0.5 * h * v2, v + 0.5 * h * a2, m + 0.5 * h * md2
            a3, md3 = self._sc_forces(sc, t + 0.5 * h, r3, v3, m3, Ph, Vh)
            r4, v4, m4 = r + h * v3, v + h * a3, m + h * md3
            a4, md4 = self._sc_forces(sc, t + h, r4, v4, m4, P1, V1)
            sc.pos = r + h / 6.0 * (v + 2 * v2 + 2 * v3 + v4)
            sc.vel = v + h / 6.0 * (a1 + 2 * a2 + 2 * a3 + a4)
            sc.mass = m + h / 6.0 * (md1 + 2 * md2 + 2 * md3 + md4)
            t += h

            if cutoff:
                sc.mass = floor
                self._engine_cutoff(sc, t, events)
            self._update_contact(sc, t, P1, events)
            self._record_trail(sc, t, P1, V1, force=cutoff)

    def _engine_cutoff(self, sc, t, events):
        sc.clear_thrust()
        events.append({"time": t, "spacecraft": sc.id, "event": "engine_cutoff",
                       "reason": "dry mass reached"})

    def _update_contact(self, sc, t, P, events):
        d = sc.pos - P
        alt = np.sqrt(np.einsum("ij,ij->i", d, d)) - self.radii
        now = {self.bodies[i].id for i in np.flatnonzero(alt < 0.0)}
        for b in sorted(now - sc.in_contact):
            events.append({"time": t, "spacecraft": sc.id, "event": "contact_start", "body": b})
        for b in sorted(sc.in_contact - now):
            events.append({"time": t, "spacecraft": sc.id, "event": "contact_end", "body": b})
        sc.in_contact = now

    # -- trails ----------------------------------------------------------------

    def _compute_spheres_of_influence(self):
        """Laplace sphere-of-influence radius of every body from current positions.

        Each body's primary is the least massive more-massive body whose Hill
        sphere contains it (so the Moon orbits Earth, not the Sun); the most
        massive body has an infinite sphere.
        """
        masses = np.array([b.mass for b in self.bodies])
        order = np.argsort(-masses, kind="stable")
        n = len(self.bodies)
        self.soi = np.full(n, np.inf)
        hill = np.full(n, np.inf)
        done = [int(order[0])]
        for i in map(int, order[1:]):
            dist = np.linalg.norm(self.bpos[done] - self.bpos[i], axis=1)
            inside = [j for j, d in zip(done, dist) if d < hill[j]]
            parent = min(inside, key=lambda j: masses[j])
            d = float(np.linalg.norm(self.bpos[i] - self.bpos[parent]))
            self.soi[i] = d * (masses[i] / masses[parent]) ** 0.4
            hill[i] = d * (masses[i] / (3.0 * masses[parent])) ** (1.0 / 3.0)
            done.append(i)

    def _reference_body(self, r, P):
        """Index of the smallest sphere of influence containing position r."""
        dist = np.sqrt(np.einsum("ij,ij->i", P - r, P - r))
        inside = np.flatnonzero(dist < self.soi)
        if not len(inside):
            return int(np.argmin(dist))
        return int(inside[np.argmin(self.soi[inside])])

    def _record_trail(self, sc, t, P, V, force=False):
        s = self.trail_settings
        ref = self._reference_body(sc.pos, P)
        body = self.bodies[ref]
        rel = sc.pos - P[ref]
        r = float(np.linalg.norm(rel))
        alt = r - body.radius
        tr = sc.trail

        # While resting on a surface, record only the touchdown point: in the
        # non-rotating frame a parked craft would otherwise trace a latitude circle.
        v_surf = sc.vel - (V[ref] + np.cross(body.omega_vec, rel))
        if alt < 0.0 and float(np.linalg.norm(v_surf)) < REST_SPEED:
            if not tr.resting:
                tr.add(t, ref, rel, not tr.points, s["max_points"])
                tr.resting = True
            return
        if tr.resting:  # leaving the surface starts a new segment
            tr.resting = False
            tr.add(t, ref, rel, True, s["max_points"])
            return

        last = tr.points[-1] if tr.points else None
        if not force and last is not None and last[2] == ref:
            spacing = min(s["radius_fraction"] * r, max(s["altitude_fraction"] * alt, s["min_spacing"]))
            moved = math.dist(last[3:6], rel)
            if moved < spacing:
                return
        tr.add(t, ref, rel, last is None, s["max_points"])

    def trails(self, since=None, ids=None):
        """Trail data per craft; with ``since`` = {id: [version, seq]}, only new points."""
        since = since if isinstance(since, dict) else {}
        out = {}
        for sc_id in (list(self.spacecraft) if ids is None else [str(i) for i in ids]):
            tr = self._craft(sc_id).trail
            known = since.get(sc_id)
            full = True
            pts = tr.points
            if (isinstance(known, (list, tuple)) and len(known) == 2
                    and known[0] == tr.version and isinstance(known[1], (int, float))):
                full = False
                k = len(pts)
                while k > 0 and pts[k - 1][0] > known[1]:
                    k -= 1
                pts = pts[k:]
            out[sc_id] = {
                "version": tr.version,
                "full": full,
                "resting": tr.resting,
                "points": [[p[0], p[1], self.bodies[p[2]].id, p[3], p[4], p[5], int(p[6])] for p in pts],
            }
        return out

    # -- public operations ---------------------------------------------------

    def step(self, dt):
        dt = _number(dt, "dt", nonnegative=True)
        events = []
        if dt == 0.0:
            return events
        self._compute_spheres_of_influence()
        traj = self._advance_bodies(dt)
        for sc in self.spacecraft.values():
            self._advance_spacecraft(sc, traj, self.time, dt, events)
        self.bpos, self.bvel = traj.P[-1].copy(), traj.V[-1].copy()
        self.time += dt
        self.step_count += 1
        return events

    def set_thrust(self, sc_id, force, mass_rate=0.0, frame="inertial", body=None):
        sc = self._craft(sc_id)
        force = _vec3(force, "force")
        mass_rate = _number(mass_rate, "mass_rate")
        frame, body_obj = self._parse_frame(frame, body)
        sc.thrust = force
        sc.mass_rate = mass_rate
        sc.thrust_frame = frame
        sc.thrust_body = body_obj

    def _parse_frame(self, frame, body):
        frame = str(frame or "inertial").lower()
        if frame == "inertial":
            return frame, None
        if frame == "enu":
            if body is None:
                raise SimError("frame 'enu' requires a 'body'")
            return frame, self._body(body)
        raise SimError("frame must be 'inertial' or 'enu'")

    def stage(self, sc_id, children, frame="inertial", body=None):
        parent = self._craft(sc_id)
        if not isinstance(children, list) or len(children) < 2:
            raise SimError("stage requires a 'children' list with at least two entries")
        new = []
        ids = set()
        for i, spec in enumerate(children):
            what = f"child #{i}"
            if not isinstance(spec, dict):
                raise SimError(f"{what} must be an object")
            cid = str(_require(spec, "id", what))
            if cid in ids or cid in self.body_by_id or (cid in self.spacecraft and cid != parent.id):
                raise SimError(f"{what}: id '{cid}' is already in use")
            ids.add(cid)
            inherit = {"dry_mass": 0.0, "reference_area": parent.reference_area,
                       "cd_table": parent.cd_table, "contact": parent.contact}
            props = _parse_spacecraft_props(spec, what, inherit)
            dv = _vec3(spec.get("delta_v", [0.0, 0.0, 0.0]), f"{what} delta_v")
            c_frame, c_body = self._parse_frame(spec.get("frame", frame), spec.get("body", body))
            if c_frame == "enu":
                east, north, up = c_body.enu_basis(parent.pos - self.bpos[c_body.index])
                dv = dv[0] * east + dv[1] * north + dv[2] * up
            craft = Spacecraft(cid, parent.pos.copy(), parent.vel + dv, **props)
            craft.in_contact = set(parent.in_contact)
            craft.trail = parent.trail.copy()  # continuous history from the pad
            self._record_trail(craft, self.time, self.bpos, self.bvel, force=True)
            new.append(craft)
        del self.spacecraft[parent.id]
        for craft in new:
            self.spacecraft[craft.id] = craft
        return [c.id for c in new]

    # -- state reporting -----------------------------------------------------

    def _origin(self, relative_to):
        if relative_to is None:
            return np.zeros(3), np.zeros(3)
        b = self._body(relative_to)
        return self.bpos[b.index], self.bvel[b.index]

    def body_state(self, body_id, relative_to=None):
        b = self._body(body_id)
        p0, v0 = self._origin(relative_to)
        return {
            "id": b.id, "mass": b.mass, "radius": b.radius,
            "position": _list(self.bpos[b.index] - p0),
            "velocity": _list(self.bvel[b.index] - v0),
            "prime_meridian": math.degrees(b.prime_meridian(self.time)) % 360.0,
            "pole": _list(b.pole),
            "rotation_period": (2.0 * math.pi / b.spin_rate) if b.spin_rate else None,
            "has_atmosphere": b.atmosphere is not None,
        }

    def spacecraft_state(self, sc_id, relative_to=None):
        sc = self._craft(sc_id)
        p0, v0 = self._origin(relative_to)
        _, acc = self._sc_forces(sc, self.time, sc.pos, sc.vel, sc.mass, self.bpos, self.bvel, diagnostics=True)

        d = sc.pos - self.bpos
        dist = np.sqrt(np.einsum("ij,ij->i", d, d))
        alt = dist - self.radii
        i = int(np.argmin(alt))
        body = self.bodies[i]
        v_rel = sc.vel - (self.bvel[i] + np.cross(body.omega_vec, d[i]))
        airspeed = float(np.linalg.norm(v_rel))
        east, north, up = body.enu_basis(d[i])
        lat, lon = body.lat_lon(d[i], self.time)
        rho = body.density(alt[i]) if alt[i] >= 0 else body.density(0.0)
        cd = sc.cd_table(airspeed) if sc.cd_table is not None else None

        return {
            "id": sc.id,
            "mass": sc.mass,
            "dry_mass": sc.dry_mass,
            "reference_area": sc.reference_area,
            "position": _list(sc.pos - p0),
            "velocity": _list(sc.vel - v0),
            "thrust": {
                "active": sc.thrust_active,
                "force": _list(sc.thrust),
                "frame": sc.thrust_frame,
                "body": sc.thrust_body.id if sc.thrust_body else None,
                "mass_rate": sc.mass_rate,
            },
            "nearest_body": {
                "id": body.id,
                "altitude": float(alt[i]),
                "latitude": lat,
                "longitude": lon,
                "in_contact": bool(alt[i] < 0.0),
                "surface_relative_velocity": _list(v_rel),
                "surface_relative_velocity_enu": [float(np.dot(v_rel, e)) for e in (east, north, up)],
                "airspeed": airspeed,
                "density": rho,
                "dynamic_pressure": 0.5 * rho * airspeed**2,
                "drag_coefficient": cd,
            },
            "acceleration": {k: _list(val) for k, val in acc.items()},
            "trail": {
                "reference_body": self.bodies[self._reference_body(sc.pos, self.bpos)].id,
                "points": len(sc.trail.points),
                "version": sc.trail.version,
            },
        }

    def state(self, relative_to=None, bodies=True, spacecraft=True):
        out = {"time": self.time, "step_count": self.step_count,
               "relative_to": relative_to}
        if bodies:
            ids = [b.id for b in self.bodies] if bodies is True else list(bodies)
            out["bodies"] = [self.body_state(b, relative_to) for b in ids]
        if spacecraft:
            ids = list(self.spacecraft) if spacecraft is True else list(spacecraft)
            out["spacecraft"] = [self.spacecraft_state(s, relative_to) for s in ids]
        return out

    def info(self):
        return {
            "scenario": str(self.scenario_path),
            "time": self.time,
            "step_count": self.step_count,
            "gravitational_constant": self.G,
            "body_max_substep": self.body_max_substep,
            "spacecraft_settings": self.sc_settings,
            "trail_settings": self.trail_settings,
            "bodies": [b.id for b in self.bodies],
            "spacecraft": list(self.spacecraft),
        }


# ----------------------------------------------------------------------------
# Command handling and ZeroMQ server
# ----------------------------------------------------------------------------

class CommandHandler:
    def __init__(self, sim):
        self.sim = sim
        self.shutdown_requested = False

    def handle(self, request):
        if not isinstance(request, dict):
            raise SimError("request must be a JSON object")
        cmd = request.get("cmd")
        method = getattr(self, f"cmd_{cmd}", None) if isinstance(cmd, str) else None
        if method is None:
            raise SimError(f"unknown command '{cmd}'")
        result = method(request) or {}
        result.setdefault("time", self.sim.time)
        return result

    def cmd_ping(self, req):
        return {"pong": True}

    def cmd_info(self, req):
        return self.sim.info()

    def cmd_step(self, req):
        dt = _require(req, "dt", "step")
        t0 = time.perf_counter()
        events = self.sim.step(dt)
        out = {"events": events, "wall_time": time.perf_counter() - t0}
        if req.get("return_state"):
            out["state"] = self.sim.state(req.get("relative_to"))
        return out

    def cmd_get_state(self, req):
        return self.sim.state(req.get("relative_to"), req.get("bodies", True), req.get("spacecraft", True))

    def cmd_get_body(self, req):
        return {"body": self.sim.body_state(_require(req, "id", "get_body"), req.get("relative_to"))}

    def cmd_get_spacecraft(self, req):
        return {"spacecraft": self.sim.spacecraft_state(_require(req, "id", "get_spacecraft"),
                                                        req.get("relative_to"))}

    def cmd_set_thrust(self, req):
        sc_id = _require(req, "id", "set_thrust")
        self.sim.set_thrust(sc_id, _require(req, "force", "set_thrust"), req.get("mass_rate", 0.0),
                            req.get("frame", "inertial"), req.get("body"))
        return {"spacecraft": sc_id}

    def cmd_stage(self, req):
        new_ids = self.sim.stage(_require(req, "id", "stage"), _require(req, "children", "stage"),
                                 req.get("frame", "inertial"), req.get("body"))
        return {"removed": req["id"], "created": new_ids}

    def cmd_get_trails(self, req):
        return {"trails": self.sim.trails(req.get("since"), req.get("ids"))}

    def cmd_reset(self, req):
        self.sim.load()
        return {"reset": True}

    def cmd_shutdown(self, req):
        self.shutdown_requested = True
        return {"shutdown": True}


def serve(sim, endpoint):
    import zmq

    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind(endpoint)
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    handler = CommandHandler(sim)
    print(f"[nbody_sim] loaded '{sim.scenario_path}': {len(sim.bodies)} bodies, "
          f"{len(sim.spacecraft)} spacecraft", flush=True)
    print(f"[nbody_sim] listening on {endpoint}", flush=True)
    try:
        while not handler.shutdown_requested:
            # Poll with a timeout so Ctrl+C is handled promptly (also on Windows).
            if not poller.poll(250):
                continue
            raw = sock.recv()
            try:
                request = json.loads(raw)
                reply = {"ok": True}
                reply.update(handler.handle(request))
            except SimError as e:
                reply = {"ok": False, "error": str(e)}
            except json.JSONDecodeError as e:
                reply = {"ok": False, "error": f"invalid JSON: {e}"}
            except Exception as e:  # keep serving; REP must always reply
                reply = {"ok": False, "error": f"internal error: {type(e).__name__}: {e}"}
            sock.send(json.dumps(reply).encode("utf-8"))
    except KeyboardInterrupt:
        pass
    finally:
        sock.close(linger=0)
        ctx.term()
        print("[nbody_sim] stopped", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="N-body simulation server (ZeroMQ REQ/REP, JSON).")
    parser.add_argument("scenario", help="YAML scenario file")
    parser.add_argument("--endpoint", help=f"ZeroMQ bind endpoint (default: scenario value or {DEFAULT_ENDPOINT})")
    args = parser.parse_args(argv)
    try:
        sim = Simulation(args.scenario)
    except SimError as e:
        print(f"[nbody_sim] error: {e}", file=sys.stderr)
        return 1
    serve(sim, args.endpoint or sim.endpoint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
