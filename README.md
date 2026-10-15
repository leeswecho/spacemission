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
| `meo_mission_server.py`, `meo_autopilot.py`, `launch_vehicles.py`, `meo_mission/` | Mission page: launch a chosen rocket to an Earth orbit, LEO to GTO (see below) |
| `moon_mission_server.py`, `moon_autopilot.py`, `lunar_nav.py`, `engines.py`, `moon_mission/` | Moon mission page: four independent parts of a lunar mission (see below) |

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
    body_interval: 7200.0         # s between body-history samples (for re-framing trails)

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
    model: {vehicle: falcon9, parts: [core, upper, fairing]}  # optional, for viewers (any JSON object)
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
`drag_coefficient` (`null` removes it), `contact` and `model`. Area, Cd table, contact and
model are inherited from the parent unless given. Children start at the parent's position with no thrust. The
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

**Body history.** To let a client draw a whole trail in one frame, the sim also samples every
body's position and velocity every `body_interval` seconds. Interpolated with cubic Hermite
polynomials, this reproduces the Moon's position to within a metre. Samples older than the
oldest trail point are dropped. Pass `body_history: [version, last_seq]` (or `true` for
everything) to `get_trails` to receive `body_history: {version, full, ids, samples, head}`,
where:
* each sample is `[seq, t, positions, velocities]`, with one `[x, y, z]` per body, in `ids` order;
* `head` is the current state, `[t, positions, velocities]`, which brackets the newest trail
  points;
* a new `version` (after a reset) means the client should replace its copy.

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
(about 5 updates per second), waiting for each reply before the next poll. The server keeps
connections open between polls (HTTP/1.1), which saves a network round trip per poll, and closes
a connection after 30 s idle.

The page uses Three.js r160, stored in `viz/vendor/three/` (MIT licence), so it works
offline.
* Everything is true scale. Each object also gets a fixed-size marker and label, and clicking
  a marker or a name in the panel focuses on it.
* **Controls:**
  * **Rotating:** drag to rotate around the focused object freely, in any direction, over the
    poles and beyond (three.js `TrackballControls`). That can leave the horizon tilted; `L`
    levels it again, putting the body's north pole or the craft's local vertical back up.
  * **Zooming:** the mouse wheel or `+`/`-` zooms in proportion to altitude, so you can zoom
    from AU scale down to a few metres above a surface.
  * **Resetting:** `F` resets the view.
* Bodies are drawn as exact ray-traced spheres, lit by the Sun, with a lat/lon grid that adds
  finer lines as you zoom in. The prime meridian and equator are highlighted.
* Earth is drawn with a map of its geography, sampled in the rotating body frame, so
  continents sit under the simulated latitudes and longitudes (LC-39A is on Cape Canaveral).
  The night side stays faintly lit for orientation. The map is `viz/textures/earth.jpg`:
  NASA's Blue Marble Next Generation (December 2004, with topography and bathymetry), resized
  to 4096 × 2048 (about 1.2 MB). It is public domain, courtesy of NASA Earth Observatory.
* The Moon is drawn with `viz/textures/moon.jpg`, from NASA's CGI Moon Kit: the Lunar
  Reconnaissance Orbiter color map, 2019 version, 4096 × 2048, about 1.4 MB. It is courtesy
  of NASA's Scientific Visualization Studio (Ernie Wright, Noah Petro).
  * The sim's lunar coordinates match selenographic ones closely: Earth stays within about 8°
    of longitude 0 as the Moon librates. So the near side faces Earth, and the Moon page's
    landing sites sit on their real terrain.
  * The viewer labels the Moon "moon", although its body id in the sim is `luna`.
    `?focus=moon` and `?focus=luna` both work.
* Other bodies keep a flat colour; the `BODY_TEXTURES` table in `viz/app.js` maps a body id
  to an image, and `DISPLAY_NAMES` maps a body id to the name shown.
