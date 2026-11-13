"""The code map behind `wf map`: a symbol graph, ranked around a seed, cut to a token budget.

Nodes are definitions (class, function, method, …) plus one node per source
file; an edge runs from a definition (or a file with none) to every definition
its file refers to. The graph comes from two sources, merged per file:

- **ast-grep** (the default; no index to build): definitions from tree-sitter
  node kinds per language, references from the identifiers each file
  contains that name a definition elsewhere. Same-name definitions in
  different files all receive the edge, so a reference is a name match, not
  a resolved one.
- **SCIP** (`[map].scip`, one or more `index.scip` written by scip-dotnet,
  scip-python, scip-typescript…): definitions and references as the compiler
  resolved them, read directly from the protobuf wire format, so no scip CLI
  is needed. Per file: a file an index covers takes its definitions and
  outgoing edges from the index; every other file keeps the ast-grep ones,
  and its name matches may reach a SCIP-defined node.

Ranking is personalized PageRank: the seed (symbol names, paths, or the
identifiers and paths found in an issue body) gets the restart mass, so the
map is the neighbourhood of the task, not the whole repository. The rendered
map lists definitions grouped by file with their signature line, as many as
fit the budget (a binary search over the ranked list).

The extraction of the clean files is cached per commit and language set in
`<git-common-dir>/wf-map/<sha>-<languages>.json`; files the working tree has
changed are re-extracted on every run and never cached.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .cache import prune
from .gh import WfError, run_git

# language → (ast-grep language, extensions, {tree-sitter node kind: label})
LANGUAGES = {
    "python": ("python", (".py",), {"function_definition": "def", "class_definition": "class"}),
    "csharp": ("csharp", (".cs",), {
        "class_declaration": "class", "struct_declaration": "struct", "interface_declaration": "interface",
        "enum_declaration": "enum", "record_declaration": "record", "delegate_declaration": "delegate",
        "method_declaration": "method", "constructor_declaration": "ctor", "property_declaration": "property"}),
    "typescript": ("typescript", (".ts",), {
        "function_declaration": "function", "class_declaration": "class", "method_definition": "method",
        "interface_declaration": "interface", "type_alias_declaration": "type", "enum_declaration": "enum"}),
    "tsx": ("tsx", (".tsx",), {
        "function_declaration": "function", "class_declaration": "class", "method_definition": "method",
        "interface_declaration": "interface", "type_alias_declaration": "type"}),
    "javascript": ("javascript", (".js", ".mjs", ".cjs"), {
        "function_declaration": "function", "class_declaration": "class", "method_definition": "method"}),
    "go": ("go", (".go",), {"function_declaration": "func", "method_declaration": "method", "type_spec": "type"}),
}
# the node kinds a definition's `name` field holds, per grammar (a TS class name is a type_identifier, a
# method's a property_identifier, a Go method's a field_identifier); a kind the grammar lacks is a rule error
NAME_KINDS = {"python": ("identifier",), "csharp": ("identifier",),
              "typescript": ("identifier", "type_identifier", "property_identifier"),
              "tsx": ("identifier", "type_identifier", "property_identifier"),
              "javascript": ("identifier", "property_identifier"),
              "go": ("identifier", "type_identifier", "field_identifier")}
SCIP_KINDS = {7: "class", 49: "struct", 21: "interface", 11: "enum", 17: "function", 26: "method",
              80: "method", 66: "method", 9: "ctor", 41: "property", 73: "delegate", 54: "type",
              55: "type", 53: "trait", 8: "const", 29: "module"}
SCIP_DEFINITION = 0x1
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
PATHISH = re.compile(r"[\w./-]+\.[A-Za-z]{1,4}\b")
CODELIKE = re.compile(r"^(?=.*[a-z])(?=.*(?:_|[a-z][A-Z]))[A-Za-z_][A-Za-z0-9_]{2,}$")  # snake_case or camelCase, not a word
BACKTICKED = re.compile(r"`([^`\n]{1,120})`")
FENCED = re.compile(r"```[^\n]*\n(.*?)```", re.S)
COMMON_SHARE = 0.05  # a name more than this share of files use is noise as a prose seed ([map].common)
TIER = {"term": 1.0, "code": 3.0, "ident": 1.0, "word": 0.3}  # seed weight per tier: an explicit term, a
# backticked or fenced span, a snake_case/camelCase token in prose, a plain word that names a definition
CHARS_PER_TOKEN = 3.5
DAMPING, ITERATIONS, TOLERANCE = 0.85, 50, 1e-6
MAX_FILE_BYTES = 2_000_000  # generated bundles are skipped as referrers


def _rules(languages):
    out = []
    for lang in languages:
        ag, _, kinds = LANGUAGES[lang]
        names = ", ".join("{kind: %s}" % k for k in NAME_KINDS[lang])
        for kind, label in kinds.items():
            out.append(f"id: {lang}:{label}\nlanguage: {ag}\nrule:\n  any: [{names}]\n"
                       f"  inside: {{field: name, kind: {kind}}}")
    return "\n---\n".join(out)


def extensions(languages):
    return tuple(e for lang in languages for e in LANGUAGES[lang][1])


_EXT_LANG = {e: lang for lang, (_, exts, _) in LANGUAGES.items() for e in exts}
LANGUAGE_FAMILY = {"typescript": "js", "tsx": "js", "javascript": "js"}


def language_of(path):
    lang = _EXT_LANG.get("." + path.rsplit(".", 1)[-1]) if "." in path else None
    return LANGUAGE_FAMILY.get(lang, lang)


class Runner:
    """git, ast-grep and the files: what the map drives, all under `cwd` (the checkout to map)."""

    def __init__(self, cwd=None):
        self.cwd = str(cwd) if cwd else None

    def git(self, *args):
        return run_git(*args, cwd=self.cwd, strip=False)

    def ast_grep(self, rules, paths):
        if not shutil.which("ast-grep"):
            raise WfError("wf map needs ast-grep on PATH (brew install ast-grep)")
        proc = subprocess.run(["ast-grep", "scan", "--inline-rules", rules, "--json=compact", *paths],
                              capture_output=True, text=True, cwd=self.cwd)
        if proc.returncode != 0:
            raise WfError(f"ast-grep: {proc.stderr.strip()[:300]}")
        return json.loads(proc.stdout or "[]")

    def read(self, path):
        path = Path(self.cwd or ".") / path
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return ""
            return path.read_text(errors="replace")
        except OSError:
            return ""


RUNNER = Runner()


# ---------------------------------------------------------------- graph

class Graph:
    """defs: [{name, kind, file, line, sig}]; files: [path]; edges: {src: {dst: weight}} over node ids,
    where defs are 0..len(defs)-1 and files follow."""

    def __init__(self, defs, files, edges, source, counts=None, referencing=None):
        self.defs, self.files, self.edges, self.source = defs, files, edges, source
        self.counts = counts or {source: len(files)}  # files per source, for the header
        self.referencing = referencing or {}  # definition name → files whose identifiers include it
        self.by_name = {}
        for i, d in enumerate(defs):
            self.by_name.setdefault(d["name"], []).append(i)
        self.by_file = {}
        for i, d in enumerate(defs):
            self.by_file.setdefault(d["file"], []).append(i)
        self.file_node = {f: len(defs) + i for i, f in enumerate(files)}

    def to_json(self):
        return {"source": self.source, "counts": self.counts, "defs": self.defs, "files": self.files,
                "edges": {str(s): t for s, t in self.edges.items()}, "referencing": self.referencing}

    def common(self, share=COMMON_SHARE):
        """The definition names more than `share` of the files use: too common to say which file an issue is
        about (row, mask, prop), so prose never seeds them. Explicit terms and backticks still may. Never
        fewer than three files: in a ten-file repository nothing is common."""
        limit = max(share * len(self.files), 2)
        return {name for name, n in self.referencing.items() if n > limit}


def _sig(text_lines, line):
    return text_lines[line - 1].strip() if 0 < line <= len(text_lines) else ""


def extract(files, languages, runner=RUNNER):
    """Per file: its definitions (tree-sitter kind) and the identifiers it contains, counted. This is what
    the cache holds, so a changed file is re-extracted alone."""
    files = [f for f in files if f.endswith(extensions(languages))]
    per_file = {f: {"defs": [], "idents": {}} for f in files}
    texts = {f: runner.read(f) for f in files}
    seen = set()
    for m in (runner.ast_grep(_rules(languages), files) if files else []):
        f, line = m["file"], m["range"]["start"]["line"] + 1
        if (f, line, m["text"]) in seen or f not in per_file:
            continue
        seen.add((f, line, m["text"]))
        per_file[f]["defs"].append({"name": m["text"], "kind": m["ruleId"].split(":", 1)[1], "line": line,
                                    "sig": _sig(texts[f].splitlines(), line)})
    for f in files:
        per_file[f]["defs"].sort(key=lambda d: d["line"])
        counts = per_file[f]["idents"]
        for ident in IDENT.findall(texts[f]):
            if not ident.startswith("__"):
                counts[ident] = counts.get(ident, 0) + 1
    return per_file


def assemble(per_file, scip=None):
    """The graph from extracted files and, per file a SCIP index covers, its resolved definitions and
    references in place of the extracted ones. From an extracted file, an edge runs from each definition (or
    the file) to every definition elsewhere that an identifier in the file names, SCIP-defined ones included;
    from a covered file, to the definitions its references resolve to, plus name matches into files the index
    does not cover for the identifiers it left unresolved there."""
    scip = scip or {}
    files = sorted(set(per_file) | set(scip))
    defs = [{**d, "file": f} for f in files for d in (scip[f] if f in scip else per_file[f])["defs"]]
    named = [f for f in files if f not in scip]
    source = "+".join(x for x, n in (("scip", len(scip)), ("ast-grep", len(named))) if n) or "ast-grep"
    g = Graph(defs, files, {}, source, {"scip": len(scip), "ast-grep": len(named)})
    referencing = {}
    for f in per_file:
        for name in per_file[f]["idents"]:
            if name in g.by_name:
                referencing[name] = referencing.get(name, 0) + 1
    g.referencing = referencing

    def edge(s, t, w):
        g.edges.setdefault(s, {})
        g.edges[s][t] = g.edges[s].get(t, 0) + w
    for f in files:
        if f not in per_file:
            continue
        referrers = g.by_file.get(f) or [g.file_node[f]]
        lang = language_of(f)
        # from a covered file, a name match only for an identifier the index left unresolved there, and only
        # into files it does not cover (e.g. Unity code): a `Count` Roslyn resolved needs nothing from ast-grep
        resolved = set(scip[f].get("resolved", ())) if f in scip else None
        for name, n in per_file[f]["idents"].items():
            if resolved is not None and name in resolved:
                continue
            # only within one language: a Python tool naming `text` does not call the C# `text`
            targets = [i for i in g.by_name.get(name, ()) if defs[i]["file"] != f and language_of(defs[i]["file"]) == lang
                       and (resolved is None or defs[i]["file"] not in scip)]
            if not targets:
                continue
            # sqrt: a file that calls a name forty times is not forty times as related; idf: a name every
            # file uses (get, items, key) says little about which file is related to which
            w = math.sqrt(n) * math.log(1 + len(files) / referencing[name]) / len(referrers) / len(targets)
            for s in referrers:
                for t in targets:
                    edge(s, t, w)
    node_of = {d["symbol"]: i for i, d in enumerate(defs) if "symbol" in d}
    for f in files:
        if f not in scip:
            continue
        referrers = g.by_file.get(f) or [g.file_node[f]]
        for symbol, n in scip[f]["refs"].items():
            t = node_of.get(symbol)
            if t is None or defs[t]["file"] == f:
                continue
            for s in referrers:
                edge(s, t, math.sqrt(n) / len(referrers))
    return g


def build_ast_grep(files, languages, runner=RUNNER):
    return assemble(extract(files, languages, runner))


# ---------------------------------------------------------------- SCIP

def _varint(buf, i):
    shift, out = 0, 0
    while True:
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if not b & 0x80:
            return out, i
        shift += 7


def _fields(buf):
    """(field number, value) for every field in a protobuf message; length-delimited values are bytes."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(buf, i)
        elif wire == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wire == 1:
            v, i = buf[i:i + 8], i + 8
        elif wire == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise WfError(f"index.scip: unsupported wire type {wire}")
        yield num, v


