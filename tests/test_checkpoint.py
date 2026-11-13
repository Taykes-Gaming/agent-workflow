import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wf import cli
from wf.commands import checkpoint
from tests.fake_gh import FakeGh

import contextlib
import io


class FakeRunner:
    def __init__(self, branch="claude/7-fix-it"):
        self.common = Path(tempfile.mkdtemp()).resolve() / ".git"
        self.branch = branch

    def git(self, *args):
        if args[-1] == "--git-common-dir":
            return str(self.common)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return self.branch
        raise AssertionError(args)


def wf(runner, *argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(checkpoint, "RUNNER", runner), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = cli.main(["checkpoint", *argv], gh=FakeGh())
    return code, out.getvalue(), err.getvalue()


class CheckpointTest(unittest.TestCase):
    def test_bare_checkpoint_prints_the_last_twenty_lines(self):
        r = FakeRunner()
        for i in range(30):
            wf(r, f"step {i}")
        lines = wf(r)[1].splitlines()
        self.assertEqual(lines[0], "… 10 earlier lines (--all)")
        self.assertEqual((len(lines), lines[1].endswith("step 10")), (21, True))
        first = json.loads(wf(r, "--json")[1].splitlines()[0])
        self.assertEqual(first["more"], 10)
        self.assertEqual(len(wf(r, "--all")[1].splitlines()), 30)

    def test_append_print_clear(self):
        r = FakeRunner()
        self.assertEqual(wf(r)[1], "claude/7-fix-it: no checkpoint\n")
        code, out, _ = wf(r, "phase 1 done,", "next   phase 2")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^\d{4}-\d\d-\d\dT\d\d:\d\dZ  phase 1 done, next phase 2\n$")
        wf(r, "phase 2 done")
        path = r.common / "wf-checkpoint" / "claude-7-fix-it.md"
        self.assertEqual(len(path.read_text().splitlines()), 2)
        code, out, _ = wf(r)
        self.assertEqual(out.splitlines()[1][19:], "phase 2 done")
        self.assertEqual(wf(r, "--clear")[1], "claude/7-fix-it: checkpoint cleared\n")
        self.assertFalse(path.exists())

    def test_needs_no_config(self):
        with mock.patch.dict("os.environ", {"WF_CONFIG": "/nonexistent.toml"}):
            code, _, err = wf(FakeRunner(), "x")
        self.assertEqual((code, err), (0, ""))

    def test_text_with_clear_is_a_usage_error(self):
        code, _, err = wf(FakeRunner(), "x", "--clear")
        self.assertEqual(code, 2)
        self.assertIn("not both", err)

    def test_json(self):
        code, out, _ = wf(FakeRunner(), "--json", "a")
        self.assertEqual(json.loads(out)["branch"], "claude/7-fix-it")

    def test_tail_keeps_the_newest_lines(self):
        r = FakeRunner()
        for i in range(15):
            wf(r, f"s{i}")
        notes = checkpoint.tail(r.common, r.branch)
        self.assertEqual(len(notes), checkpoint.RESUME_LINES)
        self.assertTrue(notes[-1].endswith("s14"))


if __name__ == "__main__":
    unittest.main()
