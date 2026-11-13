import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from wf import cli, codemap
from wf.commands import dispatch, map as map_cmd
from wf.gh import WfError
from tests.fake_gh import FakeGh, configured

PY = {
    "src/board.py": "class Board:\n    def items(self):\n        return gh_get('x')\n",
    "src/gh.py": "def gh_get(path):\n    return path\n\ndef unused():\n    pass\n",
    "src/cli.py": "from board import Board\n\ndef main():\n    Board().items()\n    Board()\n",
    "tools/x.cs": "class Board { void Items() {} }\n",  # same name, another language: no edge to it
}


def _match(f, line, text, rule):
    return {"file": f, "range": {"start": {"line": line - 1}}, "text": text, "ruleId": rule}


class FakeRunner:
    """ast-grep answers scripted from PY: every `def`/`class` line is a definition."""

    def __init__(self, files=PY, dirty="", sha="abc123"):
        self.files, self.dirty, self.sha, self.cwd = dict(files), dirty, sha, None
        self.common = Path(tempfile.mkdtemp()).resolve() / ".git"
        self.scans = []

    def git(self, *args):
        if args[:2] == ("ls-files", "-z"):
            return "\0".join(self.files) + "\0"
        if args[-1] == "--git-common-dir":
            return str(self.common) + "\n"
        if args == ("rev-parse", "HEAD"):
            return self.sha + "\n"
        if args[0] == "status":
            return self.dirty
        if args[0] == "diff":
            return "src/board.py\n" if "..." in args[-1] else ""
        raise AssertionError(args)

    def ast_grep(self, rules, paths):
        self.scans.append(list(paths))
        out = []
        for f in paths:
            for i, line in enumerate(self.files.get(f, "").splitlines(), 1):
                s = line.strip()
                if f.endswith(".py") and s.startswith(("def ", "class ")):
                    name = s.split()[1].split("(")[0].rstrip(":")
                    out.append(_match(f, i, name, "python:" + s.split()[0]))
                if f.endswith(".cs") and s.startswith("class "):
                    out.append(_match(f, i, s.split()[1], "csharp:class"))
        return out

    def read(self, path):
        return self.files.get(path, "")


def graph(runner=None):
    runner = runner or FakeRunner()
    return codemap.build_ast_grep(list(runner.files), ["python", "csharp"], runner), runner


