import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_gh import FakeGh, field_id, option, run
from wf.commands import board_model, dispatch

ISSUE = "/repos/acme/web/issues/40"
CONTRACT = "---\nname: implementer\ntools: Bash, Read\n---\n\nYou are the implementer. Never merge.\n"


def _step(cost, inp, out, write, read, session="ses_1"):
    return {"type": "step_finish", "sessionID": session, "part": {"cost": cost, "tokens": {
        "input": inp, "output": out, "reasoning": 0, "cache": {"write": write, "read": read}}}}


EVENTS = [
    {"type": "step_start", "sessionID": "ses_1", "part": {}},
    {"type": "text", "sessionID": "ses_1", "part": {"text": "PR #41 opened."}},
    _step(1.25, 1000, 9000, 200000, 3000000),
    _step(0.50, 500, 4500, 100000, 2000000),
]


class FakeRunner:
    """git and opencode, faked: `worktree add` makes the directory with the consumer's contract in it."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.common = self.root / "repo" / ".git"
        self.common.mkdir(parents=True)
        self.home_dir = self.root / "home"
        self.calls, self.runs = [], []
        self.origin = "git@github.com:acme/web.git"
        self.events, self.code = list(EVENTS), 0
        self.diff = ""  # what `git diff --name-only -z <sha>` in the worktree says the run changed, NUL-separated
        self.project_config = None  # opencode.json the worktree's checkout carries
        self.opencode_version = "opencode v2.1.0"

    def git(self, *args, cwd=None):
        self.calls.append(("git", *args))
        if args[:3] == ("remote", "get-url", "origin"):
            return self.origin
        if args[-1] == "--git-common-dir":
            return str(self.common)
        if args[0] == "rev-parse":
            return "0123456789abcdef0123"
        if args[0] == "diff":
            return self.diff
        if args[:2] == ("worktree", "add"):
            tree = Path(args[-2])
            (tree / ".claude" / "agents").mkdir(parents=True)
            (tree / ".claude" / "agents" / "implementer.md").write_text(CONTRACT)
            (tree / "CLAUDE.md").write_text("# House rules\n")
            if self.project_config is not None:
                (tree / "opencode.json").write_text(json.dumps(self.project_config))
        return ""

    def opencode(self, argv, cwd, env, log, errlog):
        self.runs.append({"argv": argv, "cwd": cwd, "env": env, "log": log})
        Path(log).write_text("".join(json.dumps(e) + "\n" for e in self.events) + "not json\n")
        Path(errlog).write_text("")
        return self.code

    def export(self, session):
        return None

    def version(self):
        return self.opencode_version

    def home(self):
        return self.home_dir


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        patcher = mock.patch.object(dispatch, "RUNNER", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = FakeGh()
        self.gh.extra[("GET", ISSUE)] = (200, {"number": 40, "title": "Add the lap table: per-lap splits!",
                                               "body": "Build the table.", "labels": [{"name": "effort:M"}]})

    @property
    def tree(self):
        return self.runner.common.parent / ".claude" / "worktrees" / "opencode-40"

    @property
    def keep(self):
        return self.runner.common / "wf-dispatch" / "40"

    def dispatch(self, *extra, **config):
        return run(["dispatch", "web#40", "--engine", "opencode", *extra], self.gh, **config)

    def test_cuts_the_worktree_from_origin_dev_on_the_contracts_branch(self):
        code, out, err = self.dispatch()
        self.assertEqual((code, err), (0, ""))
        self.assertIn(("git", "fetch", "-q", "origin", "dev"), self.runner.calls)
        self.assertIn(("git", "worktree", "add", "-q", "--no-track", "-b", "claude/40-add-the-lap-table-per-lap-splits",
                       str(self.tree), "origin/dev"), self.runner.calls)
        self.assertEqual(self.runner.runs[0]["cwd"], str(self.tree))
        self.assertTrue(out.startswith(f"web#40: worktree {self.tree} on claude/40-add-the-lap-table-per-lap-splits "
                                       "from origin/dev at 0123456"), out)

    def test_the_instruction_file_is_the_contract_plus_the_issue(self):
        self.dispatch()
        text = (self.keep / "implementer.md").read_text()
        self.assertTrue(text.startswith("You are the implementer. Never merge.\n\n## This run\n"), text)
        self.assertNotIn("tools: Bash", text)  # the Claude agent's frontmatter is not instructions
        self.assertIn("Your issue is acme/web#40 (`web#40`): Add the lap table: per-lap splits!", text)
        self.assertIn(f"{self.tree}, on branch `claude/40-add-the-lap-table-per-lap-splits`", text)
        self.assertFalse((self.tree / "AGENTS.md").exists())  # nothing written into the checkout
        config = json.loads(self.runner.runs[0]["env"]["OPENCODE_CONFIG_CONTENT"])
        # the house rules are named: opencode 2 reads AGENTS.md only
        self.assertEqual(config["instructions"], [str(self.keep / "implementer.md"), str(self.tree / "CLAUDE.md")])
        self.assertEqual(self.runner.runs[0]["env"]["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"], "1")
        self.assertNotIn("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT", self.runner.runs[0]["env"])
        self.assertNotIn("agent", config)
        perms = config["agents"]["wf-implementer"]["permissions"]
        self.assertIn({"action": "subagent", "resource": "*", "effect": "deny"}, perms)
        shell = [(r["resource"], r["effect"]) for r in perms if r["action"] == "shell"]
        self.assertEqual(shell[0], ("*", "allow"))  # last match wins: the denies come after
        self.assertIn(("gh pr merge*", "deny"), shell)
        self.assertIn(("git rebase*", "deny"), shell)

    def test_dispatch_prunes_stale_runs_but_not_its_own(self):
        import os
        for name in ("7", "40"):
            stale = self.runner.common / "wf-dispatch" / name
            stale.mkdir(parents=True)
            (stale / "old-run.jsonl").write_text("{}")
            os.utime(stale / "old-run.jsonl", (1000, 1000))
            os.utime(stale, (1000, 1000))
        self.dispatch()
        self.assertFalse((self.runner.common / "wf-dispatch" / "7").exists())
        self.assertTrue((self.keep / "old-run.jsonl").exists())  # a file dispatch itself does not write

    def test_runs_opencode_headless_naming_the_issue_and_logs_the_events(self):
        code, out, _ = self.dispatch()
        argv = self.runner.runs[0]["argv"]
        self.assertEqual(argv[:10], ["run", "--standalone", "--format", "json", "--auto", "--agent", "wf-implementer",
                                     "-m", "anthropic/claude-sonnet-5-5", "--title"])
        self.assertIn("Implement issue web#40 (acme/web#40)", argv[-1])
        self.assertNotIn("--session", argv)
        self.assertEqual(self.runner.runs[0]["log"], self.keep / "run-1.jsonl")
        self.assertIn(f"log {self.keep / 'run-1.jsonl'}", out)

    def test_reports_cost_and_the_wf_merged_flags(self):
        code, out, _ = self.dispatch()
        lines = out.splitlines()
        # kTokens = input + output + cache writes (1000+9000+200000 + 500+4500+100000); cache reads apart
        self.assertEqual(lines[-3], "web#40: opencode exited 0 after 2 turns, $1.75 (315k, cache read 5000k)")
        self.assertEqual(lines[-2], "web#40: reads 0 over 0 files; 0 reads of 0 files outside the edit set (0 files edited)")
        self.assertEqual(lines[-1], "wf merged flags: --model opencode-sonnet --ktokens 315 --cache-ktokens 5000 --turns 2 (over 1 run(s))")
        state = json.loads((self.keep / "state.json").read_text())
        self.assertEqual((state["session"], state["branch"]), ("ses_1", "claude/40-add-the-lap-table-per-lap-splits"))

    def test_each_run_spends_onto_the_issue_branchs_ledger(self):
        from wf import ledger
        self.dispatch()
        self.dispatch("--resume", "--message", "Ruling: take (a).")
        lines = ledger.read(self.runner.common, "claude/40-add-the-lap-table-per-lap-splits")
        self.assertEqual([{k: v for k, v in x.items() if k != "at"} for x in lines], [
            {"kind": "agent", "id": f"ses_1-run-{i}", "agent_type": "opencode", "transcript": str(self.keep / f"run-{i}.jsonl"),
             "model": "opencode-sonnet", "tokens": 315000, "cache_read": 5000000, "turns": 2} for i in (1, 2)])

    def test_counts_reads_outside_the_edit_set(self):
        def tool(name, **inp):
            return {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": name, "state": {"input": inp}}}
        self.runner.events[1:1] = [
            tool("read", filePath=str(self.tree / "src/laps.py")), tool("read", filePath=str(self.tree / "src/laps.py")),
            tool("read", filePath=str(self.tree / "docs/arch.md")),
            tool("bash", command="sed -n 1,40p src/util.py | head -5"), tool("bash", command="grep -rn lap src"),
            tool("bash", command="cat README.md && pytest -q")]
        self.runner.diff = "src/laps.py\0src/util.py\0"
        code, out, _ = self.dispatch("--json")
        self.assertEqual(code, 0)
        rec = next(json.loads(x) for x in out.splitlines() if "reads_outside_edit" in x)
        self.assertEqual((rec["reads"], rec["read_files"], rec["reads_outside_edit"], rec["edited"]), (5, 4, 2, 2))
        self.assertEqual(rec["files_outside_edit"], ["README.md", "docs/arch.md"])
        self.assertIn("web#40: reads 5 over 4 files; 2 reads of 2 files outside the edit set (2 files edited)", self.dispatch("--resume")[1])
        run1 = json.loads((self.keep / "state.json").read_text())["runs"][0]
        self.assertEqual(run1["reads_outside_edit"], 2)

    def test_a_long_outside_list_is_capped_but_state_keeps_it(self):
        def tool(name, **inp):
            return {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": name, "state": {"input": inp}}}
        self.runner.events[1:1] = [tool("read", filePath=str(self.tree / f"docs/f{i:02}.md")) for i in range(25)]
        self.runner.diff = "src/laps.py\0"
        code, out, _ = self.dispatch("--json")
        rec = next(json.loads(x) for x in out.splitlines() if "reads_outside_edit" in x)
        self.assertEqual((len(rec["files_outside_edit"]), rec["files_outside_edit_total"]), (20, 25))
        run1 = json.loads((self.keep / "state.json").read_text())["runs"][0]
        self.assertEqual(len(run1["files_outside_edit"]), 25)

    def test_exits_with_opencodes_status(self):
        self.runner.code = 3
        self.runner.events.append({"type": "error", "sessionID": "ses_1", "error": {"data": {"message": "rate limited"}}})
        code, out, _ = self.dispatch()
        self.assertEqual(code, 3)
        self.assertIn("opencode exited 3", out)
        self.assertIn("error: rate limited", out)

    def test_model_and_variant_from_config_and_flags(self):
        self.dispatch(opencode='model = "anthropic/claude-opus-5-5"\nvariant = "medium"\nbase = "main"')
        argv = self.runner.runs[0]["argv"]
        # opencode 2.1 takes the variant on the model; it has no --variant
        self.assertEqual(argv[argv.index("-m") + 1], "anthropic/claude-opus-5-5#medium")
        self.assertNotIn("--variant", argv)
        self.assertIn(("git", "fetch", "-q", "origin", "main"), self.runner.calls)

    def test_model_flag_wins(self):
        code, out, _ = self.dispatch("--model", "openai/gpt-5", opencode='model = "anthropic/claude-opus-5-5"')
        argv = self.runner.runs[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "openai/gpt-5")
        self.assertNotIn("--variant", argv)
        self.assertIn("--model opencode-gpt-5 ", out)

    def test_variant_flag_replaces_or_drops_the_configured_one(self):
        config = 'model = "anthropic/claude-opus-5-5#low"\nvariant = "medium"'
        self.dispatch("--variant", "high", opencode=config)
        argv = self.runner.runs[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "anthropic/claude-opus-5-5#high")
        self.setUp()
        _, out, _ = self.dispatch("--variant", "none", opencode=config)
        argv = self.runner.runs[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "anthropic/claude-opus-5-5")
        self.assertIn("--model opencode-opus ", out)

    def test_an_opencode_older_than_2_1_is_refused_before_the_worktree(self):
        self.runner.opencode_version = "1.19.3"
        code, _, err = self.dispatch()
        self.assertEqual(code, 1)
        self.assertIn("wf needs opencode 2.1 or later (found 1.19.3)", err)
        self.assertEqual((self.runner.calls, self.runner.runs), ([], []))

    def test_usage_errors_send_nothing(self):
        for argv in (["web#40"], ["web#40", "--engine", "claude"], ["web#40", "--engine", "opencode", "--model", "opus"],
                     ["web#40", "--engine", "opencode", "--message", "go on"], ["40", "--engine", "opencode"]):
            gh = FakeGh()
            self.assertEqual(run(["dispatch", *argv], gh)[0], 2, argv)
            self.assertEqual(gh.calls, [])
        self.assertEqual(self.runner.runs, [])

    def test_another_repos_checkout_is_refused(self):
        self.runner.origin = "git@github.com:acme/core.git"
        code, _, err = self.dispatch()
        self.assertEqual(code, 1)
        self.assertIn("runs from a checkout of acme/web", err)

    def test_a_second_dispatch_needs_resume_and_resume_continues_the_session(self):
        self.dispatch()
        code, _, err = self.dispatch()
        self.assertEqual(code, 1)
        self.assertIn("pass --resume", err)
        code, out, _ = self.dispatch("--resume", "--message", "Ruling: take (a).")
        self.assertEqual(code, 0)
        argv = self.runner.runs[-1]["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], "ses_1")
        self.assertEqual(argv[-1], "Ruling: take (a).")
        self.assertEqual(self.runner.runs[-1]["log"], self.keep / "run-2.jsonl")
        self.assertEqual(sum(1 for c in self.runner.calls if c[1:2] == ("worktree",)), 1)
        self.assertIn("--ktokens 630 --cache-ktokens 10000 --turns 4 (over 2 run(s))", out)

    def test_resume_passes_on_the_checkpoint_note(self):
        from wf.commands import checkpoint
        self.dispatch()
        branch = json.loads((self.keep / "state.json").read_text())["branch"]
        note = checkpoint.note_path(self.runner.common, branch)
        note.parent.mkdir(parents=True)
        note.write_text("".join(f"t{i}  step {i}\n" for i in range(12)))
        self.dispatch("--resume")
        message = self.runner.runs[-1]["argv"][-1]
        self.assertTrue(message.startswith("Continue the issue from where you stopped."))
        self.assertIn("start from it, not from git:\nt2  step 2\n", message)
        self.assertTrue(message.endswith("t11  step 11"))
        self.assertNotIn("step 1\n", message)

    def test_resume_without_an_earlier_run(self):
        code, _, err = self.dispatch("--resume")
        self.assertEqual(code, 1)
        self.assertIn("no earlier run of web#40", err)


class McpWarningTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        patcher = mock.patch.object(dispatch, "RUNNER", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)

    def dispatch(self, body, labels=()):
        gh = FakeGh()
        gh.extra[("GET", ISSUE)] = (200, {"number": 40, "title": "Sprites", "body": body,
                                          "labels": [{"name": x} for x in labels]})
        return run(["dispatch", "web#40", "--engine", "opencode"], gh)

    def test_a_scenario_mcp_issue_without_the_server_warns_and_still_runs(self):
        code, out, _ = self.dispatch("Generate the tiles with the Scenario MCP.")
        self.assertEqual(code, 0)
        self.assertIn("web#40: warning: the issue mentions Scenario MCP but no enabled opencode MCP server is named "
                      "'*scenario*' (configured: none)", out)
        self.assertEqual(len(self.runner.runs), 1)

    def test_a_label_counts_and_unity_mcp_is_checked_too(self):
        _, out, _ = self.dispatch("Place them via MCP for Unity.", labels=["needs:scenario mcp"])
        self.assertIn("mentions Scenario MCP", out)
        self.assertIn("mentions Unity MCP", out)

    def test_a_configured_server_silences_it_and_a_disabled_one_does_not(self):
        self.runner.project_config = {"mcp": {"scenario": {"type": "remote", "url": "https://mcp.scenario.com/mcp"},
                                              "unityMCP": {"type": "local", "command": ["uvx"], "enabled": False}}}
        _, out, _ = self.dispatch("Scenario MCP for art, Unity MCP to place it.")
        self.assertNotIn("mentions Scenario MCP", out)
        self.assertIn("mentions Unity MCP but no enabled opencode MCP server is named '*unity*' (configured: scenario)", out)

    def test_the_native_v2_shape_counts_and_disabled_turns_a_server_off(self):
        self.runner.project_config = {"mcp": {"timeout": {"catalog": 30000}, "servers": {
            "scenario": {"type": "remote", "url": "https://mcp.scenario.com/mcp"},
            "unityMCP": {"type": "local", "command": ["uvx"], "disabled": True}}}}
        _, out, _ = self.dispatch("Scenario MCP for art, Unity MCP to place it.")
        self.assertNotIn("mentions Scenario MCP", out)
        self.assertIn("mentions Unity MCP but no enabled opencode MCP server is named '*unity*' (configured: scenario)", out)

    def test_a_config_above_the_worktree_and_a_dot_opencode_one_count(self):
        # opencode 2 merges every opencode.json from the filesystem root down, then every .opencode/ one
        main = self.runner.common.parent
        (main / "opencode.json").write_text(json.dumps({"mcp": {"servers": {"unity": {"type": "local", "command": ["x"]}}}}))
        _, out, _ = self.dispatch("Unity MCP")
        self.assertNotIn("warning", out)
        tree = main / ".claude" / "worktrees" / "opencode-40"
        (tree / ".opencode").mkdir()
        (tree / ".opencode" / "opencode.jsonc").write_text('{"mcp": {"servers": {"unity": {"disabled": true}}}}')
        self.assertEqual(dispatch.mcp_servers(tree, self.runner.home_dir), [])

    def test_the_global_config_counts(self):
        cfg = self.runner.home_dir / ".config" / "opencode"
        cfg.mkdir(parents=True)
        (cfg / "opencode.jsonc").write_text('{\n  // mine\n  "mcp": {"scenario": {"type": "remote", "url": "x"}}\n}\n')
        _, out, _ = self.dispatch("Scenario MCP")
        self.assertNotIn("warning", out)

    def test_no_mention_no_warning(self):
        _, out, _ = self.dispatch("Plain engine work.")
        self.assertNotIn("warning", out)


class SeatModelTest(unittest.TestCase):
    def test_board_model(self):
        self.assertEqual(board_model("opus"), "opus")
        self.assertEqual(board_model("opencode-opus"), "opencode-opus")
        self.assertEqual(board_model("opencode-anthropic/claude-opus-5-5"), "opencode-opus")
        self.assertEqual(board_model("opencode-openai/gpt-5"), "opencode-gpt-5")
        self.assertEqual(board_model("opencode-anthropic/claude-opus-5-5#high"), "opencode-opus")
        self.assertEqual(board_model("opencode-openai/gpt-5#high"), "opencode-gpt-5")

    def test_merged_records_the_opencode_seat(self):
        gh = FakeGh()
        model = next(f for f in gh.fields if f["name"] == "Model")
        model["options"].append({"id": "77", "name": {"raw": "opencode-opus", "html": "opencode-opus"}})
        code, out, _ = run(["merged", "web#157", "--model", "opencode-anthropic/claude-opus-5-5", "--findings", "1",
                            "--rounds", "0", "--ktokens", "315"], gh)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("web#157: opencode-opus, findings 1"), out)
        sent = next(c for c in gh.writes() if c[1] == "/users/acme/projectsV2/2/items/5000")[2]["fields"]
        self.assertIn({"id": field_id(gh, "Model"), "value": option(gh, "Model", "opencode-opus")}, sent)

    def test_merged_without_the_option_says_how_to_add_it(self):
        gh = FakeGh()
        code, _, err = run(["merged", "web#157", "--model", "opencode-opus", "--findings", "1", "--rounds", "0",
                            "--ktokens", "315"], gh)
        self.assertEqual(code, 1)
        self.assertIn("no option named 'opencode-opus'", err)
        self.assertIn("Add 'opencode-opus' to the Model field", err)
        self.assertEqual(gh.writes(), [])

    def test_report_filters_by_model(self):
        code, out, _ = run(["report", "sprint-3", "--model", "opus"], FakeGh())
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[2:], ["| opus | 1 | L | 3 | 2 | 1250 | 1250 | 140 |"])
        _, out, _ = run(["report", "sprint-3", "--model", "opencode-anthropic/claude-opus-5-5"], FakeGh())
        self.assertEqual(len(out.splitlines()), 2)  # header only: no opencode-opus rows yet


if __name__ == "__main__":
    unittest.main()
