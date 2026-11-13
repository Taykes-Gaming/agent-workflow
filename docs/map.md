# The code map: `wf map` and `wf index`

Why an agent in this workflow spends turns, and what `wf map` replaces.

## The problem

[docs/dispatch.md](dispatch.md) measured implementers at 80–324 turns per
issue, each turn re-reading the starting context. A large share of the early
turns is the agent finding its footing: which file defines the symbol the
issue names, who calls it, where the tests are. Reviewers do the same per
lane. Those are graph questions, answered today by grep and by reading
whole files.

The alternatives considered, and why this one:

- **A repository map in every prompt** (Aider's repo map): the right idea,
  the wrong delivery for wf. A global map is paid on every turn, and wf's
  issues already name the symbols they are about, so the map should be the
  neighbourhood of the task, computed per call.
- **LSP behind MCP** (Serena): compiler-accurate, but the tool schemas were
  most of the 54.9k starting context dispatch.md measured, and a language
  server boots per session. A command called over Bash carries no schema.
- **SCIP**: the same compiler intelligence (scip-dotnet is Roslyn,
  scip-python is Pyright's analyser) written once per commit and queried
  offline. The right source when an index exists; not a requirement, since
  tree-sitter definitions plus name matching rank well enough to start.

## What `wf map` does

```
$ wf map Board --budget 400
# wf map: 32 of 453 definitions from 1 seed, ~389 tokens, identifier-seeded, source ast-grep
src/wf/board.py
    39 class Board:
    49 def fields(self):
    54 def field(self, key):
    74 def items(self):
src/wf/gh.py
    19 class WfError(Exception):
    32 def get(self, path):
…
```

1. **Extract.** Every tracked source file (`[map].languages`, default all
   of python, csharp, typescript, tsx, javascript, go) is parsed once with
   ast-grep for its definitions (classes, functions, methods, properties,
   … per language) and scanned for the identifiers it contains. The
   extraction of the clean files is cached per commit and language set in
   `<git-common-dir>/wf-map/<sha>-<languages>.json`; files the working tree
   has changed are re-extracted alone on each run and never cached. The cache keeps the 10 most recently used
   maps.
   A Unity project with 365 C# files and 5,100 definitions: 2.5 s cold, 1 s warm.
2. **Graph.** A node per definition and per file; an edge from each
   definition in a file (or the file, when it defines nothing) to every
   definition *in another file of the same language* that an identifier in
   the file names. Weight: `sqrt(count) × log(1 + files / files using the
   name)`, so forty calls are not forty times the relation, and a name every
   file uses (`get`, `items`) says little.
3. **Seed.** Terms on the command line are definition names or paths (suffix
   match), weight 1. `--text FILE` seeds from an issue body or diff in three
   tiers: identifiers and paths inside code fences and backticks (weight 3,
   what the author marked as code), then snake_case or camelCase tokens (1),
   then plain words that name a definition (0.3). A **stoplist computed from
   the index** drops a plain word when more than `[map].common` of the files
   use it (default 5%, never fewer than three files): `row`, `mask`, `prop`
   name a function somewhere in most repositories, and an issue that uses
   the word is not about that function. A backticked `row`, or a
   snake_case or camelCase token however common, is the author's choice and
   still seeds. The dropped names are listed on
   the first line (`too common to seed: …`). `--changed` seeds the files
   changed against `origin/<base>` (`--base` overrides). A named definition
   also lifts the rest of its file a little, so a class brings its methods.
   The edge weights' rarity term (step 2) is unchanged, so a common name
   that does get in still carries little rank.
4. **Rank.** Personalized PageRank (damping 0.85): the seed is the restart
   vector, so rank flows from the seed to what it uses and what uses it. No
   seed means a uniform restart, which is the repository's most referenced
   definitions.
5. **Render.** The ranked definitions grouped by file with their signature
   line, as many as fit the budget (`--budget`, `[map].budget`, 2000; tokens
   estimated at 3.5 characters each), found by binary search over the list.
   The first line says what was mapped, from how many seeds, how it was
   seeded and which seeds matched nothing: `identifier-seeded` when a term,
   a code span or an identifier-shaped token matched; `prose-seeded (low
   confidence)` when only plain words did, so the reader (and the dispatch
   log, which echoes this line) knows the map may be a coincidence of
   names; `unseeded (most referenced)` when nothing matched.

The map is an aid, never a blocker: where it cannot be built (no ast-grep,
not a checkout) the callers below carry on without it.

## Where it is used

- **Implementers.** `wf dispatch` builds the map around the issue's title
  and body in the new worktree and appends it to the instruction file as a
  `## Map` section; `docs/issue-workflow.md` §1 tells the
  implementer to run `wf map` itself at pickup.
- **Reviews.** `review_prep.py` writes `map.md` around the changed code
  files (1,500 tokens) and names it in `prep.json` as `map`; the lane prompt
  reads it before searching. It is skipped, with the reason in
  `map_skipped`, when the checkout is not at the PR head, since the map's
  lines would belong to another commit. That is the symbol slice each lane used to re-derive.
- **Architects.** When writing an issue, `wf map <subsystem path>` is the
  view of what exists, so the body can cite real files and symbols.

## Resolved references: `wf index`

Name matching has a known failure: `Length`, `One`, `Command` exist in many
places, and the ast-grep source joins them all. A SCIP index resolves each
reference to one definition, so the graph has only real edges.

```toml
[map]
env = { DOTNET_ROLL_FORWARD = "Major" }   # added to every index command
index = [
  { run = "scip-dotnet index", out = "index.scip" },
  { run = "scip-python index tools --output tools/index.scip", out = "tools/index.scip" },
]
# scip = ["index.scip"]   # indexes to read besides each entry's out; a path or a list
```

`wf index` runs the commands in sequence from the repository's top level,
keeps each command's output in `<git-dir>/wf-index/<step>.log` (one line per
command: `ok` or `FAIL`, the exit status, the log, and for a failure its first
failure line), reads every index back,
and prints one line per index with the files and
definitions it covers, then how many tracked source files all of them cover
together, so a gap (a folder no indexer reached) shows. Each entry names its
own `out`, so a second indexer does not overwrite the first one's
`index.scip`. The index is read straight from the protobuf wire format
(`wf.codemap.read_scip`), so no scip CLI is needed.

**Per file, not either-or.** `wf map` always builds the ast-grep extraction;
then every file an index covers takes its definitions and outgoing edges
from the index, and every other file keeps its ast-grep definitions and
name-matched edges. Nodes are per definition, so a name match from an
ast-grep file can still reach a SCIP-defined node, and a covered file also
name-matches into files the index does not cover (a call into Unity code).
A file edited since `wf index` takes the fresh ast-grep extraction, since
the index's line numbers no longer match it. The header says how the
files split: `source scip (120 files) + ast-grep (245 files)`. An index that
replaced ast-grep wholesale lost every file it did not cover; that was the
first trial on a Unity project.

**Kinds come from the symbol.** scip-dotnet and scip-python set no
`SymbolInformation.kind`, so the reader takes a definition's kind from the
last descriptor of its symbol (`#` type, `name().` method or, outside a type,
function, `` `.ctor`(). `` or `` `<init>`(). `` constructor, `.` a member
field when its parent is a type, `/` namespace as module) and lets the
numeric kind refine it when set. Parameters, type parameters, meta
descriptors and locals are not nodes. An index that yields no definitions
warns on stderr with the likely cause (an index written for another root,
or no symbol with a definition role) instead of rendering an empty map.
`tests/fixtures/` holds indexes scip-python and scip-dotnet wrote for
two-file projects, which the reader is tested against.

**Unity.** A Unity project has no `.csproj` files until the editor writes
them, and scip-dotnet needs a project or solution to load, so Unity code
stays on the ast-grep path for now; with the per-file merge, the rest of the
repository still gets resolved references. Unity can write the csproj set
headless (`-batchmode -quit -executeMethod` calling the editor's project
sync, as an IDE package does on open). Once a step like that exists,
`[map].index` can point scip-dotnet at those files, e.g.
`{ run = "scip-dotnet index MyGame.sln", out = "index.scip" }`. Not built
here.

## What to measure

The metrics `wf merged` records already carry turns and kTokens per issue.
The claim to check over the next issues is fewer turns before the pickup
comment and fewer `Grep`/`Read` calls per lane, at the same findings.