class GraphTest(unittest.TestCase):
    def test_definitions_and_edges_within_a_language(self):
        g, _ = graph()
        self.assertEqual([(d["file"], d["name"], d["kind"]) for d in g.defs],
                         [("src/board.py", "Board", "class"), ("src/board.py", "items", "def"),
                          ("src/cli.py", "main", "def"), ("src/gh.py", "gh_get", "def"), ("src/gh.py", "unused", "def"),
                          ("tools/x.cs", "Board", "class")])
        board, items, main, gh_get, unused, cs = range(6)
        self.assertEqual(g.defs[board]["sig"], "class Board:")
        # cli.py's main refers to Board and items; board.py's defs refer to gh_get; nothing refers to the C# Board
        self.assertEqual(set(g.edges[main]), {board, items})
        self.assertEqual(set(g.edges[board]), {gh_get})
        self.assertNotIn(cs, g.edges.get(main, {}))
        self.assertNotIn(unused, {t for e in g.edges.values() for t in e})

    def test_seeded_rank_prefers_the_neighbourhood(self):
        g, _ = graph()
        body, info = codemap.code_map(g, ["Board"], budget=500)
        self.assertTrue(info["seeded"])
        self.assertEqual(info["unmatched"], [])
        lines = body.splitlines()
        self.assertEqual(lines[0], "src/board.py")
        self.assertIn("     1 class Board:", lines)
        self.assertIn("gh_get", body)  # what Board uses
        self.assertNotIn("unused", body)  # unreachable from the seed
        self.assertTrue(body.index("gh_get") < body.index("tools/x.cs"))  # the same-named C# Board is a seed too, with nothing behind it

    def test_unseeded_is_most_referenced(self):
        g, _ = graph()
        body, info = codemap.code_map(g, [], budget=500)
        self.assertFalse(info["seeded"])
        self.assertIn("Board", body)
        self.assertIn("unused", body)  # everything, in rank order, until the budget

    def test_seeds_by_path_suffix_text_and_backticks(self):
        g, _ = graph()
        ids, unmatched, tiers = codemap.seeds(g, ["gh.py", "nothing"])
        self.assertEqual(unmatched, ["nothing"])
        self.assertEqual({g.defs[i]["name"] for i in ids}, {"gh_get", "unused"})
        self.assertEqual(tiers["term"], 1)
        # prose tiers: a backticked span, then a snake_case name, then a plain word that is a def name (main)
        ids, _, tiers = codemap.seeds(g, [], text="Call `Board.items()` after gh_get; main is unchanged.")
        names = {g.defs[i]["name"]: w for i, w in ids.items()}
        self.assertEqual(set(names), {"Board", "items", "gh_get", "unused", "main"})  # unused: half a seed spread over gh_get's file
        self.assertGreater(names["Board"], names["gh_get"])  # backticked outranks a bare identifier
        self.assertGreater(names["gh_get"], names["main"])  # which outranks a plain word
        self.assertGreater(names["gh_get"], names["unused"])
        self.assertEqual((tiers["code"], tiers["ident"], tiers["word"]), (2, 1, 1))
        self.assertEqual(codemap.seeding(tiers), ("identifier", "high"))
        # a code fence counts as marked code too
        ids, _, tiers = codemap.seeds(g, [], text="see\n```py\ngh_get('x')\n```\n")
        self.assertEqual(tiers["code"], 1)
        self.assertGreaterEqual(max(ids.values()), codemap.TIER["code"])

    def test_prose_does_not_seed_on_common_words(self):
        # row and mask are definitions, and nearly every file names them: an issue that merely uses the words
        # must not map their functions; the one real identifier it names does seed, and a backticked `row` still may
        files = {
            "src/grid.py": "def row(i):\n    return i\n\ndef mask(x):\n    return x\n",
            "src/a.py": "from grid import row, mask\n\ndef paint():\n    return row(mask(1))\n",
            "src/b.py": "from grid import row, mask\n\ndef erase():\n    return row(mask(2))\n",
            "src/c.py": "from grid import row, mask\n\ndef fill_cell():\n    return row(mask(3))\n",
            "src/d.py": "from grid import mask\n\ndef unrelated():\n    return mask(4)\n",
        }
        g, _ = graph(FakeRunner(files))
        self.assertEqual(g.common(0.05), {"row", "mask"})
        issue = "Each row of the board keeps a mask; fill_cell should clear the mask when a row completes."
        body, info = codemap.code_map(g, text=issue, budget=500)
        self.assertEqual(info["mode"], "identifier")
        self.assertEqual(info["tiers"]["dropped"], ["mask", "row"])
        self.assertEqual(body.splitlines()[0], "src/c.py")
        self.assertNotIn("unrelated", body)  # reachable only through mask
        self.assertEqual(info["seeds"], 1)
        # plain words alone: prose-seeded, low confidence
        _, info = codemap.code_map(g, text="paint the board, then erase it", budget=500)
        self.assertEqual((info["mode"], info["confidence"], info["seeds"]), ("prose", "low", 2))
        self.assertEqual(map_cmd.header(info).split(", ", 1)[1].split(", source")[0],
                         "~%d tokens, prose-seeded (low confidence)" % info["tokens"])
        self.assertIn("from 2 seeds", map_cmd.header(info))
        # a backticked common name is the author's choice
        ids, _, tiers = codemap.seeds(g, [], text="`row` is wrong")
        self.assertEqual(tiers["code"], 1)
        self.assertIn("row", {g.defs[i]["name"] for i in ids})
        # an identifier-shaped token seeds however common: writing fill_cell or gh_get is a choice
        files["src/e.py"] = "def gh_get(u):\n    return u\n"
        for f in ("src/a.py", "src/b.py", "src/c.py", "src/d.py"):
            files[f] += "\ndef via():\n    return gh_get(1)\n"
        g2, _ = graph(FakeRunner(files))
        self.assertIn("gh_get", g2.common(0.05))
        _, _, tiers = codemap.seeds(g2, [], text="gh_get now retries")
        self.assertEqual((tiers["ident"], tiers["dropped"]), (1, []))
        # the share is configurable: raise it and row (4 of 5 files, its own included) is no longer common
        self.assertEqual(g.common(0.9), {"mask"})
        # a map with nothing seeded says so, and names what was too common
        _, info = codemap.code_map(g, text="the row mask", budget=500)
        self.assertFalse(info["seeded"])
        self.assertIn("unseeded (most referenced), source ast-grep; too common to seed: mask, row", map_cmd.header(info))

    def test_render_fits_the_budget(self):
        g, _ = graph()
        order = list(range(len(g.defs)))
        text, n = codemap.render(g, order, 30)
        self.assertLessEqual(codemap.tokens(text), 30)
        self.assertLess(n, len(g.defs))
        full, n = codemap.render(g, order, 10_000)
        self.assertEqual(n, len(g.defs))
        self.assertEqual(full.count("\n") + 1, len(g.defs) + 4)  # one line per def, one per file

    def test_cache_per_commit_and_dirty_overlay(self):
        r = FakeRunner()
        g = codemap.cached_graph(r.common, r.sha, ["python", "csharp"], runner=r)
        self.assertEqual(len(g.defs), 6)
        self.assertTrue((r.common / "wf-map" / "abc123-python-csharp.json").is_file())
        self.assertEqual(len(r.scans), 1)
        r2 = FakeRunner(dirty=" M src/gh.py\0")
        r2.common = r.common
        r2.files["src/gh.py"] = "def gh_get(path):\n    return path\n"
        with mock.patch.object(Path, "is_file", return_value=True):
            g = codemap.cached_graph(r.common, r.sha, ["python", "csharp"], runner=r2)
        self.assertEqual(r2.scans, [["src/gh.py"]])  # the cache served the rest
        self.assertNotIn("unused", [d["name"] for d in g.defs])

    def test_cache_write_prunes_to_the_newest_ten(self):
        r = FakeRunner()
        folder = r.common / "wf-map"
        folder.mkdir(parents=True)
        for i in range(11):
            f = folder / f"old{i:02}-python.json"
            f.write_text("{}")
            os.utime(f, (1000 + i, 1000 + i))
        codemap.cached_graph(r.common, r.sha, ["python"], runner=r)
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(len(names), 10)
        self.assertIn("abc123-python.json", names)
        self.assertNotIn("old00-python.json", names)

    def test_cache_hit_refreshes_its_mtime(self):
        r = FakeRunner()
        codemap.cached_graph(r.common, r.sha, ["python"], runner=r)
        cache = r.common / "wf-map" / "abc123-python.json"
        os.utime(cache, (1000, 1000))
        codemap.cached_graph(r.common, r.sha, ["python"], runner=r)
        self.assertGreater(cache.stat().st_mtime, time.time() - 60)

    def test_dirty_files_parses_renames(self):
        status = " M a.py\0R  new.py\0old.py\0?? c.cs\0"
        self.assertEqual(codemap.dirty_files(status), {"a.py", "new.py", "old.py", "c.cs"})
        self.assertEqual(codemap.dirty_files(""), set())

    def test_cache_holds_clean_files_only(self):
        r = FakeRunner(dirty=" M src/gh.py\0")
        with mock.patch.object(Path, "is_file", return_value=True):
            codemap.cached_graph(r.common, r.sha, ["python"], runner=r)
        cached = json.loads((r.common / "wf-map" / "abc123-python.json").read_text())
        self.assertNotIn("src/gh.py", cached)
        self.assertIn("src/board.py", cached)

    def test_ast_grep_missing_is_a_wf_error(self):
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaises(WfError):
                codemap.Runner().ast_grep("x", ["a.py"])


