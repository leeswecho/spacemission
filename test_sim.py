#!/usr/bin/env python3
"""Tests for nbody_sim.py.  Run:  python -m unittest test_sim -v"""

import math
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import yaml

from nbody_sim import BodyTrajectory, Simulation, SimError

HERE = Path(__file__).resolve().parent
SCENARIO = HERE / "solar_system.yaml"
DAY = 86400.0


def make_scenario(spacecraft, tmpdir):
    """Write a copy of the example scenario with a different spacecraft list."""
    cfg = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    cfg["spacecraft"] = spacecraft
    path = Path(tmpdir) / "scenario.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def total_energy(sim):
    ke = 0.5 * np.sum([b.mass for b in sim.bodies] * np.sum(sim.bvel**2, axis=1))
    pe = 0.0
    masses = np.array([b.mass for b in sim.bodies])
    for i in range(len(masses)):
        for j in range(i + 1, len(masses)):
            pe -= sim.G * masses[i] * masses[j] / np.linalg.norm(sim.bpos[i] - sim.bpos[j])
    return ke + pe


class PhysicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def sim_with(self, spacecraft):
        return Simulation(make_scenario(spacecraft, self.tmp.name))

    def rel(self, sim, sc_id, body_id):
        b = sim.body_by_id[body_id].index
        sc = sim.spacecraft[sc_id]
        return sc.pos - sim.bpos[b], sc.vel - sim.bvel[b]

    def test_planets_one_year(self):
        sim = self.sim_with([])
        e0 = total_energy(sim)
        earth, sun, luna = (sim.body_by_id[n].index for n in ("earth", "sun", "luna"))
        start = sim.bpos[earth] - sim.bpos[sun]
        moon_dist = []
        for _ in range(365):
            sim.step(DAY)
            moon_dist.append(np.linalg.norm(sim.bpos[luna] - sim.bpos[earth]))
        sim.step(0.256363 * DAY)  # complete one sidereal year
        drift = abs((total_energy(sim) - e0) / e0)
        self.assertLess(drift, 1e-9)
        err = np.linalg.norm(sim.bpos[earth] - sim.bpos[sun] - start)
        self.assertLess(err, 5e8, "Earth should return near its start after a sidereal year")
        self.assertGreater(min(moon_dist), 3.5e8)
        self.assertLess(max(moon_dist), 4.1e8)

    def test_circular_orbit_closes(self):
        gm = 398600.435507e9
        r = 6.378137e6 + 400e3
        v = math.sqrt(gm / r)
        sim = self.sim_with([{
            "id": "sat", "mass": 1000.0,
            "initial_state": {"frame": "relative", "body": "earth",
                              "position": [r, 0, 0], "velocity": [0, v, 0]},
        }])
        period = 2 * math.pi * math.sqrt(r**3 / gm)
        p0, _ = self.rel(sim, "sat", "earth")
        sim.step(period)
        p1, _ = self.rel(sim, "sat", "earth")
        self.assertLess(np.linalg.norm(p1 - p0), 5e3)
        self.assertAlmostEqual(np.linalg.norm(p1), r, delta=2e3)

    def test_pad_rest(self):
        sim = Simulation(SCENARIO)
        for _ in range(6):
            sim.step(10.0)
        nb = sim.spacecraft_state("falcon9")["nearest_body"]
        g = 398600.435507e9 / 6.378137e6**2
        w = 2 * math.pi * 2.0
        self.assertTrue(nb["in_contact"])
        self.assertAlmostEqual(-nb["altitude"], g / w**2, delta=0.005)
        self.assertLess(nb["airspeed"], 0.01)

    def test_lunar_drop_comes_to_rest(self):
        sim = self.sim_with([{
            "id": "lander", "mass": 2000.0,
            "initial_state": {"frame": "surface", "body": "luna",
                              "latitude": 0.0, "longitude": 0.0, "altitude": 100.0},
        }])
        events = []
        for _ in range(30):
            events += sim.step(1.0)
        self.assertIn("contact_start", [e["event"] for e in events])
        nb = sim.spacecraft_state("lander")["nearest_body"]
        self.assertEqual(nb["id"], "luna")
        self.assertTrue(nb["in_contact"])
        self.assertLess(nb["airspeed"], 0.01)
        self.assertLess(-nb["altitude"], 0.1)

    def test_drag_decays_orbit(self):
        gm = 398600.435507e9
        r = 6.378137e6 + 150e3
        v = math.sqrt(gm / r)
        base = {"mass": 100.0, "reference_area": 1.0,
                "initial_state": {"frame": "relative", "body": "earth",
                                  "position": [r, 0, 0], "velocity": [0, v, 0]}}
        sim = self.sim_with([dict(base, id="dragged", drag_coefficient=[[0, 2.2]]),
                             dict(base, id="vacuum")])
        for _ in range(10):
            sim.step(300.0)
        _, v_drag = self.rel(sim, "dragged", "earth")
        _, v_vac = self.rel(sim, "vacuum", "earth")
        e_drag = sim.spacecraft_state("dragged", "earth")
        e_vac = sim.spacecraft_state("vacuum", "earth")
        self.assertLess(np.linalg.norm(e_drag["acceleration"]["drag"]), 1.0)
        self.assertGreater(np.linalg.norm(e_drag["acceleration"]["drag"]), 0.0)
        self.assertEqual(np.linalg.norm(e_vac["acceleration"]["drag"]), 0.0)

        def energy(sc_id):
            p, vel = self.rel(sim, sc_id, "earth")
            return 0.5 * vel @ vel - gm / np.linalg.norm(p)
        self.assertLess(energy("dragged"), energy("vacuum") - 1e3)

    def test_thrust_cutoff_at_dry_mass(self):
        sim = Simulation(SCENARIO)
        sim.set_thrust("falcon9", [0, 0, 7.607e6], -2750.0, "enu", "earth")
        events = []
        for _ in range(20):
            events += sim.step(10.0)
        cut = [e for e in events if e["event"] == "engine_cutoff"]
        self.assertEqual(len(cut), 1)
        self.assertAlmostEqual(cut[0]["time"], (549054.0 - 153354.0) / 2750.0, places=6)
        sc = sim.spacecraft["falcon9"]
        self.assertEqual(sc.mass, 153354.0)
        self.assertFalse(sc.thrust_active)

    def test_staging(self):
        sim = Simulation(SCENARIO)
        sim.set_thrust("falcon9", [0, 0, 7.607e6], -2750.0, "enu", "earth")
        sim.step(30.0)
        parent = sim.spacecraft["falcon9"]
        pos, vel = parent.pos.copy(), parent.vel.copy()
        created = sim.stage("falcon9", [
            {"id": "s1", "mass": 30000.0, "delta_v": [0, 0, -1.0]},
            {"id": "s2", "mass": 100000.0, "dry_mass": 20000.0, "delta_v": [0, 0, 1.0],
             "reference_area": 5.0, "drag_coefficient": None},
        ], frame="enu", body="earth")
        self.assertEqual(created, ["s1", "s2"])
        self.assertNotIn("falcon9", sim.spacecraft)
        s1, s2 = sim.spacecraft["s1"], sim.spacecraft["s2"]
        up = (pos - sim.bpos[sim.body_by_id["earth"].index])
        up /= np.linalg.norm(up)
        np.testing.assert_allclose(s1.pos, pos)
        np.testing.assert_allclose(s1.vel, vel - up, atol=1e-9)
        np.testing.assert_allclose(s2.vel, vel + up, atol=1e-9)
        self.assertIsNotNone(s1.cd_table)  # inherited
        self.assertIsNone(s2.cd_table)     # explicitly removed
        self.assertFalse(s1.thrust_active or s2.thrust_active)
        with self.assertRaises(SimError):
            sim.stage("s1", [{"id": "s2", "mass": 1.0}, {"id": "x", "mass": 1.0}])

    def test_trail_orbit(self):
        gm = 398600.435507e9
        r = 6.378137e6 + 400e3
        sim = self.sim_with([{
            "id": "sat", "mass": 1000.0,
            "initial_state": {"frame": "relative", "body": "earth",
                              "position": [r, 0, 0], "velocity": [0, math.sqrt(gm / r), 0]},
        }])
        period = 2 * math.pi * math.sqrt(r**3 / gm)
        for _ in range(10):
            sim.step(period / 10)
        pts = sim.spacecraft["sat"].trail.points
        earth = sim.body_by_id["earth"].index
        self.assertTrue(all(p[2] == earth for p in pts))
        self.assertTrue(100 < len(pts) < 3000, len(pts))
        self.assertEqual([p[0] for p in pts], list(range(len(pts))))
        radii = [math.hypot(*p[3:6]) for p in pts]
        self.assertAlmostEqual(min(radii), r, delta=5e3)
        self.assertAlmostEqual(max(radii), r, delta=5e3)
        self.assertLess(math.dist(pts[0][3:6], pts[-1][3:6]), 0.05 * r)  # closed loop

        # Incremental fetch returns only newer points; a wrong version forces a full copy.
        full = sim.trails()["sat"]
        sim.step(60.0)
        delta = sim.trails(since={"sat": [full["version"], full["points"][-1][0]]})["sat"]
        self.assertFalse(delta["full"])
        self.assertTrue(delta["points"])
        self.assertTrue(all(p[0] > full["points"][-1][0] for p in delta["points"]))
        self.assertEqual(delta["points"][0][2], "earth")
        self.assertTrue(sim.trails(since={"sat": [-1, 0]})["sat"]["full"])

    def test_body_history(self):
        sim = Simulation(SCENARIO)
        h = sim.body_history
        dt = h.interval
        for _ in range(5):
            sim.step(dt * 0.75)  # sample times don't line up with step ends
        times = [x[1] for x in h.samples]
        self.assertEqual(times, [k * dt for k in range(len(times))])
        self.assertEqual(len(times), int(5 * 0.75) + 1)

        # Hermite interpolation of the samples reproduces the Moon relative to Earth.
        earth, luna = sim.body_by_id["earth"].index, sim.body_by_id["luna"].index
        a, b = h.samples[-2], h.samples[-1]
        traj = BodyTrajectory(a[1], dt, np.array([a[2], b[2]]), np.array([a[3], b[3]]))
        mid = a[1] + 0.37 * dt
        P, _ = traj.at(mid)
        ref = Simulation(SCENARIO)
        ref.step(mid)
        self.assertLess(np.linalg.norm((P[luna] - P[earth]) - (ref.bpos[luna] - ref.bpos[earth])), 1.0)

        # Incremental fetch, version change on reset, pruning to the oldest trail point.
        full = sim.body_history_since(None)
        self.assertTrue(full["full"])
        self.assertEqual(full["ids"][0], "sun")
        self.assertEqual(full["head"][0], sim.time)
        sim.step(dt)
        delta = sim.body_history_since([full["version"], full["samples"][-1][0]])
        self.assertFalse(delta["full"])
        self.assertEqual(len(delta["samples"]), 1)
        sim.remove_spacecraft("falcon9")  # its trail started at t = 0
        sim.add_spacecraft({"id": "probe", "mass": 1.0, "initial_state": {
            "frame": "relative", "body": "earth", "position": [7e6, 0, 0], "velocity": [0, 7546, 0]}})
        sim.step(dt)
        self.assertLessEqual(h.samples[1][1], sim.spacecraft["probe"].trail.points[0][1] + dt)
        self.assertLessEqual(h.samples[0][1], sim.spacecraft["probe"].trail.points[0][1])
        version = h.version
        sim.load()
        self.assertNotEqual(sim.body_history.version, version)
        self.assertEqual(len(sim.body_history.samples), 1)

    def test_trail_reference_bodies(self):
        sim = Simulation(SCENARIO)
        sun, earth, luna = (sim.body_by_id[n].index for n in ("sun", "earth", "luna"))
        self.assertEqual(sim._reference_body(sim.bpos[earth] + [7e6, 0, 0], sim.bpos), earth)
        self.assertEqual(sim._reference_body(sim.bpos[luna] + [2e6, 0, 0], sim.bpos), luna)
        self.assertEqual(sim._reference_body(sim.bpos[earth] + [5e9, 0, 0], sim.bpos), sun)
        self.assertIsNone(sim.body_state("sun")["sphere_of_influence"])
        self.assertAlmostEqual(sim.body_state("earth")["sphere_of_influence"] / 9.25e8, 1.0, delta=0.05)

    def test_trail_resting_and_staging(self):
        sim = Simulation(SCENARIO)
        sc = sim.spacecraft["falcon9"]
        for _ in range(5):
            sim.step(10.0)
        self.assertTrue(sc.trail.resting)
        self.assertLessEqual(len(sc.trail.points), 2)  # nothing recorded while parked
        sim.set_thrust("falcon9", [0, 0, 7.607e6], -2750.0, "enu", "earth")
        sim.step(60.0)
        n = len(sc.trail.points)
        self.assertGreater(n, 10)
        self.assertFalse(sc.trail.resting)
        self.assertTrue(any(p[6] for p in sc.trail.points[1:]))  # liftoff starts a new segment
        sim.stage("falcon9", [{"id": "a", "mass": 1000.0}, {"id": "b", "mass": 2000.0}])
        a, b = sim.spacecraft["a"].trail, sim.spacecraft["b"].trail
        self.assertEqual(a.points[:n], sc.trail.points[:n])
        self.assertEqual(len(a.points), len(b.points))
        self.assertNotEqual(a.version, b.version)

    def test_trail_thinning(self):
        tmp = make_scenario([{
            "id": "sat", "mass": 1000.0,
            "initial_state": {"frame": "relative", "body": "earth",
                              "position": [6.778137e6, 0, 0], "velocity": [0, 7668.6, 0]},
        }], self.tmp.name)
        cfg = yaml.safe_load(tmp.read_text(encoding="utf-8"))
        cfg["simulation"]["trails"] = {"max_points": 50}
        tmp.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        sim = Simulation(tmp)
        v0 = sim.spacecraft["sat"].trail.version
        sim.step(5400.0)
        tr = sim.spacecraft["sat"].trail
        self.assertLessEqual(len(tr.points), 50)
        self.assertNotEqual(tr.version, v0)
        seqs = [p[0] for p in tr.points]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(seqs[-1], tr.next_seq - 1)

    def test_bad_input(self):
        sim = Simulation(SCENARIO)
        with self.assertRaises(SimError):
            sim.set_thrust("nope", [0, 0, 0])
        with self.assertRaises(SimError):
            sim.set_thrust("falcon9", [0, 0], 0)
        with self.assertRaises(SimError):
            sim.set_thrust("falcon9", [0, 0, 1], 0, "enu")
        with self.assertRaises(SimError):
            sim.step(-1)


