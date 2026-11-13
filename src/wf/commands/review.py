"""wf review <pr> [--no-post] [--budget USD] [--model M] [--level L] [--lean] [--engine E] [--lane-model M]
              [--tier T] [--since SHA|last].

The review gate as one command: the built-in `/code-review` run by a fresh
`claude -p` session, in a detached worktree at the PR's head commit, so it
sees exactly the PR and none of the author's context or anything written
after it. Spend is capped, the run posts its own findings (`--comment`), and
wf prints one line with what it cost.

A headless run that is denied a tool still ends in "success", so wf never
takes the run's word that it posted: after the run it looks for a comment or
review newer than the run's start, and fails if none landed. Every run's result is kept under the repo's
git dir first, so a failed post is re-posted from the file, not re-run. All of
this is wf's own GitHub and file calls; the review session is unchanged.

The review session loads no settings files (user, project or local) and no MCP
servers. wf enables only the wf-review plugin, which must be installed for
`--lean` and `--since`. User-level `env` and `apiKeyHelper` do not apply.

`--engine opencode` is the optional other engine: the wf-review recipe (wf's
own prep, then lanes and scoring) run by `opencode run`, so it can spend an
Anthropic Console key or any other provider opencode has, with a separate
model for the lanes. wf posts its review, since opencode has no budget cap
and no slash command to do it. It needs opencode 2.1 or later, run with
`--standalone`: the shared background service ignores the inline config, and
a private server ends with the run, so lanes must run in the foreground.

Two ways to spend less. `--tier` (or `[review] tiered = true`, which picks the
tier from the linked issue's size label) scales the review to the slice:
light for XS/S, standard for M, deep for L/XL. `--since <sha>|last` is a fix
round: only the commits after the last review are reviewed, and the earlier
findings are checked off. It always runs the wf-review recipe, because the
built-in /code-review cannot be pointed at a range of commits.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import ledger
from ..cache import prune
from ..config import ASSETS, ENGINES, LEVELS, review_prep
from ..gh import common_dir, run_git
from ..opencode import MARKER, export_session, opencode_result, opencode_version, require_opencode
from . import UsageError, WfError, closing_refs, frontmatter_stripped

DEFAULTS = {"level": "high", "model": "opus", "budget_usd": 6, "engine": "claude",
            "opencode_model": "anthropic/claude-sonnet-5-5", "lane_model": None}
# No settings file loads (user, project or local), so neither the owner's setup nor the PR author's
# .claude/settings.json (hooks, permissions, plugins) reaches the review. Only wf-review, which --lean and
# --since call, is enabled. No MCP server loads.
ISOLATION = ("--setting-sources", "", "--settings", json.dumps({"enabledPlugins": {"wf-review@agent-workflow": True}}),
             "--strict-mcp-config")
TIMEOUT = 45 * 60  # seconds one `claude -p` run may take
# opencode has no budget cap; these bound each agent's model turns instead.
ORCHESTRATOR_STEPS, LANE_STEPS = 40, 25
# The built-in's effort level per tier. The wf-review recipe's lanes per tier (and a fix round's) are resolved by
# review_prep.py, from [review.tiers] and [review.lanes], and read from its prep.json.
TIERS = {"light": {"level": "medium"}, "standard": {"level": None}, "deep": {"level": "xhigh"}}
# What a review needs and nothing that writes to the checkout.
READ_TOOLS = (
    "Read", "Grep", "Glob", "Agent", "Task",
    "Bash(gh api:*)", "Bash(gh pr view:*)", "Bash(gh pr diff:*)",
    "Bash(gh issue view:*)", "Bash(git log:*)", "Bash(git blame:*)", "Bash(git show:*)", "Bash(git diff:*)",
    "Bash(ast-grep run:*)",
)
# Only a run that posts its own findings gets these.
POST_TOOLS = ("Bash(gh pr comment:*)", "Bash(gh pr review:*)")
ALLOWED_TOOLS = READ_TOOLS + POST_TOOLS
# Deny beats allow, and `*` matches anywhere: the git commands above stay read-only (`--output=<file>` writes
# a file; git accepts no abbreviation of it), and ast-grep cannot rewrite files.
DENIED_TOOLS = ("Bash(git * --output*)", "Bash(ast-grep * -U*)", "Bash(ast-grep * --update-all*)",
                "Bash(ast-grep * -i*)", "Bash(ast-grep * --interactive*)",
                # --web/-w opens a browser on the reviewer's machine
                "Bash(gh pr view *--web*)", "Bash(gh pr view *-w*)",
                "Bash(gh issue view *--web*)", "Bash(gh issue view *-w*)")
_ORIGIN = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$")


class Runner:
    """The two things this command drives outside GitHub's API: git and `claude`."""

    def git(self, *args, cwd=None):
        return run_git(*args, cwd=cwd)

    def claude(self, argv, cwd):
        try:
            proc = subprocess.run(["claude", *argv], capture_output=True, text=True, cwd=cwd, timeout=TIMEOUT)
        except FileNotFoundError:
            raise WfError("claude is not installed or not on PATH") from None
        except subprocess.TimeoutExpired:
            raise WfError(f"claude did not finish within {TIMEOUT // 60} minutes") from None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise WfError(f"claude returned no JSON result: {(proc.stderr or proc.stdout).strip()[:300]}") from None

    def prep(self, argv, cwd):
        proc = subprocess.run([sys.executable, str(ASSETS / "review_prep.py"), *argv],
                              capture_output=True, text=True, cwd=cwd)
        if proc.returncode != 0:
            raise WfError(f"review prep failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        lines = proc.stdout.strip().splitlines()
        if not lines:
            raise WfError("review prep printed no prep.json path")
        return lines[-1]

    def opencode(self, argv, cwd, env):
        """The run's JSON events, one per line of `opencode run --format json`."""
        try:
            proc, stdout = self._opencode(argv, cwd=cwd, env={**os.environ, **env})
        except FileNotFoundError:
            raise WfError("opencode is not installed or not on PATH") from None
        events = []
        for line in stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        if proc.returncode != 0 and not events:
            raise WfError(f"opencode exited {proc.returncode}: {(proc.stderr or stdout).strip()[:300]}")
        return events

    def opencode_export(self, session):
        return export_session(session)

    def opencode_version(self):
        return opencode_version()

    @staticmethod
    def _opencode(argv, cwd=None, env=None):
        """opencode cuts piped stdout off at 64 KB when it exits, so stdout goes to a file."""
        with tempfile.TemporaryFile("w+") as out:
            proc = subprocess.run(["opencode", *argv], stdout=out, stderr=subprocess.PIPE, text=True,
                                  cwd=cwd, env=env, stdin=subprocess.DEVNULL)
            out.seek(0)
            return proc, out.read()

    def tempdir(self):
        return tempfile.mkdtemp(prefix="wf-review-")

    def rmtree(self, path):
        shutil.rmtree(path, ignore_errors=True)


RUNNER = Runner()


def add_parser(sub, parents):
    p = sub.add_parser("review", parents=parents, help="run the review gate on a PR from a fresh session")
    p.add_argument("pr", type=int)
    p.add_argument("--no-post", action="store_true", help="print the findings instead of posting them")
    p.add_argument("--budget", type=float, help="API-equivalent dollars the run may spend")
    p.add_argument("--model")
    p.add_argument("--level", choices=LEVELS)
    p.add_argument("--lean", action="store_true", help="also run /wf-review:review (the plugin must be installed)")
    p.add_argument("--engine", choices=ENGINES, help="claude (default) or opencode: the wf-review recipe under opencode")
    p.add_argument("--lane-model", help="opencode engine: provider/model for the lanes (default: the orchestrator's)")
    p.add_argument("--tier", choices=tuple(TIERS), help="review depth; default from the issue's size when tiered")
    p.add_argument("--since", metavar="SHA|last", help="fix round: review only the commits after SHA (last: the last review)")


def repo_from_origin(url):
    m = _ORIGIN.search(url or "")
    if not m:
        raise WfError("wf review runs from a checkout of the PR's repo: origin is not a github.com remote")
    return m.group(1)


def linked_issue(body):
    return next((n for repo, n in closing_refs(body) if repo is None), None)


def claude_md_paths(tree, changed):
    """Root CLAUDE.md plus any in a directory above a changed file, as the checkout has them."""
    root = Path(tree)
    wanted = {"CLAUDE.md"}
    for f in changed:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            wanted.add("/".join(parts[:i]) + "/CLAUDE.md")
    return sorted(p for p in wanted if (root / p).is_file())


def system_prompt(repo, pr, issue, claude_md):
    lines = [f"You are reviewing pull request #{pr} of {repo} from a fresh session; you did not write it."]
    if issue:
        lines.append(f"Its specification is issue #{issue}: read it with `gh api repos/{repo}/issues/{issue}` "
                     "and report where the change contradicts or misses what it requires.")
    if claude_md:
        lines.append("House rules are in: " + ", ".join(claude_md) + ". A rule broken is a finding; quote the rule.")
    lines.append("A rare but severe failure is still severe: never drop a finding because it is unlikely.")
    lines.append("Also report new code that duplicates something the codebase already has (a helper, type, constant "
                 "or block it should have reused), naming the existing symbol and its path; find symbols with "
                 "`ast-grep run -p <pattern>` when it is installed.")
    return " ".join(lines)


def settings(cfg, args):
    s = dict(DEFAULTS)
    s.update(cfg.review)
    if args.level:
        s["level"] = args.level
    if args.model:
        s["model"] = args.model
    if args.budget is not None:
        if args.budget <= 0:
            raise UsageError("--budget must be a positive number of dollars")
        s["budget_usd"] = args.budget
    if args.engine:
        s["engine"] = args.engine
    s["api"] = cfg.api
    if s["engine"] == "opencode":
        if args.lean or args.level or args.budget is not None:
            raise UsageError("--lean, --level and --budget apply to the claude engine; opencode runs the wf-review recipe")
        if args.model:
            s["opencode_model"] = args.model
        if args.lane_model:
            s["lane_model"] = args.lane_model
        for key in ("opencode_model", "lane_model"):
            if s[key] and "/" not in s[key]:
                raise UsageError(f"opencode models are provider/model (see `opencode models`), not '{s[key]}'")
    elif args.lane_model:
        raise UsageError("--lane-model applies to the opencode engine")
    if args.since and args.lean:
        raise UsageError("--since runs the wf-review recipe already; --lean does not apply")
    return s


def choose_tier(s, args, gh, repo, issue, pr, size_prefix):
    """(tier, why) from --tier, else from the linked issue's size label or the diff size when tiered (review_prep's
    tier_by_size, the rule a standalone prep applies); else (None, None)."""
    if args.tier:
        return args.tier, "--tier"
    if not s.get("tiered"):
        return None, None
    labels = []
    if issue:
        try:
            labels = [x.get("name", "") for x in gh.get(f"/repos/{repo}/issues/{issue}").get("labels") or []]
        except WfError:
            pass
    lines = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    tier, size = review_prep().tier_by_size(labels, size_prefix, lines)
    return tier, f"#{issue} is {size}" if size else f"{lines} changed lines"


def _state_path(keep, n):
    return keep / f"pr-{n}-last.json"


def resolve_since(args, keep, n):
    """(sha, previous review file or None) for a fix round, or (None, None)."""
    if not args.since:
        return None, None
    state = {}
    try:
        state = json.loads(_state_path(keep, n).read_text())
    except (OSError, json.JSONDecodeError):
        pass
    if args.since == "last":
        if not state.get("head"):
            raise WfError(f"no earlier review of #{n} is recorded in {keep}; pass --since <sha>")
        return state["head"], state.get("review")
    return args.since, state.get("review") if state.get("head", "").startswith(args.since) else None


def _record(keep, n, head, kept, variant):
    """What --since last reads: the head this review saw and where its text is kept."""
    keep.mkdir(parents=True, exist_ok=True)
    _state_path(keep, n).write_text(json.dumps({"head": head, "review": str(kept), "variant": variant}) + "\n")


def usage_tokens(result):
    """(tokens, cache_read) of a run, as the ledger counts them: tokens is input + output + cache creation; cache
    reads, the context re-read each turn, are apart."""
    usage = list((result.get("modelUsage") or {}).values())
    return (sum(m.get(k, 0) for m in usage for k in ("inputTokens", "outputTokens", "cacheCreationInputTokens")),
            sum(m.get("cacheReadInputTokens", 0) for m in usage))


def _usage(result):
    tokens, cache_read = usage_tokens(result)
    return {"cost_usd": round(float(result.get("total_cost_usd") or 0), 2), "tokens": tokens,
            "cache_read": cache_read, "turns": result.get("num_turns", 0)}


def _spent(u):
    """The printed cost of a run, `$C (Nk, +Mk cache)`: the same tokens its ledger line records."""
    return f"${u['cost_usd']:.2f} ({u['tokens'] // 1000}k, +{u['cache_read'] // 1000}k cache)"


def _check(result, what):
    reason = str(result.get("terminal_reason") or "")
    subtype = result.get("subtype", "success")
    if result.get("is_error") or "budget" in reason or subtype != "success":
        raise WfError(f"{what} failed ({reason or (subtype if subtype != 'success' else 'error')}): {str(result.get('result') or '')[:300]}")


REVIEW_DAYS = 30  # kept review files untouched this long are removed, but not the PR under review's


def _keep(keep, n, variant, result, branch=None):
    """Writes the run's result (JSON for diagnosis, Markdown for re-posting) and appends its spend to the ledger
    of the PR's head branch, which `wf merged` sums; returns the Markdown path. A run that failed still spent,
    so it counts."""
    keep.mkdir(parents=True, exist_ok=True)
    if branch:
        tokens, cache_read = usage_tokens(result)
        ledger.append(keep.parent, branch, {
            "kind": "review", "pr": n, "variant": variant, "tokens": tokens, "cache_read": cache_read,
            "cost_usd": round(float(result.get("total_cost_usd") or 0), 4), "turns": result.get("num_turns", 0)})
    stem = keep / f"pr-{n}-{variant}"
    stem.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    md = stem.with_suffix(".md")
    md.write_text(str(result.get("result") or "") + "\n")
    prune(keep, older_than_days=REVIEW_DAYS, spare=[p.name for p in keep.glob(f"pr-{n}-*")])
    return md


def rules(action, *pairs):
    """opencode's ordered permission rules for one action, from (resource, effect) pairs."""
    return [{"action": action, "resource": resource, "effect": effect} for resource, effect in pairs]


def _permissions(launches, tree, api):
    """Read anything in the checkout, a few read-only git and REST commands, and launch the subagents named in
    `launches`; nothing else. A rule set to `ask` auto-rejects in a headless run and ends it, so everything is
    allow or deny. The last matching rule wins, so each exception follows the rule it narrows."""
    graphql = api == "graphql"  # adds the read-only gh pr/issue views; raw gh api graphql stays denied in both modes
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return [
        *rules("*", ("*", "deny")),
        *rules("read", ("*", "allow")), *rules("grep", ("*", "allow")), *rules("glob", ("*", "allow")),
        *rules("shell", ("*", "deny"), ("gh api *", "allow"),
               *((("gh pr view *", "allow"), ("gh pr diff *", "allow"), ("gh issue view *", "allow")) if graphql else ()),
               ("git log *", "allow"), ("git blame *", "allow"),
               ("git show *", "allow"), ("git diff *", "allow"), ("ast-grep run *", "allow"),
               # gh api writes and raw GraphQL (a glob cannot tell a query from a mutation) are denied after the allow, in both modes
               ("gh api graphql*", "deny"), ("gh api *-X*", "deny"), ("gh api *--method*", "deny"),
               ("gh api * -f*", "deny"), ("gh api * -F*", "deny"), ("gh api *--field*", "deny"),
               ("gh api *--raw-field*", "deny"), ("gh api *--input*", "deny"),
               ("git *--output*", "deny"),  # diff options write a file
               ("ast-grep *-U*", "deny"), ("ast-grep *--update-all*", "deny"), ("ast-grep * -i*", "deny"),
               ("ast-grep *--interactive*", "deny"),
               ("gh pr view *--web*", "deny"), ("gh pr view *-w*", "deny"),
               ("gh issue view *--web*", "deny"), ("gh issue view *-w*", "deny")),
        *rules("subagent", ("*", "deny"), *((agent, "allow") for agent in launches)),
        # opencode's own spill files for long tool output
        *rules("external_directory", ("*", "deny"), (f"{tree}/*", "allow"),
               (f"{data}/opencode/tool-output/*", "allow"), (f"{tempfile.gettempdir()}/opencode/*", "allow")),
    ]


def house_rules(tree):
    """The worktree's CLAUDE.md when it has no AGENTS.md: opencode 2 reads AGENTS.md only, so it is named here."""
    root = Path(tree)
    return [str(root / "CLAUDE.md")] if (root / "CLAUDE.md").is_file() and not (root / "AGENTS.md").is_file() else []


def lane_agents(s, lanes):
    """(lane agent → model or None, [(lane name, agent)]): `wf-review-lane` has the default lane model and runs
    every lane with no opencode_model of its own; each other distinct opencode_model gets `wf-review-lane-1`,
    `wf-review-lane-2`, … in the order the lanes first name it."""
    agents, by_model, assigned = {"wf-review-lane": s["lane_model"]}, {s["lane_model"]: "wf-review-lane"}, []
    for lane in lanes:
        model = lane.get("opencode_model") or s["lane_model"]
        if model not in by_model:
            by_model[model] = f"wf-review-lane-{len(agents)}"
            agents[by_model[model]] = model
        assigned.append((lane["name"], by_model[model]))
    return agents, assigned


def opencode_config(s, tree, lanes):
    """The orchestrator and one lane agent per distinct lane model (lane_agents), passed inline
    (OPENCODE_CONFIG_CONTENT) so nothing is written to the checkout."""
    models, _ = lane_agents(s, lanes)
    lane = {"description": "One lane of a wf-review; returns rated findings.",
            "mode": "subagent", "steps": LANE_STEPS, "permissions": _permissions((), tree=tree, api=s["api"]),
            "system": frontmatter_stripped((ASSETS / "review-lane.md").read_text())}
    return {"$schema": "https://opencode.ai/config.json",
            "instructions": house_rules(tree),
            "agents": {"wf-review": {"description": "Orchestrates a wf-review of one pull request.",
                                     "mode": "primary", "model": s["opencode_model"], "steps": ORCHESTRATOR_STEPS,
                                     "permissions": _permissions(tuple(models), tree=tree, api=s["api"]),
                                     "system": (ASSETS / "opencode-review.md").read_text()},
                       **{agent: {**lane, **({"model": model} if model else {})} for agent, model in models.items()}}}


def _run_opencode(s, runner, gh, repo, n, tree, head, post, keep, config, tier=None, why=None, since=None,
                  previous=None, branch=None):
    # opencode resolves symlinks (macOS /var → /private/var); an unresolved path reads as outside the project
    tree = str(Path(tree).resolve())
    argv = [str(n), "--repo", repo, "--dir", str(Path(tree) / ".wf-review"), "--force"]
    if since:
        argv += ["--since", since] + (["--previous", previous] if previous and Path(previous).is_file() else [])
    elif tier:
        argv += ["--tier", tier]
    argv += ["--config", config, "--api", s["api"]]
    prep = runner.prep(argv, cwd=tree)
    try:
        lanes = json.loads(Path(prep).read_text())["lanes"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise WfError(f"review prep wrote no lanes to {prep}") from None
    _, assigned = lane_agents(s, lanes)
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps(opencode_config(s, tree, lanes)),
           # the repo's AGENTS.md, or its CLAUDE.md (see house_rules), loads; the user's Claude skills do not
           "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1"}
    # --standalone: the shared background service would ignore OPENCODE_CONFIG_CONTENT
    argv = ["run", "--standalone", "--format", "json", "--agent", "wf-review", "-m", s["opencode_model"],
            "--title", f"wf-review {repo}#{n}",
            f"Review pull request #{n} of {repo}. The prep file is {prep}. "
            f"Lanes: {', '.join(f'{name} ({agent})' for name, agent in assigned)}."
            + (f" This is a fix round: the commits since {since[:12]}." if since else "")]
    start = time.monotonic()
    events = runner.opencode(argv, cwd=tree, env=env)
    result = opencode_result(events, s["opencode_model"], runner.opencode_export, time.monotonic() - start)
    result["events"] = events
    variant = "opencode-fix" if since else "opencode"
    kept = _keep(keep, n, variant, result, branch)
    _check(result, "opencode review")
    backgrounded = result["subagent_stats"]["background"]
    if backgrounded:
        raise WfError(f"opencode review launched {len(backgrounded)} lane(s) in the background, which end with the "
                      f"run, so its review is missing their findings; nothing was posted. Run it again. Its output "
                      f"is in {kept}")
    if MARKER not in str(result["result"]):
        raise WfError(f"opencode review ended without a review (no {MARKER} marker); its output is in {kept}")
    if post:
        try:
            gh.post(f"/repos/{repo}/issues/{n}/comments", {"body": result["result"]})
        except WfError as e:
            raise WfError(f"posting the review on #{n} failed ({e}). The review is in {kept}: "
                          f"post it with `gh pr comment {n} --body-file {kept}`") from None
    _record(keep, n, head, kept, variant)
    u = _usage(result)
    lane_model = s["lane_model"] or s["opencode_model"]
    yield (f"#{n}: {'wf-review posted' if post else 'findings below'} ({_shape(tier, why, since)}"
           f"opencode {s['opencode_model']}, lanes {lane_model}), {_spent(u)}"
           + (f"; {len(result['subagent_stats']['unpriced'])} lane(s) unpriced" if result["subagent_stats"]["unpriced"] else ""),
           {"pr": n, "variant": variant, "tier": tier, "since": since, "lanes": [x["name"] for x in lanes],
            "posted": post, "head": head, **u, "kept": str(kept)})
    if not post:
        yield result["result"], {"pr": n, "variant": variant, "text": result["result"]}


def _shape(tier, why, since):
    if since:
        return f"fix round since {since[:7]}; "
    return f"{tier}: {why}; " if tier else ""


def _now():
    """The start of a run, in the form GitHub's `since` and `submitted_at` use (UTC, ISO 8601, whole seconds). It is
    rounded up, so a comment posted in the same second just before the run starts (by the run that just ended)
    is not counted as this run's."""
    t = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _posted_since(gh, repo, n, started):
    """Whether a conversation comment, inline comment or review (whatever /code-review --comment posts) landed on
    the PR at or after `started`: at most three single-page GETs, stopping at the first hit."""
    for path in (f"issues/{n}/comments", f"pulls/{n}/comments"):
        if gh.get(f"/repos/{repo}/{path}?since={started}&per_page=1"):
            return True
    return any((r.get("submitted_at") or "") >= started
               for r in gh.get(f"/repos/{repo}/pulls/{n}/reviews?per_page=100") or [])


def _check_posted(gh, repo, n, started, result, what, kept):
    if _posted_since(gh, repo, n, started):
        return
    denied = sorted({str((d.get("tool_input") or {}).get("command") or d.get("tool_name") or "?")[:80]
                     for d in result.get("permission_denials") or []})
    raise WfError(f"{what} reported success but posted nothing on #{n}"
                  + (f"; denied: {', '.join(denied)}" if denied else "")
                  + f". The review is in {kept}: post it with `gh pr comment {n} --body-file {kept}`")


def _run_fix_round(s, runner, gh, repo, n, tree, head, post, keep, since, previous, common, config, branch=None):
    """The Claude engine's fix round: the wf-review plugin with --since (the built-in takes no commit range)."""
    out = ".wf-review-fix.md"
    slash = (f"/wf-review:review {n} --since {since}"
             + (f" --previous {previous}" if previous and Path(previous).is_file() else "")
             + ("" if post else f" --no-post --out {out}") + f" --config {shlex.quote(config)} --api {s['api']}")
    started = _now()
    result = runner.claude(["-p", slash, *common, "Write"], cwd=tree)
    kept = _keep(keep, n, "lean-fix", result, branch)
    _check(result, "fix-round review")
    if post:
        _check_posted(gh, repo, n, started, result, "fix-round review", kept)
    text = Path(tree) / out
    if not post and text.is_file():
        kept.write_text(text.read_text())
    _record(keep, n, head, kept, "lean-fix")
    u = _usage(result)
    yield (f"#{n}: fix-round review {'posted' if post else 'below'} (since {since[:7]}), "
           f"{_spent(u)}",
           {"pr": n, "variant": "lean-fix", "since": since, "posted": post, "head": head, **u, "kept": str(kept)})
    if not post:
        yield kept.read_text().rstrip(), {"pr": n, "variant": "lean-fix", "text": kept.read_text()}


def run(args, cfg, board):
    s = settings(cfg, args)
    runner = RUNNER
    if s["engine"] == "opencode":
        require_opencode(runner.opencode_version())
    repo = repo_from_origin(runner.git("remote", "get-url", "origin"))
    n = args.pr
    pr = board.gh.get(f"/repos/{repo}/pulls/{n}")
    issue = linked_issue(pr.get("body"))
    branch = (pr.get("head") or {}).get("ref")  # the ledger the runs' spend goes to
    keep = Path(common_dir(git=runner.git)) / "wf-review"
    post = not args.no_post
    since, previous = resolve_since(args, keep, n)
    tier, why = (None, None) if since else choose_tier(s, args, board.gh, repo, issue, pr, cfg.labels["size_prefix"])
    level = args.level or (TIERS[tier]["level"] if tier else None) or s["level"]
    # prep runs in the PR's worktree, so it is told which config file wf read
    config = os.path.abspath(cfg.path)

    scratch = runner.tempdir()
    tree = str(Path(scratch) / f"pr-{n}")
    try:
        # A per-PR ref, not FETCH_HEAD: that file is shared by every process using the
        # checkout, so concurrent reviews of different PRs would read each other's head.
        ref = f"refs/wf-review/pr-{n}"
        runner.git("fetch", "-q", "origin", f"+pull/{n}/head:{ref}")
        head = runner.git("rev-parse", ref)
        if since and head.startswith(since):
            yield f"#{n}: no commits since {since[:7]}; nothing to review", {"pr": n, "since": since, "head": head}
            return
        runner.git("worktree", "add", "-q", "--detach", tree, head)
        if s["engine"] == "opencode":
            yield from _run_opencode(s, runner, board.gh, repo, n, tree, head, post, keep, config, tier, why, since,
                                     previous, branch)
            return
        changed = runner.git("diff", "--name-only", f"{pr['base']['sha']}...{head}", cwd=tree).splitlines()
        prompt = system_prompt(repo, n, issue, claude_md_paths(tree, changed))

        common = ["--output-format", "json", "--no-session-persistence", "--model", s["model"],
                  "--max-budget-usd", str(s["budget_usd"]), "--append-system-prompt", prompt,
                  *ISOLATION, "--disallowedTools", *DENIED_TOOLS, "--allowedTools", *(ALLOWED_TOOLS if post else READ_TOOLS)]
        if since:
            yield from _run_fix_round(s, runner, board.gh, repo, n, tree, head, post, keep, since, previous, common,
                                      config, branch)
            return
        slash = f"/code-review {n} {level}" + (" --comment" if post else "")
        started = _now()
        result = runner.claude(["-p", slash, *common], cwd=tree)
        kept = _keep(keep, n, "builtin", result, branch)
        _check(result, "review")
        if post:
            _check_posted(board.gh, repo, n, started, result, "review", kept)
        _record(keep, n, head, kept, "builtin")
        u = _usage(result)
        verb = "review posted" if post else "findings below"
        shape = f" ({tier}: {why}; level {level})" if tier else ""
        yield (f"#{n}: {verb}{shape}, {_spent(u)}",
               {"pr": n, "variant": "builtin", "tier": tier, "level": level, "posted": post, "head": head, **u,
                "kept": str(kept)})
        if args.no_post:
            yield str(result.get("result") or "").rstrip(), {"pr": n, "variant": "builtin", "text": result.get("result")}

        if args.lean:
            slash = (f"/wf-review:review {n}" + (" --no-post --out .wf-review-lean.md" if args.no_post else "")
                     + f" --config {shlex.quote(config)}" + (f" --tier {tier}" if tier else "") + f" --api {s['api']}")
            started = _now()
            result = runner.claude(["-p", slash, *common, "Write"], cwd=tree)
            kept = _keep(keep, n, "lean", result, branch)
            _check(result, "wf-review")
            if post:
                _check_posted(board.gh, repo, n, started, result, "wf-review", kept)
            u = _usage(result)
            yield (f"#{n}: wf-review {'posted' if post else 'written'}, "
                   f"{_spent(u)}",
                   {"pr": n, "variant": "lean", "posted": post, "head": head, **u, "kept": str(kept)})
            if args.no_post:
                out = Path(tree) / ".wf-review-lean.md"
                if out.is_file():
                    yield out.read_text().rstrip(), {"pr": n, "variant": "lean", "text": out.read_text()}
    finally:
        try:
            runner.git("worktree", "remove", "--force", tree)
        except WfError:
            pass
        runner.rmtree(scratch)