# ---------------------------------------------------------------- SCIP: a hand-encoded index per scip.proto

def _varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _field(num, value):
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    if isinstance(value, str):
        value = value.encode()
    return _varint(num << 3 | 2) + _varint(len(value)) + value


def _occurrence(line, symbol, roles=0, single=False):
    rng = _field(8, _field(1, line) + _field(2, 0) + _field(3, 1)) if single else _field(1, _varint(line) + _varint(0) + _varint(1))
    return _field(2, symbol) + (_field(3, roles) if roles else b"") + rng


def _info(symbol, kind, display="", sig=""):
    out = _field(1, symbol) + _field(5, kind)
    if display:
        out += _field(6, display)
    if sig:
        out += _field(7, _field(5, sig) + _field(4, "csharp"))
    return out


SYM_BOARD = "scip-dotnet . . . Wf/Board#"
SYM_ITEMS = "scip-dotnet . . . Wf/Board#Items()."
SYM_MAIN = "scip-dotnet . . . Wf/Cli#Main()."


def scip_index():
    board_cs = (_field(1, "src/Board.cs") + _field(4, "csharp")
                + _field(2, _occurrence(0, SYM_BOARD, roles=1))
                + _field(2, _occurrence(1, SYM_ITEMS, roles=1, single=True))
                + _field(2, _occurrence(3, "local 0", roles=1))
                + _field(3, _info(SYM_BOARD, 7, display="Board", sig="class Board"))
                + _field(3, _info(SYM_ITEMS, 26, sig="void Items()\nmore")))
    cli_cs = (_field(1, "src/Cli.cs")
              + _field(2, _occurrence(2, SYM_MAIN, roles=1))
              + _field(2, _occurrence(3, SYM_BOARD)) + _field(2, _occurrence(3, SYM_ITEMS))
              + _field(2, _occurrence(4, SYM_ITEMS))
              + _field(2, _occurrence(5, "scip-dotnet . . . System/Console#WriteLine()."))
              + _field(3, _info(SYM_MAIN, 26)))
    return _field(1, _field(3, "/repo")) + _field(2, board_cs) + _field(2, cli_cs)


