import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "plugins" / "wf-review" / "scripts" / "review_prep.py"
_spec = importlib.util.spec_from_file_location("review_prep", _path)
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)


class ReviewPrepTest(unittest.TestCase):
    def test_linked_issue(self):
        self.assertEqual(prep.linked_issue("Closes #9\n\nbody"), 9)
        self.assertEqual(prep.linked_issue("This fixes #12 and closes #13"), 12)
        self.assertEqual(prep.linked_issue("Resolved #4"), 4)
        self.assertIsNone(prep.linked_issue("Refs #9"))
        self.assertIsNone(prep.linked_issue(None))

    def test_claude_md_paths_are_root_and_ancestors_of_changes_only(self):
        tree = ["CLAUDE.md", "src/CLAUDE.md", "src/wf/CLAUDE.md", "docs/CLAUDE.md", "README.md"]
        self.assertEqual(prep.claude_md_paths(tree, ["src/wf/gh.py"]),
                         ["CLAUDE.md", "src/CLAUDE.md", "src/wf/CLAUDE.md"])
        self.assertEqual(prep.claude_md_paths(["README.md"], ["a.py"]), [])

    def test_stop_reason(self):
        self.assertEqual(prep.stop_reason({"state": "closed", "merged": True}, []), "merged")
        self.assertEqual(prep.stop_reason({"state": "closed", "merged": False}, []), "closed")
        self.assertEqual(prep.stop_reason({"state": "open", "draft": True}, []), "draft")
        self.assertEqual(prep.stop_reason({"state": "open"}, [{"body": "x\n<!-- wf-review -->\n"}]), "already reviewed")
        self.assertIsNone(prep.stop_reason({"state": "open"}, [{"body": "### Code review"}]))

    def test_review_comments_collects_what_a_fix_round_answers(self):
        text = prep.review_comments(
            [{"user": {"login": "bot"}, "state": "COMMENTED", "body": "Two issues."}, {"body": ""}],
            [{"path": "src/a.py", "line": 4, "body": "Off by one."}],
            [{"body": "<!-- wf-review -->\n### Review"}, {"body": "Thanks!"}])
        self.assertIn("## Review by bot (COMMENTED)\n\nTwo issues.", text)
        self.assertIn("## src/a.py:4\n\nOff by one.", text)
        self.assertIn("### Review", text)
        self.assertNotIn("Thanks!", text)
        self.assertEqual(prep.review_comments([], [], []), "none\n")

    def test_split_diff_omits_fixtures_and_lock_files(self):
        diff = ("diff --git a/src/a.py b/src/a.py\n+x\n"
                "diff --git a/tests/fixtures/items.json b/tests/fixtures/items.json\n+{}\n"
                "diff --git a/uv.lock b/uv.lock\n+lock\n"
                "diff --git a/docs/fixtures.md b/docs/fixtures.md\n+doc\n")
        kept, omitted = prep.split_diff(diff)
        self.assertEqual(omitted, ["tests/fixtures/items.json", "uv.lock"])
        self.assertIn("src/a.py", kept)
        self.assertIn("docs/fixtures.md", kept)
        self.assertNotIn("items.json", kept)

    def test_split_diff_keeps_a_rename_out_of_fixtures(self):
        diff = "diff --git a/tests/fixtures/gen.py b/src/gen.py\nsimilarity index 90%\n"
        kept, omitted = prep.split_diff(diff)
        self.assertEqual(omitted, [])
        self.assertIn("src/gen.py", kept)

    def test_history_path_follows_a_rename(self):
        self.assertEqual(prep.history_path({"filename": "src/new.py", "previous_filename": "src/old.py"}), "src/old.py")
        self.assertEqual(prep.history_path({"filename": "src/a.py"}), "src/a.py")

    def test_history_lane(self):
        self.assertTrue(prep.history_lane(["src/a.py"], ""))
        self.assertTrue(prep.history_lane([], "`wf` replaces all three copies of tools/board"))
        self.assertTrue(prep.history_lane([], "Ported from the shell scripts"))
        self.assertFalse(prep.history_lane([], "Add a new command"))

    def test_claude_md_paths_come_from_git_in_a_checkout_of_the_head(self):
        import json
        import tempfile
        from unittest import mock
        head = "abc1234"
        pr = {"title": "t", "state": "open", "body": "", "head": {"sha": head}, "base": {"sha": "base000"},
              "additions": 1, "deletions": 0}
        paths = []

        def gh(path, accept=None, paginate=False, optional=False):
            paths.append(path)
            if path.endswith("/pulls/12") and not accept:
                return pr
            if path.endswith("/pulls/12/files?per_page=100"):
                return [{"filename": "src/wf/a.py", "status": "added"}]
            return "" if accept else []

        def git(argv, **kw):
            out = {"rev-parse": head + "\n", "ls-files": "CLAUDE.md\nsrc/CLAUDE.md\ndocs/CLAUDE.md\n"}[argv[1]]
            return mock.Mock(returncode=0, stdout=out, stderr="")

        out = Path(tempfile.mkdtemp())
        with mock.patch.object(prep, "_gh", side_effect=gh), mock.patch.object(prep.subprocess, "run", side_effect=git), \
                mock.patch.object(prep.shutil, "which", return_value=None), mock.patch("builtins.print"):
            prep.main(["12", "--repo", "o/r", "--dir", str(out)])
        self.assertFalse([p for p in paths if "/git/trees/" in p])
        result = json.loads((out / "prep.json").read_text())
        self.assertEqual((result["claude_md"], result["claude_md_incomplete"]), (["CLAUDE.md", "src/CLAUDE.md"], False))


