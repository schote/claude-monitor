"""Claude usage daemon - serves 5h / weekly usage for the ESP32 display.

Runs on the Raspberry Pi next to Claude Code and shells out to

    claude -p "/usage" --output-format json

`/usage` is handled entirely inside the CLI: it costs no tokens, starts no
model turn, and returns the *account-wide* figures the server knows about -
so the numbers cover every device on the subscription, not just this one,
and stay current no matter which device was last active.

    uv run claude-usage-daemon              # or --check for a one-shot poll

Configuration lives in config.toml (see config.example.toml), not in the
environment.

Endpoints
    GET /usage    compact JSON for the microcontroller
    GET /healthz  liveness plus the raw CLI text of the last poll
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config - see config.example.toml
#
# Read from the first of:
#   1. --config PATH
#   2. ./config.toml            (the service sets WorkingDirectory)
#   3. ~/.config/claude-usage-daemon/config.toml
#   4. built-in defaults
# ---------------------------------------------------------------------------

CONFIG_PATHS = (
    Path("config.toml"),
    Path.home() / ".config/claude-usage-daemon/config.toml",
)


# "::" and not "0.0.0.0": the latter binds IPv4 *only*, and a client that
# resolves the host's .local name is handed an AAAA record first - it then
# connects over IPv6 and finds nothing listening, while ssh to the same name
# works because sshd binds both families.
#
# "::" alone is not enough either: asyncio sets IPV6_V6ONLY on the socket it
# creates, which locks IPv4 clients out - the ESP32 among them. So the
# listening socket is built by serve() with IPV6_V6ONLY cleared and handed
# to uvicorn ready-made.
@dataclass
class Config:
    claude_bin: str = ""
    claude_dir: Path = Path("~/.claude").expanduser()
    workdir: Path = Path("/tmp")
    prune_sessions: bool = True
    poll_ttl_s: float = 300.0
    cli_timeout_s: float = 60.0
    host: str = "::"
    port: int = 8000
    source: str = "defaults"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is None:
            path = next((p for p in CONFIG_PATHS if p.is_file()), None)
        if path is None:
            return cls()
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)

        claude = raw.get("claude", {})
        poll = raw.get("poll", {})
        server = raw.get("server", {})
        cfg = cls(
            claude_bin=str(claude.get("bin", "")),
            claude_dir=Path(claude.get("dir", "~/.claude")).expanduser(),
            workdir=Path(claude.get("workdir", "/tmp")).expanduser(),
            prune_sessions=bool(claude.get("prune_sessions", True)),
            poll_ttl_s=float(poll.get("ttl_s", 300)),
            cli_timeout_s=float(poll.get("cli_timeout_s", 60)),
            host=str(server.get("host", "::")),
            port=int(server.get("port", 8000)),
            source=str(path),
        )
        return cfg

    @property
    def cli(self) -> str:
        # A systemd user unit has a thin PATH, so which() often misses even
        # when the CLI is installed - hence the explicit last resort.
        return self.claude_bin or shutil.which("claude") or str(
            Path.home() / ".local/bin/claude"
        )


CFG = Config.load()


# ---------------------------------------------------------------------------
# Parsing
#
# The CLI prints, as of Claude Code 2.1.251:
#
#   You are currently using your subscription to power your Claude Code usage
#
#   Current session: 69% used · resets Aug 30, 8:40pm (Europe/Berlin)
#   Current week (all models): 43% used · resets Sep 4, 4pm (Europe/Berlin)
#
# Minutes are dropped on the hour ("4pm", not "4:00pm"), and a plan may add
# a model-specific week line ("Current week (Opus): ..."), so the patterns
# below stay deliberately loose and every field is optional.
# ---------------------------------------------------------------------------

_SESSION_RE = re.compile(r"current session:\s*(\d+)%", re.I)
_WEEK_RE = re.compile(r"current week\s*\(([^)]*)\):\s*(\d+)%", re.I)
_RESET_RE = re.compile(r"resets\s+([A-Za-z]{3}\s+\d{1,2},\s*[\d:apm]+)\s*(?:\(([^)]+)\))?", re.I)


def _parse_reset(text: str, now: float) -> float | None:
    """'Aug 30, 8:40pm (Europe/Berlin)' -> epoch seconds."""
    m = _RESET_RE.search(text)
    if not m:
        return None
    stamp, tzname = m.group(1).strip(), (m.group(2) or "").strip()
    try:
        tz = ZoneInfo(tzname) if tzname else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None

    year = datetime.fromtimestamp(now, tz).year
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d, %H:%M"):
        try:
            dt = datetime.strptime("%s %d" % (stamp, year), fmt + " %Y")
        except ValueError:
            continue
        if tz is not None:
            dt = dt.replace(tzinfo=tz)
        ts = dt.timestamp()
        # No year in the string: a reset that looks long past is next year's.
        if ts < now - 86400:
            ts = dt.replace(year=year + 1).timestamp()
        return ts
    return None


def parse_usage(text: str, now: float) -> dict:
    """Turn the CLI's report into the payload the board consumes."""
    out: dict = {}
    for line in text.splitlines():
        m = _SESSION_RE.search(line)
        if m:
            out["five_hour_pct"] = int(m.group(1))
            reset = _parse_reset(line, now)
            out["five_hour_end"] = reset
            continue
        m = _WEEK_RE.search(line)
        if m:
            scope, pct = m.group(1).strip().lower(), int(m.group(2))
            reset = _parse_reset(line, now)
            if "all models" in scope or "weekly_pct" not in out:
                out["weekly_pct"] = pct
                out["weekly_end"] = reset
            else:
                # e.g. "Current week (Opus)" - carried through for the API,
                # ignored by the display.
                out["model_week_scope"] = scope
                out["model_week_pct"] = pct
    if "five_hour_pct" not in out and "weekly_pct" not in out:
        raise ValueError("no usage figures in CLI output: %r" % text[:200])
    return out


