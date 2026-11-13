"""wf wait [PRESET] [--file PATH | --run CMD] --until REGEX [--fail REGEX] [--timeout DUR] [--context N] [--resume].

Blocks until a log says the thing happened, and prints only that. A long
Unity batch run, a Scenario job or a dev server starting up otherwise costs an
agent a sleep-and-read loop, each read a page of log it does not need.

The target is a file to follow (`--file`, created later is fine) or a command
to start (`--run`), whose stdout and stderr go to `<git-dir>/wf-wait/<n>.log`
as they arrive, with the pid beside it. The first `--until` match ends the
wait with exit 0: the matching line, `--context` lines around it, and the
log's path; a started command keeps running. A `--fail` match (by default
the gate's failure words) exits 1 the same way, the command dying before a
match exits 1 with its exit status and first failure line, and `--timeout`
(default 20m) exits 124 with the log's last line.

`wf wait --resume` re-attaches to the last command this checkout started,
after a network drop or a new session: the log is searched from its start,
so a match that already landed is reported at once.

A `[wait.<name>]` preset in .workflow.toml holds any of these flags under a
name, so `wf wait unity` is enough; flags on the command line win.
"""

import codecs
import json
import os
import re
import time
from pathlib import Path

from .. import config
from ..gh import WfError, run_git
from . import UsageError, regex, seconds, truncate
from .gate import FAILURE, NONE, first_failure

NEEDS_CONFIG = False
POLL = 0.5  # seconds between reads of the log
DEFAULT_TIMEOUT = "20m"
TIMEOUT_EXIT = 124  # the timeout(1) convention; usage errors stay 2
DETAIL = 200
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$")


def add_parser(sub, parents):
    p = sub.add_parser("wait", parents=parents, help="block until a log matches a regex; print only the match")
    p.add_argument("preset", nargs="?", help="a [wait.<name>] preset from .workflow.toml")
    p.add_argument("--file", help="the log to follow (it may not exist yet)")
    p.add_argument("--run", dest="command", help="a command to start, its output logged under <git-dir>/wf-wait/")
    p.add_argument("--until", help="regex that ends the wait with success")
    p.add_argument("--fail", help="regex that ends the wait with failure (default: the gate's failure words)")
    p.add_argument("--timeout", help=f"give up after this long, e.g. 90s, 20m, 1h30m (default {DEFAULT_TIMEOUT})")
    p.add_argument("--context", type=int, help="lines of log to show around the match (default 2)")
    p.add_argument("--resume", action="store_true", help="re-attach to the last --run command this checkout started")


def duration(text):
    m = _DURATION.match(text.strip()) if isinstance(text, str) else None
    if not m or not any(m.groups()):
        raise UsageError(f"bad duration '{text}': want seconds, or 90s, 20m, 1h30m")
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def settings(args):
    """The merged preset and flags: {file|run, until, fail, timeout (seconds), context}."""
    s = {}
    if args.preset:
        cfg = config.load()
        if not cfg.wait or args.preset not in cfg.wait:
            known = ", ".join(sorted(cfg.wait or {})) or "none"
            raise UsageError(f"no [wait.{args.preset}] in {cfg.path}; presets: {known}")
        s.update(cfg.wait[args.preset])
    for key, flag in (("file", "file"), ("run", "command"), ("until", "until"), ("fail", "fail"),
                      ("timeout", "timeout"), ("context", "context")):
        if getattr(args, flag) is not None:
            s[key] = getattr(args, flag)
    if args.file is not None and args.command is not None:
        raise UsageError("--file and --run name two targets; pass one")
    if args.resume:
        s.pop("file", None), s.pop("run", None)
    elif args.command is not None:
        s.pop("file", None)
    elif args.file is not None:
        s.pop("run", None)
    targets = [k for k in ("file", "run") if s.get(k)] + (["resume"] if args.resume else [])
    if len(targets) != 1:
        raise UsageError("wait needs exactly one of --file, --run or --resume (a preset may name run or file)")
    if not s.get("until"):
        raise UsageError("wait needs --until REGEX (or a preset with until)")
    s["until"] = regex(s["until"], "until")
    s["fail"] = regex(s.get("fail") or None, "fail")
    s["timeout"] = duration(s.get("timeout", DEFAULT_TIMEOUT))
    s["context"] = s.get("context", 2)
    return s


def _fails(s, line):
    if s["fail"] is not None:
        return s["fail"].search(line)
    return FAILURE.search(line) and not NONE.search(line)