class ScipTest(unittest.TestCase):
    def test_reads_definitions_and_resolved_references(self):
        d = Path(tempfile.mkdtemp())
        (d / "index.scip").write_bytes(scip_index())
        (d / "src").mkdir()
        (d / "src" / "Cli.cs").write_text("using Wf;\nclass Cli {\n  static void Main() {\n")
        runner = codemap.Runner(d)
        g = codemap.build_scip("index.scip", [], runner)
        self.assertEqual(g.source, "scip")
        self.assertEqual([(x["file"], x["name"], x["kind"], x["line"], x["sig"]) for x in g.defs],
                         [("src/Board.cs", "Board", "class", 1, "class Board"),
                          ("src/Board.cs", "Items", "method", 2, "void Items()"),
                          ("src/Cli.cs", "Main", "method", 3, "static void Main() {")])
        board, items, main = range(3)
        self.assertEqual(set(g.edges), {main})
        self.assertAlmostEqual(g.edges[main][items], 2 ** 0.5)  # two references, sqrt-weighted
        self.assertEqual(g.edges[main][board], 1)
        body, info = codemap.code_map(g, ["Main"], budget=200)
        self.assertEqual(body.splitlines()[0], "src/Board.cs")

    def test_restricts_to_tracked_files(self):
        d = Path(tempfile.mkdtemp())
        (d / "index.scip").write_bytes(scip_index())
        g = codemap.build_scip("index.scip", ["src/Cli.cs"], codemap.Runner(d))
        self.assertEqual([x["name"] for x in g.defs], ["Main"])
        self.assertEqual(g.files, ["src/Cli.cs"])

    def test_symbol_name(self):
        self.assertEqual(codemap.symbol_name(SYM_ITEMS), "Items")
        self.assertEqual(codemap.symbol_name(SYM_BOARD), "Board")
        self.assertEqual(codemap.symbol_name("scip-python python x 1.0 `pkg.mod`/func()."), "func")

    def test_missing_index(self):
        with self.assertRaises(WfError):
            codemap.build_scip("nope.scip", [], codemap.Runner(tempfile.mkdtemp()))


FIXTURES = Path(__file__).parent / "fixtures"


