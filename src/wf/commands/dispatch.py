"""wf dispatch <ref> --engine opencode [--model M] [--variant V] [--resume [--message TEXT]].

The OpenCode implementer seat: the same contract the Claude `implementer`
agent gets, run by `opencode run` instead of the Agent tool, so an issue can
be built on whatever provider opencode is logged in to.

wf cuts the issue's worktree from `origin/<base>` on the contract's branch
(`claude/<n>-<slug>`), writes the project's implementer agent text plus the
issue under `.git/wf-dispatch/<n>/`, and runs opencode there with that file as
its instructions (passed inline, so nothing is written to the checkout), with
the worktree's own CLAUDE.md or AGENTS.md. opencode's JSON events go
straight to a log as they arrive, so `tail -f` follows the run. wf exits with
opencode's status, appends the run's spend to the branch's ledger (which `wf
merged` reads) and prints the `wf merged` flags from what the run cost.
It needs opencode 2.1 or later, and runs it with `--standalone`, since the
shared background service ignores the inline config.

A run that stops at a `needs-ruling` comment is resumed after the ruling with
`--resume --message "<the ruling>"`: same worktree, same opencode session, a new log.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .. import ledger, reads
from ..cache import prune
from ..gh import common_dir, run_git
from ..opencode import export_session, opencode_result, opencode_version, require_opencode
from . import DEFAULT_BASE, UsageError, WfError, board_model, frontmatter_stripped
from . import checkpoint
from .review import house_rules, repo_from_origin, rules, usage_tokens

ENGINES = ("opencode",)  # the Claude seat is the Agent tool's `implementer`, not a wf command
DEFAULTS = {"model": "anthropic/claude-sonnet-5-5", "variant": None, "base": DEFAULT_BASE,
            "contract": ".claude/agents/implementer.md"}
AGENT = "wf-implementer"
# What an issue body or label names → the substring an opencode MCP server's name must contain.
MCP_MENTIONS = ((re.compile(r"scenario\s+mcp", re.I), "scenario", "Scenario MCP"),
                (re.compile(r"unity\s+mcp|mcp\s+for\s+unity", re.I), "unity", "Unity MCP"))


class Runner:
    """git and opencode, the two things dispatch drives outside GitHub's API."""

    def git(self, *args, cwd=None):
        return run_git(*args, cwd=cwd)

    def opencode(self, argv, cwd, env, log, errlog):
        """Runs opencode with stdout written to `log` as it arrives (a pipe would also cut it at 64 KB on
        exit); returns the exit status."""
        try:
            with open(log, "w") as out, open(errlog, "w") as err:
                return subprocess.run(["opencode", *argv], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                      cwd=cwd, env={**os.environ, **env}).returncode
        except FileNotFoundError:
            raise WfError("opencode is not installed or not on PATH") from None

    def export(self, session):
        return export_session(session)

    def version(self):
        return opencode_version()

    def home(self):
        return Path.home()


RUNNER = Runner()


def add_parser(sub, parents):
    p = sub.add_parser("dispatch", parents=parents, help="run an implementer on an issue in its own worktree")
    p.add_argument("ref", help="the issue, e.g. sk#12")
    p.add_argument("--engine", choices=ENGINES, required=True,
                   help="opencode (Claude implementers are launched with the Agent tool)")
    p.add_argument("--model", help="provider/model, as `opencode models` lists them")
    p.add_argument("--variant", help="provider reasoning effort, e.g. medium, high; none drops a configured one")
    p.add_argument("--resume", action="store_true", help="continue the issue's last run in its worktree and session")
    p.add_argument("--message", help="with --resume: what to tell it, e.g. the architect's ruling")


def settings(cfg, args):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (cfg.opencode or {}).items() if v})
    if args.message and not args.resume:
        raise UsageError("--message goes with --resume; a first run's prompt names the issue")
    if args.model:
        s["model"] = args.model
    if args.variant:
        s["variant"] = args.variant
    if "/" not in s["model"]:
        raise UsageError(f"opencode models are provider/model (see `opencode models`), not '{s['model']}'")
    return s


