#!/usr/bin/env python3
"""Generate the example solar-system scenario (solar_system.yaml).

Computes approximate barycentric state vectors at the J2000 epoch
(2000-01-01 12:00 TT) in the J2000 ecliptic frame:

  * Planets / Earth-Moon barycenter: JPL "Approximate Positions of the
    Planets" (E. M. Standish), Table 1 Keplerian elements, valid 1800-2050.
  * Moon (geocentric): Astronomical Almanac low-precision lunar formulae,
    velocity by central differencing.
  * Sun: placed so the system barycenter is at the origin with zero momentum.

Rotation poles and prime meridians come from the IAU WGCCRE report, converted
from ICRF (equatorial) to the ecliptic frame.  Earth's prime meridian uses
GMST at J2000 so that launch-site longitudes are placed correctly.

Usage:  python tools/generate_solar_system.py [output.yaml]
"""

import math
import sys
from pathlib import Path

import numpy as np

G = 6.67430e-11
AU = 1.495978707e11
DAY = 86400.0
OBLIQUITY = math.radians(23.4392911)

# GM in km^3/s^2 (JPL DE440).  Mars..Neptune are system values (incl. moons).
GM_KM3 = {
    "sun": 132712440041.279419,
    "mercury": 22031.868551,
    "venus": 324858.592000,
    "earth": 398600.435507,
    "luna": 4902.800118,
    "mars": 42828.375816,
    "jupiter": 126712764.100000,
    "saturn": 37940584.841800,
    "uranus": 5794556.400000,
    "neptune": 6836527.100580,
}

# Radii in metres (IAU; equatorial for Earth, mean for the others).
RADIUS = {
    "sun": 6.957e8,
    "mercury": 2.4397e6,
    "venus": 6.0518e6,
    "earth": 6.378137e6,
    "luna": 1.7374e6,
    "mars": 3.3895e6,
    "jupiter": 6.9911e7,
    "saturn": 5.8232e7,
    "uranus": 2.5362e7,
    "neptune": 2.4622e7,
}

# JPL Table 1: a [au], e, I [deg], L [deg], long. perihelion [deg], long. node [deg]
ELEMENTS = {
    "mercury": (0.38709927, 0.20563593, 7.00497902, 252.25032350, 77.45779628, 48.33076593),
    "venus": (0.72333566, 0.00677672, 3.39467605, 181.97909950, 131.60246718, 76.67984255),
    "emb": (1.00000261, 0.01671123, -0.00001531, 100.46457166, 102.93768193, 0.0),
    "mars": (1.52371034, 0.09339410, 1.84969142, -4.55343205, -23.94362959, 49.55953891),
    "jupiter": (5.20288700, 0.04838624, 1.30439695, 34.39644051, 14.72847983, 100.47390909),
    "saturn": (9.53667594, 0.05386179, 2.48599187, 49.95424423, 92.59887831, 113.66242448),
    "uranus": (19.18916464, 0.04725744, 0.77263783, 313.23810451, 170.95427630, 74.01692503),
    "neptune": (30.06992276, 0.00859048, 1.77004347, -55.12002969, 44.96476227, 131.78422574),
}

# IAU pole (RA, Dec in ICRF, deg), prime meridian W0 (deg), rotation rate (deg/day).
# Negative rate = retrograde rotation about the IAU north pole.
IAU_ROTATION = {
    "sun": (286.13, 63.87, 84.176, 14.1844000),
    "mercury": (281.0103, 61.4155, 329.5988, 6.1385108),
    "venus": (272.76, 67.16, 160.20, -1.4813688),
    "earth": (0.0, 90.0, None, 360.98564736629),  # W0 from GMST below
    "luna": (269.9949, 66.5392, 38.3213, 13.17635815),
    "mars": (317.269202, 54.432516, 176.049863, 350.891982443297),
    "jupiter": (268.056595, 64.495303, 284.95, 870.5360000),
    "saturn": (40.589, 83.537, 38.90, 810.7939024),
    "uranus": (257.311, -15.175, 203.81, -501.1600928),
    "neptune": (299.36, 43.46, 249.978, 541.1397757),
}
GMST_J2000_DEG = 280.46061837

