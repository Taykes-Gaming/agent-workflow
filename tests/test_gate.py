import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wf.commands import gate
from tests.fake_gh import FakeGh, run

NUNIT = """<test-run><test-suite>
<test-case fullname="A.Passes" result="Passed"/>
<test-case fullname="A.Breaks" result="Failed"><failure><message>Expected 3
but was 4</message></failure></test-case>
<test-case fullname="A.Later" result="Skipped"/>
</test-suite></test-run>"""


class FakeRunner:
    """git answers and a shell whose commands are scripted: command → (exit code, output, files to write)."""

    def __init__(self, script=None, changed="", status=""):
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / ".git").mkdir()
        self.script, self.changed, self.status, self.ran = script or {}, changed, status, []

    def git(self, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return f"{self.root}\n"
        if args == ("rev-parse", "--absolute-git-dir"):
            return f"{self.root / '.git'}\n"
        if args[0] == "diff":
            self.diff_base = args[-1]
            return self.changed
        if args[0] == "status":
            return self.status
        raise AssertionError(args)

    def sh(self, command, cwd, log):
        self.ran.append(command)
        code, out, files = self.script.get(command, (0, "", {}))
        for name, text in files.items():
            (Path(cwd) / name).write_text(text)
        Path(log).write_text(out)
        return code


def wf(argv, runner, cfg):
    with mock.patch.object(gate, "RUNNER", runner):
        return run(["gate", *argv], FakeGh(), gate=cfg)


class GateTest(unittest.TestCase):
    CFG = '[gate]\nsteps = ["lint", "test", "validate"]'

    def test_green_prints_one_line_per_step_and_writes_logs(self):
        r = FakeRunner({"test": (0, "Ran 9 tests\nOK\n", {})})
        code, out, err = wf([], r, self.CFG)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([x.split()[0] for x in lines], ["ok", "ok", "ok", "gate:"])
        self.assertTrue(lines[-1].startswith("gate: green (3 run"))
        self.assertEqual((r.root / ".git" / "wf-gate" / "2-test.log").read_text(), "Ran 9 tests\nOK\n")

    def test_stops_at_first_failure_with_its_first_failure_line(self):
        out_text = "Build succeeded. 0 Error(s)\nTest A ... ok\nFAILED: test_b (AssertionError)\nmore\n"
        r = FakeRunner({"test": (1, out_text, {})})
        code, out, _ = wf([], r, self.CFG)
        self.assertEqual(code, 1)
        self.assertEqual(r.ran, ["lint", "test"])
        lines = out.splitlines()
        self.assertIn("FAIL  test", lines[1])
        self.assertIn("exit 1: FAILED: test_b (AssertionError)", lines[1])
        self.assertIn("2-test.log", lines[1])
        self.assertEqual(lines[2], "--    validate: not run")
        self.assertEqual(lines[3], "gate: 1 failed (test)")

    def test_keep_going_runs_every_step(self):
        r = FakeRunner({"lint": (2, "", {}), "test": (1, "boom\n", {})})
        code, out, _ = wf(["--keep-going"], r, self.CFG)
        self.assertEqual((code, r.ran), (1, ["lint", "test", "validate"]))
        self.assertIn("exit 2: no output", out)
        self.assertIn("gate: 2 failed (lint, test)", out)

    def test_when_steps_skip_unless_a_changed_file_matches(self):
        cfg = '[gate]\nsteps = ["test"]\nbase = "main"\n[gate.when]\n"unity/*" = ["unity"]'
        r = FakeRunner(changed="src/a.cs\n")
        code, out, _ = wf([], r, cfg)
        self.assertEqual((code, r.ran, r.diff_base), (0, ["test"], "origin/main...HEAD"))
        self.assertIn("skip  unity: nothing changed under unity/*", out)
        r = FakeRunner(status=" M unity/Assets/X.cs\n")
        wf([], r, cfg)
        self.assertEqual(r.ran, ["test", "unity"])
        r = FakeRunner()
        wf(["--all"], r, cfg)
        self.assertEqual(r.ran, ["test", "unity"])

    def test_nunit_results_decide_and_summarise_the_step(self):
        cfg = '[gate]\nsteps = [{ run = "unity", name = "EditMode", xml = "r.xml" }]'
        r = FakeRunner({"unity": (0, "", {"r.xml": NUNIT})})
        code, out, _ = wf([], r, cfg)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  EditMode", out)
        self.assertIn("1 passed, 1 failed, 1 skipped: A.Breaks: Expected 3", out)

    def test_a_nonzero_exit_with_clean_results_shows_the_log_cause(self):
        cfg = '[gate]\nsteps = [{ run = "unity", xml = "r.xml" }]'
        r = FakeRunner({"unity": (3, "Licence check failed\n", {"r.xml": NUNIT.replace("Failed", "Passed")})})
        code, out, _ = wf([], r, cfg)
        self.assertEqual(code, 1)
        self.assertIn("2 passed, 0 failed, 1 skipped; exit 3: Licence check failed", out)

    def test_a_missing_results_file_fails_even_on_exit_0(self):
        cfg = '[gate]\nsteps = [{ run = "unity", xml = "r.xml" }]'
        r = FakeRunner()
        (r.root / "r.xml").write_text(NUNIT.replace("Failed", "Passed"))  # stale: removed before the step
        code, out, _ = wf([], r, cfg)
        self.assertEqual(code, 1)
        self.assertIn("no readable results at r.xml", out)

    def test_json_records(self):
        r = FakeRunner()
        code, out, _ = wf(["--json"], r, '[gate]\nsteps = ["test"]')
        records = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[-1]["gate"], "green")

    def test_without_a_gate_section(self):
        code, _, err = wf([], FakeRunner(), None)
        self.assertEqual(code, 1)
        self.assertIn("needs a [gate] section", err)


