import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_gh import FakeGh, run
from wf.commands import review
from wf.gh import WfError

OK = {"is_error": False, "terminal_reason": "completed", "num_turns": 9, "total_cost_usd": 2.3777,
      "result": "Review complete.", "modelUsage": {"claude-opus-5[1m]": {"inputTokens": 50, "outputTokens": 30000,
                                                                      "cacheReadInputTokens": 1900000,
                                                                      "cacheCreationInputTokens": 60000}}}


REVIEW = "<!-- wf-review -->\n### Review (wf-review)\n\n**Report** (0)\nnone"


def _step(cost, total, write=0):
    return {"type": "step_finish", "part": {"cost": cost, "tokens": {
        "total": total, "input": 10, "output": total - 10 - write, "reasoning": 0, "cache": {"write": write, "read": 0}}}}


def _subagent(session, background=False):
    """A subagent tool call as opencode 2.1 reports it."""
    state = {"status": "running" if background else "completed",
             "input": {"agent": "wf-review-lane", "description": "Correctness lane", "prompt": "Lane: correctness.",
                       **({"background": True} if background else {})},
             "metadata": {"metadata": {"sessionID": session, "status": "running" if background else "completed"}},
             "output": f'<subagent sessionID="{session}" state="completed">findings</subagent>'}
    return {"type": "tool_use", "part": {"type": "tool", "tool": "subagent", "state": state}}


EVENTS = [
    {"type": "step_start", "part": {}},
    {"type": "text", "part": {"text": "Launching lanes."}},
    _subagent("ses_a"),
    _subagent("ses_b"),
    _step(0.10, 20000, write=19000),
    {"type": "step_start", "part": {}},
    {"type": "text", "part": {"text": "Here it is.\n\n" + REVIEW}},
    _step(0.05, 30000),
]


def _lane(cost):
    """`opencode session export` of a lane, as opencode 2.1 writes it."""
    return {"info": {"agent": "wf-review-lane", "cost": cost}, "messages": [
        {"type": "user", "text": "Lane: correctness."},
        {"type": "assistant", "cost": cost, "model": {"id": "claude-haiku-4-5", "providerID": "anthropic"},
         "tokens": {"input": 5, "output": 95, "reasoning": 0, "cache": {"write": 0, "read": 100000}}},
        {"type": "idle"}]}


EXPORTS = {"ses_a": _lane(0.20), "ses_b": _lane(0.30)}


class FakeRunner:
    def __init__(self, results=None, origin="git@github.com:acme/web.git", changed="src/wf/gh.py"):
        self.calls = []
        self.results = list(results or [OK])
        self.origin = origin
        self.changed = changed
        self.dir = tempfile.mkdtemp()
        self.git_dir = tempfile.mkdtemp()
        self.removed = []
        self.on_claude = None  # stands in for what the session does on GitHub
        self.events = list(EVENTS)
        self.exports = dict(EXPORTS)
        self.version = "2.1.0"

    def git(self, *args, cwd=None):
        self.calls.append(("git", *args))
        if args[:3] == ("remote", "get-url", "origin"):
            return self.origin
        if args[-1] == "--git-common-dir":
            return self.git_dir
        if args[0] == "rev-parse":
            return "abc1234"
        if args[0] == "diff":
            return self.changed
        if args[:2] == ("worktree", "add"):
            Path(args[-2]).mkdir(parents=True, exist_ok=True)
        return ""

    def claude(self, argv, cwd):
        self.calls.append(("claude", *argv))
        self.cwd = cwd
        if self.on_claude:
            self.on_claude()
        return self.results.pop(0)

    def prep(self, argv, cwd):
        self.calls.append(("prep", *argv))
        self.prep_cwd = cwd
        return f"{cwd}/.wf-review/prep.json"

    def opencode(self, argv, cwd, env):
        self.calls.append(("opencode", *argv))
        self.cwd, self.env = cwd, env
        return self.events

    def opencode_export(self, session):
        return self.exports.get(session)

    def opencode_version(self):
        self.calls.append(("opencode-version",))
        return self.version

    def tempdir(self):
        return self.dir

    def rmtree(self, path):
        self.removed.append(path)


PR = "/repos/acme/web"
COMMENT_LISTS = (f"{PR}/pulls/12/reviews", f"{PR}/pulls/12/comments", f"{PR}/issues/12/comments")


def with_pr(gh, body="Closes #11"):
    """A PR with nothing posted since any run starts: the comment lists answer `since` with nothing, and its one
    review was submitted long before."""
    gh.extra = {("GET", f"{PR}/pulls/12"): (200, {
        "number": 12, "body": body, "head": {"sha": "abc1234"}, "base": {"sha": "base000"}})}
    for path in COMMENT_LISTS:
        gh.extra[("GET", path)] = (200, [])
    gh.extra[("GET", f"{PR}/pulls/12/reviews")] = (200, [{"id": 1, "submitted_at": "2020-01-01T00:00:00Z"}])
    gh.extra[("POST", f"{PR}/issues/12/comments")] = (201, {"id": 2})
    return gh


