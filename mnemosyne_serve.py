"""
mnemosyne_serve.py — long-running Mnemosyne daemon.

Purpose
-------
One process that owns the memory store, exposes a localhost HTTP API for
turn dispatch, and runs background cron threads for:

  - Dream consolidation (periodic L3→L2 abstraction)
  - Triage scan (periodic error-cluster roll-up)
  - Proposer (periodic change-proposal writing)
  - Apply (periodic execution of human-accepted proposals)
  - Goal-stack housekeeping

Without a daemon, every CLI invocation pays the cold-start cost of
opening the DB, loading skills, and re-probing Ollama. Worse, tier
promotions/demotions in short-lived processes don't accumulate the way
they would in a continuously-running agent. `mnemosyne-serve` fixes
both.

Design
------
- Stdlib only: `http.server`, `threading`, no Flask/FastAPI/uvicorn.
- Bound to 127.0.0.1 by default. Never exposes a writable endpoint on
  0.0.0.0 without explicit `--host` override.
- Optional bearer-token auth via `MNEMOSYNE_SERVE_TOKEN` env var.
- Graceful shutdown on SIGINT/SIGTERM: flush memory, cancel cron.
- All HTTP endpoints are JSON in/out. CORS is OFF by default.

Endpoints
---------
    GET  /healthz               → {"status": "ok", "uptime_s": N}
    GET  /stats                 → memory stats, session metrics
    POST /turn                  → {"user_message": "...", "metadata": {...}}
                                  returns BrainResponse dict
    POST /dream                 → trigger a dream-consolidation pass
    POST /triage                → trigger a triage scan
    POST /propose               → trigger a proposer pass
    POST /apply                 → trigger apply of accepted proposals
    GET  /goals                 → list current goals
    POST /goals                 → add/update/resolve a goal
    GET  /recent_events?limit=N → tail of the current run's events.jsonl

Usage
-----
    mnemosyne-serve                          # :8484, cron every 10 min
    mnemosyne-serve --port 8484 --dream-every 30m --triage-every 5m
    MNEMOSYNE_SERVE_TOKEN=xyz mnemosyne-serve
    curl -s -XPOST http://127.0.0.1:8484/turn \
        -H 'Authorization: Bearer xyz' \
        -d '{"user_message": "hello"}'

Stdlib only. Safe on a laptop. Safe to systemd.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


# ---- duration parsing -------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d)?$")


def parse_duration(s: str) -> float:
    """Parse strings like '30m', '1h', '10s' into seconds."""
    m = _DURATION_RE.match(s.strip().lower())
    if not m:
        raise ValueError(f"bad duration: {s!r}")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# ---- Service container ------------------------------------------------------

class Service:
    """Holds the shared Brain/Memory/Registry + cron threads + the HTTP server."""

    def __init__(
        self,
        *,
        projects_dir: Path | None = None,
        dream_every_s: float | None = 1800.0,
        triage_every_s: float | None = 600.0,
        propose_every_s: float | None = 1800.0,
        apply_every_s: float | None = None,
        auth_token: str | None = None,
    ) -> None:
        # Lazy imports so `--help` etc. don't pull in the heavy stack.
        import harness_telemetry as ht
        import mnemosyne_brain as br
        import mnemosyne_memory as mm
        import mnemosyne_skills as sk

        self.pd = projects_dir
        self.started_utc = time.time()
        self.auth_token = auth_token

        # One telemetry run owns the daemon's lifetime. Restart → new run.
        self.run_id = ht.create_run(
            model="serve",
            tags=["daemon", "serve"],
            projects_dir=projects_dir,
            notes="mnemosyne-serve daemon session",
        )
        self.session = ht.TelemetrySession(self.run_id, projects_dir=projects_dir)
        self.session.__enter__()

        self.memory = mm.MemoryStore(telemetry=self.session)
        self.skills = sk.default_registry()
        # Optionally load goals (mnemosyne_goals is stdlib-only, safe to import)
        try:
            import mnemosyne_goals as goals_mod
            self.goals = goals_mod.GoalStack(projects_dir=projects_dir)
        except Exception:
            self.goals = None

        # Brain config pulled from env if present, otherwise defaults.
        self.brain = br.Brain(
            memory=self.memory,
            skills=self.skills,
            telemetry=self.session,
            config=br.BrainConfig(
                adapt_to_context=True,
                inject_env_snapshot=True,
                inner_dialogue_enabled=bool(os.environ.get("MNEMOSYNE_INNER_DIALOGUE")),
                dreams_after_n_turns=int(os.environ.get("MNEMOSYNE_DREAMS_AFTER", 0) or 0),
            ),
        )

        self._cron_stop = threading.Event()
        self._cron_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

        # Schedule cron jobs
        if dream_every_s:
            self._start_cron("dream", dream_every_s, self._cron_dream)
        if triage_every_s:
            self._start_cron("triage", triage_every_s, self._cron_triage)
        if propose_every_s:
            self._start_cron("propose", propose_every_s, self._cron_propose)
        if apply_every_s:
            self._start_cron("apply", apply_every_s, self._cron_apply)

    # ---- cron ---------------------------------------------------------------

    def _start_cron(self, name: str, interval: float, fn):
        def loop():
            # Small initial jitter so cron doesn't stampede on startup
            self._cron_stop.wait(min(interval, 5.0))
            while not self._cron_stop.is_set():
                try:
                    fn()
                except Exception as e:
                    self.session.log(f"cron_{name}_error",
                                      status="error",
                                      error={"type": type(e).__name__, "message": str(e)})
                self._cron_stop.wait(interval)
        t = threading.Thread(target=loop, name=f"cron-{name}", daemon=True)
        t.start()
        self._cron_threads.append(t)

    def _cron_dream(self) -> None:
        import mnemosyne_dreams as dreams_mod
        dreams_mod.consolidate(
            memory=self.memory,
            projects_dir=self.pd,
            telemetry=self.session,
        )

    def _cron_triage(self) -> None:
        import mnemosyne_triage as tri
        tri.run_triage(projects_dir=self.pd, window_days=1)

    def _cron_propose(self) -> None:
        import mnemosyne_proposer as pr
        pr.propose(projects_dir=self.pd, window_days=7, min_severity=20.0)

    def _cron_apply(self) -> None:
        try:
            import mnemosyne_apply as ap
        except ImportError:
            return
        ap.apply_all_accepted(projects_dir=self.pd, telemetry=self.session)

    # ---- HTTP handlers ------------------------------------------------------

    def handle_turn(self, body: dict[str, Any]) -> dict[str, Any]:
        user_message = body.get("user_message")
        if not isinstance(user_message, str):
            raise HTTPError(400, "user_message required")
        metadata = body.get("metadata") or None
        with self._lock:
            resp = self.brain.turn(user_message, metadata=metadata)
        return {
            "text": resp.text,
            "tool_calls": resp.tool_calls,
            "duration_ms": resp.duration_ms,
            "model": resp.model,
            "error": resp.error,
            "model_calls": resp.model_calls,
            "memory_reads": resp.memory_reads,
            "memory_writes": resp.memory_writes,
        }

    def handle_stats(self) -> dict[str, Any]:
        return {
            "uptime_s": time.time() - self.started_utc,
            "run_id": self.run_id,
            "memory": self.memory.stats(),
            "brain": self.brain.session_metrics(),
            "cron": [t.name for t in self._cron_threads if t.is_alive()],
        }

    def handle_goals_list(self) -> dict[str, Any]:
        if self.goals is None:
            return {"goals": [], "note": "goals module unavailable"}
        return {"goals": [asdict(g) for g in self.goals.list_open()]}

    def handle_goals_mutate(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.goals is None:
            raise HTTPError(503, "goals module unavailable")
        op = body.get("op")
        if op == "add":
            g = self.goals.add(
                text=body["text"],
                priority=int(body.get("priority", 3)),
                tags=body.get("tags") or [],
            )
            return {"goal": asdict(g)}
        if op == "resolve":
            self.goals.resolve(int(body["id"]))
            return {"ok": True}
        if op == "reprioritize":
            self.goals.reprioritize(int(body["id"]), int(body["priority"]))
            return {"ok": True}
        raise HTTPError(400, f"unknown op {op!r}")

    def handle_avatar(self) -> dict[str, Any]:
        """Compute the current avatar state from telemetry + memory.

        Pure read; the dashboard polls this every few seconds.
        """
        try:
            import mnemosyne_avatar as av
            return av.compute_state(projects_dir=self.pd)
        except Exception as e:
            return {"error": type(e).__name__, "message": str(e)}

    def handle_memory_search(self, query: str, limit: int,
                              tier_max: int | None) -> dict[str, Any]:
        """FTS5 search over the agent's memory.

        Protected read — returns at most 50 rows per request. Used by
        the UI's Memory Browser panel. We do NOT expose raw metadata
        that might include secrets; only content + tier + kind + created.
        """
        limit = max(1, min(50, int(limit)))
        try:
            hits = self.memory.search(query, limit=limit,
                                        tier_max=tier_max)
        except Exception as e:
            return {"error": type(e).__name__, "message": str(e)}
        return {
            "query": query,
            "hits": [
                {
                    "id": h["id"],
                    "tier": h["tier"],
                    "kind": h["kind"],
                    "source": h["source"],
                    "content": (h["content"] or "")[:500],
                    "created_utc": h["created_utc"],
                    "access_count": h["access_count"],
                }
                for h in hits
            ],
        }

    def handle_recent_events(self, limit: int) -> dict[str, Any]:
        import harness_telemetry as ht
        rd = ht.run_path(self.run_id, self.pd)
        events_file = rd / "events.jsonl"
        out: list[dict[str, Any]] = []
        if events_file.exists():
            with events_file.open(encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return {"events": out}

    # ---- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        import harness_telemetry as ht
        self._cron_stop.set()
        for t in self._cron_threads:
            t.join(timeout=2.0)
        try:
            self.session.__exit__(None, None, None)
        except Exception:
            pass
        try:
            ht.finalize_run(self.run_id, metrics=self.brain.session_metrics(),
                             projects_dir=self.pd)
        except Exception:
            pass
        try:
            self.memory.close()
        except Exception:
            pass


# ---- HTTP plumbing ----------------------------------------------------------

class HTTPError(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


class Handler(BaseHTTPRequestHandler):
    service: Service  # set by run_server

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quieter access log — write to stderr as a single line
        sys.stderr.write(f"[serve] {self.address_string()} - {fmt % args}\n")

    def _auth_ok(self) -> bool:
        tok = self.service.auth_token
        if not tok:
            return True
        header = self.headers.get("Authorization") or ""
        expected = f"Bearer {tok}"
        # Constant-time comparison — protect against timing side-channels
        # when the token is exposed on a LAN or behind a reverse proxy.
        import hmac
        return hmac.compare_digest(header, expected)

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Hard cap on POST bodies. 1 MiB is plenty for /turn (user_message
    # + metadata); anything larger is almost certainly abuse. Protects
    # against lazy DoS from a client that tries to send GBs.
    MAX_BODY_BYTES = 1 * 1024 * 1024

    def _read_body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > self.MAX_BODY_BYTES:
            raise HTTPError(413, f"payload too large "
                              f"(got {n}, cap {self.MAX_BODY_BYTES})")
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPError(400, f"bad json: {e}")
        if not isinstance(obj, dict):
            raise HTTPError(400, "body must be a JSON object")
        return obj

    def _dispatch(self, method: str) -> None:
        if not self._auth_ok():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            path = self.path.split("?", 1)[0]
            query = {}
            if "?" in self.path:
                from urllib.parse import parse_qs
                query = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

            if method == "GET" and path == "/healthz":
                self._send_json(200, {"status": "ok",
                                       "uptime_s": time.time() - self.service.started_utc})
                return
            if method == "GET" and path == "/stats":
                self._send_json(200, self.service.handle_stats())
                return
            if method == "GET" and path == "/goals":
                self._send_json(200, self.service.handle_goals_list())
                return
            if method == "GET" and path == "/recent_events":
                limit = int(query.get("limit", 50))
                self._send_json(200, self.service.handle_recent_events(limit))
                return
            if method == "GET" and path == "/avatar":
                self._send_json(200, self.service.handle_avatar())
                return
            if method == "GET" and path == "/memory/search":
                q = query.get("q", "")
                lim = int(query.get("limit", 20))
                tmx = query.get("tier_max")
                tier_max = int(tmx) if tmx is not None else None
                self._send_json(200,
                    self.service.handle_memory_search(q, lim, tier_max))
                return
            if method == "GET" and path == "/events_stream":
                self._stream_events()
                return
            if method == "GET" and (path == "/" or path == "/ui"
                                      or path == "/ui/"):
                self._serve_ui_index()
                return
            if method == "GET" and path.startswith("/ui/static/"):
                self._serve_static(path)
                return
            if method == "POST":
                body = self._read_body()
                if path == "/turn":
                    self._send_json(200, self.service.handle_turn(body))
                    return
                if path == "/dream":
                    self.service._cron_dream()
                    self._send_json(200, {"ok": True})
                    return
                if path == "/triage":
                    self.service._cron_triage()
                    self._send_json(200, {"ok": True})
                    return
                if path == "/propose":
                    self.service._cron_propose()
                    self._send_json(200, {"ok": True})
                    return
                if path == "/apply":
                    self.service._cron_apply()
                    self._send_json(200, {"ok": True})
                    return
                if path == "/goals":
                    self._send_json(200, self.service.handle_goals_mutate(body))
                    return
            self._send_json(404, {"error": "not found", "path": path})
        except HTTPError as e:
            self._send_json(e.code, {"error": e.msg})
        except Exception as e:
            self._send_json(500, {"error": type(e).__name__, "message": str(e)})

    # ---- UI static + SSE helpers -------------------------------------------

    _STATIC_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js":   "application/javascript; charset=utf-8",
        ".css":  "text/css; charset=utf-8",
        ".svg":  "image/svg+xml",
        ".json": "application/json",
        ".png":  "image/png",
        ".ico":  "image/x-icon",
    }

    def _ui_root(self) -> Path:
        return Path(__file__).resolve().parent / "mnemosyne_ui" / "static"

    def _serve_ui_index(self) -> None:
        index = self._ui_root() / "index.html"
        if not index.is_file():
            self._send_json(404, {"error": "ui not bundled"})
            return
        body = index.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        rel = path[len("/ui/static/"):]
        # Reject traversal
        target = (self._ui_root() / rel).resolve()
        try:
            target.relative_to(self._ui_root())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found", "path": path})
            return
        ctype = self._STATIC_TYPES.get(target.suffix.lower(),
                                          "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _stream_events(self) -> None:
        """Server-Sent Events feed of new events.jsonl rows.

        Tails the current run's events.jsonl in 1-second polls. Cheap;
        the file is append-only so we only need to remember last seek.
        """
        import harness_telemetry as ht
        rd = ht.run_path(self.service.run_id, self.service.pd)
        events_file = rd / "events.jsonl"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Initial backfill: last 20 lines so the UI doesn't start empty
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except Exception:
            return

        last_size = 0
        if events_file.exists():
            last_size = events_file.stat().st_size
            try:
                with events_file.open("rb") as f:
                    f.seek(max(0, last_size - 8000))
                    tail = f.read().decode("utf-8", errors="replace")
                for line in tail.splitlines()[-20:]:
                    line = line.strip()
                    if line:
                        self.wfile.write(b"data: " + line.encode("utf-8")
                                            + b"\n\n")
                        self.wfile.flush()
            except Exception:
                pass

        # Poll loop
        try:
            while True:
                if not events_file.exists():
                    time.sleep(1.0)
                    continue
                size = events_file.stat().st_size
                if size > last_size:
                    with events_file.open("rb") as f:
                        f.seek(last_size)
                        chunk = f.read(size - last_size).decode(
                            "utf-8", errors="replace")
                    last_size = size
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        self.wfile.write(b"data: " + line.encode("utf-8")
                                            + b"\n\n")
                        self.wfile.flush()
                else:
                    # Heartbeat to keep the connection alive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            return

    def do_GET(self) -> None:     # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:    # noqa: N802
        self._dispatch("POST")


def run_server(service: Service, host: str, port: int) -> None:
    Handler.service = service
    httpd = ThreadingHTTPServer((host, port), Handler)

    def shutdown(signum: int, frame: Any) -> None:
        sys.stderr.write("\n[serve] shutting down ...\n")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    sys.stderr.write(f"[serve] listening on http://{host}:{port}\n")
    sys.stderr.write(f"[serve] run_id: {service.run_id}\n")
    try:
        httpd.serve_forever()
    finally:
        service.shutdown()
        httpd.server_close()


# ---- CLI --------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemosyne-serve",
        description="Long-running Mnemosyne daemon. Owns the memory store, "
                    "serves HTTP /turn, runs dream/triage/proposer/apply on "
                    "cron. Stdlib only. Bind to 127.0.0.1 by default.",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8484)
    p.add_argument("--projects-dir")
    p.add_argument("--dream-every", default="30m",
                   help="interval for dream consolidation (or 'off')")
    p.add_argument("--triage-every", default="10m",
                   help="interval for triage scan (or 'off')")
    p.add_argument("--propose-every", default="30m",
                   help="interval for proposer (or 'off')")
    p.add_argument("--apply-every", default="off",
                   help="interval for auto-apply of accepted proposals (default: off)")
    p.add_argument("--token",
                   help="require Authorization: Bearer <token> on every request "
                        "(overrides MNEMOSYNE_SERVE_TOKEN env var)")
    args = p.parse_args(argv)

    pd = Path(args.projects_dir).expanduser() if args.projects_dir else None

    def _interval(s: str) -> float | None:
        return None if s.strip().lower() == "off" else parse_duration(s)

    token = args.token or os.environ.get("MNEMOSYNE_SERVE_TOKEN") or None

    service = Service(
        projects_dir=pd,
        dream_every_s=_interval(args.dream_every),
        triage_every_s=_interval(args.triage_every),
        propose_every_s=_interval(args.propose_every),
        apply_every_s=_interval(args.apply_every),
        auth_token=token,
    )
    run_server(service, args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
