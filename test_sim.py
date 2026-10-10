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


if __name__ == "__main__":
    unittest.main()