def posts(gh, path=f"{PR}/pulls/12/comments"):
    """What a session that really posted leaves behind: one comment or review newer than the run's start."""
    def post():
        gh.extra[("GET", path)] = (200, [*gh.extra[("GET", path)][1], {"id": 2, "submitted_at": review._now()}])
    return post


def comment_gets(gh):
    """wf's GETs of the PR's comment and review lists, in order."""
    return [c[1] for c in gh.calls if c[0] == "GET" and c[1].partition("?")[0] in COMMENT_LISTS]


def assert_isolated(test, c):
    d = c.index("--disallowedTools")
    test.assertEqual(c[c.index("--setting-sources") + 1], "")
    test.assertEqual(json.loads(c[c.index("--settings") + 1])["enabledPlugins"], {"wf-review@agent-workflow": True})
    for flag in ("--setting-sources", "--settings", "--strict-mcp-config"):
        test.assertLess(c.index(flag), d)


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        patcher = mock.patch.object(review, "RUNNER", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claude_call(self, i=0):
        return [c for c in self.runner.calls if c[0] == "claude"][i]

    def posting_pr(self):
        gh = with_pr(FakeGh())
        self.runner.on_claude = posts(gh)
        return gh

    def test_review_runs_the_builtin_in_a_worktree_at_the_pr_head_and_posts(self):
        code, out, err = run(["review", "12"], self.posting_pr())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "#12: review posted, $2.38 (90k, +1900k cache)\n")
        git = [c for c in self.runner.calls if c[0] == "git"]
        tree = f"{self.runner.dir}/pr-12"
        # Fetched into a per-PR ref, never read back from the shared FETCH_HEAD.
        self.assertIn(("git", "fetch", "-q", "origin", "+pull/12/head:refs/wf-review/pr-12"), git)
        self.assertIn(("git", "rev-parse", "refs/wf-review/pr-12"), git)
        self.assertFalse([c for c in git if "FETCH_HEAD" in c])
        self.assertIn(("git", "worktree", "add", "-q", "--detach", tree, "abc1234"), git)
        self.assertEqual(git[-1], ("git", "worktree", "remove", "--force", tree))
        self.assertEqual(self.runner.removed, [self.runner.dir])
        c = self.claude_call()
        self.assertEqual(c[1:3], ("-p", "/code-review 12 high --comment"))
        self.assertEqual(self.runner.cwd, tree)
        for flag, value in (("--model", "opus"), ("--max-budget-usd", "6")):
            self.assertEqual(c[c.index(flag) + 1], value)
        prompt = c[c.index("--append-system-prompt") + 1]
        self.assertIn("issue #11", prompt)
        self.assertIn("gh api repos/acme/web/issues/11", prompt)
        self.assertNotIn("House rules", prompt)
        self.assertNotIn("Write", c[c.index("--allowedTools"):])
        self.assertEqual(len([x for x in self.runner.calls if x[0] == "claude"]), 1)

    def test_each_run_spends_onto_the_pr_head_branchs_ledger(self):
        from wf import ledger
        gh = self.posting_pr()
        gh.extra[("GET", f"{PR}/pulls/12")][1]["head"]["ref"] = "claude/11-x"
        self.runner.results = [OK, {**OK, "is_error": True}]
        self.assertEqual(run(["review", "12"], gh)[0], 0)
        self.assertEqual(run(["review", "12"], gh)[0], 1)  # a failed run spent too
        lines = ledger.read(self.runner.git_dir, "claude/11-x")
        self.assertEqual([(x["kind"], x["pr"], x["variant"], x["tokens"], x["cache_read"], x["turns"]) for x in lines],
                         [("review", 12, "builtin", 90050, 1900000, 9)] * 2)

    def test_keep_prunes_old_reviews_but_not_this_prs(self):
        import os
        folder = Path(self.runner.git_dir) / "wf-review"
        folder.mkdir(parents=True)
        for name in ("pr-3-builtin.md", "pr-12-last.json"):
            (folder / name).write_text("x")
            os.utime(folder / name, (1000, 1000))
        review._keep(folder, 12, "builtin", {"result": "r"})
        self.assertFalse((folder / "pr-3-builtin.md").exists())
        self.assertTrue((folder / "pr-12-last.json").exists())

    def test_a_run_that_posts_nothing_fails_and_keeps_the_review(self):
        denied = {"tool_name": "Bash", "tool_input": {"command": "gh api -X POST repos/x/pulls/12/reviews --input -"}}
        self.runner.results = [{**OK, "result": "1. A finding.", "num_turns": 0, "permission_denials": [denied]}]
        code, out, err = run(["review", "12"], with_pr(FakeGh()))
        kept = Path(self.runner.git_dir) / "wf-review" / "pr-12-builtin.md"
        self.assertEqual((code, out), (1, ""))
        self.assertIn("review reported success but posted nothing on #12", err)
        self.assertIn("denied: gh api -X POST repos/x/pulls/12/reviews --input -", err)
        self.assertIn(f"gh pr comment 12 --body-file {kept}", err)
        self.assertEqual(kept.read_text(), "1. A finding.\n")
        raw = json.loads(kept.with_suffix(".json").read_text())
        self.assertEqual(raw["permission_denials"], [denied])
        self.assertEqual(self.runner.removed, [self.runner.dir])

    def test_posted_check_makes_no_request_before_the_run(self):
        gh = self.posting_pr()
        post, before = self.runner.on_claude, []
        self.runner.on_claude = lambda: (before.extend(comment_gets(gh)), post())
        self.assertEqual(run(["review", "12"], gh)[0], 0)
        self.assertEqual(before, [])
        self.assertTrue(comment_gets(gh))

    def test_posted_check_stops_at_the_first_new_comment(self):
        gh = with_pr(FakeGh())
        self.runner.on_claude = posts(gh, f"{PR}/issues/12/comments")
        self.assertEqual(run(["review", "12"], gh)[0], 0)
        gets = comment_gets(gh)
        self.assertEqual(len(gets), 1)
        self.assertRegex(gets[0], r"/issues/12/comments\?since=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ&per_page=1$")

    def test_review_that_posts_nothing_still_fails(self):
        gh = with_pr(FakeGh())  # its only review was submitted before the run started
        code, out, err = run(["review", "12"], gh)
        kept = Path(self.runner.git_dir) / "wf-review" / "pr-12-builtin.md"
        self.assertEqual((code, out), (1, ""))
        self.assertIn("review reported success but posted nothing on #12", err)
        self.assertIn(f"gh pr comment 12 --body-file {kept}", err)
        self.assertEqual(len(comment_gets(gh)), 3)
        self.assertEqual(comment_gets(gh)[-1], f"{PR}/pulls/12/reviews?per_page=100")

    def test_a_run_starts_after_the_second_it_began_in(self):
        """A comment the previous run posted earlier in the same second is not this run's."""
        clock = mock.Mock(wraps=review.datetime)
        clock.now.return_value = review.datetime(2026, 11, 11, 10, 0, 5, 300000, tzinfo=review.timezone.utc)
        with mock.patch.object(review, "datetime", clock):
            self.assertEqual(review._now(), "2026-11-11T10:00:06Z")

    def test_printed_tokens_match_the_ledger_line(self):
        from wf import ledger
        gh = self.posting_pr()
        gh.extra[("GET", f"{PR}/pulls/12")][1]["head"]["ref"] = "claude/11-x"
        code, out, _ = run(["review", "12", "--json"], gh)
        self.assertEqual(code, 0)
        record = json.loads(out.splitlines()[0])
        line = ledger.read(self.runner.git_dir, "claude/11-x")[-1]
        self.assertEqual((record["tokens"], record["cache_read"]), (line["tokens"], line["cache_read"]))
        self.runner.results = [OK]
        code, out, _ = run(["review", "12"], self.posting_pr())
        self.assertIn(f"({line['tokens'] // 1000}k, +{line['cache_read'] // 1000}k cache)", out)

    def test_a_review_posted_as_a_pr_review_counts(self):
        gh = with_pr(FakeGh())
        self.runner.on_claude = posts(gh, f"{PR}/pulls/12/reviews")
        self.assertEqual(run(["review", "12"], gh)[0], 0)

    def test_the_review_session_is_unchanged_by_the_check(self):
        """The check is wf's own API calls: the session's prompt and tools are what they were."""
        run(["review", "12"], self.posting_pr())
        c = self.claude_call()
        tools = c[c.index("--allowedTools") + 1:]
        self.assertEqual(tuple(tools), review.ALLOWED_TOOLS)
        self.assertEqual(c[c.index("--append-system-prompt") + 1].count("comment"), 0)

    def test_claude_timeout_is_a_wf_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", review.TIMEOUT)):
            with self.assertRaisesRegex(review.WfError, "did not finish within 45 minutes"):
                review.Runner().claude(["-p", "x"], cwd=".")

    def test_no_post_run_gets_no_post_tools(self):
        run(["review", "12", "--no-post"], with_pr(FakeGh()))
        argv = " ".join(self.claude_call())
        self.assertNotIn("gh pr comment", argv)
        self.assertNotIn("gh pr review", argv)
        self.assertIn("gh pr diff", argv)

    def test_non_success_subtype_fails_the_review(self):
        self.runner.results = [{"subtype": "error_max_turns", "result": "stopped"}]
        code, _, err = run(["review", "12", "--no-post"], with_pr(FakeGh()))
        self.assertEqual(code, 1)
        self.assertIn("error_max_turns", err)

    def test_changed_paths_with_spaces_stay_whole(self):
        self.runner.changed = "docs/a b.md\nsrc/x.py"
        with mock.patch.object(review, "claude_md_paths", return_value=[]) as m:
            run(["review", "12", "--no-post"], with_pr(FakeGh()))
        self.assertEqual(m.call_args.args[1], ["docs/a b.md", "src/x.py"])

    def test_an_errored_run_is_reported_and_the_worktree_still_removed(self):
        self.runner.results = [{"is_error": True, "terminal_reason": "api_error",
                                "result": "Failed to authenticate. API Error: 401"}]
        code, out, err = run(["review", "12"], with_pr(FakeGh()))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "wf: review failed (api_error): Failed to authenticate. API Error: 401\n")
        self.assertEqual(self.runner.calls[-1][:3], ("git", "worktree", "remove"))
        self.assertEqual(self.runner.removed, [self.runner.dir])

    def test_budget_exhaustion_is_an_error(self):
        self.runner.results = [{**OK, "terminal_reason": "max_budget_usd_reached"}]
        code, _, err = run(["review", "12"], with_pr(FakeGh()))
        self.assertEqual(code, 1)
        self.assertIn("max_budget_usd_reached", err)

    def test_config_and_flags_set_level_model_and_budget(self):
        code, _, _ = run(["review", "12", "--level", "max"], self.posting_pr(), review='budget_usd = 3\nmodel = "sonnet"')
        self.assertEqual(code, 0)
        c = self.claude_call()
        self.assertEqual(c[2], "/code-review 12 max --comment")
        self.assertEqual(c[c.index("--max-budget-usd") + 1], "3")
        self.assertEqual(c[c.index("--model") + 1], "sonnet")
        gh = with_pr(FakeGh())
        self.assertEqual(run(["review", "12", "--budget", "0"], gh)[0], 2)
        self.assertEqual(gh.calls, [])

    def test_claude_md_files_above_changed_files_are_named(self):
        tree = Path(self.runner.dir) / "pr-12"
        (tree / "src" / "wf").mkdir(parents=True)
        (tree / "CLAUDE.md").write_text("root")
        (tree / "src" / "CLAUDE.md").write_text("src")
        (tree / "docs").mkdir()
        (tree / "docs" / "CLAUDE.md").write_text("docs, untouched")
        code, _, _ = run(["review", "12"], self.posting_pr())
        self.assertEqual(code, 0)
        c = self.claude_call()
        prompt = c[c.index("--append-system-prompt") + 1]
        self.assertIn("House rules are in: CLAUDE.md, src/CLAUDE.md.", prompt)
        self.assertNotIn("docs/CLAUDE.md", prompt)

    def test_review_session_loads_no_settings_files_or_mcp(self):
        run(["review", "12", "--no-post"], with_pr(FakeGh()))
        assert_isolated(self, self.claude_call(0))

    def test_write_still_extends_the_allowed_tools(self):
        self.runner.results = [OK, OK]
        run(["review", "12", "--no-post", "--lean"], with_pr(FakeGh()))
        c = self.claude_call(1)
        i = c.index("--allowedTools")
        self.assertEqual(c[-1], "Write")
        self.assertFalse([a for a in c[i + 1:] if a.startswith("--")])

    def test_no_post_prints_the_findings_and_lean_adds_a_second_run(self):
        self.runner.results = [{**OK, "result": "1. A finding."}, {**OK, "num_turns": 4, "total_cost_usd": 1.5}]
        code, out, _ = run(["review", "12", "--no-post", "--lean"], with_pr(FakeGh()))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2], ["#12: findings below, $2.38 (90k, +1900k cache)", "1. A finding."])
        self.assertEqual(self.claude_call(0)[2], "/code-review 12 high")
        second = self.claude_call(1)
        self.assertEqual(second[2], "/wf-review:review 12 --no-post --out .wf-review-lean.md")
        self.assertEqual(second[-1], "Write")
        self.assertIn("#12: wf-review written, $1.50", out)
        self.assertTrue((Path(self.runner.git_dir) / "wf-review" / "pr-12-lean.json").is_file())

    def test_json_output(self):
        code, out, _ = run(["review", "12", "--json"], self.posting_pr())
        self.assertEqual(code, 0)
        self.assertIn('"variant": "builtin", "tier": null, "level": "high", "posted": true, "head": "abc1234", "cost_usd": 2.38', out)

    def test_not_a_github_checkout(self):
        self.runner.origin = "/srv/git/local.git"
        code, _, err = run(["review", "12"], with_pr(FakeGh()))
        self.assertEqual(code, 1)
        self.assertIn("checkout of the PR's repo", err)
        self.assertFalse([c for c in self.runner.calls if c[0] == "claude"])


class OpencodeTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        patcher = mock.patch.object(review, "RUNNER", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)

    def opencode_call(self):
        return next(c for c in self.runner.calls if c[0] == "opencode")

    def config(self):
        return json.loads(self.runner.env["OPENCODE_CONFIG_CONTENT"])

    def test_preps_runs_opencode_in_the_worktree_prices_the_lanes_and_posts(self):
        gh = with_pr(FakeGh())
        code, out, err = run(["review", "12", "--engine", "opencode"], gh)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "#12: wf-review posted (opencode anthropic/claude-sonnet-5-5, "
                              "lanes anthropic/claude-sonnet-5-5), $0.65 (50k, +200k cache)\n")
        tree = str(Path(self.runner.dir).resolve() / "pr-12")
        self.assertIn(("prep", "12", "--repo", "acme/web", "--dir", f"{tree}/.wf-review", "--force"),
                      self.runner.calls)
        self.assertEqual((self.runner.prep_cwd, self.runner.cwd), (tree, tree))
        self.assertIn({"action": "external_directory", "resource": f"{tree}/*", "effect": "allow"},
                      self.config()["agents"]["wf-review"]["permissions"])
        c = self.opencode_call()
        self.assertEqual(c[1:6], ("run", "--standalone", "--format", "json", "--agent"))
        self.assertNotIn("--pure", c)
        self.assertEqual(c[c.index("-m") + 1], "anthropic/claude-sonnet-5-5")
        self.assertIn(f"The prep file is {tree}/.wf-review/prep.json", c[-1])
        self.assertFalse([x for x in self.runner.calls if x[0] == "claude"])
        posted = [w for w in gh.writes() if w[0] == "POST"]
        self.assertEqual(posted, [("POST", f"{PR}/issues/12/comments", {"body": REVIEW})])
        self.assertEqual(self.runner.calls[-1][:3], ("git", "worktree", "remove"))
        kept = json.loads((Path(self.runner.git_dir) / "wf-review" / "pr-12-opencode.json").read_text())
        self.assertEqual(kept["subagent_stats"], {"spawned": 2, "unpriced": [], "background": []})
        self.assertEqual(set(kept["modelUsage"]), {"claude-sonnet-5-5", "claude-haiku-4-5"})
        self.assertEqual(kept["num_turns"], 4)

    def test_the_reviewed_repos_claude_md_is_an_instruction_unless_it_has_agents_md(self):
        # opencode 2 reads AGENTS.md only, so wf names the CLAUDE.md
        tree = Path(self.runner.dir) / "pr-12"
        tree.mkdir()
        (tree / "CLAUDE.md").write_text("House rules.")
        run(["review", "12", "--engine", "opencode"], with_pr(FakeGh()))
        self.assertEqual(self.config()["instructions"], [str(tree.resolve() / "CLAUDE.md")])
        (tree / "AGENTS.md").write_text("opencode loads this itself.")
        run(["review", "12", "--engine", "opencode"], with_pr(FakeGh()))
        self.assertEqual(self.config()["instructions"], [])

    def test_the_agents_read_only_and_only_the_orchestrator_launches_lanes(self):
        run(["review", "12", "--engine", "opencode", "--lane-model", "anthropic/claude-haiku-4-5"], with_pr(FakeGh()))
        config = self.config()
        self.assertNotIn("agent", config)  # opencode 2's native shape: agents, system, permissions
        main, lane = config["agents"]["wf-review"], config["agents"]["wf-review-lane"]
        self.assertEqual(lane["model"], "anthropic/claude-haiku-4-5")
        self.assertEqual(lane["mode"], "subagent")
        self.assertNotIn("---", lane["system"][:3])
        self.assertIn("Rate each finding", lane["system"])
        self.assertIn("never set `background`", main["system"])

        def of(perms, action):
            return [(r["resource"], r["effect"]) for r in perms if r["action"] == action]

        self.assertEqual(of(main["permissions"], "subagent"), [("*", "deny"), ("wf-review-lane", "allow")])
        self.assertEqual(of(lane["permissions"], "subagent"), [("*", "deny")])
        for perms in (main["permissions"], lane["permissions"]):
            self.assertEqual(perms[0], {"action": "*", "resource": "*", "effect": "deny"})
            self.assertEqual({r["action"] for r in perms},
                             {"*", "read", "grep", "glob", "shell", "subagent", "external_directory"})
            self.assertNotIn("ask", {r["effect"] for r in perms})
            shell = of(perms, "shell")
            self.assertEqual(shell[0], ("*", "deny"))
            keys = [k for k, _ in shell]
            self.assertLess(keys.index("gh api *"), keys.index("gh api *-X*"))
            self.assertLess(keys.index("ast-grep run *"), keys.index("ast-grep *-U*"))
            self.assertLess(keys.index("git diff *"), keys.index("git *--output*"))
            self.assertIn("gh api * -f*", keys)
            self.assertEqual(of(perms, "external_directory")[0], ("*", "deny"))
        self.assertEqual(self.runner.env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"], "1")
        self.assertNotIn("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT", self.runner.env)

    def test_a_lane_launched_in_the_background_fails_the_review_and_posts_nothing(self):
        self.runner.events = [EVENTS[0], _subagent("ses_a", background=True), *EVENTS[3:]]
        gh = with_pr(FakeGh())
        code, out, err = run(["review", "12", "--engine", "opencode"], gh)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("launched 1 lane(s) in the background", err)
        self.assertIn("pr-12-opencode.md", err)
        self.assertEqual(gh.writes(), [])
        kept = json.loads((Path(self.runner.git_dir) / "wf-review" / "pr-12-opencode.json").read_text())
        self.assertEqual(kept["subagent_stats"]["background"], ["ses_a"])
        self.assertFalse((Path(self.runner.git_dir) / "wf-review" / "pr-12-last.json").exists())
        self.assertEqual(self.runner.calls[-1][:3], ("git", "worktree", "remove"))

    def test_an_opencode_older_than_2_1_is_refused_before_anything_runs(self):
        for version in ("1.19.3", "opencode v2.0.21", ""):
            self.runner.calls.clear()
            self.runner.version = version
            gh = with_pr(FakeGh())
            code, _, err = run(["review", "12", "--engine", "opencode"], gh)
            self.assertEqual(code, 1, version)
            self.assertIn("wf needs opencode 2.1 or later", err)
            self.assertEqual(self.runner.calls, [("opencode-version",)])
            self.assertEqual(gh.calls, [])
        self.runner.version = "opencode v2.10.0"
        self.assertEqual(run(["review", "12", "--engine", "opencode"], with_pr(FakeGh()))[0], 0)

    def test_the_claude_engine_does_not_need_opencode(self):
        self.runner.version = "1.0.0"
        gh = with_pr(FakeGh())
        self.runner.on_claude = posts(gh)
        self.assertEqual(run(["review", "12"], gh)[0], 0)
        self.assertNotIn(("opencode-version",), self.runner.calls)

    def test_a_lane_session_named_only_in_the_tool_output_is_still_priced(self):
        lane = _subagent("ses_b")
        lane["part"]["state"]["metadata"] = {}
        self.runner.events = [*EVENTS[:3], lane, *EVENTS[4:]]
        code, out, _ = run(["review", "12", "--engine", "opencode"], with_pr(FakeGh()))
        self.assertEqual(code, 0)
        self.assertIn("$0.65", out)

    def test_no_post_prints_the_review_and_posts_nothing(self):
        gh = with_pr(FakeGh())
        code, out, _ = run(["review", "12", "--engine", "opencode", "--no-post"], gh)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("#12: findings below (opencode"))
        self.assertIn(REVIEW, out)
        self.assertNotIn("Here it is.", out)
        self.assertEqual(gh.writes(), [])

    def test_a_run_without_a_review_fails_and_keeps_the_output(self):
        self.runner.events = [{"type": "step_start", "part": {}}, {"type": "text", "part": {"text": "I could not."}},
                              _step(0.01, 100)]
        gh = with_pr(FakeGh())
        code, _, err = run(["review", "12", "--engine", "opencode"], gh)
        self.assertEqual(code, 1)
        self.assertIn("ended without a review", err)
        self.assertIn("pr-12-opencode.md", err)
        self.assertEqual(gh.writes(), [])
        self.assertEqual(self.runner.calls[-1][:3], ("git", "worktree", "remove"))

    def test_an_error_event_is_reported(self):
        self.runner.events = [{"type": "error", "error": {"name": "APIError", "data": {"message": "credit balance too low"}}}]
        code, _, err = run(["review", "12", "--engine", "opencode"], with_pr(FakeGh()))
        self.assertEqual(code, 1)
        self.assertIn("credit balance too low", err)

    def test_an_error_event_without_a_message_is_still_reported(self):
        for error in ("rate limited", {"name": "X", "data": None}):
            self.runner.events = [{"type": "error", "error": error}]
            code, _, err = run(["review", "12", "--engine", "opencode"], with_pr(FakeGh()))
            self.assertEqual(code, 1)
            self.assertIn("failed", err)

    def test_an_unpriced_lane_is_flagged(self):
        del self.runner.exports["ses_b"]
        code, out, _ = run(["review", "12", "--engine", "opencode"], with_pr(FakeGh()))
        self.assertEqual(code, 0)
        self.assertIn("$0.35", out)
        self.assertIn("1 lane(s) unpriced", out)

    def test_engine_and_models_from_config(self):
        code, out, _ = run(["review", "12"], with_pr(FakeGh()),
                           review='engine = "opencode"\nopencode_model = "openai/gpt-6"\nlane_model = "opencode/big-pickle"')
        self.assertEqual(code, 0)
        self.assertIn("(opencode openai/gpt-6, lanes opencode/big-pickle)", out)
        self.assertEqual(self.config()["agents"]["wf-review-lane"]["model"], "opencode/big-pickle")
        code, _, _ = run(["review", "12", "--engine", "claude", "--no-post"], with_pr(FakeGh()), review='engine = "opencode"')
        self.assertEqual(code, 0)
        self.assertTrue([c for c in self.runner.calls if c[0] == "claude"])

    def test_claude_only_flags_and_bare_model_names_are_usage_errors(self):
        for argv in (["--engine", "opencode", "--lean"], ["--engine", "opencode", "--level", "max"],
                     ["--engine", "opencode", "--budget", "2"], ["--engine", "opencode", "--model", "sonnet"],
                     ["--lane-model", "anthropic/claude-haiku-4-5"]):
            gh = with_pr(FakeGh())
            self.assertEqual(run(["review", "12", *argv], gh)[0], 2, argv)
            self.assertEqual(gh.calls, [])


