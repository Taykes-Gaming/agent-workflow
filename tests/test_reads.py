import unittest

from wf import reads


class BashPathsTest(unittest.TestCase):
    def test_reader_arguments_per_stage(self):
        self.assertEqual(reads.bash_paths("cat src/a.py README.md"), ["src/a.py", "README.md"])
        self.assertEqual(reads.bash_paths("sed -n 1,40p src/util.py | head -5"), ["src/util.py"])
        self.assertEqual(reads.bash_paths("sed -n '/foo/p' f.py; sed -n '$p' g.py; sed -n /x/= h.py"), ["f.py", "g.py", "h.py"])
        self.assertEqual(reads.bash_paths("head -n 20 docs/x.md && tail -f /tmp/run.log"), ["docs/x.md", "/tmp/run.log"])
        self.assertEqual(reads.bash_paths("FOO=1 cat 'with space.txt'"), ["with space.txt"])

    def test_searches_edits_and_sed_scripts_are_not_reads(self):
        self.assertEqual(reads.bash_paths("grep -rn lap src; rg foo; find . -name '*.py'"), [])
        self.assertEqual(reads.bash_paths("sed -i 's/a/b/' src/a.py"), [])
        self.assertEqual(reads.bash_paths("pytest -q tests/test_a.py"), [])
        self.assertEqual(reads.bash_paths("cat > out.txt"), ["out.txt"])  # a write through cat still names the file
        self.assertEqual(reads.bash_paths('echo "unterminated'), [])

    def test_relative_normalises_dot_paths(self):
        self.assertEqual(reads.relative("./src/a.py", "/repo"), "src/a.py")
        self.assertEqual(reads.relative("src/../src/b.py", "/repo"), "src/b.py")
        self.assertEqual(reads.relative("/elsewhere/z.py", "/repo"), "/elsewhere/z.py")

    def test_files_outside_edit_is_capped_with_a_total(self):
        paths = [f"docs/f{i:02}.md" for i in range(25)]
        t = reads.tally(paths, set(), "/repo")
        self.assertEqual((len(t["files_outside_edit"]), t["files_outside_edit_total"]), (20, 25))
        self.assertEqual(len(reads.tally(paths, set(), "/repo", cap=None)["files_outside_edit"]), 25)
        self.assertIn("25 files outside", reads.describe(t))

    def test_tally_relativises_against_the_root(self):
        t = reads.tally(["/repo/src/a.py", "src/a.py", "/repo/docs/x.md", "/elsewhere/z.py"], {"src/a.py"}, "/repo")
        self.assertEqual((t["reads"], t["read_files"], t["reads_outside_edit"], t["edited"]), (4, 3, 2, 1))
        self.assertEqual(t["files_outside_edit"], ["/elsewhere/z.py", "docs/x.md"])
        self.assertEqual(reads.describe(t), "reads 4 over 3 files; 2 reads of 2 files outside the edit set (1 files edited)")