class IpcTest(unittest.TestCase):
    ENDPOINT = "tcp://127.0.0.1:5599"

    def test_roundtrip(self):
        from sim_client import SimClient, SimCommandError

        proc = subprocess.Popen([sys.executable, str(HERE / "nbody_sim.py"), str(SCENARIO),
                                 "--endpoint", self.ENDPOINT])
        client = SimClient(self.ENDPOINT, timeout_ms=10000)
        try:
            self.assertTrue(client.ping()["pong"])
            info = client.info()
            self.assertEqual(info["spacecraft"], ["falcon9"])
            self.assertEqual(len(info["bodies"]), 10)

            client.set_thrust("falcon9", [0, 0, 7.607e6], -2750.0, frame="enu", body="earth")
            reply = client.step(5.0, return_state=True, relative_to="earth")
            self.assertAlmostEqual(reply["time"], 5.0)
            self.assertIn("contact_end", [e["event"] for e in reply["events"]])
            sc = client.get_spacecraft("falcon9", relative_to="earth")
            self.assertGreater(sc["nearest_body"]["altitude"], 10.0)
            self.assertAlmostEqual(sc["mass"], 549054.0 - 5 * 2750.0, places=3)

            reply = client.stage("falcon9", [{"id": "a", "mass": 1000.0}, {"id": "b", "mass": 2000.0}])
            self.assertEqual(reply["created"], ["a", "b"])
            state = client.get_state(bodies=False)
            self.assertEqual([s["id"] for s in state["spacecraft"]], ["a", "b"])
            self.assertNotIn("bodies", state)

            with self.assertRaises(SimCommandError):
                client.call("bogus")
            with self.assertRaises(SimCommandError):
                client.step(-5)

            client.reset()
            self.assertEqual(client.info()["spacecraft"], ["falcon9"])
            self.assertEqual(client.ping()["time"], 0.0)
            client.shutdown()
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)
        finally:
            client.close()
            if proc.poll() is None:
                proc.kill()