def _packed(buf):
    out, i = [], 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _occurrence(buf):
    rng, symbol, roles = None, "", 0
    for num, v in _fields(buf):
        if num == 1:
            rng = _packed(v) if isinstance(v, bytes) else [v]
        elif num == 2:
            symbol = v.decode()
        elif num == 3:
            roles = v
        elif num == 8:  # SingleLineRange {line=1}
            rng = [next((x for n, x in _fields(v) if n == 1), 0)]
        elif num == 9:  # MultiLineRange {start_line=1}
            rng = [next((x for n, x in _fields(v) if n == 1), 0)]
    return (rng[0] if rng else 0), symbol, roles


def _symbol_info(buf):
    symbol, kind, display, sig = "", 0, "", ""
    for num, v in _fields(buf):
        if num == 1:
            symbol = v.decode()
        elif num == 5:
            kind = v
        elif num == 6:
            display = v.decode()
        elif num == 7:
            sig = next((x.decode() for n, x in _fields(v) if n == 5), "")
    return symbol, kind, display, sig


SUFFIXES = {"/": "namespace", "#": "type", ".": "term", ":": "meta", "!": "macro"}


def descriptors(symbol):
    """[(name, suffix)] of a SCIP symbol, per the grammar in scip.proto: `<scheme> <manager> <package> <version>
    <descriptors>` (a space in a field is doubled). Suffixes: `/` namespace, `#` type, `.` term, `:` meta, `!`
    macro, `method` for `name(disambiguator).`, `[` type parameter, `(` parameter; a name in backticks is
    escaped. A local symbol (`local 3`) has none."""
    if symbol.startswith("local "):
        return []
    i, spaces = 0, 0
    while i < len(symbol) and spaces < 4:  # past the four header fields
        if symbol[i] == " ":
            if symbol[i + 1:i + 2] == " ":
                i += 2
                continue
            spaces += 1
        i += 1
    out, n = [], len(symbol)
    while i < n:
        if symbol[i] == "[" or symbol[i] == "(":  # [type parameter] or (parameter)
            close = "]" if symbol[i] == "[" else ")"
            j = symbol.find(close, i + 1)
            j = n if j < 0 else j
            out.append((symbol[i + 1:j].strip("`"), symbol[i]))
            i = j + 1
            continue
        if symbol[i] == "`":
            j, name = i + 1, ""
            while j < n:
                if symbol[j] == "`":
                    if symbol[j + 1:j + 2] == "`":
                        name, j = name + "`", j + 2
                        continue
                    break
                name, j = name + symbol[j], j + 1
            i = j + 1
        else:
            j = i
            while j < n and (symbol[j].isalnum() or symbol[j] in "_+-$"):
                j += 1
            name, i = symbol[i:j], j
        if i >= n:
            out.append((name, "?"))
            break
        c = symbol[i]
        if c == "(":  # method: name(disambiguator).
            j = symbol.find(").", i)
            i = n if j < 0 else j + 2
            out.append((name, "method"))
        elif c in SUFFIXES:
            out.append((name, SUFFIXES[c]))
            i += 1
        else:
            out.append((name, "?"))
            i += 1
    return out


