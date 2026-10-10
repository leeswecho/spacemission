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

from sim_client import DEFAULT_ENDPOINT, SimClient, SimCommandError

VIZ_DIR = Path(__file__).resolve().parent / "viz"


class StatePoller(threading.Thread):
    """Polls get_state from the simulation and keeps the latest reply."""

    def __init__(self, endpoint, rate_hz, timeout_ms):
        super().__init__(daemon=True)
        self.endpoint = endpoint
        self.period = 1.0 / rate_hz
        self.client = SimClient(endpoint, timeout_ms=timeout_ms)
        self.lock = threading.Lock()
        self.payload = self._encode({"connected": False, "error": "waiting for first reply", "state": None})
        self.last_state = None

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
            except (TimeoutError, SimCommandError) as e:
                # Keep serving the last good state, flagged as stale.
                doc = {"connected": False, "error": str(e), "state": self.last_state}
            with self.lock:
                self.payload = self._encode(doc)
            time.sleep(max(0.0, self.period - (time.monotonic() - started)))

    def latest(self):
        with self.lock:
            return self.payload


class Handler(SimpleHTTPRequestHandler):
    # Windows can map .js to text/plain via the registry, which breaks ES modules.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript"}

    def __init__(self, *args, poller=None, **kwargs):
        self.poller = poller
        super().__init__(*args, directory=str(VIZ_DIR), **kwargs)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/state":
            body = self.poller.latest()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
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
    parser.add_argument("--rate", type=float, default=4.0, help="simulation polls per second (default 4)")
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
