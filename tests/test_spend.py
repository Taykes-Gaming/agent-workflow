import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_gh import SPEND_FIELDS, FakeGh, field_id, option, run, spend_fields
from tests.test_usage import assistant, main
from wf import ledger
from wf.commands import review

P = "/users/acme/projectsV2/2"
HEAD = "claude/157-fake-head"  # FakeGh's PR head branch


def patch(gh, item, field, value):
    return ("PATCH", f"{P}/items/{item}", {"fields": [{"id": field_id(gh, field), "value": value}]})


def at(line, time):
    line["timestamp"] = time
    return line


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


class Repo(unittest.TestCase):
    """A git checkout on `dev` as the working directory, with its ledger directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "dev", cwd=self.repo)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        self.common = self.repo / ".git"
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def transcript(self, name, lines):
        path = self.tmp / "transcripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(m) + "\n" for m in lines))
        return path

    def lines(self, branch):
        p = ledger.path(self.common, branch)
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def add(self, branch, **entry):
        ledger.append(self.common, branch, entry)


def hook(event):
    with mock.patch("sys.stdin", io.StringIO(event if isinstance(event, str) else json.dumps(event))):
        return main(["hook"])


class HookTest(Repo):
    def test_a_stopped_subagent_is_measured_onto_the_branch_it_worked_on(self):
        tree = self.tmp / "wt"
        git("worktree", "add", "-q", "-b", "claude/157-x", str(tree), cwd=self.repo)
        path = self.transcript("agent-impl.jsonl", [
            {"type": "user", "cwd": str(self.repo), "message": {"content": "Implement web#157"}},
            assistant("m1"), {**assistant("m2"), "cwd": str(tree)}])
        code, out, err = hook({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "impl",
                               "agent_type": "implementer", "agent_transcript_path": str(path), "cwd": str(self.repo)})
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.lines("dev"), [])  # the session's cwd is not where the agent worked
        [line] = self.lines("claude/157-x")
        self.assertRegex(line.pop("at"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(line, {"kind": "agent", "id": "impl", "agent_type": "implementer", "transcript": str(path),
                                "model": "opus", "tokens": 2204, "cache_read": 10000, "turns": 2})

    def test_read_only_agents_are_skipped(self):
        path = self.transcript("agent-look.jsonl", [assistant("m1")])
        code, out, _ = hook({"hook_event_name": "SubagentStop", "agent_id": "look", "agent_type": "Explore",
                             "agent_transcript_path": str(path), "cwd": str(self.repo)})
        self.assertEqual((code, out), (0, ""))
        self.assertFalse((self.common / ledger.DIR).exists())

    def test_a_stopped_session_is_named_on_its_branch(self):
        event = {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": "/t/s1.jsonl", "cwd": str(self.repo)}
        self.assertEqual(hook(event), (0, "", ""))
        hook(event)  # Stop fires every turn; wf spend dedupes
        lines = self.lines("dev")
        self.assertEqual([{k: v for k, v in x.items() if k != "at"} for x in lines],
                         [{"kind": "session", "id": "s1", "transcript": "/t/s1.jsonl", "cwd": str(self.repo)}] * 2)

    def test_failures_are_swallowed(self):
        outside = Path(tempfile.mkdtemp())
        for event in ("not json", {"hook_event_name": "SubagentStop", "agent_id": "a", "agent_type": "implementer",
                                   "agent_transcript_path": str(self.tmp / "missing.jsonl"), "cwd": str(self.repo)},
                      {"hook_event_name": "Stop", "session_id": "s", "cwd": str(outside)},
                      {"hook_event_name": "PreToolUse", "cwd": str(self.repo)}):
            self.assertEqual(hook(event), (0, "", ""), event)
        self.assertFalse((self.common / ledger.DIR).exists())


class MergedLedgerTest(Repo):
    ARGS = ["merged", "web#157", "--findings", "3", "--rounds", "1"]

    def agent(self, aid, tokens, cache, turns, model="opus"):
        self.add(HEAD, kind="agent", id=aid, agent_type="implementer", transcript=f"/t/{aid}.jsonl", model=model,
                 tokens=tokens, cache_read=cache, turns=turns)

    def review(self, variant, tokens, pr=40):
        self.add(HEAD, kind="review", pr=pr, variant=variant, tokens=tokens, cache_read=1000, cost_usd=1.5, turns=9)

    def test_sums_the_head_branchs_agents_last_line_winning_and_its_reviews_apart(self):
        self.agent("impl", 2000, 10_000, 2)
        self.review("builtin", 100_400)
        self.agent("impl", 3000, 12_000, 3)  # the fix round stopped the same agent again
        self.review("lean-fix", 20_000)  # failed runs spent too
        self.review("builtin", 900_000, pr=41)  # another PR from the same branch
        gh = spend_fields(FakeGh())
        code, out, err = run(self.ARGS, gh, deploy=False, fields=SPEND_FIELDS)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "web#157: opus, findings 3, rounds 1, kTokens 3, cache kTokens 12, turns 3, "
                              "review kTokens 120 over 2 run(s) (web#40) [agents impl; reviews builtin, lean-fix] "
                              "→ Done, closed\n")
        self.assertEqual(gh.writes()[0], ("PATCH", f"{P}/items/5000", {"fields": [
            {"id": field_id(gh, n), "value": v} for n, v in (
                ("Model", option(gh, "Model", "opus")), ("Review Findings", 3), ("Fix Rounds", 1),
                ("Agent kTokens", 3), ("Cache kTokens", 12), ("Turns", 3), ("Review kTokens", 120),
                ("Status", option(gh, "Status", "Done")))]}))

    def test_hand_flags_win(self):
        self.agent("impl", 2000, 10_000, 2)
        gh = spend_fields(FakeGh())
        code, out, _ = run(self.ARGS + ["--model", "sonnet", "--ktokens", "9", "--turns", "4"], gh, deploy=False,
                           fields=SPEND_FIELDS)
        self.assertEqual(code, 0)
        self.assertEqual(out, "web#157: sonnet, findings 3, rounds 1, kTokens 9, cache kTokens 10, turns 4, "
                              "no wf review run in the ledger (web#40) → Done, closed\n")

    def test_two_models_need_model(self):
        self.agent("a1", 1000, 0, 1)
        self.agent("a2", 1000, 0, 1, model="sonnet")
        gh = FakeGh()
        code, _, err = run(self.ARGS, gh)
        self.assertEqual((code, err), (1, "wf: the agents ran on opus/sonnet; pass --model\n"))
        self.assertEqual(gh.writes(), [])
        code, out, _ = run(self.ARGS + ["--model", "opus"], gh, deploy=False)
        self.assertEqual(code, 0)
        self.assertIn("kTokens 2, turns 2 (web#40) [agents a1, a2]", out)

    def test_without_a_ledger_it_refuses_before_any_write(self):
        gh = FakeGh()
        code, _, err = run(self.ARGS, gh)
        self.assertEqual(code, 1)
        self.assertIn(f"the ledger has no agent for {HEAD} ", err)
        self.assertIn("Pass --model and --ktokens by hand", err)
        self.assertEqual(gh.writes(), [])
        code, _, err = run(self.ARGS + ["--ktokens", "5"], gh)
        self.assertEqual((code, err), (1, "wf: --ktokens by hand needs --model: the ledger has no agent to take it "
                                          "from\n"))
        self.assertEqual(gh.writes(), [])
        # By hand: Cache kTokens is dropped without the field; the review spend is not looked for.
        code, out, _ = run(self.ARGS + ["--ktokens", "5", "--model", "opus", "--cache-ktokens", "1"], gh)
        self.assertEqual(code, 0)
        self.assertNotIn("cache", out)
        self.assertNotIn("review", out)

    def test_the_opencode_seat_is_read_from_its_dispatch_line(self):
        self.agent("ses_1-run-1", 315_000, 5_000_000, 2, model="opencode-sonnet")
        gh = FakeGh()
        model = next(f for f in gh.fields if f["name"] == "Model")
        model["options"].append({"id": "77", "name": {"raw": "opencode-sonnet", "html": "opencode-sonnet"}})
        code, out, _ = run(self.ARGS, gh, deploy=False)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("web#157: opencode-sonnet, findings 3, rounds 1, kTokens 315, turns 2"), out)


class SharedNamesTest(unittest.TestCase):
    def test_ledger_and_checkpoint_share_branch_file_names(self):
        from wf.commands import checkpoint
        from wf.gh import branch_file
        self.assertEqual(branch_file("claude/1-x"), "claude-1-x")
        self.assertEqual(ledger.path("/c", "claude/1-x").name, "claude-1-x.jsonl")
        self.assertEqual(checkpoint.note_path("/c", "claude/1-x").name, "claude-1-x.md")


class SpendTest(Repo):
    RECORD = {"id": 8050, "number": 50, "title": "Sprint 3 (record)", "state": "open",
              "created_at": "2026-10-20T09:00:00Z", "closed_at": None,
              "labels": [{"name": "sprint-record"}, {"name": "sprint-3"}]}

    def setUp(self):
        super().setUp()
        s1 = self.transcript("s1.jsonl", [
            at(assistant("old", out=900_000), "2026-10-19T23:59:59Z"),
            at(assistant("m1"), "2026-10-20T09:00:00.500Z"),
            {"type": "user", "message": {"content": "a question"}, "timestamp": "2026-10-20T10:00:00Z"},
            at(assistant("m2"), "2026-10-20T10:00:05Z")])
        # A fork repeats its parent's history: m2 is counted once.
        s2 = self.transcript("s2.jsonl", [at(assistant("m2"), "2026-10-20T10:00:05Z"),
                                          at(assistant("m3"), "2026-10-21T08:00:00Z")])
        issue = self.transcript("s3.jsonl", [at(assistant("x1", out=900_000), "2026-10-20T11:00:00Z")])
        for sid, path in (("s1", s1), ("s2", s2), ("s1", s1)):  # Stop fires every turn
            self.add("dev", kind="session", id=sid, transcript=str(path), cwd=str(self.repo))
        self.add("claude/157-x", kind="session", id="s3", transcript=str(issue), cwd=str(self.tmp / "wt"))
        self.add("dev", kind="agent", id="impl", model="opus", tokens=900_000, cache_read=0, turns=1)
        self.projects = self.tmp / "projects"
        self.project = self.projects / re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
        self.project.mkdir(parents=True)
        for path in (s1, s2):
            (self.project / path.name).write_text(path.read_text())

    def test_records_the_base_branchs_sessions_in_the_window_on_the_sprint_record(self):
        gh = spend_fields(FakeGh())
        gh.extra[("GET", "/repos/acme/web/issues/50")] = (200, self.RECORD)
        code, out, err = run(["spend", "web#50"], gh, fields=SPEND_FIELDS)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "web#50: orchestrator opus, 3 turns, 3k tokens (in 0k, out 0k, cache write 3k), "
                              "cache read 15k, 2 session(s) on dev, 2026-10-20 09:00–now UTC\n")
        self.assertEqual(gh.writes()[1:], [("PATCH", f"{P}/items/9901", {"fields": [
            {"id": field_id(gh, n), "value": v} for n, v in (("Agent kTokens", 3), ("Cache kTokens", 15), ("Turns", 3))]})])

    def test_spend_writes_one_patch(self):
        gh = spend_fields(FakeGh())
        gh.extra[("GET", "/repos/acme/web/issues/50")] = (200, self.RECORD)
        code, _, _ = run(["spend", "web#50"], gh, fields=SPEND_FIELDS)
        self.assertEqual(code, 0)
        self.assertEqual(len([c for c in gh.writes() if c[0] == "PATCH"]), 1)

    def test_closed_record_ends_the_window_and_since_moves_its_start(self):
        gh = FakeGh()
        gh.extra[("GET", "/repos/acme/web/issues/50")] = (
            200, {**self.RECORD, "state": "closed", "closed_at": "2026-10-21T00:00:00Z"})
        code, out, _ = run(["spend", "web#50", "--since", "2026-10-20T09:30:00Z"], gh)
        self.assertEqual(code, 0)
        self.assertIn("orchestrator opus, 1 turns", out)
        self.assertIn("2026-10-20 09:30–2026-10-21 00:00 UTC", out)

    def test_the_base_branch_follows_the_config(self):
        gh = FakeGh()
        gh.extra[("GET", "/repos/acme/web/issues/50")] = (200, self.RECORD)
        code, _, err = run(["spend", "web#50"], gh, opencode='base = "main"')
        self.assertEqual(code, 1)
        self.assertIn("no session on main in the ledger", err)
        self.assertEqual(gh.calls, [])  # refused before any call

    def test_refuses_an_issue_that_is_not_a_record(self):
        gh = FakeGh()
        code, _, err = run(["spend", "web#157"], gh)
        self.assertEqual((code, err), (1, "wf: web#157 has no 'sprint-record' label; "
                                          "wf spend records onto the sprint record only\n"))
        self.assertEqual(gh.writes(), [])

    def test_usage_sessions_is_the_no_ledger_fallback(self):
        code, out, err = main(["usage", "--sessions", "--since", "2026-10-20T09:00:00Z", "--projects",
                               str(self.projects)])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(f"sessions in {self.project.name}: opus, 3 turns"), out)
        self.assertEqual(main(["usage", "a1", "--sessions"])[0], 2)
        self.assertEqual(main(["usage", "a1", "--since", "2026-10-20"])[0], 2)


class ReportRowsTest(unittest.TestCase):
    def test_orchestrator_row_and_spend_columns(self):
        gh = spend_fields(FakeGh())
        record = json.loads(json.dumps(gh.items[1]))
        record["id"], record["content"]["number"] = 5099, 50
        record["content"]["labels"] = [{"name": "sprint-record"}, {"name": "sprint-3"}]
        record["fields"] = [f for f in record["fields"] if f["name"] in ("Agent kTokens", "Turns")]
        record["fields"].append({"id": 1030, "name": "Cache kTokens", "data_type": "number", "value": 40000})
        gh.items.append(record)
        sonnet = next(i for i in gh.items if i["id"] == 5001)
        sonnet["fields"].append({"id": 1031, "name": "Review kTokens", "data_type": "number", "value": 90})
        code, out, _ = run(["report", "sprint-3"], gh, fields=SPEND_FIELDS)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "| Model | Slices | Size mix | Review findings | Fix rounds | Agent kTokens | "
                                   "kTokens / slice | Turns | Cache kTokens | Review kTokens |")
        self.assertEqual(lines[1], "|---|---|---|---|---|---|---|---|---|---|")
        self.assertIn("| sonnet | 1 | S | 2 | 1 | 410 | 410 | 55 | 0 | 90 |", lines)
        self.assertEqual(lines[-1], "| orchestrator | - | - | - | - | 410 | - | 55 | 40000 | 0 |")
        code, out, _ = run(["report", "sprint-3", "--model", "sonnet"], gh, fields=SPEND_FIELDS)
        self.assertNotIn("orchestrator", out)


class ReviewLedgerTest(unittest.TestCase):
    def test_every_kept_run_goes_on_the_head_branchs_ledger(self):
        common = Path(tempfile.mkdtemp())
        keep = common / "wf-review"
        result = {"num_turns": 9, "total_cost_usd": 2.0, "modelUsage": {"claude-opus-5": {
            "inputTokens": 50, "outputTokens": 950, "cacheCreationInputTokens": 9000, "cacheReadInputTokens": 500}}}
        review._keep(keep, 12, "builtin", result, "claude/11-x")
        review._keep(keep, 12, "lean-fix", {**result, "is_error": True}, "claude/11-x")
        review._keep(keep, 12, "builtin", result)  # no head branch known: kept, not in a ledger
        lines = ledger.read(common, "claude/11-x")
        self.assertEqual([{k: v for k, v in x.items() if k != "at"} for x in lines],
                         [{"kind": "review", "pr": 12, "variant": v, "tokens": 10000, "cache_read": 500,
                           "cost_usd": 2.0, "turns": 9} for v in ("builtin", "lean-fix")])
        self.assertEqual(sorted(p.name for p in (common / ledger.DIR).iterdir()), ["claude-11-x.jsonl"])
        self.assertFalse(list(keep.glob("*spend*")))


if __name__ == "__main__":
    unittest.main()