# Atmospheric density tables: [altitude m, density kg/m^3]
EARTH_ATMOSPHERE = [  # US Standard Atmosphere 1976
    (0, 1.225), (1e3, 1.112), (2e3, 1.007), (3e3, 0.9093), (4e3, 0.8194),
    (5e3, 0.7364), (6e3, 0.6601), (8e3, 0.5258), (10e3, 0.4135), (12e3, 0.3119),
    (15e3, 0.1948), (20e3, 0.08891), (25e3, 0.04008), (30e3, 0.01841),
    (35e3, 0.008463), (40e3, 0.003996), (45e3, 0.001966), (50e3, 0.001027),
    (60e3, 3.097e-4), (70e3, 8.283e-5), (80e3, 1.846e-5), (90e3, 3.416e-6),
    (100e3, 5.604e-7), (110e3, 9.708e-8), (120e3, 2.222e-8), (150e3, 2.076e-9),
    (200e3, 2.541e-10), (250e3, 6.073e-11), (300e3, 1.916e-11), (400e3, 2.803e-12),
    (500e3, 5.215e-13), (600e3, 1.137e-13), (700e3, 3.070e-14), (800e3, 1.136e-14),
    (900e3, 5.759e-15), (1000e3, 3.561e-15),
]
VENUS_ATMOSPHERE = [  # approximate, from the Venus International Reference Atmosphere
    (0, 65.0), (10e3, 38.0), (20e3, 21.0), (30e3, 10.1), (40e3, 4.4),
    (50e3, 1.5), (60e3, 0.48), (70e3, 0.085), (80e3, 0.012), (90e3, 1.5e-3),
    (100e3, 1.2e-4), (120e3, 2.0e-6), (150e3, 5.0e-9), (200e3, 1.0e-11),
]
MARS_ATMOSPHERE = [  # approximate exponential model, scale height ~11 km
    (0, 0.020), (10e3, 0.0080), (20e3, 0.0032), (30e3, 0.0013), (40e3, 5.0e-4),
    (60e3, 8.0e-5), (80e3, 1.2e-5), (100e3, 1.5e-6), (150e3, 1.0e-8), (200e3, 1.0e-10),
]
ATMOSPHERES = {"earth": EARTH_ATMOSPHERE, "venus": VENUS_ATMOSPHERE, "mars": MARS_ATMOSPHERE}


def mass(name):
    return GM_KM3[name] * 1e9 / G


def equatorial_to_ecliptic(v):
    c, s = math.cos(OBLIQUITY), math.sin(OBLIQUITY)
    x, y, z = v
    return np.array([x, c * y + s * z, -s * y + c * z])


def kepler_state(elements, mu):
    """Heliocentric ecliptic position/velocity (m, m/s) from JPL elements."""
    a_au, e, inc, L, varpi, node = elements
    a = a_au * AU
    inc, L, varpi, node = map(math.radians, (inc, L, varpi, node))
    argp = varpi - node
    M = (L - varpi + math.pi) % (2 * math.pi) - math.pi
    E = M
    for _ in range(50):
        E -= (E - e * math.sin(E) - M) / (1 - e * math.cos(E))
    # Perifocal coordinates
    x = a * (math.cos(E) - e)
    y = a * math.sqrt(1 - e * e) * math.sin(E)
    n = math.sqrt(mu / a**3)
    edot = n / (1 - e * math.cos(E))
    vx = -a * math.sin(E) * edot
    vy = a * math.sqrt(1 - e * e) * math.cos(E) * edot

    cO, sO = math.cos(node), math.sin(node)
    cw, sw = math.cos(argp), math.sin(argp)
    ci, si = math.cos(inc), math.sin(inc)
    R = np.array([
        [cO * cw - sO * sw * ci, -cO * sw - sO * cw * ci, sO * si],
        [sO * cw + cO * sw * ci, -sO * sw + cO * cw * ci, -cO * si],
        [sw * si, cw * si, ci],
    ])
    return R @ np.array([x, y, 0.0]), R @ np.array([vx, vy, 0.0])


