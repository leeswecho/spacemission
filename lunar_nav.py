"""Trajectory prediction and targeting for the Moon mission autopilot.

The simulator is the truth, but targeting needs fast "what if" answers: where
does a craft go if it leaves with this velocity?  This module mirrors the
simulator's gravity model closely enough for that:

* Ephemeris: all bodies integrated from a snapshot of the sim with the same
  fixed-step RK4 the sim uses, stored at substep boundaries and interpolated
  with cubic Hermite polynomials.
* Craft: point mass under the gravity of the Sun, Earth and Moon (the other
  planets change a lunar trajectory by far less than a mid-course correction
  can fix), RK4 with steps scaled to the local orbital time scale.

Targeting is Newton iteration with finite-difference Jacobians on impulsive
velocity changes.  Arrival at the Moon is targeted in the B-plane (smooth,
even when the solution passes from one side of the Moon to the other);
arrival at Earth by perigee radius and inclination.
"""

import math

import numpy as np

GRAVITY_IDS = ("sun", "earth", "luna")


def unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


# ---------------------------------------------------------------------------
# Ephemeris and propagation
# ---------------------------------------------------------------------------

class Ephemeris:
    """Body positions/velocities from t0 to t0 + duration (sim frame)."""

    def __init__(self, state, G, duration, h=600.0):
        bodies = state["bodies"]
        self.ids = [b["id"] for b in bodies]
        self.index = {b: i for i, b in enumerate(self.ids)}
        self.gm = np.array([G * b["mass"] for b in bodies])
        self.radius = {b["id"]: b["radius"] for b in bodies}
        self.t0 = float(state["time"])
        n = max(1, int(math.ceil(duration / h)))
        self.h = h
        p = np.array([b["position"] for b in bodies], dtype=float)
        v = np.array([b["velocity"] for b in bodies], dtype=float)
        P = np.empty((n + 1,) + p.shape)
        V = np.empty_like(P)
        P[0], V[0] = p, v
        for k in range(n):
            a1 = self._accel(p)
            p2, v2 = p + 0.5 * h * v, v + 0.5 * h * a1
            a2 = self._accel(p2)
            p3, v3 = p + 0.5 * h * v2, v + 0.5 * h * a2
            a3 = self._accel(p3)
            p4, v4 = p + h * v3, v + h * a3
            a4 = self._accel(p4)
            p = p + h / 6.0 * (v + 2 * v2 + 2 * v3 + v4)
            v = v + h / 6.0 * (a1 + 2 * a2 + 2 * a3 + a4)
            P[k + 1], V[k + 1] = p, v
        self.P, self.V = P, V
        self.t_end = self.t0 + n * h
        self.sel = np.array([self.index[b] for b in GRAVITY_IDS])
        self.sel_gm = self.gm[self.sel]
        self.Ps, self.Vs = P[:, self.sel], V[:, self.sel]

    def _accel(self, pos):
        d = pos[None, :, :] - pos[:, None, :]
        r2 = np.einsum("ijk,ijk->ij", d, d)
        np.fill_diagonal(r2, 1.0)
        inv_r3 = r2 ** -1.5
        np.fill_diagonal(inv_r3, 0.0)
        return np.einsum("ij,ijk->ik", inv_r3 * self.gm[None, :], d)

    def _interp(self, t, P, V):
        s = (t - self.t0) / self.h
        k = min(max(int(s), 0), len(P) - 2)
        u = s - k
        h = self.h
        p0, p1, v0, v1 = P[k], P[k + 1], V[k], V[k + 1]
        u2, u3 = u * u, u * u * u
        pos = ((2 * u3 - 3 * u2 + 1) * p0 + (u3 - 2 * u2 + u) * h * v0
               + (-2 * u3 + 3 * u2) * p1 + (u3 - u2) * h * v1)
        vel = ((6 * u2 - 6 * u) * p0 + (3 * u2 - 4 * u + 1) * h * v0
               + (-6 * u2 + 6 * u) * p1 + (3 * u2 - 2 * u) * h * v1) / h
        return pos, vel

    def body(self, body_id, t):
        """(position, velocity) of one body at time t."""
        i = self.index[body_id]
        return self._interp(t, self.P[:, i], self.V[:, i])

    def gravity_bodies(self, t):
        return self._interp(t, self.Ps, self.Vs)[0]