def scip_kind(symbol, kind=0):
    """The label a SCIP symbol's definition gets, or None when it is not a node. The descriptor suffix decides
    (scip-dotnet and scip-python set no kind); the numeric kind refines the label when the indexer set one.
    Kept: types, methods, constructors, terms that are class members, namespaces as module. Skipped: locals,
    parameters, type parameters, meta descriptors (a Python module's `__init__:`), terms outside a type."""
    ds = descriptors(symbol)
    if not ds:
        return None
    name, suffix = ds[-1]
    parent = ds[-2][1] if len(ds) > 1 else None
    refined = SCIP_KINDS.get(kind)
    if suffix == "type":
        label = "type"
    elif suffix == "method":
        label = "ctor" if name in (".ctor", "<init>") else "method" if parent == "type" else "function"
    elif suffix == "term":
        label = "field" if parent == "type" else None
    elif suffix == "namespace":
        label = "module"
    else:
        label = None
    if label is None:
        return refined if refined and suffix not in ("[", "(") else None
    return refined or label


def symbol_name(symbol):
    """The last descriptor of a SCIP symbol, without its suffix: `…/Board#set_field().` → `set_field`."""
    ds = descriptors(symbol)
    if ds and ds[-1][0]:
        return ds[-1][0]
    tail = symbol.rstrip("#./:!)]")
    tail = re.split(r"[#/.:!]", tail)[-1] if tail else symbol
    return tail.split("(")[0].strip("`") or symbol