class ScipKindTest(unittest.TestCase):
    def test_descriptors(self):
        self.assertEqual(codemap.descriptors("scip-dotnet nuget . . Wf/Board#`.ctor`()."),
                         [("Wf", "namespace"), ("Board", "type"), (".ctor", "method")])
        self.assertEqual(codemap.descriptors("scip-python python p 1.0 `pkg.board`/Board#items().(self)"),
                         [("pkg.board", "namespace"), ("Board", "type"), ("items", "method"), ("self", "(")])
        self.assertEqual(codemap.descriptors("a b  c d e Wf/List#[T]"), [("Wf", "namespace"), ("List", "type"), ("T", "[")])
        self.assertEqual(codemap.descriptors("x . . . `a``b`#"), [("a`b", "type")])
        self.assertEqual(codemap.descriptors("local 4"), [])

    def test_kind_from_the_symbol_refined_by_the_number(self):
        k = codemap.scip_kind
        self.assertEqual(k("x . . . Wf/Board#"), "type")
        self.assertEqual(k("x . . . Wf/Board#", 49), "struct")
        self.assertEqual(k("x . . . Wf/Board#Items()."), "method")
        self.assertEqual(k("x . . . `m`/helper()."), "function")
        self.assertEqual(k("x . . . Wf/Board#`.ctor`()."), "ctor")
        self.assertEqual(k("x . . . a/Board#`<init>`()."), "ctor")
        self.assertEqual(k("x . . . Wf/Board#Size."), "field")
        self.assertEqual(k("x . . . Wf/Board#Size.", 41), "property")
        self.assertEqual(k("x . . . Wf/"), "module")
        self.assertIsNone(k("x . . . `m`/top_level."))  # a term outside a type: a module variable
        self.assertEqual(k("x . . . `m`/LIMIT.", 8), "const")  # unless the indexer says what it is
        self.assertIsNone(k("x . . . `m`/__init__:"))
        self.assertIsNone(k("x . . . Wf/List#[T]"))
        self.assertIsNone(k("x . . . Wf/Board#Items().(self)", 26))
        self.assertIsNone(k("local 3", 26))

    def test_scip_python_fixture(self):
        """An index scip-python 0.6 wrote for tests/fixtures/scip-python: no symbol carries a kind."""
        g = codemap.build_scip("index.scip", [], codemap.Runner(FIXTURES / "scip-python"))
        self.assertEqual([(x["file"], x["name"], x["kind"], x["line"], x["sig"]) for x in g.defs],
                         [("pkg/board.py", "Board", "type", 1, "class Board:"),
                          ("pkg/board.py", "items", "method", 2, "def items(self):"),
                          ("pkg/board.py", "helper", "function", 6, "def helper():"),
                          ("pkg/cli.py", "main", "function", 4, "def main():")])
        self.assertEqual(set(g.edges[3]), {0, 1})  # main uses Board and items

    def test_scip_dotnet_fixture(self):
        """An index scip-dotnet 0.2.14 wrote for tests/fixtures/scip-dotnet: no kinds, generated obj/ documents."""
        d = FIXTURES / "scip-dotnet"
        g = codemap.build_scip("index.scip", ["Board.cs", "Helper.cs"], codemap.Runner(d))
        self.assertEqual([(x["file"], x["name"], x["kind"]) for x in g.defs],
                         [("Board.cs", "Board", "type"), ("Board.cs", "Size", "field"), ("Board.cs", ".ctor", "ctor"),
                          ("Board.cs", "Items", "method"), ("Helper.cs", "Helper", "type"), ("Helper.cs", "Get", "method")])
        items, helper, get = 3, 4, 5
        self.assertIn(get, g.edges[items])
        self.assertEqual(g.files, ["Board.cs", "Helper.cs"])

    def test_no_definitions_warns_with_the_cause(self):
        d = FIXTURES / "scip-python"
        warned = []
        codemap.build_scip("index.scip", ["elsewhere/a.py"], codemap.Runner(d), warn=warned.append)
        self.assertIn("another root", warned[0])
        tmp = Path(tempfile.mkdtemp())
        (tmp / "index.scip").write_bytes(_field(2, _field(1, "a.py") + _field(2, _occurrence(0, "x . . . a/b#"))))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            g = codemap.build_scip("index.scip", [], codemap.Runner(tmp))
        self.assertEqual(g.defs, [])
        self.assertIn("index.scip yields no definitions; no symbol in it has a definition role", err.getvalue())


SYM_GH = "scip-python python x 1 `src.gh`/gh_get()."
SYM_MAIN_PY = "scip-python python x 1 `src.cli`/main()."


def merge_index():
    gh = _field(1, "src/gh.py") + _field(2, _occurrence(0, SYM_GH, roles=1))
    cli = (_field(1, "src/cli.py") + _field(2, _occurrence(2, SYM_MAIN_PY, roles=1))
           + _field(2, _occurrence(3, SYM_GH)) + _field(2, _occurrence(4, SYM_GH)))
    path = Path(tempfile.mkdtemp()) / "index.scip"
    path.write_bytes(_field(2, gh) + _field(2, cli))
    return str(path)


