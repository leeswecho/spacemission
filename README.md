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
| `meo_mission_server.py`, `meo_autopilot.py`, `launch_vehicles.py`, `meo_mission/` | Mission page: launch a chosen rocket to a medium Earth orbit (see below) |

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
* **Sleeping**: a craft resting on a surface with no thrust, still to within 5 cm/s for 2 s,
  is put to sleep. It is pinned to the rotating surface and no longer integrated, so parked
  craft cost nothing. `set_thrust` (with nonzero thrust or mass flow) and `set_mass` wake it.

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
  trails:                         # trajectory history per spacecraft
    max_points: 5000              # older history is thinned beyond this
    min_spacing: 20.0             # m
    altitude_fraction: 0.1        # point spacing <= this * altitude, but >= min_spacing...
    radius_fraction: 0.02         # ...and <= this * distance from the reference body

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
| `set_mass` | `id`, `mass` (kg), optional `dry_mass` (kg) | — |
| `set_thrust` | `id`, `force` (N), `mass_rate` (kg/s, default 0), `frame` (`inertial` \| `enu`), `body` (required for `enu`) | — |
| `stage` | `id`, `children` (list, ≥ 2), optional default `frame` / `body` | `removed`, `created` |
| `get_trails` | optional `since` (`{id: [version, seq]}`), `ids` | `trails` (see below) |
| `add_spacecraft` | `spacecraft`: a spec exactly like a scenario file's `spacecraft` entry | `spacecraft` (id) |
| `remove_spacecraft` | `id` | `removed` |
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
`dynamic_pressure` and `drag_coefficient`. A `trail` block gives the craft's current
`reference_body`, its trail point count and trail `version`.

**Trails.** Each spacecraft records its trajectory as it is integrated, so even a single
1-day step traces the full path.
* **Reference body:** each point is stored relative to the body whose sphere of influence
  holds the craft at that moment, in non-rotating axes: Earth in LEO, the Moon in lunar
  orbit, the Sun in interplanetary cruise. Re-drawn around that body's current position, an
  orbit closes into an ellipse.
* **Point spacing:** finer near surfaces and coarser far away (see `simulation.trails`).
  Beyond `max_points`, the older half of the history is thinned out.
* **Resting:** nothing is recorded while a craft rests on a surface, which would otherwise
  sweep a latitude circle in this frame. Leaving the surface starts a new segment.
* **Staging:** children inherit the parent's trail, so each stage's path is continuous
  from the pad.

`get_trails` returns `{id: {version, full, resting, points}}`. Each point is
`[seq, t, body_id, x, y, z, brk]`: position in metres relative to `body_id`'s centre, with
`brk = 1` marking the start of a new segment. `seq` increases monotonically. Passing
`since = {id: [version, last_seq]}` returns only newer points (`full: false`). A `version`
change (from thinning, staging or reset) means the client should replace its copy (`full: true`).

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

`viz_server.py` is just another ZeroMQ client: it polls `get_state` (default 10 Hz) and serves
the latest state at `/api/state` plus the static page in `viz/`. It never steps the simulation,
so any controller can drive the sim while the page updates live. Options: `--sim <endpoint>`,
`--host`, `--port` (8080), `--rate` (polls/s), `--timeout` (ms). If the sim stops responding,
the page keeps showing the last state and marks it as stale. The page polls every 200 ms
(about 5 updates per second).

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
  a polar grid in the ecliptic plane centred on the Sun. Rings at 0.1 / 1 / 10 AU appear as
  you zoom and carry AU labels. Spokes are every 30°, and the vernal-equinox direction (♈,
  ecliptic longitude 0°, +X) is highlighted in teal. It fades out within about 0.002–0.02 AU
  of the focus.
* Body grid (toggle with the checkbox or `G`): a polar grid in the equatorial plane of the
  focused body, or, when a spacecraft is focused, of its reference body (the smallest sphere
  of influence containing it). Rings are distances from the body's centre in km, at powers of
  ten that follow the zoom. Spokes are every 30°, with the equator's ascending node on the
  ecliptic highlighted in teal (the vernal equinox ♈ for Earth), and an axis marks the north
  pole. The grid fades out as the camera leaves the sphere of influence, so the Sun has none.
* Spacecraft trails (toggle with the checkbox or `T`): each craft's recorded trajectory,
  drawn around its reference body's current position and fading with age. `viz_server.py`
  keeps a cached copy that it updates incrementally, so the sim's load doesn't grow with the
  number of open browsers.
* URL parameters: `?focus=<id>` sets the initial focus. `&dist=<km>` sets the initial camera
  distance from that object's centre.

## Mission page: rocket to medium Earth orbit

```
python meo_mission_server.py                # -> open http://127.0.0.1:8081/
```

