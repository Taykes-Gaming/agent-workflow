import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wf import cli
from wf.commands import wait
from tests.fake_gh import FakeGh, run


def main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class WaitTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        patcher = mock.patch.object(wait, "POLL", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_duration(self):
        self.assertEqual([wait.duration(x) for x in ("90", "90s", "20m", "1h30m", "1h")], [90, 90, 1200, 5400, 3600])
        with self.assertRaises(cli.UsageError):
            wait.duration("soon")

    def test_file_match_prints_context_and_the_match_only(self):
        log = self.root / "unity.log"
        log.write_text("noise 1\nnoise 2\nnoise 3\nImporting assets\nExiting batchmode successfully\nmore\n")
        code, out, err = main(["wait", "--file", str(log), "--until", "Exiting batchmode", "--context", "1"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[:2], ["Importing assets", "Exiting batchmode successfully"])
        self.assertTrue(out.splitlines()[2].startswith("wait: matched after"))
        self.assertNotIn("noise", out)

    def test_file_appearing_later_is_followed(self):
        log = self.root / "later.log"

        def writer():
            time.sleep(0.05)
            log.write_text("starting\n")
            time.sleep(0.05)
            with open(log, "a") as f:
                f.write("Listening on :8080\n")
        threading.Thread(target=writer).start()
        code, out, err = main(["wait", "--file", str(log), "--until", "Listening on", "--timeout", "5s", "--json"])
        self.assertEqual((code, err), (0, ""))
        records = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(records[-1]["wait"], "ok")
        self.assertEqual(records[-1]["match"], "Listening on :8080")

    def test_follower_survives_truncation_and_split_utf8(self):
        log = self.root / "t.log"
        log.write_bytes("first line\nsecond ré".encode()[:-1])  # a multibyte char cut in two
        f = wait.Follower(log)
        self.assertEqual(f.lines(), ["first line"])
        with open(log, "ab") as out:
            out.write("é".encode()[1:] + b"sum\n")
        self.assertEqual(f.lines(), ["second résum"])
        log.write_bytes(b"rotated\nREADY\n")  # shorter than what was read: start over
        self.assertEqual(f.lines(), ["rotated", "READY"])

    def test_default_fail_words_end_the_wait_with_exit_1(self):
        log = self.root / "f.log"
        log.write_text("compiling\n0 errors so far\nerror CS1002: ; expected\n")
        code, out, err = main(["wait", "--file", str(log), "--until", "Exiting batchmode"])
        self.assertEqual((code, err), (1, ""))
        self.assertIn("wait: FAIL", out)
        self.assertIn("error CS1002", out)
        self.assertNotIn("0 errors so far\nwait", out)  # the "0 errors" line is not a failure

    def test_explicit_fail_regex_replaces_the_default(self):
        log = self.root / "f.log"
        log.write_text("error: harmless\nJOB FAILED hard\n")
        code, out, _ = main(["wait", "--file", str(log), "--until", "DONE", "--fail", "JOB FAILED"])
        self.assertEqual(code, 1)
        self.assertIn("JOB FAILED hard", out)

    def test_timeout_exits_124(self):
        log = self.root / "slow.log"
        log.write_text("step one\nstill going\n")
        code, out, err = main(["wait", "--file", str(log), "--until", "never", "--timeout", "0s"])
        self.assertEqual((code, err), (124, ""))
        self.assertIn("wait: timeout after", out)
        self.assertIn("still going", out)

    def test_run_logs_under_git_and_matches_while_the_command_runs(self):
        code, out, err = main(["wait", "--run", "echo booting; echo READY; sleep 2", "--until", "READY"])
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("wait: started pid"))
        self.assertIn("READY", lines)
        log = Path(lines[-1].split("(log ")[1].rstrip(")"))
        self.assertEqual(log.parent, self.root / ".git" / "wf-wait")
        self.assertIn("booting", log.read_text())
        record = json.loads((log.parent / "1.json").read_text())
        self.assertTrue(wait._alive(record["pid"]))  # a match does not stop the command
        os.kill(record["pid"], 15)

    def test_run_exiting_without_a_match_reports_its_status_and_first_failure(self):
        code, out, _ = main(["wait", "--run", "echo fine; echo Build FAILED: missing ref; exit 3", "--until", "READY",
                             "--fail", "NEVER"])
        self.assertEqual(code, 1)
        self.assertIn("command exited 3", out)
        self.assertIn("Build FAILED: missing ref", out)

    def test_resume_reattaches_to_the_last_run_and_finds_an_earlier_match(self):
        main(["wait", "--run", "echo READY; sleep 2", "--until", "READY"])
        code, out, err = main(["wait", "--resume", "--until", "READY"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("wait: re-attached to run 1", out)
        self.assertIn("READY", out.splitlines())
        self.assertIn("wait: matched", out)
        record = json.loads((self.root / ".git" / "wf-wait" / "1.json").read_text())
        os.kill(record["pid"], 15)

    def test_resume_without_a_run_is_an_error(self):
        code, out, err = main(["wait", "--resume", "--until", "x"])
        self.assertEqual(code, 1)
        self.assertIn("no earlier wf wait --run", err)

    def test_needs_exactly_one_target_and_an_until(self):
        code, _, err = main(["wait", "--until", "x"])
        self.assertEqual(code, 2)
        self.assertIn("exactly one of --file, --run or --resume", err)
        code, _, err = main(["wait", "--file", "a", "--run", "b", "--until", "x"])
        self.assertEqual(code, 2)
        code, _, err = main(["wait", "--file", "a"])
        self.assertEqual((code, "needs --until" in err), (2, True))
        code, _, err = main(["wait", "--file", "a", "--until", "("])
        self.assertEqual((code, "--until is not a regex" in err), (2, True))

    def test_preset_supplies_the_flags_and_the_command_line_wins(self):
        log = self.root / "u.log"
        log.write_text("Scripts have compiler errors\n")
        cfg = ('[wait.unity]\nuntil = "Exiting batchmode successfully"\n'
               'fail = "Scripts have compiler errors|error CS\\\\d+"\ntimeout = "30m"\ncontext = 0\n'
               f'file = "{log}"\n')
        code, out, err = run(["wait", "unity"], FakeGh(), gate=cfg)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out.splitlines()[0], "Scripts have compiler errors")
        log.write_text("whatever\nExiting batchmode successfully\n")
        code, out, err = run(["wait", "unity", "--fail", "NOPE"], FakeGh(), gate=cfg)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "Exiting batchmode successfully")

    def test_unknown_preset_lists_the_known_ones(self):
        cfg = '[wait.unity]\nuntil = "x"\n'
        code, _, err = run(["wait", "scenario", "--file", "a"], FakeGh(), gate=cfg)
        self.assertEqual(code, 2)
        self.assertIn("no [wait.scenario]", err)
        self.assertIn("presets: unity", err)

    def test_config_rejects_bad_presets(self):
        for cfg, message in [('[wait.u]\nfail = "x"\n', "needs until"),
                             ('[wait.u]\nuntil = "("\n', "not a regex"),
                             ('[wait.u]\nuntil = "x"\nrun = "a"\nfile = "b"\n', "both run and file"),
                             ('[wait.u]\nuntil = "x"\ncontext = -1\n', "context must be"),
                             ('[wait.u]\nuntil = "x"\nbogus = 1\n', "unknown [wait.u] keys: bogus"),
                             ('[wait]\nuntil = "x"\n', "must hold [wait.<name>] tables")]:
            code, _, err = run(["wait", "u", "--file", "a"], FakeGh(), gate=cfg)
            self.assertEqual(code, 1, cfg)
            self.assertIn(message, err)
