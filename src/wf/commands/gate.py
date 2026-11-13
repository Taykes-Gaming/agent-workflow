"""wf gate: the project's local gate from `[gate]`, one line per step.

Each step's full output goes to `<git-dir>/wf-gate/<k>-<slug>.log`, unfiltered,
and the command prints one line per step: `ok`, `skip`, or `FAIL` with the
first failure line and the log's path. The summary is what an implementer
reads and pastes; the logs are the evidence, read only for a failing step.

Steps run in order from the repository's top level and stop at the first
failure unless `--keep-going`. `[gate.when]` steps run only when a changed
file (against `origin/<base>`, plus the working tree) matches the glob; `*`
crosses `/`. A step with `xml` names an NUnit 3 results file (Unity's
`-testResults`): it is removed before the step, and the step passes only if
the file then exists with no failed test case.

`--only-failures` prints only `FAIL` lines and the summary. `--watch [GLOB…]`
keeps going after the run: it polls the tracked and untracked files (those
matching the globs, when given) about once a second, waits for a quiet second
after a change, reruns the gate and prints only the steps whose status or
detail flipped, then one summary line; Ctrl-C ends it with the last run's
exit status.
"""

import fnmatch
import re
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ..gh import run_git
from . import WfError, base_branch, changed, seconds, truncate

FAILURE = re.compile(r"\b(error|errors|fail|failed|failure|failures|exception)\b|✗|FAIL", re.I)
NONE = re.compile(r"\b0 (errors?|failed|failures?)\b|\b(errors?|failed|failures?):\s*0\b|\bno errors?\b", re.I)
DETAIL = 160


class Runner:
    """git and the shell: what the gate drives."""

    def git(self, *args):
        return run_git(*args, strip=False)

    def sh(self, command, cwd, log=None, env=None):
        """Runs `command` through the shell, stdout and stderr both in `log` (or inherited), with `env` added to
        the environment; returns the exit status."""
        env = {**os.environ, **env} if env else None
        if log is None:
            return subprocess.run(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, env=env).returncode
        with open(log, "w") as out:
            return subprocess.run(command, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=env).returncode

    def sleep(self, seconds):
        time.sleep(seconds)

    def files(self, top):
        """Tracked and untracked (not ignored) paths relative to `top`."""
        return self.git("-C", str(top), "ls-files", "-co", "--exclude-standard", "-z").split("\0")


RUNNER = Runner()
POLL = 1.0  # seconds between scans, and the quiet period before a rerun


def add_parser(sub, parents):
    p = sub.add_parser("gate", parents=parents, help="run the [gate] steps, one line per step")
    p.add_argument("--keep-going", action="store_true", help="run every step instead of stopping at the first failure")
    p.add_argument("--all", action="store_true", help="run [gate.when] steps whatever changed")
    p.add_argument("--base", help="branch changed files are measured against (default [gate].base, [opencode].base, dev)")
    p.add_argument("--only-failures", action="store_true", help="print only FAIL lines and the summary")
    p.add_argument("--watch", nargs="*", metavar="GLOB",
                   help="after the run, rerun whenever a tracked or untracked file (matching GLOB, if any) changes, "
                        "printing only the steps that flipped")


def log_slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "step"


