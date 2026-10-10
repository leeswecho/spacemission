# spacemission2 — n-body simulation server

A passive n-body gravity simulation with spacecraft, atmospheric drag, and surface contact.
It loads a YAML scenario, then waits for JSON commands over **ZeroMQ REQ/REP**. Time
advances only when a client sends `step`.

| File | Purpose |
|---|---|
| `nbody_sim.py` | Simulation server |
| `sim_client.py` | Python client library + command-line tool |
| `solar_system.yaml` | Example: Sun, 8 planets, Moon, Falcon 9 on LC-39A |
| `tools/generate_solar_system.py` | Regenerates `solar_system.yaml` (J2000 ephemeris) |
| `test_sim.py` | Tests (`python -m unittest test_sim -v`) |
| `viz_server.py`, `viz/` | Web 3D viewer of the live state (see below) |

Requirements: Python 3.9+, `numpy`, `pyyaml`, `pyzmq`.

```
python nbody_sim.py solar_system.yaml [--endpoint tcp://127.0.0.1:5555]
python sim_client.py step dt=1
```

## Conventions

* SI units throughout (m, kg, s, N). Angles in YAML and commands are in **degrees**.
* Global frame: whatever the scenario uses. The example is barycentric J2000 ecliptic
  (X = vernal equinox, Z = ecliptic north), with t = 0 at 2000-01-01 12:00 TT.
* **ENU frame**: East-North-Up at the spacecraft's current position relative to a named body.
  "North" points toward the body's rotation pole.
* Simulation time starts at 0 and is reported in seconds.

## Physics

* **Bodies**: Newtonian gravity between all pairs, RK4 with substeps no longer than
  `body_max_substep`. Each body is a sphere rotating at a constant rate about a fixed pole.
* **Spacecraft**: feel gravity from every body but do not exert it. Each craft uses RK4 with
  adaptive substeps (orbital time scale, atmosphere, thrust, and approach to a surface).
  Body positions within a step are cubic-Hermite interpolated.
* **Drag** (only if the body has `atmosphere` *and* the craft has `drag_coefficient`):
  `F = -½ ρ(alt) Cd(|v_rel|) A |v_rel| v_rel`, where `v_rel` is relative to the
  co-rotating atmosphere. Density is interpolated log-linearly and is 0 above the table's top.
  Cd is interpolated linearly. Both tables clamp at their ends.
* **Contact** (penetration depth `δ` below the radius):
  normal force `m(ω²δ − 2ζω·v_n)` along the local vertical, never pulling,
  with `ω = 2π·natural_frequency`. There is also viscous tangential friction
  `−m·tangential_damping·v_t` relative to the moving surface, capped at
  `friction_coefficient × normal force`. Expressing the spring per unit mass means every craft
  sinks the same depth (`g/ω²`, about 6 cm on Earth at 2 Hz) whatever its mass.
* **Thrust**: a constant force plus a constant `mass_rate` (dm/dt, negative while burning).
  It persists until replaced. When mass reaches `dry_mass`, thrust is cut automatically
  (event `engine_cutoff`).

Not modelled: spacecraft-to-spacecraft collisions, body oblateness, lift, attitude.

## Scenario file

```yaml
simulation:                       # all optional
  gravitational_constant: 6.67430e-11
  endpoint: tcp://127.0.0.1:5555  # overridden by --endpoint
  body_max_substep: 600.0
  spacecraft:
    max_substep: 10.0
    min_substep: 1.0e-4
    orbit_step_fraction: 0.005
    atmosphere_max_substep: 1.0
    thrust_max_substep: 0.5
    contact_steps_per_period: 25

bodies:
  - id: earth
    mass: 5.97e24
    radius: 6.378137e6
    position: [x, y, z]
    velocity: [vx, vy, vz]
    rotation:                     # optional (default: no rotation)
      pole: [x, y, z]             # spin axis (north pole), global frame
      period: 86164.1             # s; negative = retrograde
      prime_meridian: 280.46      # deg at t=0, measured east from pole × Z
    contact:                      # optional; defaults shown
      natural_frequency: 2.0      # Hz
      damping_ratio: 1.0
      tangential_damping: 5.0     # 1/s
      friction_coefficient: 1.0
    atmosphere:                   # optional: [altitude m, density kg/m^3]
      - [0, 1.225]
      - [10000, 0.4135]

spacecraft:
  - id: falcon9
    mass: 549054.0
    dry_mass: 153354.0            # optional, default 0 (engine cut-off floor)
    reference_area: 10.52         # required if drag_coefficient given
    drag_coefficient:             # optional: [airspeed m/s, Cd]
      - [0, 0.30]
      - [340, 0.55]
    contact: {...}                # optional per-craft override of body contact values
    initial_state:                # one of three frames:
      frame: surface              # lat/lon/alt on a body, co-rotating
      body: earth
      latitude: 28.608
      longitude: -80.604          # east positive
      altitude: 0.0
      velocity_enu: [0, 0, 0]     # optional, relative to the surface
    # frame: inertial  -> position, velocity (global)
    # frame: relative  -> body, position, velocity (offsets from the body's centre/velocity)
```

## Command protocol

Send one JSON object per request with a `cmd` field. Every reply contains `ok`
(true/false) and `time`. A failed reply also has `error`, and the server keeps running.
Vectors are `[x, y, z]` lists.