class Follower:
    """Reads a growing file from an offset, yielding whole lines; a partial last line waits for its end. A
    file truncated below the offset (rotated, rewritten) is read again from its start."""

    def __init__(self, path):
        self.path, self.pos = Path(path), 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.partial = ""

    def lines(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() < self.pos:
                    self.pos, self.partial = 0, ""
                    self.decoder.reset()
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        if not data:
            return []
        self.pos += len(data)
        text = self.partial + self.decoder.decode(data)
        parts = text.split("\n")
        self.partial = parts.pop()
        return parts

    def flush(self):
        """The partial line, once nothing more will come."""
        rest, self.partial = self.partial + self.decoder.decode(b"", final=True), ""
        return [rest] if rest else []


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _runs_dir():
    try:
        return Path(run_git("rev-parse", "--absolute-git-dir")) / "wf-wait"
    except WfError:
        raise WfError("wf wait --run keeps its log under the repository's .git; run it inside a checkout, "
                      "or follow a file with --file") from None


def start(command):
    """Starts `command` in its own session, output to a new log, and returns its record. Spawned directly,
    not through Popen: the command outlives this process when the match lands first."""
    runs = _runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    n = max([int(p.stem) for p in runs.glob("*.json") if p.stem.isdigit()] + [0]) + 1
    log = runs / f"{n}.log"
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        pid = os.posix_spawn("/bin/sh", ["sh", "-c", command], os.environ, setsid=True,
                             file_actions=[(os.POSIX_SPAWN_DUP2, devnull, 0), (os.POSIX_SPAWN_DUP2, out, 1),
                                           (os.POSIX_SPAWN_DUP2, out, 2)])
    finally:
        os.close(out)
        os.close(devnull)
    record = {"n": n, "pid": pid, "command": command, "log": str(log), "started": time.time(), "cwd": os.getcwd()}
    (runs / f"{n}.json").write_text(json.dumps(record, indent=2) + "\n")
    (runs / "last").write_text(str(n))
    return record


def last_run():
    runs = _runs_dir()
    try:
        n = (runs / "last").read_text().strip()
        return json.loads((runs / f"{n}.json").read_text())
    except (OSError, json.JSONDecodeError):
        raise WfError(f"no earlier wf wait --run in {runs} to resume") from None


class Child:
    """A started command's exit, by pid: reaped with waitpid when it is ours, else a liveness check."""

    def __init__(self, pid, own):
        self.pid, self.own, self.code = pid, own, None

    def exited(self):
        """The exit status once the command is gone (an int, or "?" when it was not ours), else None."""
        if self.code is not None:
            return self.code
        if self.own:
            try:
                done, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                done, status = self.pid, 0
            if done:
                self.code = os.waitstatus_to_exitcode(status)
        elif not _alive(self.pid):
            self.code = "?"
        return self.code


def run(args, cfg, board):
    s = settings(args)
    child = record = None
    if args.resume:
        record = last_run()
        if not Path(record["log"]).exists():
            raise WfError(f"the last run's log is gone ({record['log']})")
        child, path = Child(record["pid"], own=False), record["log"]
        yield (f"wait: re-attached to run {record['n']} (pid {record['pid']}, "
               f"{'running' if _alive(record['pid']) else 'exited'}): {truncate(record['command'], 80)}",
               {"resume": True, **record})
    elif s.get("run"):
        record = start(s["run"])
        child, path = Child(record["pid"], own=True), record["log"]
        yield f"wait: started pid {record['pid']}: {truncate(s['run'], 80)}; log {path}", {"started": True, **record}
    else:
        path = s["file"]
    start_at = time.monotonic()
    follower = Follower(path)
    recent = []  # the last `context` lines before the match
    outcome = None  # (status, line)
    shown = []
    done = False
    while outcome is None:
        exited = child is not None and child.exited() is not None
        lines = follower.lines()
        if exited:
            lines += follower.flush()
        for line in lines:
            if s["until"].search(line):
                outcome = ("ok", line)
            elif _fails(s, line):
                outcome = ("fail", line)
            if outcome:
                shown = recent[-s["context"]:] + [line] if s["context"] else [line]
                break
            recent.append(line)
            del recent[:-max(s["context"], 1)]
        if outcome:
            break
        if exited:
            done = True
            break
        if time.monotonic() - start_at >= s["timeout"]:
            break
        time.sleep(POLL)
    took = time.monotonic() - start_at
    base = {"log": str(path), "seconds": round(took, 1), "pid": record["pid"] if record else None}
    if outcome:
        status, line = outcome
        for x in shown:
            yield x, {"line": x, **base}
        if status == "ok":
            yield f"wait: matched after {seconds(took)} (log {path})", {"wait": "ok", "match": line, **base}
        else:
            args.exit_code = 1
            yield f"wait: FAIL after {seconds(took)}: {truncate(line, DETAIL)} (log {path})", \
                {"wait": "fail", "match": line, **base}
        return
    text = Path(path).read_text(errors="replace") if Path(path).exists() else ""
    if done:
        code = child.exited()
        args.exit_code = 1
        yield (f"wait: FAIL: command exited {code} after {seconds(took)} without a match: "
               f"{first_failure(text)} (log {path})", {"wait": "exited", "code": code, **base})
        return
    args.exit_code = TIMEOUT_EXIT
    last = [x for x in text.splitlines() if x.strip()]
    tail = truncate(last[-1], DETAIL) if last else ("no output yet" if Path(path).exists() else "file not created")
    running = f"; pid {record['pid']} still running" if record and _alive(record["pid"]) else ""
    yield (f"wait: timeout after {seconds(took)}: {tail}{running} (log {path})",
           {"wait": "timeout", "last": tail, **base})

