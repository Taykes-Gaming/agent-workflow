"""wf logread PATH [--match REGEX] [--exclude REGEX] [--context N] [--last N] [--raw] [--xml].

A log or a test results file, cut down to what an agent acts on. A Unity
batch log is mostly noise an implementer pays for line by line: package
resolution, shader compilation, licensing, the same warning repeated. A
results file is mostly passing cases.

For a text log: known noise is dropped, runs of an identical line collapse to
one with `(×N)`, and a stack trace whose first line was already shown
collapses to that line, `(trace shown above)`. `--match` keeps matching lines
(with `--context` around them), `--exclude` drops more, `--last` caps the
output to the final N lines (default 200), `--raw` keeps every line. A final
line says what was hidden: `logread: 41 of 3200 lines shown (2900 noise, 259
collapsed); <path>, 1.2 MB`.

For `--xml` (or a `.xml` path): NUnit 3 or JUnit results as the tally and one
block per failed case, name, message and first stack frame, nothing for the
passing ones. Needs no config or network.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..gh import WfError
from . import UsageError, regex, truncate

NEEDS_CONFIG = False
DEFAULT_LAST = 200
DETAIL = 240
# Lines that carry nothing an implementer acts on. Unity's batch log first; add a project's own with --exclude.
NOISE = re.compile(
    r"^\[Licensing::|^\[Package Manager\]|^\[Subsystems\]|^\[PhysX\]|^\[XR\]|^\[Performance\]|"
    r"^Refresh(ing native plugins| completed in)|^Mono: |^Loading GUID |^Registering precompiled|"
    r"^Compiling shader|^Shader compiled|^\s*Compiled shader|^- Starting compile |^- Finished compile |"
    r"^Begin MonoManager|^Native extension for|^Initialize engine version|^\[ScriptCompilation\]|"
    r"^Start importing |^  artifactKey:|^  -> \(artifact id|^Start loading |^Done loading |"
    r"^\(Filename: .* Line: \d+\)$|^UnityEngine\.(Debug|Logger|StackTraceUtility):|"
    r"^Memory Statistics:|^\[ALLOC_|^\s*Peak (Allocated|Largest)|^\s*Peak main deferred|"
    r"^Setting up \d+ worker threads|^Player connection |^\[\d+:\d+:\d+\] |"
    r"^Updating .* - GUID: |^\s*Total: \d+\.\d+ ms \(|^\[usbmuxd\]|^IsTimeToCheckForNewEditor",
)
_TRACE = re.compile(r"^(\s+at\s|\s+in\s.*:line\s|\s*File \"|\w[\w.]*\.\w+ \(at |\s+\S+\.\S+ \(\)|\s+\S+\.\S+\(.*\) \(at )")
_EXCEPTION = re.compile(r"^\w*(Exception|Error)\b.*:")


def add_parser(sub, parents):
    p = sub.add_parser("logread", parents=parents, help="a log or test results file, noise and repeats cut")
    p.add_argument("path", help="the log, or an NUnit 3 / JUnit results file")
    p.add_argument("--match", help="keep only lines matching this regex")
    p.add_argument("--exclude", help="drop lines matching this regex as well as the built-in noise")
    p.add_argument("--context", type=int, default=0, help="with --match: lines of log around each match")
    p.add_argument("--last", type=int, help=f"show at most the last N lines (default {DEFAULT_LAST})")
    p.add_argument("--raw", action="store_true", help="no noise filter and no collapsing; --match/--exclude still apply")
    p.add_argument("--xml", action="store_true", help="read as test results (NUnit 3 or JUnit)")


def filter_lines(lines, match=None, exclude=None, context=0, raw=False):
    """(kept lines, counts): noise dropped, runs and repeated traces collapsed, then --match/--exclude."""
    noise = 0
    kept = []
    if not raw:
        for line in lines:
            if NOISE.search(line):
                noise += 1
            else:
                kept.append(line)
        kept, collapsed = collapse(kept)
    else:
        kept, collapsed = list(lines), 0
    if exclude:
        before = len(kept)
        kept = [x for x in kept if not exclude.search(x)]
        noise += before - len(kept)
    if match:
        hits = [i for i, x in enumerate(kept) if match.search(x)]
        keep = set()
        for i in hits:
            keep.update(range(max(0, i - context), min(len(kept), i + context + 1)))
        kept = [x for i, x in enumerate(kept) if i in keep]
        matched = len(hits)
    else:
        matched = None
    return kept, {"noise": noise, "collapsed": collapsed, "matched": matched}


def collapse(lines):
    """Runs of one line become one with `(×N)`; a stack trace under an exception line seen before becomes
    that line with `(trace shown above)`. Returns (lines, how many were folded away)."""
    out, folded, seen_traces = [], 0, set()
    i = 0
    while i < len(lines):
        line = lines[i]
        j = i + 1
        while j < len(lines) and lines[j] == line:
            j += 1
        run = j - i
        if _EXCEPTION.search(line.strip()) and j < len(lines) and _TRACE.search(lines[j]):
            k = j
            while k < len(lines) and _TRACE.search(lines[k]):
                k += 1
            key = line.strip()
            if key in seen_traces:
                out.append(f"{line}  (×{run})" if run > 1 else line)
                out.append("  (trace shown above)")
                folded += (k - j) + (run - 1)
            else:
                seen_traces.add(key)
                out.append(f"{line}  (×{run})" if run > 1 else line)
                out.extend(lines[j:k])
                folded += run - 1
            i = k
            continue
        out.append(f"{line}  (×{run})" if run > 1 else line)
        folded += run - 1
        i = j
    return out, folded


def _size(n):
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{n / 1000:.0f} kB" if n >= 1000 else f"{n} B"


def results(path):
    """Test results as (tally dict, [failure dicts]) from NUnit 3 (`test-case`) or JUnit (`testcase`)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        raise WfError(f"cannot read test results at {path}: {e}") from None
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    failures = []
    for case in root.iter("test-case"):  # NUnit 3
        result = case.get("result", "")
        if result == "Passed":
            counts["passed"] += 1
        elif result == "Failed":
            counts["failed"] += 1
            failures.append(_failure(case.get("fullname") or case.get("name") or "?",
                                     case.findtext("failure/message"), case.findtext("failure/stack-trace")))
        else:
            counts["skipped"] += 1
    for case in root.iter("testcase"):  # JUnit
        name = ".".join(x for x in (case.get("classname"), case.get("name")) if x) or "?"
        bad = case.find("failure") if case.find("failure") is not None else case.find("error")
        if bad is not None:
            counts["failed"] += 1
            failures.append(_failure(name, bad.get("message") or (bad.text or "").strip().split("\n")[0], bad.text))
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return counts, failures