| cmd | Parameters | Reply |
|---|---|---|
| `ping` | — | `pong` |
| `info` | — | scenario path, settings, body and spacecraft ids |
| `step` | `dt` (s, ≥ 0); optional `return_state`, `relative_to` | `events`, `wall_time`, optional `state` |
| `get_state` | optional `relative_to` (body id); `bodies` / `spacecraft`: `true`, `false` or a list of ids | `bodies`, `spacecraft` |
| `get_body` | `id`, optional `relative_to` | `body` |
| `get_spacecraft` | `id`, optional `relative_to` | `spacecraft` |
| `set_thrust` | `id`, `force` (N), `mass_rate` (kg/s, default 0), `frame` (`inertial` \| `enu`), `body` (required for `enu`) | — |
| `stage` | `id`, `children` (list, ≥ 2), optional default `frame` / `body` | `removed`, `created` |
| `reset` | — | reloads the scenario file, t = 0 |
| `shutdown` | — | server exits |

**`stage` children**: each child has `id` and `mass`, and optionally `delta_v` (velocity
relative to the parent, in `frame`), `frame`, `body`, `dry_mass`, `reference_area`,
`drag_coefficient` (`null` removes it) and `contact`. Area, Cd table and contact are inherited
from the parent unless given. Children start at the parent's position with no thrust. The
parent id may be reused by one child. Mass conservation is not enforced.

**Events** in a `step` reply are `engine_cutoff`, `contact_start` and `contact_end`, each
with `time` and `spacecraft`.

**Spacecraft state** includes `mass`, `position`, `velocity`, the `thrust` settings,
`acceleration` broken down into `gravity` / `drag` / `contact` / `thrust` / `total`, and
`nearest_body`. That last block covers `altitude`, `latitude`, `longitude`, `in_contact`,
`surface_relative_velocity` (inertial axes and ENU), `airspeed`, `density`,
`dynamic_pressure` and `drag_coefficient`.

### Example session (Python)

```python
from sim_client import SimClient
sim = SimClient("tcp://127.0.0.1:5555")
sim.set_thrust("falcon9", [0, 0, 7.607e6], mass_rate=-2750, frame="enu", body="earth")
for _ in range(60):
    sim.step(1.0)
s = sim.get_spacecraft("falcon9")
print(s["nearest_body"]["altitude"], s["nearest_body"]["airspeed"])
sim.stage("falcon9", [
    {"id": "f9_s1", "mass": 25600, "delta_v": [0, 0, -0.5]},
    {"id": "f9_s2", "mass": 127754, "dry_mass": 35084, "reference_area": 10.52,
     "delta_v": [0, 0, 0.5]},
], frame="enu", body="earth")
```

From any other language: open a ZeroMQ `REQ` socket, connect to the endpoint, send the JSON
text, and receive the JSON reply. Each request needs exactly one reply before the next send.

## Web viewer

```
python nbody_sim.py solar_system.yaml       # terminal 1
python viz_server.py                        # terminal 2 -> open http://127.0.0.1:8080/
```

`viz_server.py` is just another ZeroMQ client: it polls `get_state` (default 4 Hz) and serves
the latest state at `/api/state` plus the static page in `viz/`. It never steps the simulation,
so any controller can drive the sim while the page updates live. Options: `--sim <endpoint>`,
`--host`, `--port` (8080), `--rate` (polls/s), `--timeout` (ms). If the sim stops responding,
the page keeps showing the last state and marks it as stale.

The page uses Three.js r160, stored in `viz/vendor/three/` (MIT licence), so it works
offline.
* Everything is true scale. Each object also gets a fixed-size marker and label, and clicking
  a marker or a name in the panel focuses on it.
* Controls: drag to orbit the focused object. The mouse wheel or `+`/`-` zooms in proportion to
  altitude, so you can zoom from AU scale down to a few metres above a surface. `F` resets the
  view.
* Bodies are drawn as exact ray-traced spheres, lit by the Sun, with a lat/lon grid that adds
  finer lines as you zoom in. The prime meridian and equator are highlighted.
* Spacecraft are spheres sized to their reference diameter.
* Ecliptic grid (toggle with the panel checkbox or `E`; the choice is remembered per browser):
  * At solar-system scale: a polar grid in the ecliptic plane centred on the Sun. Rings at
    0.1 / 1 / 10 AU appear as you zoom and carry AU labels. Spokes are every 30°, and the
    vernal-equinox direction (♈, ecliptic longitude 0°, +X) is highlighted in teal.
  * Within about 0.002–0.02 AU of the focus: the grid fades into a local ring around the focused
    object. The ring is parallel to the ecliptic, with a ♈ spoke and an "ecliptic N" axis.
* URL parameters: `?focus=<id>` sets the initial focus. `&dist=<km>` sets the initial camera
  distance from that object's centre.

## Notes and limitations

* Step cost grows with the number of spacecraft substeps. A craft resting on a surface uses
  ~0.02 s substeps (about 5 ms of wall time per simulated second). A craft in low orbit uses
  ~4 s substeps. A single very large `dt` with spacecraft present can take a while.
* Viscous friction cannot hold a craft perfectly still on a rotating body. On Earth it creeps
  toward the equator at about 3 mm/s, driven by the sideways part of the centrifugal force.
* Planet poles and prime meridians come from IAU values. Only Earth's prime meridian is
  tied precisely to a real clock (GMST at J2000).