def first_failure(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for line in lines:
        if FAILURE.search(line) and not NONE.search(line):
            return truncate(line, DETAIL)
    return truncate(lines[-1], DETAIL) if lines else "no output"


def nunit(path):
    """(passed, failed, skipped, first failure) from an NUnit 3 results file, or None if unreadable."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    passed = failed = skipped = 0
    first = None
    for case in root.iter("test-case"):
        result = case.get("result", "")
        if result == "Passed":
            passed += 1
        elif result == "Failed":
            failed += 1
            if first is None:
                message = (case.findtext("failure/message") or "").strip().splitlines()
                first = case.get("fullname") or case.get("name") or "?"
                if message:
                    first += f": {message[0].strip()}"
        else:
            skipped += 1
    return passed, failed, skipped, first


def snapshot(runner, top, globs, ignore):
    """path → mtime for the files `runner.files` lists under `top` (matching any of `globs`, when given),
    `ignore` and `.git` left out; files that vanish mid-scan are left out too."""
    out = {}
    for rel in runner.files(top):
        if not rel or rel == ".git" or rel.startswith(".git/"):
            continue
        if globs and not any(fnmatch.fnmatch(rel, g) for g in globs):
            continue
        path = top / rel
        if ignore in path.parents:
            continue
        try:
            out[rel] = path.stat().st_mtime_ns
        except OSError:
            pass
    return out


def _flips(before, after):
    """(line, record) of the steps whose status or detail differ between two runs, matched by position:
    the plan is the same each run, and step names need not be unique."""
    old = [r for _, r in before if "step" in r]
    out = []
    for k, (line, r) in enumerate((x for x in after if "step" in x[1])):
        was = old[k] if k < len(old) else {}
        if (r["status"], r.get("detail")) != (was.get("status"), was.get("detail")):
            out.append((line, r))
    return out


def run(args, cfg, board):
    gate = cfg.gate
    if not gate:
        raise WfError(f"wf gate needs a [gate] section in {cfg.path}")
    runner = RUNNER
    base = base_branch(cfg, gate, args.base)
    top = Path(runner.git("rev-parse", "--show-toplevel").strip())
    logs = Path(runner.git("rev-parse", "--absolute-git-dir").strip()) / "wf-gate"
    logs.mkdir(parents=True, exist_ok=True)

    results = []
    for line, record in _once(args, gate, runner, base, top, logs):
        results.append((line, record))
        if args.only_failures and record.get("status") in ("ok", "skip", "not run"):
            continue
        yield line, record
    if args.watch is None:
        return
    try:
        seen = snapshot(runner, top, args.watch, logs)
        while True:
            runner.sleep(POLL)
            now = snapshot(runner, top, args.watch, logs)
            if now == seen:
                continue
            while True:  # quiet period: no further change for one poll
                runner.sleep(POLL)
                again = snapshot(runner, top, args.watch, logs)
                if again == now:
                    break
                now = again
            fresh = list(_once(args, gate, runner, base, top, logs))
            # the steps' own output (a results file, a build) changed mtimes: not a reason to run again
            seen = snapshot(runner, top, args.watch, logs)
            flipped = _flips(results, fresh)
            results = fresh
            args.exit_code = 1 if fresh[-1][1]["gate"] == "fail" else 0
            for line, record in flipped:
                if args.only_failures and record["status"] != "fail":
                    continue
                yield line, record
            summary_line, summary = fresh[-1]
            what = ", ".join(f"{truncate(r['step'], 40)} flipped to {'FAIL' if r['status'] == 'fail' else r['status']}"
                             for _, r in flipped) or "unchanged"
            head = summary_line.split(" (")[0]
            yield f"{head} ({what})", {**summary, "flipped": [r["step"] for _, r in flipped]}
    except KeyboardInterrupt:
        return


def _once(args, gate, runner, base, top, logs):
    for old in logs.glob("*.log"):
        old.unlink()
    plan = [(s, None) for s in gate["steps"]]
    if gate["when"]:
        files = None if args.all else changed(runner, base)
        for glob, steps in gate["when"].items():
            hit = files is None or any(fnmatch.fnmatch(f, glob) for f in files)
            plan += [(s, None if hit else f"nothing changed under {glob}") for s in steps]

    failed, ran, total = [], 0, 0.0
    for k, (step, skip) in enumerate(plan, 1):
        name = truncate(step["name"], 60)
        if failed and not args.keep_going:
            yield f"--    {name}: not run", {"step": step["name"], "status": "not run"}
            continue
        if skip:
            yield f"skip  {name}: {skip}", {"step": step["name"], "status": "skip", "detail": skip}
            continue
        log = logs / f"{k}-{log_slug(step['name'])}.log"
        xml = top / step["xml"] if step["xml"] else None
        if xml and xml.exists():
            xml.unlink()
        start = time.monotonic()
        code = runner.sh(step["run"], cwd=str(top), log=log)
        took = time.monotonic() - start
        total += took
        ran += 1
        ok, detail = code == 0, None
        if xml:
            counts = nunit(xml)
            if counts is None:
                ok, detail = False, f"no readable results at {step['xml']}"
            else:
                passed, nfail, skipped, first = counts
                tally = f"{passed} passed, {nfail} failed" + (f", {skipped} skipped" if skipped else "")
                ok = ok and nfail == 0
                detail = tally + (f": {truncate(first, DETAIL)}" if first else "")
                if code != 0 and not first:  # died after writing results: the log says why
                    detail += f"; exit {code}: {first_failure(log.read_text(errors='replace'))}"
        if not ok and detail is None:
            detail = f"exit {code}: {first_failure(log.read_text(errors='replace'))}"
        record = {"step": step["name"], "status": "ok" if ok else "fail", "code": code,
                  "seconds": round(took, 1), "detail": detail, "log": str(log)}
        if ok:
            yield f"ok    {name}  {seconds(took)}" + (f": {detail}" if detail else ""), record
        else:
            failed.append(step["name"])
            yield f"FAIL  {name}  {seconds(took)}: {detail}  (log {log})", record
    if failed:
        args.exit_code = 1
        line = f"gate: {len(failed)} failed ({', '.join(truncate(f, 40) for f in failed)})"
    else:
        line = f"gate: green ({ran} run, {seconds(total)})"
    yield line, {"gate": "fail" if failed else "green", "failed": failed, "ran": ran, "seconds": round(total, 1)}
