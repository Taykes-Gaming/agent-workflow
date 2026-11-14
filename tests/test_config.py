import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_gh import CONFIG, FakeGh, run
from wf import cli, config
from wf.config import Ref, UsageError
from wf.gh import WfError


class ConfigTest(unittest.TestCase):
    def test_org_board_paths_start_orgs(self):
        gh = FakeGh(owner_type="org")
        code, _, err = run(["move", "web#157", "ready"], gh, owner_type="org")
        self.assertEqual((code, err), (0, ""))
        board_calls = [c[1] for c in gh.calls if "projectsV2" in c[1]]
        self.assertTrue(board_calls)
        self.assertTrue(all(p.startswith("/orgs/acme/projectsV2/2/") for p in board_calls), board_calls)

    def test_user_board_paths_start_users(self):
        gh = FakeGh()
        run(["board"], gh)
        self.assertTrue(all(c[1].startswith("/users/acme/projectsV2/2/") for c in gh.calls))

    def test_found_by_walking_up_from_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / ".workflow.toml").write_text(CONFIG.format(owner_type="user", deploy="", review=""))
            deep = root / "a" / "b"
            deep.mkdir(parents=True)
            env = {k: v for k, v in os.environ.items() if k != "WF_CONFIG"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("pathlib.Path.cwd", return_value=deep):
                self.assertEqual(config.find(), root / ".workflow.toml")
                self.assertEqual(config.load().owner, "acme")

    def test_wf_config_wins(self):
        with mock.patch.dict(os.environ, {"WF_CONFIG": "/somewhere/x.toml"}):
            self.assertEqual(config.find(), Path("/somewhere/x.toml"))

    def test_missing_required_key_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(CONFIG.format(owner_type="user", deploy="", review="").replace('size = "Size"\n', ""))
            with self.assertRaisesRegex(WfError, r"missing \[fields\]\.size"):
                config.load(p)
            p.write_text(CONFIG.format(owner_type="team", deploy="", review=""))
            with self.assertRaisesRegex(WfError, "owner_type"):
                config.load(p)

    def test_malformed_values_are_wf_errors_not_tracebacks(self):
        base = CONFIG.format(owner_type="user", deploy="", review="")
        cases = {  # config text → the message it must produce
            base.replace("number = 2", 'number = "two"'): r"\[board\]\.number must be a positive integer, not 'two'",
            base.replace("number = 2", "number = 0"): r"\[board\]\.number must be a positive integer, not 0",
            'fields = "Status"\n' + base.replace('[fields]\nstatus = "Status"', '[x]\nstatus = "Status"'):
                r"\[fields\] must be a table, not str",
            base.replace('size = "Size"', "size = 3"): r"\[fields\]\.size must be a string, not int",
            base.replace('size_prefix = "effort:"', 'size_prefix = ""'): r"\[labels\]\.size_prefix must not be empty",
            'repos = "web"\n' + base.replace('[repos]\nweb = "acme/web"', '[y]\nweb = "acme/web"'):
                r"\[repos\] must be a table, not str",
            base + '[review]\nengine = "codex"\n': r"\[review\]\.engine must be claude or opencode",
            base + '[review]\nlane_model = 3\n': r"\[review\]\.lane_model must be a string",
            base.replace("[board]", 'review = "x"\n[board]', 1): r"\[review\] must be a table",
            base + '[review]\nlevel = "extreme"\n': r"\[review\]\.level must be one of low, medium",
            base + '[review]\ntiered = "yes"\n': r"\[review\]\.tiered must be true or false",
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            for text, message in cases.items():
                p.write_text(text)
                with self.assertRaisesRegex(WfError, message):
                    config.load(p)

    def test_a_quoted_board_number_is_still_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(CONFIG.format(owner_type="user", deploy="", review="").replace("number = 2", 'number = "2"'))
            self.assertEqual(config.load(p).number, 2)

    def test_malformed_config_exits_1_through_the_cli(self):
        gh = FakeGh()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(CONFIG.format(owner_type="user", deploy="", review="").replace("number = 2", 'number = "two"'))
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"WF_CONFIG": str(p)}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["board"], gh=gh)
        self.assertEqual(code, 1)
        self.assertIn("[board].number must be a positive integer, not 'two'", err.getvalue())
        self.assertEqual(gh.calls, [])

    def test_numbers_are_checked_with_their_bounds(self):
        self.assertTrue(config._number(0.5, above=0, at_most=1))
        self.assertTrue(config._number(50, at_least=50, integer=True))
        for bad, kw in [(True, {"above": 0}), ("1", {"above": 0}), (0, {"above": 0}), (1.5, {"above": 0, "at_most": 1}),
                        (49, {"at_least": 50, "integer": True}), (50.0, {"at_least": 50, "integer": True})]:
            self.assertFalse(config._number(bad, **kw), (bad, kw))

    def test_refs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(CONFIG.format(owner_type="user", deploy="", review=""))
            cfg = config.load(p)
        self.assertEqual(cfg.parse_ref("web#12"), Ref("acme/web", 12))
        self.assertEqual(cfg.parse_ref("acme/thing#3"), Ref("acme/thing", 3))
        self.assertEqual(cfg.show_ref("acme/core", 3), "core#3")
        self.assertEqual(cfg.show_ref("acme/thing", 3), "acme/thing#3")
        for bad in ("web", "web#x", "web#1#2", "a/b/c#1", "web#1 mobile#2", "ios#1"):
            with self.assertRaises(UsageError, msg=bad):
                cfg.parse_ref(bad)

    def _load_api(self, line):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(line + CONFIG.format(owner_type="user", deploy="", review=""))
            return config.load(p), p

    def test_api_defaults_to_rest(self):
        self.assertEqual(self._load_api("")[0].api, "rest")

    def test_api_graphql(self):
        self.assertEqual(self._load_api('api = "graphql"\n')[0].api, "graphql")

    def test_api_rejects_other_values(self):
        for line in ('api = "REST"\n', "api = 1\n"):
            with self.subTest(line=line):
                with self.assertRaisesRegex(WfError, r'c\.toml: api must be "rest" or "graphql"'):
                    self._load_api(line)

    def test_review_lane_errors_raise(self):
        review = ('[review]\ntiered = true\n[review.lanes.security]\nwhen = "never"\n'
                  '[review.tiers]\nlight = []\ndeep = ["correctness", "secrity"]\n')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(CONFIG.format(owner_type="user", deploy="", review=review))
            with self.assertRaises(WfError) as cm:
                config.load(p)
            lines = str(cm.exception).splitlines()
            self.assertEqual(lines, [
                f"{p}: missing_brief: [review.lanes.security]: a lane that is not built in needs a brief",
                f"{p}: bad_when: [review.lanes.security].when: must be always, issue_or_claude_md, history, not 'never'",
                f"{p}: empty_tier: [review.tiers].light: names no lanes",
                f"{p}: unknown_lane: [review.tiers].deep: no lane named 'secrity'"])
            p.write_text(CONFIG.format(owner_type="user", deploy="", review=(
                '[review.lanes.security]\nbrief = "Find injection."\n[review.tiers]\ndeep = ["correctness", "security"]\n')))
            self.assertEqual(config.load(p).review["tiers"], {"deep": ["correctness", "security"]})


if __name__ == "__main__":
    unittest.main()
