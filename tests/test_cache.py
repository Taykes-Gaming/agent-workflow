import os
import tempfile
import unittest
from pathlib import Path

from wf.cache import prune

NOW = 2_000_000_000
DAY = 86400


class PruneTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def make(self, name, age_days, folder=False):
        path = self.dir / name
        if folder:
            path.mkdir()
        else:
            path.write_text("x")
        os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
        return path

    def test_keep_leaves_the_n_newest(self):
        for i in range(12):
            self.make(f"f{i:02}", i + 1)
        self.assertEqual(prune(self.dir, keep=10, now=NOW), ["f10", "f11"])
        self.assertEqual(len(list(self.dir.iterdir())), 10)

    def test_older_than_removes_only_old_entries(self):
        self.make("old", 15)
        self.make("new", 13)
        self.assertEqual(prune(self.dir, older_than_days=14, now=NOW), ["old"])
        self.assertTrue((self.dir / "new").exists())

    def test_a_folder_is_as_new_as_its_newest_file(self):
        folder = self.make("run", 30, folder=True)
        inner = folder / "log"
        inner.write_text("x")
        os.utime(inner, (NOW - DAY, NOW - DAY))
        os.utime(folder, (NOW - 30 * DAY, NOW - 30 * DAY))
        self.assertEqual(prune(self.dir, older_than_days=14, now=NOW), [])
        self.assertTrue(folder.exists())

    def test_an_old_folder_is_removed_whole(self):
        folder = self.make("run", 30, folder=True)
        (folder / "log").write_text("x")
        os.utime(folder / "log", (NOW - 30 * DAY, NOW - 30 * DAY))
        self.assertEqual(prune(self.dir, older_than_days=14, now=NOW), ["run"])
        self.assertFalse(folder.exists())

    def test_spare_is_never_removed(self):
        self.make("mine", 99)
        self.make("other", 99)
        self.assertEqual(prune(self.dir, older_than_days=14, spare=("mine",), now=NOW), ["other"])
        self.assertTrue((self.dir / "mine").exists())

    def test_a_missing_folder_is_not_an_error(self):
        self.assertEqual(prune(self.dir / "nope", keep=1, older_than_days=1), [])


if __name__ == "__main__":
    unittest.main()