def sized(gh, size):
    gh.extra[("GET", f"{PR}/issues/11")] = (200, {"number": 11, "labels": [{"name": "bug"}, {"name": f"effort:{size}"}]})
    return gh


class TierTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        patcher = mock.patch.object(review, "RUNNER", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claude_call(self, i=0):
        return [c for c in self.runner.calls if c[0] == "claude"][i]

    def test_untiered_runs_as_before_and_reads_no_issue(self):
        gh = with_pr(FakeGh())
        run(["review", "12", "--no-post"], gh)
        self.assertEqual(self.claude_call()[2], "/code-review 12 high")
        self.assertNotIn(("GET", f"{PR}/issues/11", None), gh.calls)

    def test_tiered_takes_the_level_from_the_issue_size(self):
        for size, level in (("S", "medium"), ("M", "high"), ("XL", "xhigh")):
            self.runner.calls.clear()
            self.runner.results = [OK]
            code, out, _ = run(["review", "12", "--no-post"], sized(with_pr(FakeGh()), size), review="tiered = true")
            self.assertEqual(code, 0)
            self.assertEqual(self.claude_call()[2], f"/code-review 12 {level}")
            self.assertIn(f"(#11 is {size}; level {level})"[1:], out)

    def test_tiered_without_a_size_label_goes_by_diff_size(self):
        gh = with_pr(FakeGh())
        gh.extra[("GET", f"{PR}/pulls/12")][1].update({"additions": 700, "deletions": 200})
        code, out, _ = run(["review", "12", "--no-post"], gh, review="tiered = true")
        self.assertEqual(code, 0)
        self.assertIn("deep: 900 changed lines; level xhigh", out)

    def test_tier_and_level_flags_win(self):
        run(["review", "12", "--no-post", "--tier", "light"], with_pr(FakeGh()))
        self.assertEqual(self.claude_call()[2], "/code-review 12 medium")
        self.runner.calls.clear()
        self.runner.results = [OK]
        run(["review", "12", "--no-post", "--tier", "light", "--level", "max"], with_pr(FakeGh()))
        self.assertEqual(self.claude_call()[2], "/code-review 12 max")

    def test_opencode_lanes_follow_the_tier(self):
        for argv, lanes in ((["--tier", "light"], "correctness, reuse"), ([], "correctness, spec, reuse"),
                            (["--tier", "deep"], "correctness, spec, reuse, history")):
            self.runner.calls.clear()
            run(["review", "12", "--engine", "opencode", "--no-post", *argv], with_pr(FakeGh()))
            message = next(c for c in self.runner.calls if c[0] == "opencode")[-1]
            self.assertIn(f"Lanes: {lanes}.", message)

    def test_the_builtin_is_asked_for_reuse_and_ast_grep_is_allowed(self):
        run(["review", "12", "--no-post"], with_pr(FakeGh()))
        c = self.claude_call()
        self.assertIn("duplicates something the codebase already has", c[c.index("--append-system-prompt") + 1])
        self.assertIn("Bash(ast-grep run:*)", c)
        denied = c[c.index("--disallowedTools") + 1:c.index("--allowedTools")]
        self.assertEqual(tuple(denied), review.DENIED_TOOLS)
        self.assertIn("Bash(git * --output*)", denied)


class FixRoundTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        patcher = mock.patch.object(review, "RUNNER", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keep = Path(self.runner.git_dir) / "wf-review"

    def record(self, head="old5678"):
        self.keep.mkdir(parents=True, exist_ok=True)
        previous = self.keep / "pr-12-builtin.md"
        previous.write_text("1. A finding.\n")
        (self.keep / "pr-12-last.json").write_text(json.dumps({"head": head, "review": str(previous)}))
        return previous

    def test_a_review_records_its_head_for_since_last(self):
        run(["review", "12", "--engine", "opencode", "--no-post"], with_pr(FakeGh()))
        state = json.loads((self.keep / "pr-12-last.json").read_text())
        self.assertEqual(state["head"], "abc1234")
        self.assertTrue(state["review"].endswith("pr-12-opencode.md"))

    def test_since_last_with_no_earlier_review_is_an_error(self):
        code, _, err = run(["review", "12", "--since", "last"], with_pr(FakeGh()))
        self.assertEqual(code, 1)
        self.assertIn("no earlier review of #12", err)
        self.assertFalse([c for c in self.runner.calls if c[0] in ("claude", "opencode")])

    def test_nothing_new_since_the_last_review(self):
        self.record(head="abc1234")
        code, out, _ = run(["review", "12", "--since", "last"], with_pr(FakeGh()))
        self.assertEqual((code, out), (0, "#12: no commits since abc1234; nothing to review\n"))
        self.assertFalse([c for c in self.runner.calls if c[0] in ("claude", "opencode", "prep")])

    def test_opencode_fix_round_reviews_the_new_commits_with_the_earlier_review(self):
        previous = self.record()
        code, out, _ = run(["review", "12", "--engine", "opencode", "--since", "last"], with_pr(FakeGh()))
        self.assertEqual(code, 0)
        self.assertIn("(fix round since old5678; opencode", out)
        prep = next(c for c in self.runner.calls if c[0] == "prep")
        self.assertEqual(prep[-4:], ("--since", "old5678", "--previous", str(previous)))
        message = next(c for c in self.runner.calls if c[0] == "opencode")[-1]
        self.assertIn("Lanes: correctness. This is a fix round: the commits since old5678.", message)
        self.assertEqual(json.loads((self.keep / "pr-12-last.json").read_text())["head"], "abc1234")

    def test_lean_and_fix_round_sessions_are_isolated(self):
        self.record()
        self.runner.results = [OK]
        run(["review", "12", "--since", "old5678", "--no-post"], with_pr(FakeGh()))
        assert_isolated(self, self.claude_call())
        self.runner.calls.clear()
        self.runner.results = [OK, OK]
        run(["review", "12", "--no-post", "--lean"], with_pr(FakeGh()))
        assert_isolated(self, self.claude_call(1))

    def test_claude_fix_round_runs_the_plugin_not_the_builtin(self):
        self.record()
        self.runner.results = [{**OK, "total_cost_usd": 0.9}]
        out_file = Path(self.runner.dir) / "pr-12" / ".wf-review-fix.md"
        self.runner.on_claude = lambda: out_file.write_text("<!-- wf-review -->\nPrevious findings (1)\n")
        code, out, _ = run(["review", "12", "--since", "old5678", "--no-post"], with_pr(FakeGh()))
        self.assertEqual(code, 0)
        c = self.claude_call()
        self.assertEqual(c[2], f"/wf-review:review 12 --since old5678 --previous {self.keep / 'pr-12-builtin.md'} "
                               "--no-post --out .wf-review-fix.md")
        self.assertEqual(c[-1], "Write")
        self.assertIn("#12: fix-round review below (since old5678), $0.90", out)
        self.assertIn("Previous findings (1)", out)

    def claude_call(self, i=0):
        return [c for c in self.runner.calls if c[0] == "claude"][i]

    def test_opencode_fix_no_post_record_names_its_variant(self):
        self.record()
        code, out, _ = run(["review", "12", "--engine", "opencode", "--since", "last", "--no-post", "--json"],
                           with_pr(FakeGh()))
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(x)["variant"] for x in out.splitlines()], ["opencode-fix", "opencode-fix"])

    def test_since_and_lean_together_is_a_usage_error(self):
        gh = with_pr(FakeGh())
        self.assertEqual(run(["review", "12", "--since", "abc", "--lean"], gh)[0], 2)
        self.assertEqual(gh.calls, [])


class HelpersTest(unittest.TestCase):
    def test_repo_from_origin(self):
        self.assertEqual(review.repo_from_origin("https://github.com/acme/game.git"), "acme/game")
        self.assertEqual(review.repo_from_origin("git@github.com:a/b"), "a/b")
        with self.assertRaises(WfError):
            review.repo_from_origin("")

    def test_linked_issue(self):
        self.assertEqual(review.linked_issue("Closes #9\n"), 9)
        self.assertIsNone(review.linked_issue("Refs #9"))


if __name__ == "__main__":
    unittest.main()