class WatchRunner(FakeRunner):
    """A FakeRunner whose clock is a list of scripted ticks: each sleep applies the next tick, a dict of
    (file → text to write) or a script to swap in; the list running out is the user's Ctrl-C."""

    def __init__(self, ticks, script=None):
        super().__init__(script)
        self.ticks, self.sleeps = list(ticks), 0

    def files(self, top):
        return sorted(str(f.relative_to(top)) for f in top.rglob("*") if f.is_file() and ".git" not in f.parts)

    def sleep(self, seconds):
        self.sleeps += 1
        if not self.ticks:
            raise KeyboardInterrupt
        tick = self.ticks.pop(0)
        if callable(tick):
            tick(self)
        for name, text in tick.items() if isinstance(tick, dict) else ():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            stamp = (self.sleeps + 1) * 10 ** 9
            import os
            os.utime(path, ns=(stamp, stamp))


class GateWatchTest(unittest.TestCase):
    CFG = '[gate]\nsteps = ["lint", "test"]'

    def test_only_failures_prints_fail_lines_and_the_summary(self):
        r = FakeRunner({"test": (1, "boom\n", {})})
        code, out, _ = wf(["--only-failures", "--keep-going"], r, '[gate]\nsteps = ["lint", "test", "check"]')
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual([x.split()[0] for x in lines], ["FAIL", "gate:"])
        code, out, _ = wf(["--only-failures"], FakeRunner(), self.CFG)
        self.assertEqual((code, out.splitlines()[0][:12]), (0, "gate: green "))

    def test_watch_reruns_on_change_and_prints_only_flipped_steps(self):
        def break_lint(r):
            r.script["lint"] = (1, "lint error: bad\n", {})

        def fix_lint(r):
            r.script["lint"] = (0, "", {})

        # tick 1: nothing changed (no rerun). tick 2: a file changes; lint will fail. tick 3: quiet → rerun.
        # tick 4: another change with lint fixed; tick 5 quiet → rerun. then Ctrl-C.
        ticks = [{}, {"src/a.py": "x"}, break_lint, {"src/a.py": "y"}, fix_lint]
        r = WatchRunner(ticks)
        (r.root / "src").mkdir()
        (r.root / "src" / "a.py").write_text("")
        code, out, err = wf(["--watch"], r, self.CFG)
        self.assertEqual(err, "")
        lines = out.splitlines()
        self.assertEqual([x.split()[0] for x in lines[:3]], ["ok", "ok", "gate:"])
        self.assertIn("FAIL  lint", lines[3])
        self.assertEqual(lines[4], "--    test: not run")
        self.assertEqual(lines[5], "gate: 1 failed (lint flipped to FAIL, test flipped to not run)")
        self.assertTrue(lines[6].startswith("ok    lint"), lines[6])
        self.assertTrue(lines[7].startswith("ok    test"), lines[7])
        self.assertEqual(lines[8], "gate: green (lint flipped to ok, test flipped to ok)")
        self.assertEqual(len(lines), 9)
        self.assertEqual(code, 0)
        self.assertEqual(r.ran, ["lint", "test", "lint", "lint", "test"])

    def test_watch_exits_with_the_last_runs_status_and_says_unchanged(self):
        r = WatchRunner([{"b.txt": "1"}, {}], {"test": (1, "boom\n", {})})
        code, out, _ = wf(["--watch", "*.txt"], r, self.CFG)
        lines = out.splitlines()
        self.assertEqual(lines[-1], "gate: 1 failed (unchanged)")
        self.assertEqual((code, len(lines)), (1, 4))  # lint, FAIL test, summary; then one rerun summary

    def test_watch_globs_limit_what_is_watched(self):
        r = WatchRunner([{"notes.md": "1"}, {}])
        code, out, _ = wf(["--watch", "*.py"], r, self.CFG)
        self.assertEqual(len(out.splitlines()), 3)  # the first run only: notes.md is not watched
        self.assertEqual(r.ran, ["lint", "test"])

    def test_watch_does_not_rerun_on_the_steps_own_output(self):
        # `test` writes results.xml into the tree on every run; after the rerun that write must not count as a change
        class Writes(WatchRunner):
            def sh(self, command, cwd, log):
                code = super().sh(command, cwd, log)
                if command == "test":
                    self.sleeps += 1
                    stamp = (self.sleeps + 100) * 10 ** 9
                    os.utime(Path(cwd) / "results.xml", ns=(stamp, stamp))
                return code
        r = Writes([{"src/a.py": "x"}, {}, {}, {}, {}], {"test": (0, "", {"results.xml": "<r/>"})})
        (r.root / "src").mkdir()
        code, out, _ = wf(["--watch"], r, self.CFG)
        self.assertEqual(r.ran, ["lint", "test", "lint", "test"])  # the first run, one rerun, then quiet
        self.assertEqual(out.splitlines()[-1], "gate: green (unchanged)")

    def test_watch_keeps_the_last_status_until_a_rerun_finishes(self):
        # Ctrl-C arrives inside the rerun: the exit status is the last finished run's (1), not a reset 0
        class Interrupts(WatchRunner):
            def sh(self, command, cwd, log):
                if command == "lint" and self.ran:
                    raise KeyboardInterrupt
                return super().sh(command, cwd, log)
        r = Interrupts([{"a.py": "1"}, {}], {"test": (1, "boom\n", {})})
        code, out, _ = wf(["--watch", "--keep-going"], r, self.CFG)
        self.assertEqual(code, 1)
        self.assertEqual(r.ran, ["lint", "test"])

    def test_flips_match_steps_by_position_not_name(self):
        before = [("", {"step": "check", "status": "ok"}), ("", {"step": "check", "status": "fail", "detail": "x"})]
        after = [("", {"step": "check", "status": "ok"}), ("", {"step": "check", "status": "ok"})]
        self.assertEqual([r["status"] for _, r in gate._flips(before, after)], ["ok"])

    def test_snapshot_ignores_git_and_the_log_dir(self):
        r = WatchRunner([])
        (r.root / "x.py").write_text("")
        (r.root / ".git" / "wf-gate").mkdir()
        (r.root / ".git" / "wf-gate" / "1-lint.log").write_text("")
        self.assertEqual(list(gate.snapshot(r, r.root, [], r.root / ".git" / "wf-gate")), ["x.py"])