if __name__ == "__main__":
    unittest.main()

    def test_map_skips_when_not_at_the_pr_head(self):
        import tempfile
        from unittest import mock
        out = Path(tempfile.mkdtemp())
        with mock.patch.object(prep.shutil, "which", return_value="/usr/bin/wf"), \
                mock.patch.object(prep.subprocess, "run") as run:
            self.assertEqual(prep._map(out, ["README.md"], "2222222"), (None, "no changed code files"))
            run.return_value = mock.Mock(stdout="1111111\n")
            self.assertEqual(prep._map(out, ["a.py"], "2222222"), (None, "checkout is at 1111111, PR head is 2222222"))
            run.side_effect = [mock.Mock(stdout="2222222\n"), mock.Mock(returncode=0, stdout="# wf map: 1\n", stderr="")]
            self.assertEqual(prep._map(out, ["a.py", "README.md"], "2222222"), (str(out / "map.md"), None))
            self.assertEqual(run.call_args.args[0][:3], ["wf", "map", "--budget"])
            self.assertEqual(run.call_args.args[0][-1], "a.py")  # README.md is not a seed
        with mock.patch.object(prep.shutil, "which", return_value=None):
            self.assertEqual(prep._map(out, ["a.py"], "2222222"), (None, "wf is not installed"))