def _documents(buf):
    for num, v in _fields(buf):
        if num == 2:
            path, occs, infos = "", [], {}
            for dn, dv in _fields(v):
                if dn == 1:
                    path = dv.decode()
                elif dn == 2:
                    occs.append(_occurrence(dv))
                elif dn == 3:
                    s, kind, display, sig = _symbol_info(dv)
                    infos[s] = (kind, display, sig)
            yield path, occs, infos


def read_scip(index_paths, files, runner=RUNNER, warn=None):
    """Per document a SCIP index covers: its definitions ({name, kind, line, sig, symbol}) and the symbols it
    references, counted. Several indexes merge per document (definitions by symbol). Only documents among
    `files` count (all when empty). An index that yields no definitions is reported on `warn` (stderr) with
    its likely cause, rather than becoming a silent empty map."""
    if isinstance(index_paths, (str, Path)):
        index_paths = [index_paths]
    warn = warn or (lambda line: print(line, file=sys.stderr))
    wanted = set(files)
    out = {}
    for index_path in index_paths:
        try:
            buf = (Path(runner.cwd or ".") / index_path).read_bytes()
        except OSError as e:
            raise WfError(f"cannot read {index_path}: {e.strerror}") from None
        seen_docs, found = 0, 0
        for path, occs, infos in _documents(buf):
            if wanted and path not in wanted:
                continue
            seen_docs += 1
            doc = out.setdefault(path, {"defs": [], "refs": {}, "resolved": []})
            have = {d["symbol"] for d in doc["defs"]}
            lines = None
            refs = {}
            resolved = set(doc["resolved"])
            for line, symbol, roles in occs:
                if not symbol.startswith("local "):
                    resolved.add(symbol_name(symbol))  # the indexer knows what this name means here
                if not roles & SCIP_DEFINITION:
                    if not symbol.startswith("local "):
                        refs[symbol] = refs.get(symbol, 0) + 1
                    continue
                if symbol in have:
                    continue
                kind, display, sig = infos.get(symbol, (0, "", ""))
                label = scip_kind(symbol, kind)
                if label is None:
                    continue
                if lines is None:
                    lines = runner.read(path).splitlines()
                have.add(symbol)
                found += 1
                doc["defs"].append({"name": display or symbol_name(symbol), "kind": label, "line": line + 1,
                                    "sig": sig.splitlines()[0].strip() if sig else _sig(lines, line + 1),
                                    "symbol": symbol})
            for symbol, n in refs.items():
                doc["refs"][symbol] = max(doc["refs"].get(symbol, 0), n)
            doc["defs"].sort(key=lambda d: d["line"])
            doc["resolved"] = sorted(resolved)
        if not found:
            cause = ("no document in it is a tracked file here: an index written for another root?" if not seen_docs
                     else "no symbol in it has a definition role the map keeps")
            warn(f"wf map: {index_path} yields no definitions; {cause}")
    return out