def model_ref(s):
    """provider/model#variant, the one form opencode 2.1 takes a variant in; variant `none` drops it."""
    if not s["variant"]:
        return s["model"]
    base = s["model"].split("#", 1)[0]
    return base if s["variant"] == "none" else f"{base}#{s['variant']}"


def branch_slug(title):
    words = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if len(words) > 40:
        words = words[:41].rsplit("-", 1)[0] if "-" in words[:41] else words[:40]
    return words or "issue"


def instructions(contract, repo, shown, n, title, tree, branch, base, sha, code_map=""):
    """The project's implementer agent text, then the issue, then (when it could be built) the code map
    around the issue: what the Claude seat's dispatch prompt names."""
    text = (frontmatter_stripped(contract) + "\n\n## This run\n\n"
            f"Your issue is {repo}#{n} (`{shown}`): {title}. You are in your own worktree, {tree}, on branch "
            f"`{branch}`, cut from `origin/{base}` at {sha[:12]}; do not create another. You are the OpenCode "
            f"seat: you have no subagents, and you never merge. When you stop, at PR-opened or at a "
            f"`needs-ruling` comment, end with the hand-back from docs/issue-workflow.md §6 as your final "
            f"message, and make no tool call after it.\n")
    if code_map:
        text += ("\n## Map\n\nThe definitions most related to the issue, ranked (`wf map`); `path` then "
                 "`line signature`. Start from these instead of searching; `wf map <symbol or path>…` "
                 "or `wf map --text <file>` gives the map around anything else.\n\n```\n" + code_map + "\n```\n")
    return text


def issue_map(cfg, tree, issue):
    """The map around the issue's title and body, built in the worktree; "" when it cannot be."""
    from .map import text_map
    return text_map(cfg, cwd=tree, text=f"{issue.get('title') or ''}\n{issue.get('body') or ''}")


def _read_config(path):
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:  # opencode.jsonc: drop whole-line comments, then try again
        try:
            return json.loads("\n".join(x for x in text.splitlines() if not x.lstrip().startswith("//")))
        except json.JSONDecodeError:
            return {}


def _mcp_entries(mcp):
    """Server name → config from an `mcp` table, native (`servers`) or the older one (names at the top)."""
    if not isinstance(mcp, dict):
        return {}
    entries = {k: v for k, v in mcp.items() if k not in ("servers", "timeout") and isinstance(v, dict)}
    if isinstance(mcp.get("servers"), dict):
        entries.update({k: v for k, v in mcp["servers"].items() if isinstance(v, dict)})
    return entries


def mcp_servers(tree, home):
    """Enabled MCP servers opencode would load in `tree`, in its order: the global config, every
    opencode.json(c) from the filesystem root down to `tree`, then every .opencode/opencode.json(c)."""
    dirs = [*reversed(Path(tree).parents), Path(tree)]
    paths = [Path(home) / ".config/opencode" / name for name in ("opencode.json", "opencode.jsonc")]
    paths += [d / name for d in dirs for name in ("opencode.json", "opencode.jsonc")]
    paths += [d / ".opencode" / name for d in dirs for name in ("opencode.json", "opencode.jsonc")]
    servers = {}
    for path in paths:
        for name, conf in _mcp_entries((_read_config(path) or {}).get("mcp")).items():
            servers.setdefault(name, {}).update(conf)
    return sorted(k for k, v in servers.items() if v.get("disabled") is not True and v.get("enabled", True) is not False)


def mcp_warnings(issue, servers):
    text = " ".join([issue.get("title") or "", issue.get("body") or "",
                     *[x.get("name", "") for x in issue.get("labels") or []]])
    return [f"the issue mentions {name} but no enabled opencode MCP server is named '*{key}*' "
            f"(configured: {', '.join(servers) or 'none'}); see docs/dispatch.md"
            for pattern, key, name in MCP_MENTIONS
            if pattern.search(text) and not any(key in s.lower() for s in servers)]


