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

from nbody_sim import Simulation, SimError

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

    def test_trail_reference_bodies(self):
        sim = Simulation(SCENARIO)
        sun, earth, luna = (sim.body_by_id[n].index for n in ("sun", "earth", "luna"))
        self.assertEqual(sim._reference_body(sim.bpos[earth] + [7e6, 0, 0], sim.bpos), earth)
        self.assertEqual(sim._reference_body(sim.bpos[luna] + [2e6, 0, 0], sim.bpos), luna)
        self.assertEqual(sim._reference_body(sim.bpos[earth] + [5e9, 0, 0], sim.bpos), sun)

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


class MeoAutopilotTest(unittest.TestCase):
    def test_validation(self):
        import meo_autopilot as ap
        ok = {"perigee_km": 20180, "apogee_km": 20180, "inclination_deg": 55, "payload_kg": 4300, "pace": "max"}
        params, errors, est = ap.validate(ok)
        self.assertEqual(errors, [])
        self.assertGreater(est["margin"], 0)
        self.assertTrue(5000 < est["max_payload"] < 5600)
        for bad in ({"perigee_km": 500}, {"apogee_km": 40000}, {"perigee_km": 25000},
                    {"inclination_deg": 20}, {"payload_kg": -1}, {"payload_kg": "lots"}, {"pace": "warp"}):
            _, errors, est = ap.validate({**ok, **bad})
            self.assertTrue(errors, bad)
            self.assertIsNone(est)

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

    def test_gps_mission(self):
        import meo_autopilot as ap
        from sim_client import SimClient

        endpoint = "tcp://127.0.0.1:5596"
        sim = subprocess.Popen([sys.executable, str(HERE / "nbody_sim.py"), str(SCENARIO), "--endpoint", endpoint])
        try:
            params, errors, est = ap.validate({"perigee_km": 20180, "apogee_km": 20180, "inclination_deg": 55,
                                               "payload_kg": 4300, "pace": "max", "reset": True})
            self.assertEqual(errors, [])
            status = ap.MissionStatus()
            status.begin(params, est)
            ap.MeoMission(SimClient(endpoint, timeout_ms=60000), params, status).run()
            snap = status.snapshot()
            self.assertEqual(snap["state"], "succeeded", snap["message"])
            self.assertTrue(all(p["state"] == "done" for p in snap["phases"]))
            r = snap["result"]
            self.assertEqual(r["craft"], "payload")
            ids = SimClient(endpoint, timeout_ms=5000).info()["spacecraft"]
            self.assertEqual(sorted(ids), ["fairing", "falcon9", "falcon9_s1", "payload"])
            self.assertLess(abs(r["errors"]["perigee_km"]), 50)
            self.assertLess(abs(r["errors"]["apogee_km"]), 50)
            self.assertLess(abs(r["errors"]["inclination_deg"]), 0.5)
        finally:
            sim.kill()
            sim.wait(timeout=10)


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
                self.assertIn(b"Medium Earth Orbit", res.read())

            good = {"perigee_km": 20180, "apogee_km": 20180, "inclination_deg": 55, "payload_kg": 4300,
                    "pace": "watch", "reset": True}
            status, reply = self.request("/api/validate", good)
            self.assertTrue(reply["ok"])
            self.assertIn("margin", reply["estimate"])
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
        finally:
            for p in (srv, sim):
                p.kill()
                p.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
