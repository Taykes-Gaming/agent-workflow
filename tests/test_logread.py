import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from wf import cli
from wf.commands import logread

UNITY = """Initialize engine version: 6000.0.30f1
[Licensing::Client] Handshaking with LicensingClient
[Package Manager] Done resolving packages
Compiling shader "Hidden/Internal-Colored" pass "" (vp)
Refreshing native plugins compatibility
Building project
warning CS0168: unused variable
warning CS0168: unused variable
warning CS0168: unused variable
NullReferenceException: Object reference not set to an instance of an object
  at Lap.Table.Render () [0x0001a] in /proj/Assets/Lap/Table.cs:42
  at Lap.Table.Update () [0x00005] in /proj/Assets/Lap/Table.cs:20
UnityEngine.Debug:LogException(Exception)
(Filename: Assets/Lap/Table.cs Line: 42)
still running
NullReferenceException: Object reference not set to an instance of an object
  at Lap.Table.Render () [0x0001a] in /proj/Assets/Lap/Table.cs:42
  at Lap.Table.Update () [0x00005] in /proj/Assets/Lap/Table.cs:20
Exiting batchmode successfully now!
"""

NUNIT = """<test-run><test-suite>
<test-case fullname="A.Passes" result="Passed"/>
<test-case fullname="A.Breaks" result="Failed"><failure><message>Expected 3
but was 4</message><stack-trace>at A.Breaks () [0x0] in /p/A.cs:12
at NUnit.Run()</stack-trace></failure></test-case>
<test-case fullname="A.Later" result="Skipped"/>
</test-suite></test-run>"""

JUNIT = """<testsuites><testsuite name="s"><testcase classname="pkg.T" name="ok"/>
<testcase classname="pkg.T" name="bad"><failure message="assert 1 == 2">Traceback
  File "/p/test_t.py", line 9, in bad
AssertionError</failure></testcase>
<testcase classname="pkg.T" name="skip"><skipped/></testcase></testsuite></testsuites>"""


def main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class LogreadTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, name, text):
        p = self.dir / name
        p.write_text(text)
        return str(p)

    def test_drops_noise_collapses_runs_and_repeated_traces(self):
        code, out, err = main(["logread", self.write("u.log", UNITY)])
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[:4], ["Building project", "warning CS0168: unused variable  (×3)",
                                     "NullReferenceException: Object reference not set to an instance of an object",
                                     "  at Lap.Table.Render () [0x0001a] in /proj/Assets/Lap/Table.cs:42"])
        self.assertIn("  (trace shown above)", lines)
        self.assertEqual(lines.count("  at Lap.Table.Render () [0x0001a] in /proj/Assets/Lap/Table.cs:42"), 1)
        for noise in ("Licensing", "Package Manager", "Compiling shader", "Debug:LogException", "(Filename:"):
            self.assertNotIn(noise, out)
        self.assertRegex(lines[-1], r"^logread: 9 of 19 lines shown \(7 noise, 4 collapsed\); .*u\.log, \d+ B$")

    def test_match_with_context_and_exclude(self):
        path = self.write("u.log", UNITY)
        code, out, _ = main(["logread", path, "--match", "Exiting batchmode", "--context", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:-1], ["  (trace shown above)", "Exiting batchmode successfully now!"])
        self.assertIn("1 matched", out)
        code, out, _ = main(["logread", path, "--exclude", "warning CS"])
        self.assertNotIn("CS0168", out)
        self.assertIn("8 noise", out)

    def test_last_caps_and_says_so(self):
        path = self.write("big.log", "\n".join(f"line {i}" for i in range(500)) + "\n")
        code, out, _ = main(["logread", path, "--last", "3"])
        lines = out.splitlines()
        self.assertEqual(lines[0], "… 497 earlier lines (--last 3; raise it or --match to see them)")
        self.assertEqual(lines[1:4], ["line 497", "line 498", "line 499"])
        self.assertTrue(lines[4].startswith("logread: 3 of 500 lines shown;"))
        code, out, _ = main(["logread", path, "--last", "0"])
        self.assertEqual(len(out.splitlines()), 501)

    def test_raw_keeps_everything_but_still_matches(self):
        code, out, _ = main(["logread", self.write("u.log", UNITY), "--raw", "--last", "0"])
        self.assertEqual(len(out.splitlines()), 20)
        self.assertIn("[Licensing::Client]", out)

    def test_nunit_results_show_failures_only(self):
        code, out, err = main(["logread", self.write("results.xml", NUNIT)])
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out.splitlines(), [
            "FAIL  A.Breaks", "      Expected 3", "      at A.Breaks () [0x0] in /p/A.cs:12",
            f"logread: 1 passed, 1 failed, 1 skipped; {self.dir / 'results.xml'}"])

    def test_junit_results_and_json(self):
        code, out, err = main(["logread", self.write("junit.out", JUNIT), "--xml", "--json"])
        self.assertEqual((code, err), (1, ""))
        records = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(records[0]["failure"], {"name": "pkg.T.bad", "message": "assert 1 == 2",
                                                 "frame": 'File "/p/test_t.py", line 9, in bad'})
        self.assertEqual(records[-1]["results"], {"passed": 1, "failed": 1, "skipped": 1})

    def test_green_results_exit_0(self):
        code, out, _ = main(["logread", self.write("ok.xml", '<test-run><test-case fullname="A" result="Passed"/></test-run>')])
        self.assertEqual((code, out), (0, f"logread: 1 passed, 0 failed; {self.dir / 'ok.xml'}\n"))

    def test_errors(self):
        code, _, err = main(["logread", str(self.dir / "missing.log")])
        self.assertEqual((code, "no file at" in err), (1, True))
        code, _, err = main(["logread", self.write("bad.xml", "<not xml")])
        self.assertEqual((code, "cannot read test results" in err), (1, True))
        path = self.write("a.log", "x\n")
        code, _, err = main(["logread", path, "--match", "("])
        self.assertEqual((code, "--match is not a regex" in err), (2, True))
        code, _, err = main(["logread", path, "--context", "2"])
        self.assertEqual((code, "--context goes with --match" in err), (2, True))

    def test_collapse_counts(self):
        out, folded = logread.collapse(["a", "a", "b", "a"])
        self.assertEqual((out, folded), (["a  (×2)", "b", "a"], 1))
