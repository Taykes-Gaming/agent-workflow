import importlib.util
import unittest
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