class MergeTest(unittest.TestCase):
    def test_files_the_index_covers_take_its_definitions_and_edges(self):
        r = FakeRunner()
        g = codemap.cached_graph(r.common, r.sha, ["python", "csharp"], scip=[merge_index()], runner=r)
        self.assertEqual(g.source, "scip+ast-grep")
        self.assertEqual(g.counts, {"scip": 2, "ast-grep": 2})
        names = [(d["file"], d["name"]) for d in g.defs]
        self.assertEqual(names, [("src/board.py", "Board"), ("src/board.py", "items"), ("src/cli.py", "main"),
                                 ("src/gh.py", "gh_get"), ("tools/x.cs", "Board")])  # gh.py's `unused`: not in the index
        board, items, main, gh_get, _ = range(5)
        self.assertAlmostEqual(g.edges[main][gh_get], 2 ** 0.5)  # resolved
        self.assertEqual(set(g.edges[main]), {gh_get, board, items})  # plus name matches into board.py, not covered
        self.assertNotIn(4, g.edges[main])  # not the C# Board: another language
        self.assertIn(gh_get, g.edges[items])  # an ast-grep file's name match reaches the SCIP node
        self.assertIn(gh_get, g.edges[board])

    def test_name_matches_from_a_covered_file_skip_covered_targets(self):
        r = FakeRunner({**PY, "src/cli.py": PY["src/cli.py"] + "gh_get(1)\n"})
        g = codemap.cached_graph(r.common, r.sha, ["python"], scip=[merge_index()], runner=r)
        main, gh_get = 2, 3
        self.assertAlmostEqual(g.edges[main][gh_get], 2 ** 0.5)  # the resolved weight alone, no name match added

    def test_a_name_the_index_resolved_gets_no_name_match(self):
        # cli.py's `Board` resolves, in the index, to an external package's Board: nothing for ast-grep to add
        ext = "scip-python python other 1.0 `other`/Board#"
        cli = (_field(1, "src/cli.py") + _field(2, _occurrence(2, SYM_MAIN_PY, roles=1))
               + _field(2, _occurrence(3, SYM_GH)) + _field(2, _occurrence(0, ext)))
        gh = _field(1, "src/gh.py") + _field(2, _occurrence(0, SYM_GH, roles=1))
        path = Path(tempfile.mkdtemp()) / "index.scip"
        path.write_bytes(_field(2, gh) + _field(2, cli))
        r = FakeRunner()
        g = codemap.cached_graph(r.common, r.sha, ["python"], scip=[str(path)], runner=r)
        board, items, main, gh_get = range(4)
        self.assertEqual(set(g.edges[main]), {gh_get, items})  # items is unresolved there, Board is not

    def test_a_dirty_file_takes_the_fresh_extraction(self):
        files = {**PY, "src/gh.py": "\n\ndef gh_get(path):\n    return path\n\ndef added():\n    pass\n"}
        r = FakeRunner(files, dirty=" M src/gh.py\0")
        r.cwd = str(Path(tempfile.mkdtemp()))
        (Path(r.cwd) / "src").mkdir()
        (Path(r.cwd) / "src" / "gh.py").write_text(files["src/gh.py"])
        g = codemap.cached_graph(r.common, r.sha, ["python"], scip=[merge_index()], runner=r)
        self.assertEqual(g.counts, {"scip": 1, "ast-grep": 2})
        gh = [(d["name"], d["line"]) for d in g.defs if d["file"] == "src/gh.py"]
        self.assertEqual(gh, [("gh_get", 3), ("added", 6)])  # the index said gh_get at line 1, no added
        items = next(i for i, d in enumerate(g.defs) if d["name"] == "items")
        fresh = next(i for i, d in enumerate(g.defs) if d["name"] == "gh_get")
        self.assertIn(fresh, g.edges[items])  # name matches reach the fresh definition

    def test_header_counts_both_sources(self):
        code, out, err = wf(FakeRunner(), "Board", gate=f'[map]\nscip = ["{merge_index()}", "missing.scip"]')
        self.assertEqual((code, err), (0, ""))
        self.assertIn("source scip (2 files) + ast-grep (2 files)", out.splitlines()[0])


# ---------------------------------------------------------------- the commands

