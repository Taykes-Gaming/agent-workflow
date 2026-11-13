"""wf hook: Claude Code's SubagentStop and Stop hooks, appending to the spend ledger.

The wf-setup plugin runs it on both events (`hooks/hooks.json`) with the
event's JSON on stdin. SubagentStop measures the subagent's transcript there
and then, as `wf usage` does, and appends an `agent` line; Stop appends a
`session` line naming the main session's transcript, measured later by `wf
spend`. The line goes to the ledger of the branch checked out where the work
happened: the subagent's last working directory in its transcript (an
implementer works in its own worktree), else the event's `cwd`. Read-only
agent types are not implementers and are skipped.

A hook must never get in the session's way: it prints nothing and exits 0
whatever happens (no git, no transcript, bad JSON); a missed line is a
number `wf merged` cannot measure, and it says so.
"""

import json
import sys
from pathlib import Path

from .. import ledger
from ..gh import WfError
from . import usage

NEEDS_CONFIG = False
# Subagents that only read; never an implementer's work.
READ_ONLY_AGENTS = {"Explore", "Plan", "claude-code-guide", "statusline-setup"}


def add_parser(sub, parents):
    sub.add_parser("hook", parents=parents, help="Claude Code hook: record a stopped agent or session in the ledger")


def _last_cwd(transcript):
    try:
        lines = Path(transcript).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"cwd"' not in line:
            continue
        try:
            cwd = json.loads(line).get("cwd")
        except (ValueError, AttributeError):
            continue
        if cwd:
            return cwd
    return None


def _where(*cwds):
    for cwd in cwds:
        if not cwd:
            continue
        try:
            return ledger.where(cwd)
        except (WfError, OSError):
            continue
    raise WfError("not in a git checkout")


def record(event):
    """Appends the event's ledger line; returns its path, or None for an event wf does not record."""
    name = event.get("hook_event_name")
    if name == "SubagentStop":
        kind = event.get("agent_type")
        if kind in READ_ONLY_AGENTS:
            return None
        transcript = event["agent_transcript_path"]
        t = usage.measure(transcript)
        common, branch = _where(_last_cwd(transcript), event.get("cwd"))
        return ledger.append(common, branch, {
            "kind": "agent", "id": event.get("agent_id"), "agent_type": kind, "transcript": transcript,
            "model": "/".join(t["models"]), "tokens": t["tokens"], "cache_read": t["cache_read"],
            "turns": t["turns"]})
    if name == "Stop":
        common, branch = _where(event.get("cwd"))
        return ledger.append(common, branch, {"kind": "session", "id": event.get("session_id"),
                                              "transcript": event.get("transcript_path"), "cwd": event.get("cwd")})
    return None


def run(args, cfg, board):
    try:
        record(json.load(sys.stdin))
    except Exception:  # a hook never fails the session it runs in
        pass
    yield from ()