def propagate(eph, t0, r0, v0, t_max, watch=None, h_max=900.0, frac=0.01):
    """Coast a craft (sim-frame state) from t0 until t_max or until watch says stop.

    watch(t, r, v, bodies_pos) -> True to stop.  Returns (t, r, v).
    """
    t, r, v = t0, np.array(r0, dtype=float), np.array(v0, dtype=float)
    gm = eph.sel_gm

    def acc(tt, rr):
        P = eph.gravity_bodies(tt)
        d = P - rr
        dist = np.sqrt(np.einsum("ij,ij->i", d, d))
        return np.einsum("i,ij->j", gm / dist**3, d), P, dist

    a, P, dist = acc(t, r)
    while t < t_max - 1e-9:
        h = min(h_max, frac * float(np.min(np.sqrt(dist**3 / gm))) * 2 * math.pi, t_max - t)
        h = max(h, 0.5)
        k1v, k1r = a, v
        a2, _, _ = acc(t + 0.5 * h, r + 0.5 * h * k1r)
        k2v, k2r = a2, v + 0.5 * h * k1v
        a3, _, _ = acc(t + 0.5 * h, r + 0.5 * h * k2r)
        k3v, k3r = a3, v + 0.5 * h * k2v
        a4, _, _ = acc(t + h, r + h * k3r)
        k4v, k4r = a4, v + h * k3v
        r = r + h / 6.0 * (k1r + 2 * k2r + 2 * k3r + k4r)
        v = v + h / 6.0 * (k1v + 2 * k2v + 2 * k3v + k4v)
        t += h
        a, P, dist = acc(t, r)
        if watch is not None and watch(t, r, v, P):
            break
    return t, r, v


def closest_approach(eph, t0, r0, v0, body_id, t_max, near=math.inf):
    """Coast to the closest approach to a body (the target, not the body just left).

    The search ends at the first distance minimum once the craft is within
    `near`, at impact, or at t_max.  Returns (t, r_rel, v_rel) relative to the
    body at the closest point found.
    """
    j = list(GRAVITY_IDS).index(body_id)
    R = eph.radius[body_id]
    best = {"d": math.inf, "state": None}

    def watch(t, r, v, P):
        d = float(np.linalg.norm(r - P[j]))
        if d < best["d"]:
            best["d"], best["state"] = d, (t, r.copy(), v.copy())
            return d < R
        return d < near

    propagate(eph, t0, r0, v0, t_max, watch)
    t, r, v = best["state"]
    pb, vb = eph.body(body_id, t)
    return t, r - pb, v - vb


# ---------------------------------------------------------------------------
# Two-body geometry
# ---------------------------------------------------------------------------

def elements(r, v, mu, pole):
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


def time_to_periapsis(r, v, mu):
    """Seconds until the next periapsis (ellipse or hyperbola); 0 if already past on a hyperbola."""
    el = elements(r, v, mu, np.array([0.0, 0.0, 1.0]))
    e, rn = el["e"], np.linalg.norm(r)
    rv = float(r @ v)
    if e < 1e-6:
        return 0.0
    if e < 1.0:
        a = el["a"]
        E = math.acos(max(-1.0, min(1.0, (1 - rn / a) / e)))
        if rv < 0:
            E = 2 * math.pi - E
        M = E - e * math.sin(E)
        n = math.sqrt(mu / a**3)
        return ((2 * math.pi - M) % (2 * math.pi)) / n
    a = abs(mu / (2 * el["energy"]))
    F = math.acosh(max(1.0, (1 + rn / a) / e))
    if rv >= 0:
        return 0.0
    M = e * math.sinh(F) - F
    return M / math.sqrt(mu / a**3)


def time_to_apoapsis(r, v, mu):
    el = elements(r, v, mu, np.array([0.0, 0.0, 1.0]))
    a, e = el["a"], el["e"]
    if not math.isfinite(a) or e < 1e-6:
        return 0.0
    rn = np.linalg.norm(r)
    E = math.acos(max(-1.0, min(1.0, (1 - rn / a) / e)))
    if float(r @ v) < 0:
        E = 2 * math.pi - E
    M = E - e * math.sin(E)
    n = math.sqrt(mu / a**3)
    return ((math.pi - M) % (2 * math.pi)) / n


def plane_through(direction, pole, inc_deg, prefer=None):
    """Unit normal of a plane containing `direction` with inclination inc_deg to `pole`.

    If no such plane exists (the direction is further from the equator than
    the inclination allows), the plane of that inclination that comes closest
    is returned.  Of the candidates, the one nearest `prefer` wins.
    """
    d = unit(direction)
    k = unit(pole)
    x = unit(np.cross(k, [1.0, 0.0, 0.0]) if abs(k[0]) < 0.9 else np.cross(k, [0.0, 1.0, 0.0]))
    y = np.cross(k, x)
    ci, si = math.cos(math.radians(inc_deg)), math.sin(math.radians(inc_deg))
    # Normal n(phi) = ci k + si (cos phi x + sin phi y); want n . d = 0.
    A = si * float(d @ x)
    B = si * float(d @ y)
    C = ci * float(d @ k)
    amp = math.hypot(A, B)
    base = math.atan2(B, A)
    if amp > 1e-12 and abs(C) <= amp:
        off = math.acos(-C / amp)
        phis = [base + off, base - off]
    else:
        phis = [base if C < 0 else base + math.pi]
    cands = [ci * k + si * (math.cos(p) * x + math.sin(p) * y) for p in phis]
    if prefer is not None:
        return max(cands, key=lambda n: float(n @ prefer))
    return cands[0]