class SleepAndMassTest(unittest.TestCase):
    def test_parked_craft_sleeps_and_wakes(self):
        sim = Simulation(SCENARIO)
        sc = sim.spacecraft["falcon9"]
        sim.step(5.0)
        self.assertIsNotNone(sc.sleep)
        before = sim.spacecraft_state("falcon9")["nearest_body"]
        t0 = time.perf_counter()
        sim.step(6 * 3600.0)
        self.assertLess(time.perf_counter() - t0, 1.0)  # no contact integration while asleep
        after = sim.spacecraft_state("falcon9")["nearest_body"]
        self.assertAlmostEqual(after["latitude"], before["latitude"], places=6)
        self.assertAlmostEqual(after["longitude"], before["longitude"], places=6)
        self.assertLess(after["airspeed"], 1e-6)
        self.assertTrue(after["sleeping"])
        sim.set_thrust("falcon9", [0, 0, 7.607e6], -2750.0, "enu", "earth")
        self.assertIsNone(sc.sleep)
        sim.step(10.0)
        self.assertGreater(sim.spacecraft_state("falcon9")["nearest_body"]["altitude"], 100.0)

    def test_set_mass(self):
        sim = Simulation(SCENARIO)
        sim.set_mass("falcon9", 500000.0, 120000.0)
        sc = sim.spacecraft["falcon9"]
        self.assertEqual((sc.mass, sc.dry_mass), (500000.0, 120000.0))
        sim.set_mass("falcon9", 400000.0)
        self.assertEqual((sc.mass, sc.dry_mass), (400000.0, 120000.0))
        with self.assertRaises(SimError):
            sim.set_mass("falcon9", 1000.0, 2000.0)
        with self.assertRaises(SimError):
            sim.set_mass("falcon9", -5.0)

    def test_add_and_remove_spacecraft(self):
        sim = Simulation(SCENARIO)
        spec = {"id": "probe", "mass": 1000.0, "reference_area": 2.0, "drag_coefficient": [[0, 0.5]],
                "initial_state": {"frame": "surface", "body": "earth", "latitude": 0.0, "longitude": 0.0,
                                  "altitude": 0.0}}
        self.assertEqual(sim.add_spacecraft(spec), "probe")
        st = sim.spacecraft_state("probe")
        self.assertAlmostEqual(st["nearest_body"]["altitude"], 0.0, delta=1e-3)
        self.assertEqual(st["trail"]["points"], 1)
        with self.assertRaises(SimError):
            sim.add_spacecraft(spec)  # duplicate id
        with self.assertRaises(SimError):
            sim.add_spacecraft({**spec, "id": "earth"})
        sim.step(5.0)
        self.assertEqual(sim.remove_spacecraft("probe"), "probe")
        self.assertNotIn("probe", sim.spacecraft)
        with self.assertRaises(SimError):
            sim.remove_spacecraft("probe")

    def test_model_tag(self):
        sim = Simulation(SCENARIO)
        self.assertEqual(sim.spacecraft_state("falcon9")["model"]["vehicle"], "falcon9")
        sim.stage("falcon9", [{"id": "s1", "mass": 1000.0, "model": {"parts": ["core"]}},
                              {"id": "falcon9", "mass": 1000.0}])
        self.assertEqual(sim.spacecraft_state("s1")["model"], {"parts": ["core"]})
        self.assertEqual(sim.spacecraft_state("falcon9")["model"]["parts"], ["core", "upper", "fairing"])  # inherited
        sim.stage("falcon9", [{"id": "a", "mass": 10.0, "model": None}, {"id": "b", "mass": 10.0}])
        self.assertIsNone(sim.spacecraft_state("a")["model"])
        for bad in ("rocket", {"x": "y" * 5000}):
            with self.assertRaises(SimError):
                sim.stage("b", [{"id": "c", "mass": 1.0, "model": bad}, {"id": "d", "mass": 1.0}])


