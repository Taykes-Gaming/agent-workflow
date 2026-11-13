"""What `wf review` and `wf dispatch` share of opencode: its version gate, session export and the run's
events read back as a `claude -p` result."""

import json
import re
import subprocess
import tempfile

from .gh import WfError

MARKER = "<!-- wf-review -->"  # opens every review the recipe posts; the text before it is not the review
# --standalone, model#variant, `session export` and the native config shape are all opencode 2.1's.
OPENCODE_MIN = (2, 1)
_VERSION = re.compile(r"(\d+)\.(\d+)")


def opencode_version():
    try:
        proc = subprocess.run(["opencode", "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise WfError("opencode is not installed or not on PATH") from None
    return (proc.stdout or proc.stderr).strip()


def require_opencode(version):
    """Fails on an opencode older than OPENCODE_MIN, whose flags and config shape wf no longer passes."""
    m = _VERSION.search(version or "")
    if not m or (int(m[1]), int(m[2])) < OPENCODE_MIN:
        need = ".".join(map(str, OPENCODE_MIN))
        raise WfError(f"wf needs opencode {need} or later (found {version or 'no version'}); run `opencode upgrade`")


def export_session(session):
    """`opencode session export`, from a private server so it works whether or not the shared one runs.
    Its stdout goes to a file: opencode cuts piped stdout off at 64 KB when it exits."""
    with tempfile.TemporaryFile("w+") as out:
        try:
            subprocess.run(["opencode", "session", "export", "--standalone", session], stdout=out,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            out.seek(0)
            return json.loads(out.read())
        except (OSError, json.JSONDecodeError):
            return None


def _add_usage(usage, model, tokens):
    m = usage.setdefault(model, {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0,
                                 "cacheCreationInputTokens": 0})
    cache = tokens.get("cache") or {}
    m["inputTokens"] += tokens.get("input", 0)
    m["outputTokens"] += tokens.get("output", 0) + tokens.get("reasoning", 0)
    m["cacheReadInputTokens"] += cache.get("read", 0)
    m["cacheCreationInputTokens"] += cache.get("write", 0)


def _lane_session(state):
    meta = state.get("metadata") or {}
    session = (meta.get("metadata") or {}).get("sessionID") or meta.get("sessionID")
    if not session:
        m = re.search(r"sessionID[=:]\s*\"?(ses_\w+)", str(state.get("output") or ""))
        session = m and m[1]
    return session


def opencode_result(events, model, export, seconds):
    """The run as a `claude -p` result (total_cost_usd, modelUsage, num_turns…), so _usage, _keep and
    tools/review_summary.py read it unchanged. Lane sessions are not in the event stream: their cost
    comes from `opencode session export <session>`, by the id each subagent call reports. A lane
    launched in the background is listed apart: under --standalone it ends with the run."""
    cost, turns, usage, text, errors, lanes, background = 0.0, 0, {}, [], [], [], []
    for e in events:
        part = e.get("part") or {}
        if e.get("type") == "error":
            err = e.get("error")
            data = err.get("data") if isinstance(err, dict) else None
            errors.append(str((data.get("message") if isinstance(data, dict) else None) or err)[:300])
        elif e.get("type") == "step_start":
            text = []
        elif e.get("type") == "text":
            text.append(part.get("text") or "")
        elif e.get("type") == "step_finish":
            cost += float(part.get("cost") or 0)
            turns += 1
            _add_usage(usage, model.split("/", 1)[-1], part.get("tokens") or {})
        elif e.get("type") == "tool_use" and part.get("tool") == "subagent":
            state = part.get("state") or {}
            session = _lane_session(state)
            if session:
                lanes.append(session)
            if (state.get("input") or {}).get("background") is True:
                background.append(session or "?")
    unpriced = []
    for session in lanes:
        child = export(session)
        if not child:
            unpriced.append(session)
            continue
        for msg in child.get("messages") or []:
            if msg.get("type") == "assistant":
                cost += float(msg.get("cost") or 0)
                turns += 1
                _add_usage(usage, (msg.get("model") or {}).get("id") or "?", msg.get("tokens") or {})
    review = "".join(text).strip()
    if MARKER in review:
        review = review[review.index(MARKER):]
    return {"is_error": bool(errors), "terminal_reason": "error" if errors else "completed",
            "result": "; ".join(errors) if errors else review, "total_cost_usd": cost, "num_turns": turns,
            "modelUsage": usage, "duration_ms": int(seconds * 1000),
            "subagent_stats": {"spawned": len(lanes), "unpriced": unpriced, "background": background},
            "engine": "opencode"}