def conic_state(mu, r_p, r_a, normal, peri_dir, t_to_peri):
    """State (r, v) on an orbit (periapsis/apoapsis radii, plane normal, periapsis
    direction) t_to_peri seconds before reaching periapsis."""
    p_hat = unit(peri_dir - float(peri_dir @ normal) * normal)
    q_hat = np.cross(normal, p_hat)
    a = 0.5 * (r_p + r_a)
    e = (r_a - r_p) / (r_a + r_p)
    n = math.sqrt(mu / a**3)
    M = (-n * t_to_peri) % (2 * math.pi)
    E = M if e < 0.8 else math.pi
    for _ in range(50):
        E -= (E - e * math.sin(E) - M) / (1 - e * math.cos(E))
    nu = 2 * math.atan2(math.sqrt(1 + e) * math.sin(E / 2), math.sqrt(1 - e) * math.cos(E / 2))
    p = a * (1 - e * e)
    rn = p / (1 + e * math.cos(nu))
    r = rn * (math.cos(nu) * p_hat + math.sin(nu) * q_hat)
    vk = math.sqrt(mu / p)
    v = vk * (-math.sin(nu) * p_hat + (e + math.cos(nu)) * q_hat)
    return r, v


def bplane(r, v, mu, pole):
    """B-plane coordinates of a hyperbolic approach: (B.T, B.R, S, T, R, vinf)."""
    el = elements(r, v, mu, pole)
    e = el["e"]
    if e <= 1.0:
        return None
    h = el["h"]
    h_hat = unit(h)
    e_hat = unit(el["e_vec"])
    q_hat = np.cross(h_hat, e_hat)
    S = e_hat / e + math.sqrt(1 - 1 / e**2) * q_hat
    vinf = math.sqrt(2 * el["energy"])
    b = np.linalg.norm(h) / vinf
    B = b * unit(np.cross(S, h_hat))
    T = unit(np.cross(S, pole))
    R = np.cross(S, T)
    return float(B @ T), float(B @ R), S, T, R, vinf


def bplane_target(S, T, R, vinf, mu, r_p, inc_deg, pole, current=None):
    """Desired (B.T, B.R) for periapsis radius r_p and inclination inc_deg."""
    b = r_p * math.sqrt(1 + 2 * mu / (r_p * vinf**2))
    rk = float(R @ pole)
    c = -math.cos(math.radians(inc_deg)) / rk if abs(rk) > 1e-9 else 0.0
    c = max(-1.0, min(1.0, c))
    th = math.acos(c)
    cands = [(b * math.cos(t), b * math.sin(t)) for t in (th, -th)]
    if current is not None:
        return min(cands, key=lambda x: (x[0] - current[0])**2 + (x[1] - current[1])**2)
    return cands[0]


# ---------------------------------------------------------------------------
# Newton solver for impulsive corrections
# ---------------------------------------------------------------------------

def solve(residual, x0, step=0.05, max_iter=25, tol=1.0, max_move=150.0, log=None):
    """Find x (3-vector, m/s) with residual(x) ~ 0 (2-vector, scaled so 1 unit ~ 1 km).

    Gauss-Newton with a minimum-norm update, finite-difference Jacobian, a
    per-iteration move limit and step halving when the residual grows.
    Returns (x, residual, ok).
    """
    x = np.array(x0, dtype=float)
    f = residual(x)
    for it in range(max_iter):
        fn = float(np.linalg.norm(f))
        if log:
            log(it, x, f)
        if fn < tol:
            return x, f, True
        J = np.empty((len(f), 3))
        for k in range(3):
            dx = np.zeros(3)
            dx[k] = step
            J[:, k] = (residual(x + dx) - f) / step
        dx = -np.linalg.pinv(J) @ f
        n = np.linalg.norm(dx)
        if n > max_move:
            dx *= max_move / n
        lam = 1.0
        for _ in range(8):
            x_new = x + lam * dx
            f_new = residual(x_new)
            if np.linalg.norm(f_new) < fn:
                break
            lam *= 0.5
        x, f = x_new, f_new
    return x, f, float(np.linalg.norm(f)) < tol
