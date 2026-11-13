import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wf import cli


def assistant(mid, model="claude-opus-5-5", inp=2, out=100, write=1000, read=5000, block="tool_use"):
    return {"type": "assistant", "message": {
        "id": mid, "model": model, "content": [{"type": block}],
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_creation_input_tokens": write, "cache_read_input_tokens": read}}}


def transcript(root, aid, lines, session="s1", project="-Users-me-app"):
    d = Path(root) / project / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"agent-{aid}.jsonl"
    path.write_text("".join(json.dumps(m) + "\n" for m in lines) + "not json\n")
    return path


def main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        # No .workflow.toml anywhere: usage must not need one.
        self.cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())
        self.addCleanup(os.chdir, self.cwd)

    def test_counts_each_response_once(self):
        transcript(self.root, "abc", [
            {"type": "user", "message": {"content": "Implement #12"}},
            assistant("m1", block="thinking"), assistant("m1", block="text"), assistant("m1"),
            assistant("m2", inp=3, out=900, write=2000, read=9000),
            assistant("m3", model="<synthetic>", out=0, write=0, read=0),
        ])
        code, out, err = main(["usage", "abc", "--projects", self.root])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "abc: opus, 2 turns, 4k tokens (in 0k, out 1k, cache write 3k), cache read 14k, reads 0",
            "wf merged flags: --model opus --ktokens 4 --cache-ktokens 14 --turns 2",
        ])

    def test_edits_counts_reads_outside_what_changed(self):
        import subprocess
        repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.py").write_text("1\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], check=True)
        (repo / "a.py").write_text("2\n")
        (repo / "b.py").write_text("new\n")
        (repo / "new dir").mkdir()
        (repo / "new dir" / "c.py").write_text("untracked, inside an untracked directory\n")
        os.chdir(repo)
        reads = [{"type": "tool_use", "name": "Read", "input": {"file_path": str(repo / "a.py")}},
                 {"type": "tool_use", "name": "Read", "input": {"file_path": str(repo / "docs/x.md")}},
                 {"type": "tool_use", "name": "Bash", "input": {"command": "head -20 ./b.py; cat lib/y.py 'new dir/c.py'"}}]
        line = assistant("m1")
        line["message"]["content"] = reads
        transcript(self.root, "r1", [line])
        code, out, err = main(["usage", "r1", "--projects", self.root, "--edits", "HEAD"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("reads 5 over 5 files; 2 reads of 2 files outside the edit set (3 files edited)", out)
        code, out, err = main(["usage", "r1", "--projects", self.root, "--edits", "HEAD", "--json"])
        rec = json.loads(out.splitlines()[0])
        self.assertEqual(rec["files_outside_edit"], ["docs/x.md", "lib/y.py"])
        code, _, err = main(["usage", "r1", "--projects", self.root, "--edits", "nonsense"])
        self.assertEqual((code, "git cannot diff against nonsense" in err), (1, True))

    def test_several_agents_total_and_json(self):
        transcript(self.root, "a1", [assistant("m1", out=500_000)])
        path = transcript(self.root, "a2", [assistant("m1", model="claude-sonnet-5-5", out=500_000)], session="s2")
        code, out, _ = main(["usage", "agent-a1", str(path), "--projects", self.root, "--json"])
        records = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual([r.get("agent") for r in records], ["a1", "a2", "total", None])
        self.assertEqual(records[2]["turns"], 2)
        self.assertEqual(records[2]["models"], ["opus", "sonnet"])
        # Two models: --model is left for the architect to choose.
        self.assertEqual(records[3]["flags"], "--ktokens 1002 --cache-ktokens 10 --turns 2")

    def test_unknown_agent_fails_before_output(self):
        transcript(self.root, "a1", [assistant("m1")])
        code, out, err = main(["usage", "a1", "missing", "--projects", self.root])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no transcript for agent missing", err)

    def test_default_root_follows_claude_config_dir(self):
        transcript(Path(self.root) / "projects", "a1", [assistant("m1")])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.root}):
            code, out, _ = main(["usage", "a1"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("a1: opus, 1 turns"))


if __name__ == "__main__":
    unittest.main()
