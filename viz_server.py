#!/usr/bin/env python3
"""Web visualization server for nbody_sim.py.

Runs as a separate process.  A background thread polls the simulation over
ZeroMQ (as an ordinary client) and caches the latest state; the HTTP server
serves the static viewer from ./viz and the cached state at /api/state.
The simulation is never stepped by this server.

Run:  python viz_server.py [--sim tcp://127.0.0.1:5555] [--port 8080]
Then open http://127.0.0.1:8080/
"""

import argparse
import functools
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from sim_client import DEFAULT_ENDPOINT, SimClient, SimCommandError

VIZ_DIR = Path(__file__).resolve().parent / "viz"


class StatePoller(threading.Thread):
    """Polls the simulation and keeps the latest state plus a copy of all trails.

    Trails are fetched incrementally (only new points) and cached here, so the
    sim's load doesn't grow with the number of open browsers.
    """

    def __init__(self, endpoint, rate_hz, timeout_ms):
        super().__init__(daemon=True)
        self.endpoint = endpoint
        self.period = 1.0 / rate_hz
        self.client = SimClient(endpoint, timeout_ms=timeout_ms)
        self.lock = threading.Lock()
        self.payload = self._encode({"connected": False, "error": "waiting for first reply", "state": None})
        self.last_state = None
        self.trails = {}  # id -> {"version", "resting", "points"}
        self.bodies = None  # body history: {"version", "ids", "samples", "head"}

    @staticmethod
    def _encode(obj):
        return json.dumps(obj).encode("utf-8")

    def run(self):
        while True:
            started = time.monotonic()
            try:
                state = self.client.get_state()
                state.pop("ok", None)
                self.last_state = state
                doc = {"connected": True, "error": None, "state": state}
                try:
                    self._update_trails()
                except SimCommandError:
                    pass  # e.g. an older sim without get_trails: just no trails
            except (TimeoutError, SimCommandError) as e:
                # Keep serving the last good state, flagged as stale.
                doc = {"connected": False, "error": str(e), "state": self.last_state}
            with self.lock:
                self.payload = self._encode(doc)
            time.sleep(max(0.0, self.period - (time.monotonic() - started)))

    def _update_trails(self):
        with self.lock:
            since = {i: [c["version"], c["points"][-1][0] if c["points"] else -1] for i, c in self.trails.items()}
        with self.lock:
            known = ([self.bodies["version"], self.bodies["samples"][-1][0] if self.bodies["samples"] else -1]
                     if self.bodies else True)
        full_reply = self.client.call("get_trails", since=since, body_history=known)
        reply = full_reply["trails"]
        bh = full_reply.get("body_history")
        updated = {}
        with self.lock:
            for sc_id, tr in reply.items():
                cached = self.trails.get(sc_id)
                if tr["full"] or cached is None or cached["version"] != tr["version"]:
                    cached = {"version": tr["version"], "points": tr["points"]}
                else:
                    cached["points"].extend(tr["points"])
                cached["resting"] = tr["resting"]
                updated[sc_id] = cached
            self.trails = updated  # drops craft that no longer exist
            if bh is not None:
                if bh["full"] or self.bodies is None or self.bodies["version"] != bh["version"]:
                    self.bodies = {"version": bh["version"], "ids": bh["ids"], "samples": bh["samples"]}
                else:
                    self.bodies["samples"].extend(bh["samples"])
                    # Keep only what the oldest trail point still needs (the sim prunes the same way).
                    oldest = min((c["points"][0][1] for c in updated.values() if c["points"]), default=None)
                    s = self.bodies["samples"]
                    k = 0
                    while oldest is not None and k + 2 < len(s) and s[k + 1][1] <= oldest:
                        k += 1
                    del s[:k]
                self.bodies["head"] = bh["head"]

    def latest(self):
        with self.lock:
            return self.payload

    def trails_since(self, since, bodies_since=None):
        """Trails for a browser that already holds {id: [version, seq]}, plus the body
        history samples newer than bodies_since = [version, seq]."""
        out = {}
        with self.lock:
            for sc_id, c in self.trails.items():
                known = since.get(sc_id) if isinstance(since, dict) else None
                pts = c["points"]
                full = True
                if isinstance(known, list) and len(known) == 2 and known[0] == c["version"]:
                    full = False
                    k = len(pts)
                    while k > 0 and pts[k - 1][0] > known[1]:
                        k -= 1
                    pts = pts[k:]
                out[sc_id] = {"version": c["version"], "full": full, "resting": c["resting"], "points": list(pts)}
            bodies = None
            if self.bodies is not None:
                b = self.bodies
                full = not (isinstance(bodies_since, list) and len(bodies_since) == 2
                            and bodies_since[0] == b["version"])
                samples = b["samples"] if full else [x for x in b["samples"] if x[0] > bodies_since[1]]
                bodies = {"version": b["version"], "full": full, "ids": b["ids"], "samples": samples,
                          "head": b.get("head")}
        return self._encode({"trails": out, "bodies": bodies})


class Handler(SimpleHTTPRequestHandler):
    # Windows can map .js to text/plain via the registry, which breaks ES modules.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript", ".svg": "image/svg+xml"}

    # Keep connections open between polls, saving a network round trip per request; every
    # response must therefore carry a Content-Length. An idle connection (a browser that went
    # away, a sleeping laptop) is closed after `timeout` seconds so its thread doesn't linger.
    # Headers and body go out in separate writes; with Nagle's algorithm on, a kept-open
    # connection holds the body back until the headers are acknowledged (an extra round trip).
    protocol_version = "HTTP/1.1"
    timeout = 30
    disable_nagle_algorithm = True

    def __init__(self, *args, poller=None, **kwargs):
        self.poller = poller
        super().__init__(*args, directory=str(VIZ_DIR), **kwargs)

    def _send_json_bytes(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/state":
            self._send_json_bytes(self.poller.latest())
            return
        if path == "/api/trails":
            # ?since=<JSON {id: [version, seq]}>&bodies=<JSON [version, seq]> (body history held);
            # anything unparsable means "send everything".
            q = parse_qs(query)
            try:
                since = json.loads(q.get("since", ["{}"])[0])
            except json.JSONDecodeError:
                since = {}
            try:
                bodies_since = json.loads(q.get("bodies", ["null"])[0])
            except json.JSONDecodeError:
                bodies_since = None
            self._send_json_bytes(self.poller.trails_since(since, bodies_since))
            return
        super().do_GET()

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; the browser polls continuously


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve a 3D web view of a running nbody_sim.")
    parser.add_argument("--sim", default=DEFAULT_ENDPOINT, help=f"simulation endpoint (default {DEFAULT_ENDPOINT})")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080)")
    parser.add_argument("--rate", type=float, default=10.0, help="simulation polls per second (default 10)")
    parser.add_argument("--timeout", type=int, default=2000, help="simulation reply timeout, ms (default 2000)")
    args = parser.parse_args(argv)

    poller = StatePoller(args.sim, args.rate, args.timeout)
    poller.start()
    server = ThreadingHTTPServer((args.host, args.port), functools.partial(Handler, poller=poller))
    print(f"[viz_server] polling simulation at {args.sim}", flush=True)
    print(f"[viz_server] open http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[viz_server] stopped", flush=True)


if __name__ == "__main__":
    main()