* Spacecraft are drawn as simple true-size models built in `viz/models.js`: core, boosters,
  upper stage, ogive fairing (two half shells once jettisoned) and a satellite with solar
  panels, in each rocket's colours. New Glenn's first stage also has its two aft strakes
  and four fins below the interstage. The craft's `model` tag says which rocket and which parts
  it is made of.
  * **Moon page vehicles:** drawn by engine type.
    * A lander is Apollo-style, sized by the cube root of its mass against the 15 t LM:
      gold-foil descent stage on four legs, grey ascent cabin, docking tunnel and thruster
      quads.
    * With Raptor or Raptor Vacuum engines, a lander is a full-size Starship HLS, and the
      tab 2 and 4 vehicles are a plain Starship.
    * Other vehicles are a generic tank with engine bells, and cargo left on the surface is
      a pallet of crates.
  * **Orientation:** while its engines fire, a model points along its thrust, with the bells
    behind. Otherwise it points along its velocity relative to the nearest body: the
    surface-relative velocity low down, blending to the orbital velocity between 50 and
    150 km. A craft at rest stands radially outward from the body.
  * **Fallback:** a craft without a recognised tag is a sphere of its reference diameter.
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
* **Spacecraft trails** (toggle with the checkbox or `T`): each craft's recorded trajectory,
  fading with age.
  * **One frame per trail:** each trail is drawn whole in one frame. Every point is
    re-expressed, using the body history, relative to one body as it was when the point was
    recorded, and drawn around that body's current position. So a trail stays continuous
    when a craft crosses from one sphere of influence into another.
  * **Choosing the frame:** "Trails drawn relative to" offers Auto or any body. Auto uses the
    focused body, or for a focused craft the body whose sphere of influence it is in (the same
    body the body grid shows).
  * **What it looks like:** in Earth's frame, a lunar transfer is an arc out to where the Moon
    was, and the time in lunar orbit shows as loops strung along the Moon's path. In the
    Moon's frame, the approach curves into a closed lunar orbit.
  * **Caching:** `viz_server.py` keeps cached copies of the trails and the body history and
    updates them incrementally, so the sim's load doesn't grow with the number of open
    browsers.
* URL parameters: `?focus=<id>` sets the initial focus. `&dist=<km>` sets the initial camera
  distance from that object's centre.

## Mission page: rocket to Earth orbit

```
python meo_mission_server.py                # -> open http://127.0.0.1:8081/
```

A page built for one mission. It reads as a questionnaire:
* **Rocket:** Falcon 9 Block 5, Atlas V 551, Vulcan Centaur VC4, Delta IV Heavy, New Glenn,
  New Glenn 9×4, SLS Block 1 or Starship (`launch_vehicles.py`; approximate public figures).
  The figures for New Glenn 9×4 and Starship are estimates, since both are in development.
  Both of their first stages are expended here, and Starship carries its payload inside.
  * **Editable figures:** for every stage, dry mass, propellant, sea-level and vacuum thrust,
    and propellant flow. Also the number of boosters, Delta IV Heavy's core throttle while
    its side cores burn, the fairing mass and the pitch-kick angle. The page shows each
    stage's Isp and burn time, and picking a rocket restores its defaults.
* **Orbit:** perigee and apogee altitude, anywhere from the 200 km parking orbit up to
  40,000 km, and the inclination (28.7°–151.3° from the Cape, since there are no plane
  changes). There are presets for the ISS, Starlink, a polar orbit, a sun-synchronous orbit,
  GPS, Galileo, GLONASS, a geostationary transfer orbit (GTO) and a Molniya orbit.
* **Payload mass, playback speed,** and whether to reset the shared simulation first.

As you type, the page validates the answers. It also shows a rough feasibility estimate: liftoff mass and thrust-to-weight, the ideal Δv of each stage against the Δv needed (with about 1.7 km/s of ascent losses assumed), the margin, and the largest payload this orbit allows.