def opencode_config(instructions_path, tree):
    """The seat's agent, inline (OPENCODE_CONFIG_CONTENT). The default tools and prompt, with the contract's
    file and the house rules as instructions. Denied: subagents (the contract has none) and the moves the
    contract forbids."""
    return {"$schema": "https://opencode.ai/config.json",
            "instructions": [str(instructions_path), *house_rules(tree)],
            "agents": {AGENT: {"description": "Implements one issue end to end in its own worktree.",
                               "mode": "primary",
                               "permissions": [*rules("subagent", ("*", "deny")),
                                               # last match wins: the forbidden moves after the allow
                                               *rules("shell", ("*", "allow"), ("gh pr merge*", "deny"),
                                                      ("git push *--force*", "deny"), ("git push * -f*", "deny"),
                                                      ("git rebase*", "deny"))]}}}


def ktokens(result):
    return tuple(round(n / 1000) for n in usage_tokens(result))


def _events(log):
    events = []
    try:
        lines = Path(log).read_text().splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def read_paths(events):
    """Files the run read, in order: the read tool's paths and the shell's cat/head/tail/sed -n arguments."""
    out = []
    for e in events:
        part = e.get("part") or {}
        if e.get("type") != "tool_use":
            continue
        inp = (part.get("state") or {}).get("input") or {}
        if part.get("tool") == "read" and inp.get("filePath"):
            out.append(inp["filePath"])
        elif part.get("tool") == "bash":
            out += reads.bash_paths(inp.get("command") or "")
    return out


DISPATCH_DAYS = 14  # a dispatch folder untouched this long is removed by the next dispatch


