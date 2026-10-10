#!/usr/bin/env python3
"""Web control page server for nbody_sim.py.

Runs as a separate process, independent of the viewer.  Serves the static
control page from ./control and forwards JSON commands posted to
/api/command to the simulation over ZeroMQ (as an ordinary client), returning
the simulation's reply unchanged.

Run:  python control_server.py [--sim tcp://127.0.0.1:5555] [--port 8081]
Then open http://127.0.0.1:8081/
"""

import argparse
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sim_client import DEFAULT_ENDPOINT, SimClient, SimCommandError

CONTROL_DIR = Path(__file__).resolve().parent / "control"
MAX_BODY = 1 << 20  # 1 MiB


class SimProxy:
    """Serialises commands from many HTTP threads onto one ZeroMQ REQ socket."""

    def __init__(self, endpoint, timeout_ms):
        self.client = SimClient(endpoint, timeout_ms=timeout_ms)
        self.lock = threading.Lock()

    def send(self, request):
        cmd = request.pop("cmd", None)
        if not isinstance(cmd, str):
            return {"ok": False, "error": "request must include a string 'cmd'"}
        with self.lock:
            try:
                return self.client.call(cmd, **request)
            except SimCommandError as e:
                return {"ok": False, "error": str(e)}
            except TimeoutError as e:
                return {"ok": False, "error": str(e), "timeout": True}


class Handler(SimpleHTTPRequestHandler):
    # Windows can map .js to text/plain via the registry, which breaks ES modules.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript"}

    def __init__(self, *args, proxy=None, config=None, **kwargs):
        self.proxy = proxy
        self.config = config
        super().__init__(*args, directory=str(CONTROL_DIR), **kwargs)

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/config":
            self._send_json(200, self.config)
            return
        super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/command":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"ok": False, "error": "missing or oversized request body"})
            return
        try:
            request = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            self._send_json(400, {"ok": False, "error": f"invalid JSON: {e}"})
            return
        if not isinstance(request, dict):
            self._send_json(400, {"ok": False, "error": "request must be a JSON object"})
            return
        self._send_json(200, self.proxy.send(request))

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # the page polls continuously; keep the console quiet


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve a web control page for a running nbody_sim.")
    parser.add_argument("--sim", default=DEFAULT_ENDPOINT, help=f"simulation endpoint (default {DEFAULT_ENDPOINT})")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8081, help="HTTP port (default 8081)")
    parser.add_argument("--timeout", type=int, default=120000,
                        help="simulation reply timeout in ms; long steps need a generous value (default 120000)")
    parser.add_argument("--viewer-url", default=None,
                        help="URL of the 3D viewer to link to (default: same host, port 8080)")
    args = parser.parse_args(argv)

    proxy = SimProxy(args.sim, args.timeout)
    config = {"viewer_url": args.viewer_url}
    server = ThreadingHTTPServer((args.host, args.port),
                                 functools.partial(Handler, proxy=proxy, config=config))
    print(f"[control_server] forwarding commands to {args.sim}", flush=True)
    print(f"[control_server] open http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[control_server] stopped", flush=True)


if __name__ == "__main__":
    main()