**Launch** starts the autopilot (`meo_autopilot.py`) inside the mission server. It flies the
mission with ordinary sim commands, so it keeps flying if the page is closed, and every open
page sees the same flight. Only one mission runs at a time. The page shows the phase checklist,
live telemetry, the flight log, and finally target against achieved orbit.
* **Pause / Resume** (`POST /api/pause` with `{"paused": true|false}`) stops the autopilot
  stepping the simulation, so simulated time stands still, engines included, until you resume.
  Both events go into the flight log. The Moon page has the same button.
* **Abort** shuts the engines down immediately, paused or not.

Once a mission has ended (succeeded, failed or aborted), **Download command log**
(`GET /api/mission/commands.txt`) saves a plain-text report. It covers the setup, the outcome,
the flight log, and every control command the autopilot sent to the simulation, with its
mission and simulation time and a plain-English description: reset, craft added or removed,
each thrust setting with direction and propellant flow, and every staging.

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
   inclination. When the target perigee is the parking altitude (200 km circular, or GTO),
   the transfer orbit already is the target orbit and this step is skipped.
7. **Payload separation:** the spent stage, which keeps the rocket's ID, backs away. The result
   is then scored. Success means within max(5 km, 1 %) on both altitudes and within 0.5° on
   inclination.
8. **One full orbit:** after a success, the payload coasts for one orbital period of its
   final orbit, so the orbit can be seen closing in the viewer. It plays at the coasting
   speed of the chosen pace, so ×5 makes a 12-hour GPS orbit take 2.4 hours. An abort only
   cuts this coast short; the mission still counts as succeeded.

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
| New Glenn 9×4 | within ~1 km and 0.04° | ~11.7 t |
| Starship | within ~1.1 km and 0.04° | ~8.0 t |

Also tested with a 4,300 kg payload, all within tolerance:
* **All six rockets:** an ISS-like orbit (420 km circular, 51.6°), 200 km circular at 28.7°
  and GTO (200 × 35,786 km at 28.7°).
* **Falcon 9:** also 200 × 1,000 km at 45°, sun-synchronous (700 km, 98.2°) and Molniya
  (500 × 39,800 km, 63.4°).
* **Heavier payloads:** SLS with 60 t and New Glenn with 40 t to the ISS-like orbit, and Delta IV Heavy
  with 20 t to sun-synchronous orbit. SLS's core stage alone reaches a 200 km orbit.
* **New Glenn 9×4:** 80 t to 200 km circular, 60 t to the ISS-like orbit and 25 t to GTO;
  with 4.3 t, also the ISS-like orbit, 200 km circular and GTO.
* **Starship:** 150 t to 200 km circular, 100 t to the ISS-like orbit, 50 t to sun-synchronous
  orbit, 30 t to GTO and 10 t to a Molniya orbit. With no payload (a tanker flight for the
  Moon page) it leaves about 214 t of propellant in a 200 km orbit.

Orientation is not controlled: the argument of perigee (where the apogee lies, which matters for
Molniya orbits) and the orbit's node follow from the launch time. Equatorial orbits and GEO
would need plane changes, which the autopilot does not do; GTO is as far as it goes.

Other tests:
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

**Glossary.** On both mission pages, known acronyms and jargon are underlined with dots. Hover
over one for a short plain-English explanation, most with a link to the Wikipedia article.
Examples: LEO, TLI, Isp, Δv, LC-39A, BE-4, LH2, perigee.
* **Where it lives:** `web_shared/glossary.js`, served to both pages at `/shared/glossary.js`.
* **What gets marked:** text the page adds later too, such as the flight log, results and
  rocket summaries.
* **Left alone:** buttons, form fields, dropdown options and the live telemetry tiles.