class GateConfigTest(unittest.TestCase):
    def check(self, cfg, message):
        code, _, err = wf([], FakeRunner(), cfg)
        self.assertEqual(code, 1)
        self.assertIn(message, err)

    def test_rejects_bad_sections(self):
        self.check('[gate]\nsteps = ["a"]\nfast = true', "unknown [gate] keys: fast")
        self.check('[gate]\nsteps = "a"', "[gate].steps must be a list")
        self.check('[gate]\nsteps = [{ name = "x" }]', "[gate].steps[0] needs run")
        self.check('[gate]\nsteps = [{ run = "a", log = "x" }]', "unknown keys in [gate].steps[0]: log")
        self.check('[gate]\nsteps = [3]', "must be a command or a table")
        self.check('[gate]\nsteps = []', "[gate] has no steps")
        self.check('[gate]\nsteps = ["a"]\nbase = 1', "[gate].base must be a string")


class FirstFailureTest(unittest.TestCase):
    def test_skips_zero_counts_and_falls_back_to_the_last_line(self):
        self.assertEqual(gate.first_failure("0 errors\nFailed: 0\nall good\n"), "all good")
        self.assertEqual(gate.first_failure("x\nerror CS1002: ; expected\n"), "error CS1002: ; expected")


if __name__ == "__main__":
    unittest.main()
