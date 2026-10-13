"""Order log for the mission pages: one JSON file per press of a launch button.

Each file records when the launch was ordered, from which page and tab,
everything the page sent, and (if the server accepted it) the validated
settings and the pre-flight estimate.  Refused presses are logged too, with
the reasons.  When an accepted mission ends, its outcome (state, message,
result and flight log) is added to the same file.

Files are named <UTC time>_<page>-<part>[_rejected].json and written
atomically, so a half-written file is never left behind.  Logging problems
are reported on the console and never interfere with a mission.
"""

import datetime
import json
import os
from pathlib import Path


class OrderLog:
    def __init__(self, directory, page):
        """directory: where the files go (created if missing), or None to log nothing.
        page: short name of the mission page, e.g. "earth-orbit" or "moon"."""
        self.dir = Path(directory) if directory else None
        self.page = page
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _write(self, path, doc):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def order(self, part, request, accepted, reasons=None, params=None, estimate=None):
        """Record a launch press.  Returns the file's path (or None when logging is off or fails)."""
        if self.dir is None:
            return None
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            stem = f"{now:%Y-%m-%dT%H-%M-%S}Z_{self.page}-{part or 'unknown'}" + ("" if accepted else "_rejected")
            path = self.dir / f"{stem}.json"
            n = 2
            while path.exists():  # two presses within a second
                path = self.dir / f"{stem}_{n}.json"
                n += 1
            doc = {
                "ordered_at_utc": now.isoformat(timespec="seconds"),
                "page": self.page,
                "part": part,
                "accepted": accepted,
                "rejection_reasons": None if accepted else list(reasons or []),
                "request": request,
                "settings": params if accepted else None,
                "estimate": estimate if accepted else None,
                "outcome": None,
            }
            self._write(path, doc)
            return path
        except Exception as e:  # never let logging break a launch
            print(f"[mission_log] could not log the order: {e}", flush=True)
            return None

    def outcome(self, path, snapshot):
        """Add a finished mission's outcome to its order file."""
        if path is None:
            return
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            doc["outcome"] = {
                "finished_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "state": snapshot.get("state"),
                "message": snapshot.get("message"),
                "result": snapshot.get("result"),
                "phases": [{"title": p["title"], "state": p["state"]} for p in snapshot.get("phases", [])],
                "flight_log": [{"sim_time_s": round(e["t"], 1), "text": e["text"]} for e in snapshot.get("log", [])],
            }
            self._write(Path(path), doc)
        except Exception as e:
            print(f"[mission_log] could not log the outcome: {e}", flush=True)