class ApiModeTest(unittest.TestCase):
    def config(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / ".workflow.toml"
        path.write_text(text)
        return str(path)

    def mode(self, flag=None, config=None):
        env = {"WF_CONFIG": config} if config else {}
        with mock.patch.dict(os.environ, env):
            if not config:
                os.environ.pop("WF_CONFIG", None)
            return prep.api_mode(flag)

    def test_api_flag_wins(self):
        self.assertEqual(self.mode("graphql", self.config('api = "rest"\n')), "graphql")

    def test_api_from_config(self):
        self.assertEqual(self.mode(config=self.config('api = "graphql"\n')), "graphql")
        self.assertEqual(self.mode(config=self.config('x = 1\n')), "rest")

    def test_api_no_config_is_rest(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(self.mode(), "rest")
            finally:
                os.chdir(cwd)

    def test_api_bad_value_exits(self):
        path = self.config('api = "soap"\n')
        with self.assertRaises(SystemExit) as cm:
            self.mode(config=path)
        self.assertIn(path, str(cm.exception))
        self.assertIn("'soap'", str(cm.exception))

    def test_api_unparsable_config_exits(self):
        path = self.config("api = \n")
        with self.assertRaises(SystemExit) as cm:
            self.mode(config=path)
        self.assertIn(path, str(cm.exception))

    def test_api_missing_wf_config_exits(self):
        with self.assertRaises(SystemExit) as cm:
            self.mode(config="/nonexistent/.workflow.toml")
        self.assertIn("/nonexistent/.workflow.toml", str(cm.exception))

    def test_api_in_stop_prep(self):
        with tempfile.TemporaryDirectory() as d:
            pr = {"title": "t", "state": "closed", "merged": False}
            with mock.patch.object(prep, "_gh", side_effect=lambda path, **k: pr if "pulls" in path else []):
                prep.main(["5", "--repo", "o/r", "--dir", d, "--api", "graphql"])
            data = json.loads((Path(d) / "prep.json").read_text())
        self.assertEqual((data["stop"], data["api"]), ("closed", "graphql"))


GATES = {"always": True, "issue_or_claude_md": True, "history": False}


class LanesTest(unittest.TestCase):
    def run_prep(self, *argv, config="", issue=None, additions=1, deletions=0, history=False, state="open"):
        """prep.json from main on PR 12 (closing #7 when `issue` is given), with `config` as the --config file."""
        d = Path(tempfile.mkdtemp())
        (d / "c.toml").write_text(config)
        pr = {"title": "t", "state": state, "body": "Closes #7" if issue else "", "head": {"sha": "abc1234"},
              "base": {"sha": "base000"}, "additions": additions, "deletions": deletions}

        def gh(path, accept=None, paginate=False, optional=False):
            if path.endswith("/pulls/12") and not accept:
                return pr
            if path.endswith("/issues/7"):
                return issue
            if path.endswith("/pulls/12/files?per_page=100"):
                return [{"filename": "src/a.py", "status": "modified"}]
            if "/commits?" in path:
                return [{"sha": "old"}] if history else []
            return "" if accept else []

        def git(cmd, **kw):
            return mock.Mock(returncode=0, stdout={"rev-parse": "abc1234\n"}.get(cmd[1], ""), stderr="")

        with mock.patch.object(prep, "_gh", side_effect=gh), mock.patch.object(prep.subprocess, "run", side_effect=git), \
                mock.patch.object(prep.shutil, "which", return_value=None), mock.patch("builtins.print"):
            prep.main(["12", "--repo", "o/r", "--dir", str(d / "out"), "--config", str(d / "c.toml"), *argv])
        return json.loads((d / "out" / "prep.json").read_text())

    def names(self, review, tier, **gates):
        return [x["name"] for x in prep.resolve_lanes(review, tier, {**GATES, **gates})]

    def test_default_lanes_match_today(self):
        today = {"light": ("correctness", "reuse"), "standard": ("correctness", "spec", "reuse", "history?"),
                 "deep": ("correctness", "spec", "reuse", "history"), "fix": ("correctness",)}
        for tier, lanes in today.items():
            for history in (True, False):
                for issue in (True, False):
                    # wf review's old _lanes(), then the recipe's "skip spec if there is no issue and no CLAUDE.md"
                    want = [x.rstrip("?") for x in lanes if not x.endswith("?") or history]
                    want = [x for x in want if x != "spec" or issue]
                    self.assertEqual(self.names({}, tier, issue_or_claude_md=issue, history=history), want,
                                     (tier, history, issue))
        issue = {"title": "T", "body": "", "labels": []}
        data = self.run_prep(issue=issue, history=True)
        self.assertEqual((data["tier"], [x["name"] for x in data["lanes"]]),
                         ("standard", ["correctness", "spec", "reuse", "history"]))
        self.assertEqual(data["lanes"][0], {"name": "correctness", "brief": prep.BUILTIN_LANES["correctness"]["brief"],
                                            "model": None, "opencode_model": None})
        self.assertEqual([x["name"] for x in self.run_prep()["lanes"]], ["correctness", "reuse"])

    def test_custom_lane_added_and_removed(self):
        data = self.run_prep("--tier", "deep", config='[review.lanes.security]\nbrief = "Find injection."\n'
                                                      '[review.tiers]\ndeep = ["correctness", "security"]\n')
        self.assertEqual(data["lanes"], [
            {"name": "correctness", "brief": prep.BUILTIN_LANES["correctness"]["brief"], "model": None,
             "opencode_model": None},
            {"name": "security", "brief": "Find injection.", "model": None, "opencode_model": None}])

    def test_builtin_override_keeps_other_keys(self):
        review = {"lanes": {"reuse": {"model": "haiku"}, "spec": {"opencode_model": "openai/gpt-6"}}}
        lanes = {x["name"]: x for x in prep.resolve_lanes(review, "standard", GATES)}
        self.assertEqual(lanes["reuse"], {"name": "reuse", "brief": prep.BUILTIN_LANES["reuse"]["brief"],
                                          "model": "haiku", "opencode_model": None})
        self.assertEqual(lanes["spec"]["brief"], prep.BUILTIN_LANES["spec"]["brief"])
        # spec keeps its issue_or_claude_md gate
        self.assertNotIn("spec", self.names(review, "standard", issue_or_claude_md=False))

    def test_question_mark_gate(self):
        review = {"tiers": {"standard": ["correctness", "history?"], "deep": ["correctness", "history"]}}
        self.assertEqual(self.names(review, "standard", history=False), ["correctness"])
        self.assertEqual(self.names(review, "standard", history=True), ["correctness", "history"])
        self.assertEqual(self.names(review, "deep", history=False), ["correctness", "history"])

    def test_standalone_tier_from_size_label(self):
        issue = {"title": "T", "body": "", "labels": [{"name": "effort:L"}]}
        self.assertEqual(self.run_prep(issue=issue, config="[review]\ntiered = true\n")["tier"], "deep")
        self.assertEqual(self.run_prep(issue=issue, config='[review]\ntiered = true\n[labels]\nsize_prefix = "size/"\n',
                                       additions=100)["tier"], "light")  # no size/ label: by the diff
        self.assertEqual(self.run_prep("--tier", "light", issue=issue, config="[review]\ntiered = true\n")["tier"],
                         "light")

    def test_standalone_tier_from_diff(self):
        self.assertEqual(self.run_prep(config="[review]\ntiered = true\n", additions=150, deletions=50)["tier"],
                         "standard")
        self.assertEqual(self.run_prep(config="[review]\ntiered = false\n", additions=900)["tier"], "standard")
        self.assertEqual(self.run_prep(additions=900)["tier"], "standard")

    def test_fix_round_uses_fix_list(self):
        data = self.run_prep("--since", "old5678", "--tier", "deep")
        self.assertEqual((data["tier"], [x["name"] for x in data["lanes"]]), ("fix", ["correctness"]))
        data = self.run_prep("--since", "old5678", config='[review.tiers]\nfix = ["correctness", "reuse"]\n')
        self.assertEqual([x["name"] for x in data["lanes"]], ["correctness", "reuse"])

    def test_stopped_prep_has_no_tier_or_lanes(self):
        data = self.run_prep(state="closed")
        self.assertEqual(data["stop"], "closed")
        self.assertNotIn("tier", data)
        self.assertNotIn("lanes", data)

    def test_lane_config_errors(self):
        review = {"lanes": {"security": {},  # missing_brief
                            "perf": {"brief": "b", "when": "sometimes"},  # bad_when
                            "style": {"brief": "b", "model": "gpt"},  # bad_model
                            "docs": {"brief": "b", "colour": "red"}},  # unknown_key
                  "tiers": {"deep": ["correctness", "secrity"],  # unknown_lane
                            "huge": ["correctness"],  # bad_tier
                            "light": [],  # empty_tier
                            "standard": ["correctness", "reuse?", "reuse"]}}  # duplicate_lane
        errors = prep.lane_config_errors(review)
        self.assertEqual(sorted(e.split(": ")[0] for e in errors),
                         sorted(["missing_brief", "bad_when", "bad_model", "unknown_key", "unknown_lane", "bad_tier",
                                 "empty_tier", "duplicate_lane"]))
        self.assertIn("unknown_lane: [review.tiers].deep: no lane named 'secrity'", errors)
        self.assertEqual(prep.lane_config_errors({}), [])
        self.assertEqual(prep.lane_config_errors({
            "lanes": {"security": {"brief": "Find injection.", "when": "history", "model": "opus",
                                   "opencode_model": "anthropic/claude-opus-5-5"}, "reuse": {"model": "haiku"}},
            "tiers": {"deep": ["correctness", "security?", "reuse"], "fix": ["correctness"]}}), [])
        with self.assertRaises(SystemExit) as cm:
            self.run_prep(config='[review.tiers]\ndeep = ["correctness", "secrity"]\n')
        self.assertIn("unknown_lane: [review.tiers].deep: no lane named 'secrity'", str(cm.exception))