def build_scip(index_path, files, runner=RUNNER, warn=None):
    """The graph from SCIP alone: definitions and resolved references. Locals and external symbols are not
    nodes."""
    return assemble({}, read_scip(index_path, files, runner, warn))


# ---------------------------------------------------------------- cache

def dirty_files(status_z):
    """Paths from `git status --porcelain -z`: `XY path\0`, and for a rename `XY new\0old\0`; both paths of a
    rename count, so the old name's cached entry is dropped too."""
    out, fields, expect_old = set(), status_z.split("\0"), False
    for field in fields:
        if expect_old:
            out.add(field)
            expect_old = False
        elif len(field) > 3:
            out.add(field[3:])
            expect_old = field[0] in "RC" or field[1] in "RC"
    return out


MAP_KEEP = 10  # the maps kept in the cache, by most recent use


def cached_graph(common, sha, languages, scip=None, refresh=False, runner=RUNNER):
    """The graph for the working tree: HEAD's extraction from the cache (built from the clean files when
    missing), with the files the working tree has changed re-extracted on top. SCIP indexes (`scip`, a path
    or a list), read as `wf index` left them, replace the extraction of every file they cover."""
    tracked = [f for f in runner.git("ls-files", "-z").split("\0") if f]
    dirty = {f for f in dirty_files(runner.git("status", "--porcelain", "-z")) if f.endswith(extensions(languages))}
    cache = Path(common) / "wf-map" / f"{sha}-{'-'.join(languages)}.json"
    per_file = None
    if cache.is_file() and not refresh:
        try:
            per_file = json.loads(cache.read_text())
        except (OSError, ValueError):
            per_file = None
        else:
            try:
                os.utime(cache)  # a map's mtime is its last use
            except OSError:
                pass
    if not isinstance(per_file, dict):
        per_file = extract([f for f in tracked if f not in dirty], languages, runner)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(per_file))
        prune(cache.parent, keep=MAP_KEEP, spare=(cache.name,))
    per_file = {f: v for f, v in per_file.items() if f not in dirty}
    if dirty:
        per_file.update(extract(sorted(f for f in dirty if (Path(runner.cwd or ".") / f).is_file()), languages, runner))
    # a file edited since `wf index` takes the fresh extraction: the index's lines no longer match it
    docs = {f: v for f, v in read_scip(scip, tracked, runner).items() if f not in dirty} if scip else None
    return assemble(per_file, docs)