# ---------------------------------------------------------------------------
# Polling the CLI
# ---------------------------------------------------------------------------


def run_cli() -> tuple[str, str | None]:
    """Returns (report text, session id). Raises on any failure."""
    proc = subprocess.run(
        [CFG.cli, "-p", "/usage", "--output-format", "json"],
        cwd=str(CFG.workdir),
        capture_output=True,
        text=True,
        timeout=CFG.cli_timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "claude exited %d: %s" % (proc.returncode, (proc.stderr or "")[:200])
        )
    payload = json.loads(proc.stdout)
    if payload.get("is_error"):
        raise RuntimeError("claude reported an error: %s" % str(payload)[:200])
    return payload.get("result", ""), payload.get("session_id")


def _prune(session_id: str | None) -> None:
    """Delete just the transcript this poll created."""
    if not (CFG.prune_sessions and session_id):
        return
    slug = "-" + str(CFG.workdir).strip("/").replace("/", "-")
    path = CFG.claude_dir / "projects" / slug / (session_id + ".jsonl")
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Claude usage daemon", docs_url=None, redoc_url=None)

_lock = threading.Lock()
_sample: dict = {}      # last successful parse, plus "_at" and "_raw"
_error: str | None = None
_attempted_at = 0.0     # last CLI attempt, successful or not


def _refresh(now: float) -> None:
    """Run the CLI and publish the result. Called only by the poller thread.

    The subprocess deliberately runs *outside* _lock: a request that had to
    wait behind a slow CLI run would blow the display's 5 s socket timeout
    and be reported on screen as "no daemon", even though the daemon was
    healthy and simply busy.
    """
    global _sample, _error, _attempted_at
    _attempted_at = now
    try:
        text, session_id = run_cli()
        parsed = parse_usage(text, now)
        _prune(session_id)
        parsed["_at"] = now
        parsed["_raw"] = text
        # Wall-clock of this poll, preformatted in the Pi's local timezone:
        # the board has no clock and no tz database, so it can only print a
        # string it is handed.
        parsed["_at_str"] = time.strftime("%H:%M", time.localtime(now))
        with _lock:
            _sample = parsed
            _error = None
    except Exception as exc:  # keep serving the last good sample
        with _lock:
            _error = "%s: %s" % (type(exc).__name__, exc)