class MeoAutopilotTest(unittest.TestCase):
    def test_validation(self):
        import meo_autopilot as ap
        ok = {"perigee_km": 20180, "apogee_km": 20180, "inclination_deg": 55, "payload_kg": 4300, "pace": "max"}
        params, errors, est = ap.validate(ok)
        self.assertEqual(errors, [])
        self.assertEqual(params["vehicle"], "falcon9")
        self.assertGreater(est["margin"], 0)
        self.assertTrue(4000 < est["max_payload"] < 6000)
        for key in ap.lv.VEHICLES:
            params, errors, est = ap.validate({**ok, "vehicle": key})
            self.assertEqual(errors, [], key)
            self.assertGreater(est["liftoff_tw"], 1.05, key)
        for low in ({"perigee_km": 200, "apogee_km": 200, "inclination_deg": 28.7},      # parking orbit
                    {"perigee_km": 200, "apogee_km": 35786, "inclination_deg": 28.7},    # GTO
                    {"perigee_km": 500, "apogee_km": 39800, "inclination_deg": 63.4}):   # Molniya
            self.assertEqual(ap.validate({**ok, **low})[1], [], low)
        for bad in ({"perigee_km": 150}, {"apogee_km": 45000}, {"perigee_km": 25000},
                    {"inclination_deg": 20}, {"payload_kg": -1}, {"payload_kg": "lots"}, {"pace": "warp"},
                    {"vehicle": "saturn_v"},
                    {"vehicle_config": {"core": {"thrust_sl": 9e6}}},             # sea level > vacuum
                    {"vehicle_config": {"upper": {"mdot": 1.0}}},                 # Isp ~ 100,000 s
                    {"vehicle_config": {"upper": {"prop": None}}},
                    {"vehicle": "vulcan", "vehicle_config": {"boosters": {"count": 7}}},
                    {"vehicle": "vulcan", "vehicle_config": {"boosters": {"count": 0}}},  # T/W < 1.05
                    {"vehicle_config": {"kick_angle": 60}}):
            _, errors, est = ap.validate({**ok, **bad})
            self.assertTrue(errors, bad)
            self.assertIsNone(est)
        # Edits apply on top of the catalog.
        params, errors, _ = ap.validate({**ok, "vehicle": "atlas_v_551",
                                         "vehicle_config": {"boosters": {"count": 2}, "fairing": 3500}})
        self.assertEqual(errors, [])
        cfg = params["vehicle_config"]
        self.assertEqual((cfg["boosters"]["count"], cfg["fairing"]), (2, 3500.0))
        self.assertEqual(cfg["core"], ap.lv.VEHICLES["atlas_v_551"]["core"])
        self.assertIn("booster_sep", [k for k, _ in ap.phases_for(cfg)])
        self.assertNotIn("booster_sep", [k for k, _ in ap.phases_for(ap.lv.configure("falcon9")[0])])

    def test_orbital_helpers(self):
        import meo_autopilot as ap
        mu, r0 = ap.EARTH_MU, ap.EARTH_RADIUS + 20180e3
        pole = np.array([0.0, 0.0, 1.0])
        i = math.radians(55)
        v = math.sqrt(mu / r0)
        el = ap.elements(np.array([r0, 0, 0]), np.array([0, v * math.cos(i), v * math.sin(i)]), mu, pole)
        self.assertAlmostEqual(el["inc"], 55.0, places=6)
        self.assertAlmostEqual(el["r_p"], r0, delta=1.0)
        self.assertAlmostEqual(el["r_a"], r0, delta=1.0)
        # From perigee of a transfer orbit, apoapsis is half a period away.
        r_p, r_a = ap.EARTH_RADIUS + 200e3, r0
        a = (r_p + r_a) / 2
        vp = math.sqrt(mu * (2 / r_p - 1 / a))
        t_apo = ap.time_to_apoapsis(np.array([r_p, 0, 0]), np.array([0, vp, 0]), mu)
        self.assertAlmostEqual(t_apo, math.pi * math.sqrt(a**3 / mu), delta=1.0)

    def test_command_descriptions(self):
        import meo_autopilot as ap
        text = ap.describe_command("set_thrust", {"sc_id": "x", "force": [0.0, 1e6, 1e6], "mass_rate": -300.0,
                                                  "frame": "enu", "body": "earth"})
        self.assertIn("1,414.2 kN, pitch 45.0° above the horizon, heading 0.0°", text)
        self.assertIn("propellant flow 300.0 kg/s", text)
        self.assertEqual(ap.describe_command("set_thrust", {"sc_id": "x", "force": [0, 0, 0], "mass_rate": 0.0,
                                                            "frame": "inertial", "body": None}),
                         "set_thrust 'x': engines off.")
        text = ap.describe_command("stage", {"sc_id": "v", "frame": "inertial", "body": None, "children": [
            {"id": "v_boosters", "mass": 20000.0, "delta_v": [1.0, 0, 0],
             "model": {"parts": ["boosters"], "boosters": 4}},
            {"id": "v", "mass": 1e5}]})
        self.assertEqual(text, "stage 'v' into 'v_boosters' [4 boosters] 20,000 kg, pushed 1.0 m/s; 'v' 100,000 kg.")

    def test_pacing_holds_warp_through_long_steps(self):
        import meo_autopilot as ap

        class FakeSim:
            def __init__(self):
                self.t, self.steps = 0.0, []

            def step(self, dt):
                self.steps.append(dt)
                self.t += dt
                return {"time": self.t, "events": []}

        params, errors, _ = ap.validate({"perigee_km": 20180, "apogee_km": 20180, "inclination_deg": 55,
                                         "payload_kg": 1000, "pace": "x5"})
        self.assertEqual(errors, [])
        status = ap.MissionStatus()
        status.begin(params, None)
        sim = FakeSim()
        m = ap.MeoMission(sim, params, status)
        m.warp = (20.0, 20.0)  # constant warp, scaled up so the test is quick
        t0 = time.monotonic()
        m.step(30.0, powered=False)  # a long coast step
        coast_wall = time.monotonic() - t0
        m.step(0.5)                  # followed by a powered tick
        total_wall = time.monotonic() - t0
        self.assertAlmostEqual(coast_wall, 30.0 / 20.0, delta=0.15)
        self.assertAlmostEqual(total_wall, 30.5 / 20.0, delta=0.15)  # the burn isn't slowed afterwards
        self.assertLessEqual(max(sim.steps), 20.0 * ap.PACE_CHUNK + 1e-9)  # long steps were chunked
        self.assertAlmostEqual(sim.t, 30.5)

    def fly(self, endpoint, vehicle, ids, orbit=(20180, 20180, 55), **extra):
        import meo_autopilot as ap
        from sim_client import SimClient

        sim = subprocess.Popen([sys.executable, str(HERE / "nbody_sim.py"), str(SCENARIO), "--endpoint", endpoint])
        try:
            params, errors, est = ap.validate({"vehicle": vehicle, "perigee_km": orbit[0], "apogee_km": orbit[1],
                                               "inclination_deg": orbit[2], "payload_kg": 4300, "pace": "max",
                                               "reset": True, **extra})
            self.assertEqual(errors, [])
            status = ap.MissionStatus()
            status.begin(params, est)
            ap.MeoMission(SimClient(endpoint, timeout_ms=60000), params, status).run()
            snap = status.snapshot()
            self.assertEqual(snap["state"], "succeeded", snap["message"])
            self.assertTrue(all(p["state"] == "done" for p in snap["phases"]))
            r = snap["result"]
            self.assertEqual(r["craft"], "payload")
            client = SimClient(endpoint, timeout_ms=5000)
            self.assertEqual(sorted(client.info()["spacecraft"]), ids)
            # Every piece carries a model tag describing what it is.
            parts = {i: client.get_spacecraft(i)["model"]["parts"] for i in ids}
            self.assertEqual(parts["payload"], ["payload"])
            self.assertEqual(parts[vehicle], ["upper"])
            self.assertEqual(parts[f"{vehicle}_s1"], ["core"])
            self.assertEqual(parts["fairing"], ["fairing"])
            client.close()
            self.assertLess(abs(r["errors"]["perigee_km"]), ap.alt_tolerance(orbit[0] * 1e3) / 1e3)
            self.assertLess(abs(r["errors"]["apogee_km"]), ap.alt_tolerance(orbit[1] * 1e3) / 1e3)
            self.assertLess(abs(r["errors"]["inclination_deg"]), 0.5)
            return snap
        finally:
            sim.kill()
            sim.wait(timeout=10)

    def test_gps_mission(self):
        self.fly("tcp://127.0.0.1:5596", "falcon9", ["fairing", "falcon9", "falcon9_s1", "payload"])

    def test_leo_and_gto_missions(self):
        ids = ["fairing", "falcon9", "falcon9_s1", "payload"]
        self.fly("tcp://127.0.0.1:5596", "falcon9", ids, orbit=(420, 420, 51.6))      # ISS-like
        snap = self.fly("tcp://127.0.0.1:5596", "falcon9", ids, orbit=(200, 200, 28.7))  # parking orbit
        self.assertTrue(any(e["text"].startswith("Already in the target orbit") for e in snap["log"]))
        self.fly("tcp://127.0.0.1:5596", "falcon9", ids, orbit=(200, 35786, 28.7))    # GTO

    def test_gps_mission_with_boosters(self):
        # Vulcan replaces the scenario's Falcon 9 on the pad; boosters, first stage and
        # fairing are released as their own craft and the upper stage keeps the vehicle ID.
        snap = self.fly("tcp://127.0.0.1:5598", "vulcan",
                        ["fairing", "payload", "vulcan", "vulcan_boosters", "vulcan_s1"])
        self.assertTrue(any("Cleared 'falcon9'" in e["text"] for e in snap["log"]))
        self.assertTrue(any(e["text"].startswith("Booster burnout") for e in snap["log"]))


