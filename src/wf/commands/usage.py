"""wf usage <agent>… | --sessions: model, turns and kTokens from Claude Code transcripts.

The diagnostic, and the fallback for a checkout without the ledger (`wf
hook`): `wf merged` and `wf spend` read the ledger, which names each
transcript, and measure with `measure` here. Reads Claude Code's transcripts under `<projects>`, which is
`$CLAUDE_CONFIG_DIR/projects` or `~/.claude/projects`. An implementer is a
subagent, `<projects>/<project>/<session>/subagents/agent-<id>.jsonl`, named
by its id (with or without `agent-`) or by a transcript path. `--sessions`
reads the main sessions instead, `<projects>/<project>/<session>.jsonl`: the
orchestrator and every other interactive session started in this checkout,
not their subagents. `<project>` is the checkout's path with every character
but a letter or digit made `-`, as Claude Code names it.

One API response is written as several lines (one per content block), each
repeating the response's usage, so every message id is counted once, across
files too: a resumed or forked session repeats the history it came from.

Transcripts appear to record a response's output tokens as they stood when it
began streaming, so output is undercounted; it is a small share of kTokens.

kTokens is input + output + cache writes. Cache reads, the context re-read on
every turn, are kept apart: they are most of what a long session costs, but
they would swamp a per-issue comparison.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import reads
from ..config import UsageError
from ..gh import WfError, run_git
from . import MODEL_FAMILY

NEEDS_CONFIG = False


def add_parser(sub, parents):
    p = sub.add_parser("usage", parents=parents, help="model, turns and kTokens of agents or of the main sessions")
    p.add_argument("agents", nargs="*", metavar="AGENT", help="agent id or transcript path")
    p.add_argument("--sessions", action="store_true", help="the main sessions of this checkout instead of agents")
    p.add_argument("--since", type=when, metavar="TIME", help="with --sessions: responses at or after TIME (ISO 8601)")
    p.add_argument("--until", type=when, metavar="TIME", help="with --sessions: responses before TIME (ISO 8601)")
    p.add_argument("--projects", metavar="DIR", help="Claude Code projects directory (default ~/.claude/projects)")
    p.add_argument("--edits", metavar="REV", help="count file reads outside what changed since REV in this checkout")


def when(text):
    """An ISO 8601 time, as an aware datetime (UTC when it names no zone)."""
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        raise UsageError(f"not an ISO 8601 time: {text}") from None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def projects_dir(override=None):
    if override:
        return Path(override)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / "projects" if base else Path.home() / ".claude" / "projects"


def project_dir(root):
    """The transcripts of sessions started at this checkout's top level (or here, outside git)."""
    try:
        top = run_git("rev-parse", "--show-toplevel")
    except (WfError, OSError):
        top = str(Path.cwd())
    return root / re.sub(r"[^A-Za-z0-9]", "-", top)


def find(agent, root):
    path = Path(agent)
    if agent.endswith(".jsonl"):
        if not path.is_file():
            raise WfError(f"no transcript at {agent}")
        return path
    aid = agent.removeprefix("agent-")
    matches = sorted(root.glob(f"*/*/subagents/agent-{aid}.jsonl"))
    if not matches:
        raise WfError(f"no transcript for agent {aid} under {root}")
    return matches[0]


def sessions(project):
    paths = sorted(project.glob("*.jsonl"))
    if not paths:
        raise WfError(f"no session transcripts in {project}")
    return paths


def measure(*paths, since=None, until=None):
    seen = {}  # message id → (model, usage); the last line of a response wins
    read = []  # file paths, in order: the Read tool's, and the shell's cat/head/tail/sed -n
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                msg = m.get("message") if m.get("type") == "assistant" else None
                if not isinstance(msg, dict) or not msg.get("usage") or msg.get("model") == "<synthetic>":
                    continue
                if since or until:
                    try:
                        t = when(m.get("timestamp") or "")
                    except UsageError:
                        continue
                    if (since and t < since) or (until and t >= until):
                        continue
                for block in msg.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    inp = block.get("input") or {}
                    if block.get("name") == "Read" and inp.get("file_path"):
                        read.append(inp["file_path"])
                    elif block.get("name") == "Bash":
                        read += reads.bash_paths(inp.get("command") or "")
                seen[msg.get("id") or m.get("uuid") or len(seen)] = (msg.get("model", ""), msg["usage"])
    t = {"turns": len(seen), "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "models": []}
    for model, u in seen.values():
        t["input"] += u.get("input_tokens", 0)
        t["output"] += u.get("output_tokens", 0)
        t["cache_write"] += u.get("cache_creation_input_tokens", 0)
        t["cache_read"] += u.get("cache_read_input_tokens", 0)
        family = MODEL_FAMILY.search(model)
        if family and family[1] not in t["models"]:
            t["models"].append(family[1])
    t["tokens"] = t["input"] + t["output"] + t["cache_write"]
    t["read_paths"] = read
    return t


def kilo(n):
    return round(n / 1000)


def _k(n):
    return f"{kilo(n)}k"


def _record(t, edited, top):
    out = {k: v for k, v in t.items() if k != "read_paths"}
    out["reads"] = len(t["read_paths"])
    if edited is not None:
        out.update(reads.tally(t["read_paths"], edited, top))
    return out


def describe(t):
    return (f"{'/'.join(t['models']) or '?'}, {t['turns']} turns, {_k(t['tokens'])} tokens "
            f"(in {_k(t['input'])}, out {_k(t['output'])}, cache write {_k(t['cache_write'])}), "
            f"cache read {_k(t['cache_read'])}")


def _line(name, t, edited=None, top=None):
    line = f"{name}: {describe(t)}"
    if edited is not None:
        line += ", " + reads.describe(reads.tally(t["read_paths"], edited, top))
    else:
        line += f", reads {len(t['read_paths'])}"
    return line


def run(args, cfg, board):
    root = projects_dir(args.projects)
    if args.sessions == bool(args.agents):
        raise UsageError("name agents or pass --sessions, not both")
    if (args.since or args.until) and not args.sessions:
        raise UsageError("--since and --until need --sessions")
    if args.sessions:
        project = project_dir(root)
        t = measure(*sessions(project), since=args.since, until=args.until)
        yield (f"sessions in {project.name}: {describe(t)}",
               {"project": str(project), **{k: v for k, v in t.items() if k != "read_paths"}})
        return
    paths = [find(a, root) for a in args.agents]  # every name resolves before any output
    edited = top = None
    if args.edits:
        edited = reads.edited_files(run_git, args.edits)
        if edited is None:
            raise WfError(f"git cannot diff against {args.edits} here; --edits needs a checkout of the work")
        top = run_git("rev-parse", "--show-toplevel")
    total = {"turns": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "tokens": 0, "models": [],
             "read_paths": []}
    for agent, path in zip(args.agents, paths):
        t = measure(path)
        name = path.stem.removeprefix("agent-")
        for k in total:
            if k == "models":
                total[k] += [m for m in t[k] if m not in total[k]]
            else:
                total[k] += t[k]
        yield _line(name, t, edited, top), {"agent": name, "path": str(path), **_record(t, edited, top)}
    if len(paths) > 1:
        yield _line("total", total, edited, top), {"agent": "total", **_record(total, edited, top)}
    flags = f"--ktokens {kilo(total['tokens'])} --cache-ktokens {kilo(total['cache_read'])} --turns {total['turns']}"
    if len(total["models"]) == 1:
        flags = f"--model {total['models'][0]} " + flags
    yield f"wf merged flags: {flags}", {"flags": flags}
