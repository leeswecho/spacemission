#!/usr/bin/env python3
"""Mission page: four independent parts of a Moon mission.

Serves a page (./moon_mission) with one tab per part:
  1. staging payload and propellant in Earth orbit (a rocket launch),
  2. transfer from Earth orbit to lunar orbit,
  3. landing at a chosen site and return to lunar orbit,
  4. return from lunar orbit to Earth orbit.
Each tab is a questionnaire; on launch the autopilot in moon_autopilot.py
flies that part server-side through ordinary simulation commands, and the
page shows its progress and what was delivered.  One mission runs at a time;
every open page sees the same mission.

Run:  python moon_mission_server.py [--sim tcp://127.0.0.1:5555] [--port 8082]
Then open http://127.0.0.1:8082/
"""

import argparse
import functools
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engines as eng
import meo_autopilot as ap
import moon_autopilot as ma
from meo_mission_server import other_mission_running, serve_shared
from mission_log import OrderLog
from sim_client import DEFAULT_ENDPOINT, SimClient, SimCommandError

PAGE_DIR = Path(__file__).resolve().parent / "moon_mission"
MAX_BODY = 64 * 1024


class MissionControl:
    """Owns the mission status and the autopilot thread."""

    def __init__(self, endpoint, timeout_ms, other_mission=None, log_dir=None):
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        self.other_mission = other_mission
        self.orders = OrderLog(log_dir, "moon")
        self.status = ma.MoonStatus()
        self.thread = None
        self.start_lock = threading.Lock()
        self.ping_client = SimClient(endpoint, timeout_ms=2000)
        self.ping_lock = threading.Lock()
        self.ping_cache = (0.0, None)

    def sim_status(self):
        now = time.monotonic()
        with self.ping_lock:
            if now - self.ping_cache[0] > 2.0:
                try:
                    reply = self.ping_client.ping()
                    self.ping_cache = (now, {"connected": True, "time": reply["time"]})
                except (TimeoutError, SimCommandError) as e:
                    self.ping_cache = (now, {"connected": False, "error": str(e)})
            return self.ping_cache[1]

    def start(self, raw):
        part = raw.get("part")
        params, errors, est, phases = ma.validate(part, raw)
        if errors:
            self.orders.order(part, raw, False, errors)
            return 400, {"ok": False, "errors": errors}
        with self.start_lock:
            refusal = None
            if self.thread and self.thread.is_alive():
                refusal = "A mission is already in flight."
            elif other_mission_running(self.other_mission):
                refusal = ("The Earth-orbit mission page has a mission flying in the shared simulation; "
                           "wait for it to finish.")
            if refusal:
                self.orders.order(part, raw, False, [refusal])
                return 409, {"ok": False, "errors": [refusal]}
            log_path = self.orders.order(part, raw, True, params=params, estimate=est)
            vehicle = params["vehicle_config"]["id"] if part == "launch" else None
            self.status.begin_part(part, params, est, phases, vehicle)
            mission = ma.make_mission(SimClient(self.endpoint, timeout_ms=self.timeout_ms), params, self.status)

            def fly():
                mission.run()
                self.orders.outcome(log_path, self.status.snapshot())

            self.thread = threading.Thread(target=fly, name=f"moon-{part}", daemon=True)
            self.thread.start()
        return 200, {"ok": True}

    def abort(self):
        return self.status.request_abort()


class Handler(SimpleHTTPRequestHandler):
    # Windows can map .js to text/plain via the registry, which breaks ES modules.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript", ".svg": "image/svg+xml"}

    def __init__(self, *args, control=None, config=None, **kwargs):
        self.control = control
        self.config = config
        super().__init__(*args, directory=str(PAGE_DIR), **kwargs)

    def _json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            self._json(200, self.config)
        elif path == "/api/mission":
            snap = self.control.status.snapshot()
            snap["sim"] = self.control.sim_status()
            self._json(200, snap)
        elif path == "/api/mission/commands.txt":
            self._command_log()
        elif not serve_shared(self, path):
            super().do_GET()

    def _command_log(self):
        """The finished mission's command log as a text download."""
        status = self.control.status
        if status.state not in ("succeeded", "failed", "aborted"):
            self._json(409, {"ok": False, "errors": ["No finished mission to report on."]})
            return
        body = status.command_report().encode("utf-8")
        name = f"moon-{status.part}-{status.started:%Y%m%d-%H%M%S}.txt"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()
        if body is None:
            self._json(400, {"ok": False, "errors": ["Request body must be a JSON object."]})
        elif path == "/api/validate":
            params, errors, est, phases = ma.validate(body.get("part"), body)
            phases = [{"key": k, "title": t} for k, t in phases] if phases else None
            self._json(200, {"ok": not errors, "errors": errors, "estimate": est, "phases": phases})
        elif path == "/api/start":
            status, reply = self.control.start(body)
            self._json(status, reply)
        elif path == "/api/abort":
            self._json(200, {"ok": self.control.abort()})
        else:
            self._json(404, {"ok": False, "errors": ["Not found."]})

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # the page polls continuously; keep the console quiet


def page_config(viewer_url):
    return {
        "viewer_url": viewer_url,
        "parts": ma.PARTS,
        "launch_latitude": ap.LAUNCH_LATITUDE,
        "earth_orbit_km": [ma.EARTH_MIN_ALT / 1e3, ma.EARTH_MAX_ALT / 1e3],
        "lunar_orbit_km": [ma.LUNAR_MIN_ALT / 1e3, ma.LUNAR_MAX_ALT / 1e3],
        "launch_paces": {k: {"powered": v[0], "coast": v[1]} for k, v in ap.PACES.items()},
        "paces": {k: {"powered": v[0], "coast": v[1]} for k, v in ma.PACES.items()},
        "vehicles": ap.lv.catalog(),
        "default_vehicle": ap.lv.DEFAULT_VEHICLE,
        "engines": eng.catalog(),
        "default_engine": eng.DEFAULT_ENGINE,
        "sites": ma.LUNAR_SITES,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the Moon mission page.")
    parser.add_argument("--sim", default=DEFAULT_ENDPOINT, help=f"simulation endpoint (default {DEFAULT_ENDPOINT})")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8082, help="HTTP port (default 8082)")
    parser.add_argument("--timeout", type=int, default=120000, help="simulation reply timeout in ms (default 120000)")
    parser.add_argument("--viewer-url", default=None,
                        help="URL of the 3D viewer to link to (default: same host, port 8080)")
    parser.add_argument("--other-mission", default="http://127.0.0.1:8081/api/mission",
                        help="mission status URL of the Earth-orbit page sharing the simulation ('' for none)")
    parser.add_argument("--log-dir", default=None,
                        help="folder for the order log: one JSON file per launch press (default: no log)")
    args = parser.parse_args(argv)

    control = MissionControl(args.sim, args.timeout, args.other_mission, args.log_dir)
    server = ThreadingHTTPServer((args.host, args.port),
                                 functools.partial(Handler, control=control, config=page_config(args.viewer_url)))
    print(f"[moon_mission] simulation at {args.sim}", flush=True)
    print(f"[moon_mission] open http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        control.abort()
        server.server_close()
        print("[moon_mission] stopped", flush=True)


if __name__ == "__main__":
    main()