Options: `--sim <endpoint>`, `--host`, `--port` (8081), `--timeout` (ms), `--viewer-url`,
`--other-mission` (the Moon page's `/api/mission`, default `http://127.0.0.1:80/api/mission`).
The two mission pages share one simulation, so neither launches while the other has a mission
in flight.

## Mission page: Moon mission in four parts

```
python moon_mission_server.py               # -> open http://127.0.0.1/
```

For design iteration, a lunar mission is split into four parts that are flown independently,
one tab each. Every tab is a questionnaire with a live feasibility estimate (Δv, propellant and
burn time per burn, propellant left at the end). After a successful flight, buttons copy what
was delivered into the next tab's starting point:

1. **Stage in Earth orbit:** a rocket launch exactly as on the Earth-orbit page. The result
   reports the delivered payload and the propellant still in the upper stage. With 0 kg of
   payload, it's a tanker flight. "Add to tab 2's vehicle" adds the delivered payload and
   propellant to tab 2's vehicle, so several launches can build one.
2. **Transfer to the Moon:** a vehicle assembled in Earth orbit flies to a lunar orbit.
   * **Inputs:** the starting Earth orbit, the payload (everything that isn't propellant,
     engines included), the propellant, the engines, and the target lunar orbit (perilune,
     apolune, inclination to the lunar equator).
   * **Flight:** the autopilot places the vehicle in its starting orbit at the point and
     orientation that suit the departure. It burns for the Moon (trans-lunar injection), makes
     up to three course corrections, brakes into lunar orbit at perilune, and trims.
3. **Land and return to orbit:** part of a vehicle in lunar orbit becomes a lander (a
   percentage of the payload and of the propellant).
   * **Inputs:** the lander's engines (the same engines fly the descent and the ascent), the
     landing site (presets: Apollo 11, 15 and 17, Chang'e 4, the south pole), the payload and
     propellant to leave on the surface, and a minimum stay.
   * **Flight:**
     1. The orbit is placed so that it passes over the site.
     2. The lander separates and lowers its low point to 15 km.
     3. It flies powered descent: braking guidance to 1 km above the site, an approach to
        30 m, then a slow vertical descent to touchdown.
     4. It unloads cargo (left behind as `surface_cargo`).
     5. It lifts off, either when the site next passes under the orbit's plane (Apollo-style,
        possibly up to about two weeks later) or right after the minimum stay into an orbit of
        the same altitude and inclination.
     6. It climbs back to the starting orbit's perilune and apolune.
   * **Result:** the payload and propellant in lunar orbit afterwards (the lander plus what
     stayed in orbit), what was left on the surface, and how accurate and gentle the landing
     was. The lander doesn't rendezvous with the part that stayed in orbit.
4. **Return to Earth:** a vehicle in lunar orbit flies back to an Earth orbit.
   * **Flight:** trans-Earth injection, course corrections, and a burn at perigee into the
     target orbit (engines only, no aerobraking), then trims.
   * **Result:** the payload and propellant left in Earth orbit.

How it works:
* **Targeting (`lunar_nav.py`):** a fast predictor mirrors the simulator's gravity. It
  integrates the bodies with the sim's RK4 from a snapshot of the sim, and the craft under the
  Sun, Earth and Moon. Over a 3–5 day coast it agrees with the sim to about a kilometre.
  * **Arrival at the Moon** is targeted in the B-plane (perilune radius and inclination).
  * **Arrival at Earth** is targeted by perigee radius and inclination.
  * Both use Newton iteration on impulsive velocity changes.
  * Long burns follow the planned trajectory's energy, plane and periapsis direction; the
    course corrections clean up afterwards.
* **Engines (`engines.py`):** Apollo LM descent and ascent engines, the Apollo service-module
  engine, AJ10-190, RL10B-2, RL10C-1, the RL10 deep-throttle (CECE) demonstrator, BE-7, J-2,
  Merlin Vacuum, Raptor Vacuum and Raptor 2.
  * Each engine has thrust, Isp, mass and lowest throttle setting. Approximate public figures;
    engines in development are estimates.
  * All four figures can be edited, or entered for a custom engine.
  * Thrust can go as low as one engine at its lowest throttle (the others shut down). A lander
    must be able to hover, so its lowest thrust must be below its weight on the Moon.
* **Pacing:** Watchable (×5 in burns, ×3,000 while coasting: a few minutes per part, longer
  for a long surface wait), Fast (×50 / ×50,000) or as fast as possible.
* **Command log:** each flight's control commands download as a text report, as on the
  Earth-orbit page.
* **One full orbit:** as on the Earth-orbit page, each successful part ends by coasting one
  orbital period of the final orbit (in tab 3, the lander's) to show it.

Tested, all within tolerance at "as fast as possible":
* **Transfer:** 39 t (two RL10B-2) from a 200 km parking orbit to a 100 km polar lunar orbit,
  arriving 100.0 × 100.1 km at 90.00°. 1,141 kg of propellant left; the estimate was 1,227 kg.
* **Landings** at Apollo 11, Apollo 17 and the south pole, each from a different orbit and
  with a different engine set (RL10 CECE, BE-7, Apollo descent engines). Each landed within
  10 m of the site at under 1 m/s, and was back in its starting orbit within 1 km.
* **Return:** from a 100 km lunar orbit to 400 km at 51.6°, arriving 400.0 × 400.0 km at 51.60°.

Limitations:
* The Moon is a smooth sphere; landing sites have no terrain.
* The sim's lunar coordinates only approximately match selenographic ones.
* The Moon's equator lies in the ecliptic in this scenario.
* There is no rendezvous or docking.
* Burns stay at full thrust apart from landing; there are no crew acceleration limits.

Options: `--sim <endpoint>`, `--host`, `--port` (80), `--timeout` (ms), `--viewer-url`,
`--other-mission` (default `http://127.0.0.1:8081/api/mission`).

Port 80 is privileged on Linux: run as a systemd service with
`AmbientCapabilities=CAP_NET_BIND_SERVICE`, or pick another port with `--port` for local runs.

## Mission order log

For teaching, both mission servers can keep a record of every launch press. Pass
`--log-dir <folder>` and each press of a launch button writes one readable JSON file there:
`<UTC time>_<page>-<part>.json`, e.g. `2026-10-13T06-42-01Z_moon-transfer.json`. Each file
holds:

* **When and where:** `ordered_at_utc`, `page` (`earth-orbit` or `moon`) and `part` (the
  Moon page's tab; `launch` on the Earth-orbit page).
* **The request:** exactly what the page sent.
* **For an accepted launch:** the validated `settings` (rocket or engine figures included) and
  the pre-flight `estimate`.
* **Its outcome,** added when the mission ends: succeeded, failed or aborted, the message, the
  result (orbit reached, payload and propellant delivered), the checklist, and the flight log.
* **For a refused press,** `_rejected` in the file name and the `rejection_reasons` instead:
  invalid inputs, or a mission already flying.

A whole mission's file is a few KB. Nothing is cleaned up automatically. The log is never
served over HTTP: fetch it from the server, e.g.
`scp -i <key> 'ubuntu@<server>:/var/lib/spacemission/mission-orders/*.json' .`. On the
deployed server the files go to `/var/lib/spacemission/mission-orders/` (systemd
`StateDirectory`, since the services can't write to the home directory). Without
`--log-dir`, nothing is logged.

## Notes and limitations

* Step cost grows with the number of spacecraft substeps. A craft in low orbit uses ~4 s
  substeps. A craft settling onto a surface uses ~0.02 s substeps until it falls asleep.
* Viscous friction alone can't hold a craft perfectly still on a rotating body. Sleeping pins
  it once it is nearly stationary, which stops the slow creep toward the equator.
* Planet poles and prime meridians come from IAU values. Only Earth's prime meridian is
  tied precisely to a real clock (GMST at J2000).