def _poll_loop() -> None:
    """Refresh on a timer of its own, forever, whether or not anyone asks.

    Polling on demand meant the first request after an idle stretch paid for
    the CLI run, so the sample is kept warm here instead and every request
    is answered straight from memory. The thread is never allowed to die:
    _refresh already swallows poll failures, and the guard below catches
    anything it could not.
    """
    while True:
        try:
            _refresh(time.time())
        except Exception as exc:  # pragma: no cover - belt and braces
            print("poller: %s: %s" % (type(exc).__name__, exc), flush=True)
        time.sleep(CFG.poll_ttl_s)


def _reset_seconds(end: float | None, now: float) -> int:
    # -1 means "unknown" - the display leaves the countdown blank rather
    # than pretending the window is about to roll over.
    if end is None:
        return -1
    return max(0, int(end - now))


@app.get("/usage")
def usage() -> JSONResponse:
    """Answer from the cached sample, always immediately.

    Nothing here blocks: no subprocess, no lock held across I/O. That is
    what lets the ESP32 keep a short socket timeout and treat any failure
    to answer as a genuine "the daemon or the network is gone".
    """
    now = time.time()
    with _lock:
        sample = dict(_sample)

    if not sample:
        return JSONResponse(
            {"error": _error or "no sample yet"}, status_code=503
        )

    body = {
        "five_hour_pct": sample.get("five_hour_pct", 0),
        "weekly_pct": sample.get("weekly_pct", 0),
        # Seconds remaining, not absolute times - the board has no clock sync.
        "five_hour_reset_s": _reset_seconds(sample.get("five_hour_end"), now),
        "weekly_reset_s": _reset_seconds(sample.get("weekly_end"), now),
        # How old the underlying CLI poll is, so the board can show "stale"
        # even while the daemon itself answers happily.
        "age_s": int(now - sample.get("_at", now)),
        # When the figures were actually read from Claude, as epoch seconds
        # and as a ready-to-print local time.
        "polled_at": int(sample.get("_at", now)),
        "polled_at_str": sample.get("_at_str", ""),
        "ts": int(now),
    }
    if "model_week_pct" in sample:
        body["model_week_pct"] = sample["model_week_pct"]
        body["model_week_scope"] = sample["model_week_scope"]
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _lan_address() -> str | None:
    """The local address a client on the LAN would reach this host on.

    connect() on a UDP socket sends no packets - it only asks the kernel
    which source address the route to that destination would use. Reported
    by /healthz because it is the number that goes in the display's config,
    and it changes whenever the Pi moves between Wi-Fi and a cable.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))     # TEST-NET-1, routed nowhere
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": bool(_sample),
        "address": _lan_address(),
        "claude_bin": CFG.cli,
        "config": CFG.source,
        "last_error": _error,
        "age_s": int(time.time() - _sample["_at"]) if _sample else None,
        "polled_at": int(_sample["_at"]) if _sample else None,
        "polled_at_str": _sample.get("_at_str"),
        "poll_ttl_s": CFG.poll_ttl_s,
        "timezone": time.strftime("%Z"),
        "raw": _sample.get("_raw"),
    }


def serve(cfg: Config) -> None:
    """Bind one socket that answers on both families, then let uvicorn use it."""
    import uvicorn

    threading.Thread(target=_poll_loop, name="poller", daemon=True).start()

    if ":" in cfg.host:                       # IPv6 literal, "::" included
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            # A platform that refuses dual-stack: say so, rather than
            # silently locking out the IPv4-only display.
            print('warning: could not clear IPV6_V6ONLY - IPv4 clients cannot '
                  'connect; set server.host = "0.0.0.0" instead')
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((cfg.host, cfg.port))
    sock.listen(128)

    uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port)).run(
        sockets=[sock]
    )


def main() -> None:
    global CFG

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", type=Path, help="path to config.toml")
    ap.add_argument("--check", action="store_true",
                    help="poll the CLI once, print the result, and exit")
    args = ap.parse_args()

    if args.config is not None:
        if not args.config.is_file():
            sys.exit("no such config file: %s" % args.config)
        CFG = Config.load(args.config)

    print("config: %s | cli: %s | poll every %gs"
          % (CFG.source, CFG.cli, CFG.poll_ttl_s))

    if args.check:
        _refresh(time.time())
        if _error:
            sys.exit("CLI poll failed - %s" % _error)
        print(_sample["_raw"])
        return

    serve(CFG)


if __name__ == "__main__":
    main()