def wf(runner, *argv, stdin="", **config):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(codemap, "RUNNER", runner), mock.patch("sys.stdin", io.StringIO(stdin)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if config:
            with configured(**config):
                code = cli.main(["map", *argv], gh=FakeGh())
        else:
            with mock.patch.dict("os.environ", {"WF_CONFIG": "/nonexistent.toml"}):
                code = cli.main(["map", *argv], gh=FakeGh())
    return code, out.getvalue(), err.getvalue()


class MapCommandTest(unittest.TestCase):
    def test_seeded_map_without_config(self):
        code, out, err = wf(FakeRunner(), "Board", "--budget", "300")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("# wf map: "))
        self.assertIn(" from 1 seed, ~", out)
        self.assertIn("identifier-seeded, source ast-grep\nsrc/board.py\n     1 class Board:", out)

    def test_unmatched_seeds_are_reported(self):
        _, out, _ = wf(FakeRunner(), "Nope")
        self.assertIn("unseeded (most referenced), source ast-grep; no match for Nope", out.splitlines()[0])

    def test_text_from_stdin_and_changed(self):
        _, out, _ = wf(FakeRunner(), "--text", "-", stdin="see `gh_get`")
        self.assertIn("gh_get", out)
        self.assertIn("seeded", out)
        _, out, _ = wf(FakeRunner(), "--changed", "--base", "main", "--budget", "100")
        self.assertIn("src/board.py", out)
        _, out, _ = wf(FakeRunner(), "--changed", "Board", "--budget", "100")  # a seed after the flag is a seed
        self.assertIn("seeded", out)

    def test_config_budget_and_languages(self):
        _, out, err = wf(FakeRunner(), "Board", gate='[map]\nbudget = 60\nlanguages = ["python"]')
        self.assertEqual(err, "")
        self.assertLessEqual(int(out.splitlines()[0].split("~")[1].split()[0]), 60)
        self.assertNotIn("x.cs", out)

    def test_bad_config(self):
        for text in ('[map]\nbudget = 10', '[map]\nlanguages = ["cobol"]', '[map]\nindex = "x"', '[map]\nfoo = 1',
                     '[map]\nscip = []', '[map]\nscip = 3', '[map]\nindex = [{ out = "a.scip" }]',
                     '[map]\nindex = [{ run = "x", outt = "a" }]', '[map]\nindex = [3]', '[map]\nenv = { A = 1 }',
                     '[map]\nenv = "A=1"', '[map]\ncommon = 0', '[map]\ncommon = 2', '[map]\ncommon = true'):
            code, _, err = wf(FakeRunner(), gate=text)
            self.assertEqual(code, 1, text)
            self.assertIn("[map]", err)

    def test_small_budget_is_a_usage_error(self):
        code, _, err = wf(FakeRunner(), "--budget", "5")
        self.assertEqual(code, 2)
        self.assertIn("--budget", err)

    def test_json_graph(self):
        _, out, _ = wf(FakeRunner(), "--json-graph")
        self.assertEqual(json.loads(out)["source"], "ast-grep")

    def test_text_map_never_raises(self):
        with mock.patch.object(codemap, "Runner", side_effect=WfError("no git")):
            self.assertEqual(map_cmd.text_map(None, cwd="/nowhere", terms=["x"]), "")


class DispatchMapTest(unittest.TestCase):
    def test_instructions_carry_the_map(self):
        text = dispatch.instructions("---\nname: x\n---\nBody", "o/r", "a#1", 1, "T", "/t", "b", "dev", "a" * 40,
                                     "# wf map: 1 of 1\nsrc/a.py\n     1 def f():")
        self.assertIn("## Map\n", text)
        self.assertIn("```\n# wf map: 1 of 1\nsrc/a.py\n     1 def f():\n```", text)
        self.assertNotIn("## Map", dispatch.instructions("Body", "o/r", "a#1", 1, "T", "/t", "b", "dev", "a" * 40))


@unittest.skipUnless(shutil.which("ast-grep") and shutil.which("git"), "needs ast-grep and git")
class RealAstGrepTest(unittest.TestCase):
    def test_end_to_end_on_a_small_checkout(self):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / "a.py").write_text("class Board:\n    def items(self):\n        return helper()\n")
        (d / "b.py").write_text("def helper():\n    return 1\n")
        (d / "c.cs").write_text("public class Cs { public void Go() { } }\n")
        (d / "k.ts").write_text("export class K { m() {} }\ninterface I { x: number }\ntype T = string\n")
        (d / "k.go").write_text("package p\ntype S struct{}\nfunc (s S) M() {}\nfunc F() {}\n")
        subprocess.run(["git", "-C", str(d), "add", "."], check=True)
        subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
        r = codemap.Runner(d)
        sha = r.git("rev-parse", "HEAD").strip()
        g = codemap.cached_graph(str(d / ".git"), sha, ["python", "csharp", "typescript", "go"], runner=r)
        self.assertEqual({(x["name"], x["kind"]) for x in g.defs},
                         {("Board", "class"), ("items", "def"), ("helper", "def"), ("Cs", "class"), ("Go", "method"),
                          ("K", "class"), ("m", "method"), ("I", "interface"), ("T", "type"),
                          ("S", "type"), ("M", "method"), ("F", "func")})
        body, info = codemap.code_map(g, ["Board"], budget=300)
        self.assertIn("helper", body)
        self.assertNotIn("c.cs", body)
        self.assertTrue((d / ".git" / "wf-map" / f"{sha}-python-csharp-typescript-go.json").is_file())