A page built for one mission. It reads as a questionnaire:
* **Rocket:** Falcon 9 Block 5, Atlas V 551, Vulcan Centaur VC4, Delta IV Heavy, New Glenn or
  SLS Block 1 (`launch_vehicles.py`; approximate public figures).
  * **Editable figures:** for every stage, dry mass, propellant, sea-level and vacuum thrust,
    and propellant flow. Also the number of boosters, Delta IV Heavy's core throttle while
    its side cores burn, the fairing mass and the pitch-kick angle. The page shows each
    stage's Isp and burn time, and picking a rocket restores its defaults.
* **Orbit:** perigee and apogee altitude, which must lie within MEO (2,000–35,786 km), and
  the inclination. There are presets for GPS, Galileo and GLONASS.
* **Payload mass, playback speed,** and whether to reset the shared simulation first.

As you type, the page validates the answers. It also shows a rough feasibility estimate: liftoff mass and thrust-to-weight, the ideal Δv of each stage against the Δv needed (with about 1.7 km/s of ascent losses assumed), the margin, and the largest payload this orbit allows.

**Launch** starts the autopilot (`meo_autopilot.py`) inside the mission server. It flies the
mission with ordinary sim commands, so it keeps flying if the page is closed, and every open
page sees the same flight. Only one mission runs at a time. The page shows the phase checklist,
live telemetry, the flight log, and finally target against achieved orbit. **Abort** shuts the
engines down immediately.

Flight profile:
1. **Configure:** whatever stands on the LC-39A pad is removed (`remove_spacecraft`), and the
   rocket is placed there with `add_spacecraft` under its own ID (`falcon9`, `atlas_v_551`,
   `vulcan`, `delta_iv_heavy`, `new_glenn`, `sls`), its masses and its drag area.
2. **Gravity turn:** vertical rise, a pitch kick at 60 m/s (per rocket, 4–15°), then a gravity
   turn along the launch azimuth for the target inclination (corrected for Earth's rotation).
   It lasts until 45 km, or earlier if the flight path gets shallower than 15°.
3. **Boosters:** they burn alongside the first stage from liftoff and are released together at
   burnout as `<id>_boosters`.
4. **Closed-loop ascent:** every few seconds the guidance flies the rest of the burn in a 2D
   predictor. That covers booster burnout, the first stage, the staging coast, the upper stage,
   the fairing jettison and throttling. It then solves for a linear-tangent pitch program that
   reaches 200 km with zero vertical speed exactly at the transfer orbit's perigee speed. The
   velocity normal to the target plane is nulled at the same time.
   * **Separations:** the first stage (`<id>_s1`) is released at depletion, and the fairing
     above 110 km.
   * **Throttling:** liquid stages throttle to stay under 4 g.
5. **Transfer burn:** the ascent flows straight into a transfer orbit whose apogee is the target
   apogee. A first stage that is still attached is dropped afterwards.
6. **Apogee:** the upper stage coasts to apogee, then burns to raise the perigee and trim the
   inclination.
7. **Payload separation:** the spent stage, which keeps the rocket's ID, backs away. The result
   is then scored. Success means within max(50 km, 1 %) on both altitudes and within 0.5° on
   inclination.

Tested with a 4,300 kg payload to the GPS orbit (20,180 km circular, 55°), at default
settings:

| Rocket | Result | Upper-stage propellant left |
|---|---|---|
| Falcon 9 | within ~1 km and 0.04° | ~950 kg |
| Atlas V 551 | within ~0.4 km and 0.06° | ~1.6 t |
| Vulcan VC4 | within ~0.3 km and 0.07° | ~4.5 t |
| Delta IV Heavy | within ~0.7 km and 0.06° | ~4.3 t |
| New Glenn | within ~0.3 km and 0.04° | ~3.2 t |
| SLS Block 1 | within ~0.8 km and 0.08° | ~5.6 t |

Also tested:
* **Successful edits:**
  * Vulcan with six boosters and 10 t of payload.
  * Atlas V with no boosters (an "Atlas V 401") and 2 t.
  * Delta IV Heavy with a 12° kick, and SLS with a 15° kick (both switch to closed-loop
    guidance early).
* **Expected failures:**
  * Vulcan with no boosters is rejected before launch (liftoff thrust-to-weight below 1.05).
  * SLS with 30 t runs out of propellant, as its guidance warns at booster separation.

The Δv estimate is rough. It is fairly close for Falcon 9, Atlas V and New Glenn. It is
optimistic for rockets whose first stage burns long and high, so for SLS it overstates the
payload limit.

Options: `--sim <endpoint>`, `--host`, `--port` (8081), `--timeout` (ms), `--viewer-url`.

## Notes and limitations

* Step cost grows with the number of spacecraft substeps. A craft in low orbit uses ~4 s
  substeps. A craft settling onto a surface uses ~0.02 s substeps until it falls asleep.
* Viscous friction alone can't hold a craft perfectly still on a rotating body. Sleeping pins
  it once it is nearly stationary, which stops the slow creep toward the equator.
* Planet poles and prime meridians come from IAU values. Only Earth's prime meridian is
  tied precisely to a real clock (GMST at J2000).