class MissionServerTest(unittest.TestCase):
    SIM = "tcp://127.0.0.1:5597"
    PORT = 8098

    def request(self, path, payload=None):
        import json
        import urllib.error
        import urllib.request

        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.PORT}{path}", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_validate_start_abort(self):
        import urllib.request

        sim = subprocess.Popen([sys.executable, str(HERE / "nbody_sim.py"), str(SCENARIO), "--endpoint", self.SIM])
        srv = subprocess.Popen([sys.executable, str(HERE / "meo_mission_server.py"), "--sim", self.SIM,
                                "--port", str(self.PORT)])
        try:
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}/api/config", timeout=1)
                    break
                except OSError:
                    time.sleep(0.1)
            with urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}/", timeout=5) as res:
                self.assertIn(b"Launch to Earth Orbit", res.read())

            good = {"perigee_km": 20180, "apogee_km": 20180, "inclination_deg": 55, "payload_kg": 4300,
                    "pace": "watch", "reset": True}
            status, _ = self.request("/api/mission/commands.txt")
            self.assertEqual(status, 409)  # nothing has flown yet
            status, config = self.request("/api/config")
            self.assertIn("sls", config["vehicles"])
            status, reply = self.request("/api/validate", good)
            self.assertTrue(reply["ok"])
            self.assertIn("margin", reply["estimate"])
            status, reply = self.request("/api/validate", {**good, "vehicle": "delta_iv_heavy"})
            self.assertIn("booster_sep", [p["key"] for p in reply["phases"]])
            status, reply = self.request("/api/start", {**good, "inclination_deg": 10})
            self.assertEqual(status, 400)

            status, reply = self.request("/api/start", good)
            self.assertEqual((status, reply["ok"]), (200, True))
            status, reply = self.request("/api/start", good)
            self.assertEqual(status, 409)  # one mission at a time
            time.sleep(2.0)
            status, snap = self.request("/api/mission")
            self.assertEqual(snap["state"], "running")
            self.assertTrue(snap["sim"]["connected"])
            status, reply = self.request("/api/abort", {})
            self.assertTrue(reply["ok"])
            for _ in range(50):
                status, snap = self.request("/api/mission")
                if snap["state"] != "running":
                    break
                time.sleep(0.2)
            self.assertEqual(snap["state"], "aborted")
            with urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}/api/mission/commands.txt", timeout=10) as res:
                self.assertIn("attachment", res.headers["Content-Disposition"])
                report = res.read().decode("utf-8")
            self.assertIn("Outcome:         aborted", report)
            self.assertIn("add_spacecraft 'falcon9'", report)
            self.assertIn("set_thrust 'falcon9':", report)
            self.assertTrue(report.rstrip().endswith("engines off."))
        finally:
            for p in (srv, sim):
                p.kill()
                p.wait(timeout=10)


