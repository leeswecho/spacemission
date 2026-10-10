#!/usr/bin/env python3
"""Minimal client for nbody_sim.py (ZeroMQ REQ, JSON messages).

Library use:
    from sim_client import SimClient
    sim = SimClient()                       # tcp://127.0.0.1:5555
    sim.step(1.0)
    print(sim.get_spacecraft("falcon9", relative_to="earth"))

Command line:
    python sim_client.py <cmd> [key=value ...]
    python sim_client.py step dt=10
    python sim_client.py get_spacecraft id=falcon9 relative_to=earth
    python sim_client.py set_thrust id=falcon9 force=[0,0,7.6e6] mass_rate=-2750 frame=enu body=earth
Values are parsed as JSON when possible, otherwise used as plain strings.
"""

import argparse
import json
import sys

import zmq

DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"


class SimCommandError(RuntimeError):
    pass


class SimClient:
    def __init__(self, endpoint=DEFAULT_ENDPOINT, timeout_ms=None):
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._connect()

    def _connect(self):
        self._sock = self._ctx.socket(zmq.REQ)
        self._sock.setsockopt(zmq.LINGER, 0)
        if self.timeout_ms is not None:
            self._sock.setsockopt(zmq.RCVTIMEO, int(self.timeout_ms))
        self._sock.connect(self.endpoint)

    def call(self, cmd, **params):
        """Send a command and return the reply dict; raise SimCommandError on failure."""
        params["cmd"] = cmd
        self._sock.send(json.dumps(params).encode("utf-8"))
        try:
            reply = json.loads(self._sock.recv())
        except zmq.Again:
            # A REQ socket is unusable after a missed reply; recreate it.
            self._sock.close()
            self._connect()
            raise TimeoutError(f"no reply from {self.endpoint} within {self.timeout_ms} ms")
        if not reply.get("ok"):
            raise SimCommandError(reply.get("error", "unknown error"))
        return reply

    def close(self):
        self._sock.close()

    # Convenience wrappers ---------------------------------------------------

    def ping(self):
        return self.call("ping")

    def info(self):
        return self.call("info")

    def step(self, dt, return_state=False, relative_to=None):
        return self.call("step", dt=dt, return_state=return_state, relative_to=relative_to)

    def get_state(self, relative_to=None, bodies=True, spacecraft=True):
        return self.call("get_state", relative_to=relative_to, bodies=bodies, spacecraft=spacecraft)

    def get_body(self, body_id, relative_to=None):
        return self.call("get_body", id=body_id, relative_to=relative_to)["body"]

    def get_spacecraft(self, sc_id, relative_to=None):
        return self.call("get_spacecraft", id=sc_id, relative_to=relative_to)["spacecraft"]

    def set_thrust(self, sc_id, force, mass_rate=0.0, frame="inertial", body=None):
        return self.call("set_thrust", id=sc_id, force=list(force), mass_rate=mass_rate, frame=frame, body=body)

    def set_mass(self, sc_id, mass, dry_mass=None):
        return self.call("set_mass", id=sc_id, mass=mass, dry_mass=dry_mass)

    def get_trails(self, since=None, ids=None):
        return self.call("get_trails", since=since, ids=ids)["trails"]

    def stage(self, sc_id, children, frame="inertial", body=None):
        return self.call("stage", id=sc_id, children=children, frame=frame, body=body)

    def reset(self):
        return self.call("reset")

    def shutdown(self):
        return self.call("shutdown")


def _parse_value(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send one command to nbody_sim.")
    parser.add_argument("cmd")
    parser.add_argument("params", nargs="*", help="key=value pairs")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=int, default=None, help="reply timeout in ms")
    args = parser.parse_args(argv)

    params = {}
    for item in args.params:
        key, sep, value = item.partition("=")
        if not sep:
            parser.error(f"parameter '{item}' must be key=value")
        params[key] = _parse_value(value)

    client = SimClient(args.endpoint, args.timeout)
    try:
        reply = client.call(args.cmd, **params)
    except SimCommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