def run(args, cfg, board):
    s = settings(cfg, args)
    ref = cfg.parse_ref(args.ref)
    shown = cfg.show_ref(ref.repo, ref.number)
    runner, n = RUNNER, ref.number
    require_opencode(runner.version())
    origin = repo_from_origin(runner.git("remote", "get-url", "origin"))
    if origin.lower() != ref.repo.lower():
        raise WfError(f"wf dispatch runs from a checkout of {ref.repo}; this one is {origin}")
    common = Path(common_dir(git=runner.git))
    keep = common / "wf-dispatch" / str(n)
    prune(keep.parent, older_than_days=DISPATCH_DAYS, spare=(str(n),))
    state_file = keep / "state.json"
    tree = (common.parent / ".claude" / "worktrees" / f"opencode-{n}").resolve()
    issue = board.gh.get(f"/repos/{ref.repo}/issues/{n}")

    state = {}
    if args.resume:
        try:
            state = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            raise WfError(f"no earlier run of {shown} is recorded in {keep}; dispatch it without --resume") from None
        if not state.get("session") or not tree.is_dir():
            raise WfError(f"{shown}'s last run left no session or worktree to resume ({tree})")
        branch, sha = state["branch"], state["sha"]
    else:
        if tree.exists():
            raise WfError(f"{tree} exists: {shown} was dispatched before; pass --resume, or remove the worktree")
        branch = f"claude/{n}-{branch_slug(issue.get('title'))}"
        runner.git("fetch", "-q", "origin", s["base"])
        sha = runner.git("rev-parse", f"origin/{s['base']}")
        runner.git("worktree", "add", "-q", "--no-track", "-b", branch, str(tree), f"origin/{s['base']}")
        yield (f"{shown}: worktree {tree} on {branch} from origin/{s['base']} at {sha[:7]}",
               {"ref": shown, "tree": str(tree), "branch": branch, "base": s["base"], "sha": sha})

    contract = tree / s["contract"]
    if not contract.is_file():
        raise WfError(f"no implementer contract at {contract} ([opencode].contract names it)")
    keep.mkdir(parents=True, exist_ok=True)
    inst = keep / "implementer.md"
    code_map = issue_map(cfg, tree, issue)
    inst.write_text(instructions(contract.read_text(), ref.repo, shown, n, issue.get("title") or "", tree,
                                 branch, s["base"], sha, code_map))
    if code_map:
        yield f"{shown}: {code_map.splitlines()[0][2:]}", {"ref": shown, "map": code_map.splitlines()[0]}
    for warning in mcp_warnings(issue, mcp_servers(tree, runner.home())):
        yield f"{shown}: warning: {warning}", {"ref": shown, "warning": warning}

    runs = state.get("runs") or []
    log, errlog = keep / f"run-{len(runs) + 1}.jsonl", keep / f"run-{len(runs) + 1}.stderr"
    if args.resume:
        message = args.message or "Continue the issue from where you stopped."
        notes = checkpoint.tail(common, branch)
        if notes:
            message += ("\n\nYour checkpoint note (`wf checkpoint`), newest last; start from it, not from git:\n"
                        + "\n".join(notes))
    else:
        message = (f"Implement issue {shown} ({ref.repo}#{n}) per your instructions, from pickup to PR-opened. "
                   f"docs/issue-workflow.md is the contract.")
    # --standalone: the shared background service would ignore OPENCODE_CONFIG_CONTENT
    argv = ["run", "--standalone", "--format", "json", "--auto", "--agent", AGENT, "-m", model_ref(s),
            "--title", f"wf-dispatch {shown}"]
    if state.get("session"):
        argv += ["--session", state["session"]]
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps(opencode_config(inst, tree)),
           # the worktree's AGENTS.md or CLAUDE.md load (see house_rules); the user's Claude skills do not
           "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1"}
    yield (f"{shown}: opencode {s['model']}{' ' + s['variant'] if s['variant'] else ''} running; log {log}",
           {"ref": shown, "model": s["model"], "variant": s["variant"], "log": str(log), "resume": args.resume})

    start = time.monotonic()
    run_from = runner.git("rev-parse", "HEAD", cwd=str(tree))  # this run's edit set, not every run's
    code = runner.opencode(argv + [message], cwd=str(tree), env=env, log=log, errlog=errlog)
    events = _events(log)
    result = opencode_result(events, s["model"], runner.export, time.monotonic() - start)
    session = state.get("session") or next((e["sessionID"] for e in events if e.get("sessionID")), None)
    k, cache_read = ktokens(result)
    this = {"log": str(log), "code": code, "model": s["model"], "cost_usd": round(result["total_cost_usd"], 2),
            "ktokens": k, "cache_read_ktokens": cache_read, "turns": result["num_turns"],
            "errors": result["result"] if result["is_error"] else None}
    edited = reads.edited_files(runner.git, run_from, cwd=str(tree))
    tally = None
    if edited is not None:
        full = reads.tally(read_paths(events), edited, tree, cap=None)
        tally = reads.capped(full)
        this.update(full)  # state.json keeps every path
    runs.append(this)
    state = {"ref": shown, "session": session, "tree": str(tree), "branch": branch, "sha": sha, "runs": runs}
    state_file.write_text(json.dumps(state, indent=2) + "\n")
    raw, raw_cache = usage_tokens(result)
    ledger.append(common, branch, {  # one line per run: a resumed session's runs are each their own spend
        "kind": "agent", "id": f"{session}-run-{len(runs)}" if session else str(log), "agent_type": "opencode",
        "transcript": str(log), "model": board_model("opencode-" + s["model"]), "tokens": raw,
        "cache_read": raw_cache, "turns": result["num_turns"]})
    (keep / f"run-{len(runs)}.json").write_text(json.dumps(result, indent=2) + "\n")

    err = f"; error: {this['errors'][:200]}" if this["errors"] else ""
    yield (f"{shown}: opencode exited {code} after {this['turns']} turns, ${this['cost_usd']:.2f} "
           f"({k}k, cache read {cache_read}k){err}", {"ref": shown, "session": session, **{**this, **(tally or {})}})
    if tally:
        yield f"{shown}: {reads.describe(tally)}", {"ref": shown, **tally}
    models = {board_model("opencode-" + r["model"]) for r in runs}
    flags = (f"--ktokens {sum(r['ktokens'] for r in runs)} "
             f"--cache-ktokens {sum(r.get('cache_read_ktokens', 0) for r in runs)} --turns {sum(r['turns'] for r in runs)}")
    if len(models) == 1:
        flags = f"--model {models.pop()} " + flags
    yield f"wf merged flags: {flags} (over {len(runs)} run(s))", {"flags": flags}
    args.exit_code = code