class ConfigShapeTest(unittest.TestCase):
    def test_map_is_normalised(self):
        from wf import config
        with configured(gate='[map]\nscip = "a.scip"\nindex = ["x", { run = "y", out = "t/i.scip" }]\nenv = { A = "1" }') as path:
            m = config.load(path).map
        self.assertEqual(m["scip"], ["a.scip"])
        self.assertEqual(m["index"], [{"run": "x", "out": None}, {"run": "y", "out": "t/i.scip"}])
        self.assertEqual(m["env"], {"A": "1"})
        self.assertEqual(map_cmd.index_paths({**map_cmd.DEFAULTS, **m}), ["a.scip", "t/i.scip"])


class IndexRunner:
    """git and the shell for `wf index`: each command copies a fixture index to where its entry says."""

    def __init__(self, top, fail=None):
        self.top, self.fail, self.ran = Path(top), fail, []

    def git(self, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(self.top) + "\n"
        if args == ("rev-parse", "--absolute-git-dir"):
            return str(self.top / ".git") + "\n"
        if args[-2:] == ("ls-files", "-z"):
            return "\0".join(["pkg/board.py", "pkg/cli.py", "Board.cs", "Helper.cs", "unity/Game.cs", "README.md"]) + "\0"
        raise AssertionError(args)

    def sh(self, command, cwd, log=None, env=None):
        self.ran.append((command, cwd, env))
        if log is not None:
            Path(log).write_text(f"noisy output of {command}\n")
        if command == self.fail:
            return 3
        src, dst = command.split()
        shutil.copy(FIXTURES / src / "index.scip", self.top / dst)
        return 0


class IndexCommandTest(unittest.TestCase):
    def run_index(self, runner, toml):
        from wf.commands import index as index_cmd
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(index_cmd, "RUNNER", runner), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), configured(gate=toml):
            code = cli.main(["index"], gh=FakeGh())
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.top = Path(tempfile.mkdtemp())
        for f in ("pkg/board.py", "pkg/cli.py"):
            (self.top / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / "scip-python" / f, self.top / f)
        for f in ("Board.cs", "Helper.cs"):
            shutil.copy(FIXTURES / "scip-dotnet" / f, self.top / f)

    def test_two_indexers_with_env_each_reported(self):
        r = IndexRunner(self.top)
        code, out, err = self.run_index(r, '[map]\nenv = { DOTNET_ROLL_FORWARD = "Major" }\nindex = ['
                                        '{ run = "scip-python py.scip", out = "py.scip" },'
                                        '{ run = "scip-dotnet cs.scip", out = "cs.scip" }]')
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual([(c, e) for c, _, e in r.ran], [("scip-python py.scip", {"DOTNET_ROLL_FORWARD": "Major"}),
                                                          ("scip-dotnet cs.scip", {"DOTNET_ROLL_FORWARD": "Major"})])
        self.assertIn("index: py.scip: 2 files, 4 definitions\n", out)
        self.assertIn("index: cs.scip: 2 files, 6 definitions\n", out)
        self.assertIn("index: 4 of 5 tracked source files covered", out)  # unity/Game.cs is the gap

    def test_index_logs_indexer_output(self):
        r = IndexRunner(self.top)
        code, out, _ = self.run_index(r, '[map]\nindex = ["scip-python index.scip"]')
        self.assertEqual(code, 0, out)
        self.assertNotIn("noisy output", out)
        log = next((self.top / ".git" / "wf-index").glob("*.log"))
        self.assertIn("noisy output of scip-python", log.read_text())
        self.assertIn(f"(log {log})", out)

    def test_a_failing_command_stops(self):
        r = IndexRunner(self.top, fail="scip-python a.scip")
        code, out, _ = self.run_index(r, '[map]\nindex = ["scip-python a.scip", "scip-dotnet b.scip"]\nscip = "a.scip"')
        self.assertEqual(code, 1)
        self.assertEqual(len(r.ran), 1)
        self.assertIn("exit 3", out)

    def test_an_index_not_written_is_an_error(self):
        code, _, err = self.run_index(IndexRunner(self.top), '[map]\nindex = ["scip-python a.scip"]\nscip = ["a.scip", "b.scip"]')
        self.assertEqual(code, 1)
        self.assertIn("b.scip not written", err)