def _failure(name, message, trace):
    message = (message or "").strip().splitlines()
    frames = [x.strip() for x in (trace or "").splitlines() if x.strip()]
    frame = next((x for x in frames if re.search(r"\bat\b|:line \d+|\.cs:\d+|\.py\", line", x)), frames[0] if frames else "")
    return {"name": name, "message": truncate(message[0] if message else "", DETAIL), "frame": truncate(frame, DETAIL)}


def run(args, cfg, board):
    path = Path(args.path)
    if not path.is_file():
        raise WfError(f"no file at {path}")
    if args.xml or path.suffix.lower() == ".xml":
        counts, failures = results(path)
        tally = f"{counts['passed']} passed, {counts['failed']} failed" + (
            f", {counts['skipped']} skipped" if counts["skipped"] else "")
        for f in failures:
            yield f"FAIL  {f['name']}", {"failure": f}
            if f["message"]:
                yield f"      {f['message']}", {"failure": f, "message": True}
            if f["frame"]:
                yield f"      {f['frame']}", {"failure": f, "frame": True}
        yield f"logread: {tally}; {path}", {"results": counts, "path": str(path)}
        if counts["failed"]:
            args.exit_code = 1
        return
    match, exclude = regex(args.match, "match"), regex(args.exclude, "exclude")
    if args.context and not match:
        raise UsageError("--context goes with --match")
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    kept, counts = filter_lines(lines, match, exclude, args.context, args.raw)
    last = args.last if args.last is not None else DEFAULT_LAST
    cut = max(0, len(kept) - last) if last > 0 else 0
    shown = kept[cut:] if last > 0 else kept
    if cut:
        yield f"… {cut} earlier lines (--last {last}; raise it or --match to see them)", {"cut": cut}
    for line in shown:
        yield line, {"line": line}
    hidden = []
    if counts["noise"]:
        hidden.append(f"{counts['noise']} noise")
    if counts["collapsed"]:
        hidden.append(f"{counts['collapsed']} collapsed")
    if counts["matched"] is not None:
        hidden.append(f"{counts['matched']} matched")
    summary = f"logread: {len(shown)} of {len(lines)} lines shown"
    if hidden:
        summary += f" ({', '.join(hidden)})"
    yield f"{summary}; {path}, {_size(len(text.encode('utf-8', errors='replace')))}", \
        {"shown": len(shown), "lines": len(lines), **counts, "path": str(path)}