# ---------------------------------------------------------------- rank

def seeds(g, terms, text="", common=COMMON_SHARE):
    """Node ids the seed names, by tier. A term is a definition name or a path (suffix match), weight 1. Text
    (an issue body, a diff) seeds in three tiers: identifiers and paths inside code fences and backticks
    (weight 3), then snake_case or camelCase tokens (1), then plain words that name a definition (0.3). A
    plain word more than `common` of the files use (`g.common`) is dropped: it says nothing about which file
    the issue is about. An identifier-shaped token is kept whatever its share: writing `gh_get` is a choice. Returns (ids, unmatched terms, tiers), where tiers counts the matched
    seeds per tier ({term, code, ident, word}) plus the names the stoplist dropped."""
    ids, unmatched = {}, []
    tiers = {"term": 0, "code": 0, "ident": 0, "word": 0, "dropped": []}
    files_by_suffix = {}
    for f in g.files:
        parts = f.split("/")
        for i in range(len(parts)):
            files_by_suffix.setdefault("/".join(parts[i:]), []).append(f)
    def add_file(f, w):
        for i in g.by_file.get(f, []):
            ids[i] = ids.get(i, 0) + w / max(1, len(g.by_file.get(f, [])))
        if f not in g.by_file:
            ids[g.file_node[f]] = ids.get(g.file_node[f], 0) + w
    def add_name(name, w):
        """A named definition, and half as much again spread over the rest of its file (a class's methods)."""
        for i in g.by_name.get(name, ()):
            ids[i] = ids.get(i, 0) + w / len(g.by_name[name])
            add_file(g.defs[i]["file"], w / 2 / len(g.by_name[name]))
    def add_path(p, w):
        files = files_by_suffix.get(p.lstrip("./"), [])
        for f in files:
            add_file(f, w)
        return bool(files)
    for term in terms:
        t = term.strip().strip("`'\"")
        if t in g.by_name:
            add_name(t, TIER["term"])
            tiers["term"] += 1
        elif t.rstrip("/") in files_by_suffix:
            add_path(t.rstrip("/"), TIER["term"])
            tiers["term"] += 1
        else:
            unmatched.append(term)
    if text:
        stop = g.common(common)
        seen = set()
        # tier 1: what the author marked as code (fences, backticks) and paths: the names and files they mean
        spans = FENCED.findall(text) + BACKTICKED.findall(FENCED.sub(" ", text))
        for span in spans:
            for p in set(PATHISH.findall(span)):
                if add_path(p, TIER["code"]) and ("path", p) not in seen:
                    seen.add(("path", p))
                    tiers["code"] += 1
            for ident in set(IDENT.findall(span)) - {n for _, n in seen}:
                if ident in g.by_name:
                    add_name(ident, TIER["code"])
                    seen.add(("name", ident))
                    tiers["code"] += 1
        for p in set(PATHISH.findall(text)):
            if ("path", p) not in seen and add_path(p, TIER["code"] * 2 / 3):
                seen.add(("path", p))
                tiers["code"] += 1
        # tier 2: identifier-shaped tokens (an author who writes gh_get means it, common or not);
        # tier 3: plain words past the stoplist
        for ident in sorted(set(IDENT.findall(text))):
            if ("name", ident) in seen or ident not in g.by_name:
                continue
            tier = "ident" if CODELIKE.match(ident) else "word"
            if tier == "word" and ident in stop:
                tiers["dropped"].append(ident)
                continue
            add_name(ident, TIER[tier])
            tiers[tier] += 1
    return ids, unmatched, tiers