def moon_geocentric_position(days_from_j2000):
    """Astronomical Almanac low-precision Moon position, ecliptic, metres."""
    T = days_from_j2000 / 36525.0
    d = math.radians

    def sin(deg):
        return math.sin(d(deg))

    def cos(deg):
        return math.cos(d(deg))

    lam = (218.32 + 481267.881 * T
           + 6.29 * sin(135.0 + 477198.87 * T) - 1.27 * sin(259.3 - 413335.36 * T)
           + 0.66 * sin(235.7 + 890534.22 * T) + 0.21 * sin(269.9 + 954397.74 * T)
           - 0.19 * sin(357.5 + 35999.05 * T) - 0.11 * sin(186.5 + 966404.03 * T))
    beta = (5.13 * sin(93.3 + 483202.02 * T) + 0.28 * sin(228.2 + 960400.89 * T)
            - 0.28 * sin(318.3 + 6003.15 * T) - 0.17 * sin(217.6 - 407332.21 * T))
    par = (0.9508 + 0.0518 * cos(135.0 + 477198.87 * T) + 0.0095 * cos(259.3 - 413335.36 * T)
           + 0.0078 * cos(235.7 + 890534.22 * T) + 0.0028 * cos(269.9 + 954397.74 * T))
    r = 6378.14e3 / math.sin(d(par))
    return r * np.array([cos(beta) * cos(lam), cos(beta) * sin(lam), sin(beta)])


def moon_geocentric_state():
    h = 0.05  # days
    p0 = moon_geocentric_position(0.0)
    v = (moon_geocentric_position(h) - moon_geocentric_position(-h)) / (2 * h * DAY)
    return p0, v


def rotation_params(name):
    """Return (pole unit vector [ecliptic], prime meridian deg, period s)."""
    ra, dec, w0_iau, rate = IAU_ROTATION[name]
    ra, dec = math.radians(ra), math.radians(dec)
    pole_eq = np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])
    pole = equatorial_to_ecliptic(pole_eq)
    period = 360.0 / rate * DAY

    if w0_iau is None:  # Earth: reference direction is the vernal equinox
        w0 = GMST_J2000_DEG
    else:
        # IAU W is measured from the ascending node of the body equator on the
        # ICRF equator (direction RA = ra + 90 deg).  The simulator measures
        # from pole x Z_ecliptic, so add the angle between the two.
        q = equatorial_to_ecliptic(np.array([math.cos(ra + math.pi / 2), math.sin(ra + math.pi / 2), 0.0]))
        n = np.cross(pole, [0.0, 0.0, 1.0])
        n /= np.linalg.norm(n)
        delta = math.degrees(math.atan2(np.dot(np.cross(n, q), pole), np.dot(n, q)))
        w0 = (w0_iau + delta) % 360.0
    return pole, w0, period


def compute_states():
    mu_sun = GM_KM3["sun"] * 1e9
    helio = {}
    for name, el in ELEMENTS.items():
        if name == "emb":
            mu = mu_sun + (GM_KM3["earth"] + GM_KM3["luna"]) * 1e9
        else:
            mu = mu_sun + GM_KM3[name] * 1e9
        helio[name] = kepler_state(el, mu)

    # Split the Earth-Moon barycenter into Earth and Moon.
    emb_r, emb_v = helio.pop("emb")
    moon_r, moon_v = moon_geocentric_state()
    m_e, m_m = GM_KM3["earth"], GM_KM3["luna"]
    f_e, f_m = m_m / (m_e + m_m), m_e / (m_e + m_m)
    helio["earth"] = (emb_r - f_e * moon_r, emb_v - f_e * moon_v)
    helio["luna"] = (emb_r + f_m * moon_r, emb_v + f_m * moon_v)
    helio["sun"] = (np.zeros(3), np.zeros(3))

    # Shift to the barycentric frame.
    total_m = sum(GM_KM3.values())
    r_cm = sum(GM_KM3[n] * helio[n][0] for n in helio) / total_m
    v_cm = sum(GM_KM3[n] * helio[n][1] for n in helio) / total_m
    return {n: (r - r_cm, v - v_cm) for n, (r, v) in helio.items()}