MOON_CASES = {
    "transfer": {"start_periapsis_km": 200, "start_apoapsis_km": 200, "start_inclination_deg": 28.7,
                 "target_periapsis_km": 100, "target_apoapsis_km": 100, "target_inclination_deg": 90,
                 "payload_kg": 15000, "propellant_kg": 24000, "engine": {"key": "rl10b2", "count": 2}},
    "landing": {"start_periapsis_km": 100, "start_apoapsis_km": 100, "start_inclination_deg": 90,
                "payload_kg": 20000, "propellant_kg": 16000, "lander_payload_pct": 40, "lander_propellant_pct": 100,
                "engine": {"key": "rl10_cece", "count": 1}, "site_lat": 20.191, "site_lon": 30.772,
                "site_name": "Apollo 17", "surface_payload_kg": 1000, "surface_propellant_kg": 200,
                "stay_hours": 2, "liftoff": "asap"},
    "return": {"start_periapsis_km": 100, "start_apoapsis_km": 100, "start_inclination_deg": 20,
               "target_periapsis_km": 400, "target_apoapsis_km": 400, "target_inclination_deg": 51.6,
               "payload_kg": 10000, "propellant_kg": 15000, "engine": {"key": "rl10b2", "count": 1}},
}


class MoonAutopilotTest(unittest.TestCase):
    def test_validation(self):
        import moon_autopilot as ma
        for part, raw in MOON_CASES.items():
            params, errors, est, phases = ma.validate(part, raw)
            self.assertEqual(errors, [], part)
            self.assertGreater(est["propellant_left"], 0, part)
            self.assertTrue(phases)
        params, errors, est, phases = ma.validate("launch", {"perigee_km": 200, "apogee_km": 200,
                                                             "inclination_deg": 28.7, "payload_kg": 0})
        self.assertEqual(errors, [])
        bad = [
            ("transfer", {"payload_kg": 500}),                                 # lighter than its engines
            ("transfer", {"engine": {"key": "warp_drive"}}),
            ("transfer", {"engine": {"key": "custom", "custom": {"thrust": 1e5, "isp": 900, "mass": 100,
                                                                 "min_throttle": 1}}}),
            ("transfer", {"target_periapsis_km": 5}),                          # below 15 km
            ("transfer", {"engine": {"key": "aj10_190", "count": 1}}),        # TLI burn far too long
            ("landing", {"start_inclination_deg": 10}),                        # orbit never passes over 20° N
            ("landing", {"surface_payload_kg": 9000}),                         # more than the lander carries
            ("landing", {"surface_propellant_kg": 20000}),
            ("landing", {"engine": {"key": "j2", "count": 1}}),                # can't throttle down to land
            ("landing", {"engine": {"key": "apollo_aps", "count": 1}}),        # thrust-to-weight below 1
            ("landing", {"liftoff": "now"}),
            ("return", {"target_periapsis_km": 100}),                          # below the Earth minimum
            ("nowhere", {}),
        ]
        for part, change in bad:
            raw = {**MOON_CASES.get(part, {}), **change}
            self.assertTrue(ma.validate(part, raw)[1], (part, change))

    def test_flights(self):
        import moon_autopilot as ma
        from sim_client import SimClient

        endpoint = "tcp://127.0.0.1:5595"
        sim = subprocess.Popen([sys.executable, str(HERE / "nbody_sim.py"), str(SCENARIO), "--endpoint", endpoint])
        try:
            for part, raw in MOON_CASES.items():
                params, errors, est, phases = ma.validate(part, {**raw, "pace": "max", "reset": True})
                self.assertEqual(errors, [])
                status = ma.MoonStatus()
                status.begin_part(part, params, est, phases, None)
                ma.make_mission(SimClient(endpoint, timeout_ms=120000), params, status).run()
                snap = status.snapshot()
                self.assertEqual(snap["state"], "succeeded", (part, snap["message"]))
                self.assertTrue(all(p["state"] == "done" for p in snap["phases"]), part)
                d = snap["result"]["delivered"]
                self.assertGreater(d["propellant_kg"], 0, part)
                self.assertIn("Commands sent to the simulation", status.command_report())
                if part == "landing":
                    r = snap["result"]
                    self.assertLess(r["landing"]["site_error_km"], 1.0)
                    self.assertLess(r["landing"]["vertical"], ma.MAX_TOUCHDOWN[0])
                    self.assertEqual(d["payload_kg"], 20000 - 1000)  # everything but the surface cargo
                    client = SimClient(endpoint, timeout_ms=5000)
                    cargo = client.get_spacecraft("surface_cargo", relative_to="luna")
                    self.assertLess(cargo["nearest_body"]["altitude"], 5.0)
                    self.assertEqual(cargo["model"]["parts"], ["cargo"])
                    client.close()
                else:
                    self.assertEqual(d["payload_kg"], raw["payload_kg"])
        finally:
            sim.kill()
            sim.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