def seeding(tiers):
    """(mode, confidence) of a seed: identifier-seeded when a term, a code span or an identifier-shaped
    token matched; prose-seeded, with low confidence, when only plain words did (a word that is also a
    function's name may be a coincidence); none when nothing matched."""
    strong = tiers["term"] + tiers["code"] + tiers["ident"]
    if not strong and not tiers["word"]:
        return "none", "none"
    return ("identifier", "high") if strong else ("prose", "low")


def pagerank(g, personal=None):
    """Personalized PageRank: the restart vector is the seed (uniform when there is none); dangling mass
    restarts too. Stops when the ranks move less than TOLERANCE in total."""
    n = len(g.defs) + len(g.files)
    if n == 0:
        return []
    if personal:
        total = sum(personal.values())
        p = [personal.get(i, 0) / total for i in range(n)]
    else:
        p = [1 / n] * n
    out = {}
    for s, targets in g.edges.items():
        total = sum(targets.values())
        out[s] = [(t, w / total) for t, w in targets.items()]
    rank = list(p)
    for _ in range(ITERATIONS):
        new = [0.0] * n
        dangling = 0.0
        for s in range(n):
            r = rank[s]
            if s in out:
                for t, share in out[s]:
                    new[t] += r * share
            else:
                dangling += r
        new = [DAMPING * (new[i] + dangling * p[i]) + (1 - DAMPING) * p[i] for i in range(n)]
        delta = sum(abs(a - b) for a, b in zip(new, rank))
        rank = new
        if delta < TOLERANCE:
            break
    return rank


def tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def render(g, order, budget):
    """The top-ranked definitions grouped by file, as many as fit `budget` tokens; returns (text, count)."""
    def layout(k):
        chosen = set(order[:k])
        lines = []
        for f in g.files:
            ids = [i for i in g.by_file.get(f, []) if i in chosen]
            if not ids:
                continue
            lines.append(f)
            lines.extend(f"  {g.defs[i]['line']:>4} {g.defs[i]['sig'] or g.defs[i]['kind'] + ' ' + g.defs[i]['name']}"
                         for i in ids)
        return "\n".join(lines)
    lo, hi = 0, len(order)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tokens(layout(mid)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return layout(lo), lo


def code_map(g, terms=(), text="", budget=2000, common=COMMON_SHARE):
    """Rank the graph around the seed and render it; returns (text, {count, tokens, unmatched, seeded, seeds,
    mode, confidence, tiers, …})."""
    personal, unmatched, tiers = seeds(g, terms, text, common)
    mode, confidence = seeding(tiers)
    rank = pagerank(g, personal or None)
    order = sorted(range(len(g.defs)), key=lambda i: (-rank[i], g.defs[i]["file"], g.defs[i]["line"]))
    order = [i for i in order if rank[i] > 0] if personal else order
    body, count = render(g, order, budget)
    return body, {"count": count, "tokens": tokens(body), "unmatched": unmatched, "seeded": bool(personal),
                  "seeds": sum(tiers[k] for k in ("term", "code", "ident", "word")), "mode": mode,
                  "confidence": confidence, "tiers": tiers, "defs": len(g.defs), "source": g.source, "counts": g.counts}