def vec(v):
    return "[" + ", ".join(f"{x:.10e}" for x in v) + "]"


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "solar_system.yaml"
    states = compute_states()
    order = ["sun", "mercury", "venus", "earth", "luna", "mars", "jupiter", "saturn", "uranus", "neptune"]

    lines = [
        "# Example scenario: the Sun, eight planets, the Moon, and a Falcon 9 on the pad.",
        "# Generated by tools/generate_solar_system.py -- edit that script to regenerate.",
        "#",
        "# Frame: solar-system barycentric, J2000 ecliptic (X = vernal equinox, Z = ecliptic north).",
        "# Epoch (t = 0): 2000-01-01 12:00 TT.  All units SI (m, kg, s), angles in degrees.",
        "",
        "simulation:",
        "  gravitational_constant: 6.67430e-11",
        "  endpoint: tcp://127.0.0.1:5555",
        "  body_max_substep: 600.0          # s, RK4 substep for the n-body integration",
        "  spacecraft:",
        "    max_substep: 10.0              # s, longest spacecraft substep in free space",
        "    min_substep: 1.0e-4            # s",
        "    orbit_step_fraction: 0.005     # substep <= fraction * sqrt(r^3/GM) for every body",
        "    atmosphere_max_substep: 1.0    # s, cap while inside an atmosphere",
        "    thrust_max_substep: 0.5        # s, cap while thrusting",
        "    contact_steps_per_period: 25   # substeps per contact-spring oscillation period",
        "",
        "bodies:",
    ]
    for name in order:
        r, v = states[name]
        pole, w0, period = rotation_params(name)
        lines += [
            f"  - id: {name}",
            f"    mass: {mass(name):.10e}",
            f"    radius: {RADIUS[name]:.6e}",
            f"    position: {vec(r)}",
            f"    velocity: {vec(v)}",
            "    rotation:",
            f"      pole: [{pole[0]:.9f}, {pole[1]:.9f}, {pole[2]:.9f}]",
            f"      period: {period:.6f}{'   # negative = retrograde' if period < 0 else ''}",
            f"      prime_meridian: {w0:.6f}",
            "    contact:",
            "      natural_frequency: 2.0       # Hz, spring stiffness k = m * (2 pi f)^2",
            "      damping_ratio: 1.0           # 1 = critically damped",
            "      tangential_damping: 5.0      # 1/s, viscous friction relative to the surface",
            "      friction_coefficient: 1.0    # tangential force capped at mu * normal force",
        ]
        if name in ATMOSPHERES:
            lines.append("    atmosphere:                    # [altitude m, density kg/m^3]")
            lines += [f"      - [{alt:.0f}, {rho:.4e}]" for alt, rho in ATMOSPHERES[name]]
        lines.append("")

    lines += [
        "spacecraft:",
        "  # Falcon 9 Block 5 at liftoff (approximate).  Combined vehicle dry mass = total minus",
        "  # first-stage propellant, so the engine cuts off automatically at first-stage depletion.",
        "  # Sea-level thrust ~7.6 MN, mass flow ~2750 kg/s.",
        "  - id: falcon9",
        "    mass: 549054.0",
        "    dry_mass: 153354.0",
        "    reference_area: 10.52             # m^2, 3.66 m diameter",
        "    drag_coefficient:                 # [airspeed m/s, Cd]",
        "      - [0, 0.30]",
        "      - [200, 0.30]",
        "      - [270, 0.38]",
        "      - [320, 0.52]",
        "      - [380, 0.55]",
        "      - [450, 0.50]",
        "      - [600, 0.42]",
        "      - [1000, 0.33]",
        "      - [2000, 0.27]",
        "      - [4000, 0.23]",
        "      - [8000, 0.20]",
        "    initial_state:",
        "      frame: surface                  # Kennedy Space Center LC-39A",
        "      body: earth",
        "      latitude: 28.60822",
        "      longitude: -80.60409",
        "      altitude: 0.0",
        "      velocity_enu: [0.0, 0.0, 0.0]",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
