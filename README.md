# agent-workflow

Tooling for running a software project with Claude Code agents against a
GitHub Projects v2 board:

- **`wf`**, a CLI that moves issues across the board, records per-issue
  agent metrics (model, review findings, fix rounds, tokens, turns) and
  reports on them. It works through the **REST** API, which has a separate
  rate budget from GraphQL; `wf` makes one GraphQL call, as a fallback.
  `wf` itself always uses REST; the `api` setting decides what the agents
  and reviewers may use. Every action prints one line.
- **`wf review`** and the **`wf-review`** plugin: a pull-request review gate
  that runs in a fresh session, so the reviewer never sees the author's
  context.
- **The `wf-setup` plugin**: a `/wf-setup:setup` skill that sets the
  workflow up in a repository (board, config, docs, agent definition,
  GitHub templates), the templates it uses, and `/wf-setup:process`, the
  whole loop on one screen (roles, steps, the `wf` command for each) so an
  agent reads forty lines instead of the two workflow docs.
- [Notes on dispatching](docs/dispatch.md) implementers as low-context
  subagents, and **`wf dispatch --engine opencode`**, the OpenCode
  implementer seat.
- **`wf map`**, a ranked map of the definitions around a symbol, a path or
  an issue body, cut to a token budget, so agents start from the code the
  task touches instead of searching for it ([docs/map.md](docs/map.md)).

You can use any part on its own. The workflow assumed throughout: an
*architect* session writes issues and dispatches them; an *implementer*
agent builds one issue in its own worktree and opens a PR; a review from a
separate session gates the merge; `wf` keeps the board and the metrics up to
date.

## Quick start

From the repository you want to run the workflow in:

```sh
uv tool install git+https://github.com/Taykes-Gaming/agent-workflow
claude plugin marketplace add Taykes-Gaming/agent-workflow
claude plugin install wf-setup@agent-workflow
claude plugin install wf-review@agent-workflow
```

Then, in a Claude Code session in that repository, run `/wf-setup:setup` (or
ask Claude to set up agent-workflow). It checks `gh`, asks what it cannot
infer, creates the board and labels after you confirm, and writes the files
on a new branch. [Adopting it in a project](#adopting-it-in-a-project) lists
the same steps for doing it by hand. With `wf-setup` enabled, its hooks
record each implementer's and session's spend for `wf merged` and `wf spend`
([Recording metrics](#recording-metrics)).

## Install

Python ≥ 3.11, standard library only. `gh` must be installed and logged in
with the `project` and `repo` scopes; `wf` calls GitHub only through
`gh api`, so `gh` owns authentication.

```sh
uv tool install git+https://github.com/Taykes-Gaming/agent-workflow
```

Upgrade with `uv tool upgrade wf`. From a checkout, `python -m unittest` runs
the tests.

## The board

`wf` works with any Projects v2 board (user- or org-owned) that has these
fields. You choose the names and map them in the config.
[`board-setup.md`](plugins/wf-setup/skills/setup/board-setup.md) creates
them with `gh`, including the one GraphQL call that sets Status's options.

| Field (config key) | Type | Options / values |
|---|---|---|
| `status` | single select | Six options, one per verb: backlog, ready, in progress, blocked, in review, done. Any names. |
| `size` | single select | `XS` `S` `M` `L` `XL` |
| `model` | single select | One option per model you dispatch with (e.g. `opus`, `sonnet`). |
| `findings`, `rounds`, `ktokens` | number | Review findings, fix rounds, agent kTokens per issue. |
| `turns` (optional) | number | Agent turns per issue. |
| `cache_ktokens` (optional) | number | Agent cache reads per issue, in thousands: the context re-read each turn, kept apart from kTokens. From the ledger. |
| `review_ktokens` (optional) | number | kTokens the issue's `wf review` runs spent, kept apart from the implementer's. From the ledger. |
| `deploy` (optional) | single select | `n/a` `Pending` `Staging` `Production` |
| `release` (optional) | text | Free text, e.g. a version or tag. |

If a repo lacks one of the labels named in `[labels]` (`backlog`,
`effort:XS`…, `deploy:pending`…), GitHub creates it with a default colour
the first time `wf` adds it. To pick the colours, create the labels first.

## Config

`wf` reads `.workflow.toml`, found by walking up from the current directory,
or the file named by `WF_CONFIG`.

```toml
api = "rest"            # rest | graphql: which GitHub API the agents may use (default rest)

[board]
owner = "your-org"      # login
owner_type = "org"      # user | org  → /users/… or /orgs/…
number = 1              # from the board URL: …/projects/<number>

[repos]                 # alias → owner/name; refs are <alias>#<n>
app = "your-org/app"
api = "your-org/api"

[fields]                # board field NAMES; ids are looked up at runtime
status = "Status"
size = "Size"
model = "Model"
findings = "Review Findings"
rounds = "Fix Rounds"
ktokens = "Agent kTokens"
turns = "Turns"         # optional
cache_ktokens = "Cache kTokens"    # optional
review_ktokens = "Review kTokens"  # optional
deploy = "Deploy"       # optional
release = "Release"     # optional

[status]                # verb → option name, exactly as spelled on the board
backlog = "Backlog"
ready = "Ready"
progress = "In progress"
blocked = "Blocked"
review = "In review"
done = "Done"

[labels]                # board ↔ label mirror
backlog = "backlog"
size_prefix = "effort:"
deploy_prefix = "deploy:"
fix_prefix = "fix-"     # optional: wf board warns on a fix-N stream past its cap

# [review.tiers]        # optional: the wf-review lanes per tier (see Custom lanes); each lane adds spend
# light = ["correctness", "reuse"]
# standard = ["correctness", "spec?", "reuse", "history?"]
# deep = ["correctness", "spec?", "reuse", "history"]
# fix = ["correctness"]
```

Commit `.workflow.toml` at the root of the repo the architect works from. A
board that spans several repos needs the file in only one of them; set
`WF_CONFIG` to use it from another checkout.

| Key | Required | Meaning |
|---|---|---|
| `api` | no | `rest` (default) or `graphql`: which GitHub API the agents may use ([REST or GraphQL](#rest-or-graphql)). |
| `board.owner`, `board.owner_type`, `board.number` | yes | The board: `/users/<owner>/projectsV2/<n>` or `/orgs/<owner>/projectsV2/<n>`. |
| `repos.<alias>` | no | Short names for refs. `owner/name#<n>` works without one. |
| `fields.status` … `fields.ktokens` | yes | Names of the board's fields. |
| `fields.turns`, `fields.cache_ktokens`, `fields.review_ktokens`, `fields.deploy`, `fields.release` | no | Without `deploy`, the deploy commands fail and `merged` sets no Deploy. |
| `status.<verb>` | all six | The Status option each verb selects. |
| `gate.steps`, `gate.when`, `gate.base` | no | The local gate `wf gate` runs. A step is a command or `{ run, name, xml }`; `when` maps a glob of changed files to steps run only then; `base` is the branch changes are measured against (default `opencode.base`, then `dev`). |
| `map.budget`, `map.languages`, `map.scip`, `map.index`, `map.base`, `map.common` | no | `wf map` and `wf index` ([docs/map.md](docs/map.md)): the token budget per map (2000), the languages extracted, the SCIP index to rank over once `wf index` has run the `index` commands, the branch `--changed` measures against, and the share of files (0.05) past which a name is too common for prose to seed. |
| `wait.<name>.*` | no | A `wf wait` preset: `until` (required), `fail`, `timeout`, `context`, and `run` or `file`. |
| `opencode.*` | no | `wf dispatch --engine opencode` defaults: `model`, `variant`, `base`, `contract` ([docs/dispatch.md](docs/dispatch.md#the-opencode-seat)). |
| `labels.*` | yes | The label kept in step with Backlog, and the prefixes mirroring Size (`effort:L`) and Deploy (`deploy:pending`). |
| `labels.record` | no | The sprint-record label `wf spend` requires and `wf report` prints as the orchestrator row (default `sprint-record`). |
| `labels.fix_prefix` | no | The prefix of fix-stream labels (`fix-4b`); `wf board` warns when a stream with an open issue passes 10 issues or 7 days since its first issue. |

Field and option **ids** are never written down: `wf` reads them from
`GET …/projectsV2/<n>/fields` once per run, because rewriting a single-select
field's options regenerates every option id. A name that is not on the board
fails with the list of valid ones. Deploy options are named `n/a`, `Pending`,
`Staging` and `Production`; sizes are `XS` `S` `M` `L` `XL`.

### REST or GraphQL

The top-level `api` key picks which GitHub API the agents and reviewers may
use: `"rest"` (the default) or `"graphql"`. It is one switch, with no
per-surface overrides, and it is read by the tools themselves, so it applies
whether or not `wf` launched them.

- **`rest`** keeps the GraphQL bans: it is the default because the GraphQL
  rate budget is shared across sessions, and agents reading pull requests
  with `gh pr view` can exhaust it.
- **`graphql`** only lifts the read-only bans: `wf review --engine opencode`
  lets its agents run `gh pr view`, `gh pr diff` and `gh issue view` (the
  `gh api` write denials stay), and the `wf-review` plugin and the review
  prompts do the same (`gh pr view`, `gh pr diff`, `gh issue view`, never raw
  `gh api graphql`). `--web`/`-w` on `gh pr view` and `gh issue view` is
  denied by `wf review` in both engines and both modes. `wf` gets no GraphQL client; it stays
  REST, with its one `add_item` fallback, in both modes.

Two calls stay GraphQL in both modes, because the tool that makes them is not
ours to change: the setup call that sets Status's options (`gh` has no REST
route for it), and Claude Code's built-in `/code-review`, which the Claude
engine of `wf review` runs and which reads the PR with `gh pr view` and `gh pr
diff`. The Claude engine's tool lists do not change with `api`.

A third exception runs the other way: raw `gh api graphql` stays denied for
review agents in both modes, because a glob cannot tell a query from a
mutation (shell quoting or `$(<file)` defeats a `*mutation*` deny), so
allowing it would let a PR steer a review agent into writing.

Some GitHub Enterprise Server instances lack REST routes, so `rest` mode may
fail there; set `api = "graphql"` on those.

## Refs

`<alias>#<n>` (`app#157`) or `owner/name#<n>`
(`your-org/app#157`). Several refs are separate arguments; a single
argument is never split. Anything else is a usage error.

An issue not yet on the board is added to it (`POST …/items` with the issue's
id) before the write.

The board's item listing lags an add by up to ~11 minutes, and adding an
issue that is already on the board fails with `422 Content already exists in
this project`. On exactly that error, `wf` asks the issue for its project
items with one GraphQL query and uses the item on the configured board. This
is `wf`'s only GraphQL call, in both `api` modes. The GraphQL budget is shared across sessions,
so it is used only on this path.

## Commands

Every command takes `--json` (one JSON object per action on stdout) and
`--quiet` (errors only).

| Command | Does |
|---|---|
| `wf move <ref>… <verb>` | Sets Status (`backlog` `ready` `progress` `blocked` `review` `done`); adds the `backlog` label for `backlog`, removes it otherwise. Moving to Ready or In progress refuses an issue with no milestone, before any write. |
| `wf merged <ref> --findings N --rounds N [--model M] [--ktokens N] [--cache-ktokens N] [--turns N] [--deploy na\|pending] [--pr REF]` | Refuses unless a merged PR references the issue (`--pr` names it; otherwise the issue's timeline is searched, preferring a PR whose body closes the issue over one that only mentions it). Sums the spend ledger of the PR's head branch (see [Recording metrics](#recording-metrics)): Model, kTokens, and Cache kTokens and Turns when configured, from its agents; Review kTokens from its `wf review` runs. Flags given by hand win; with no ledger it refuses unless `--model` and `--ktokens` are given. Then sets the fields, Status Done (dropping `backlog`), Deploy — `pending` by default when a deploy field is configured — with its `deploy:*` label, and closes the issue as completed: `Closes #n` is honoured only on a merge to the default branch. |
| `wf deploy <ref> <na\|pending\|staging\|production> [--release TEXT]` | Sets Deploy and its `deploy:*` label, and Release when given. Fails if `deploy` is not configured. |
| `wf size <ref> <XS\|S\|M\|L\|XL>` | Sets Size and the matching `effort:*` label. |
| `wf fields [--create]` | The configured `[fields]` the board lacks, and a Status whose options miss a `[status]` name. `--create` creates each missing field over REST with the type and options wf expects (Model starts with `opus`, `sonnet`, `haiku`, `fable`); it never alters an existing field, since rewriting a single-select's options regenerates every option id and blanks the column. Use it for a new board and after an upgrade that adds fields. |
| `wf board [--check]` | First a drift line — items Done but open, and closed but not Done; `drift: none` is the normal — any fix stream past its cap (`labels.fix_prefix`), and, when `cache_ktokens` or `review_ktokens` is not configured, a warning with the `[fields]` entries to add and `wf fields --create`. Then open items in Status order, then (when Deploy is configured) Done items not live (Deploy neither Production nor n/a): `<ref> <size> <status> <title>`, titles cut to 60 characters, at most 40 lines then `… and N more`. `--check` exits 1 on drift or a stream past its cap; the fields warning never fails it. |
| `wf report <label> [--model M]` | The sprint metrics table in markdown, one row per Model, over items carrying the label: Slices, Size mix, Review findings, Fix rounds, Agent kTokens, kTokens / slice, Turns, and Cache kTokens and Review kTokens when configured. The sprint record, if it carries the label, is a last `orchestrator` row. `--model` keeps one row and drops it. |
| `wf spend <record-ref> [--since TIME]` | The orchestrator's spend onto the sprint-record issue: the `session` lines of the base branch's ledger (`gate.base`, `opencode.base`, then `dev`), each measured from the record's creation (or `--since`) to its close or now. Sets Agent kTokens, and Cache kTokens and Turns when configured; never Model. Recomputed each run, so it never counts twice. Refuses an issue without the record label. |
| `wf hook` | Claude Code's SubagentStop and Stop hooks (the `wf-setup` plugin runs it): reads the event on stdin and appends an `agent` or `session` line to the ledger. Prints nothing and always exits 0. Needs no config or network. |
| `wf deploy-report [--all]` | Done items whose Deploy is not Production or n/a — merged work that is not live. Prints the first 40 and `… and N more` (as `wf board` does); `--all` prints every one, and `--json` always has one record per item. |
| `wf dispatch <ref> --engine opencode [--model M] [--variant V] [--resume [--message TEXT]]` | Runs the OpenCode implementer seat on an issue in its own worktree, logs it under `.git/wf-dispatch/<n>/`, exits with opencode's status and prints the `wf merged` flags ([docs/dispatch.md](docs/dispatch.md#the-opencode-seat)). |
| `wf gate [--keep-going] [--all] [--base B] [--only-failures] [--watch [GLOB…]]` | Runs the `[gate]` steps from the repository's top level and prints one line per step: `ok`, `skip`, or `FAIL` with the first failure line and the log path. Full output per step is kept in `<git-dir>/wf-gate/`. Stops at the first failure unless `--keep-going`; `--all` runs `[gate.when]` steps whatever changed. A step with `xml` passes only if that NUnit 3 file exists afterwards with no failed test. `--only-failures` prints only the `FAIL` lines and the summary. `--watch` keeps running after the first run: it polls the tracked and untracked files (only those matching the globs, when given) about once a second, waits for a quiet second after a change, reruns the gate and prints only the steps that flipped plus one summary such as `gate: 1 failed (lint flipped to FAIL)` or `gate: green (unchanged)`; nothing is printed while nothing changes. Ctrl-C ends it with the last run's exit status. Exits 1 on any failure. Needs no network. |
| `wf checkpoint [TEXT…] [--clear]` | The current branch's progress note, kept in `<git-common-dir>/wf-checkpoint/<branch>.md`: appends a timestamped line, prints the last 20 lines of the note with no text, after a first line `… N earlier lines (--all)` when it cut some (`--all` prints it whole; `--json` has the same cap and a `more` record), or empties it. `wf dispatch --resume` hands its last 10 lines to the session. Needs no config or network. |
| `wf map [SEED…] [--text FILE] [--changed [--base REF]] [--budget N] [--refresh]` | The definitions most related to the seed (symbol names, paths, the identifiers and paths in a file, or the files changed against a branch), ranked by personalized PageRank over the symbol graph and cut to the token budget, grouped by file with line and signature. Prose seeds in tiers (code spans, then identifier-shaped tokens, then plain words, those past a stoplist of names most files use), and the first line says whether the map is identifier- or prose-seeded; agents call it over Bash instead of searching; `wf dispatch` and `wf review` add it to their packs ([docs/map.md](docs/map.md)). Needs ast-grep; files a `[map].scip` index covers use its resolved references instead; no config or network. |
| `wf index` | Runs the `[map].index` commands (SCIP indexers, each with its own `out`, `[map].env` added) so `wf map` ranks over resolved references rather than name matches in the files they cover, sends each command's output to `<git-dir>/wf-index/<step>.log` and prints one line per command (`ok` or `FAIL`, the exit status, the log path, and for a failure its first failure line, also `detail` in `--json`), then the files and definitions each index covers. |
| `wf wait [PRESET] (--file PATH \| --run CMD \| --resume) --until RE [--fail RE] [--timeout DUR] [--context N]` | Blocks until a log matches `--until` and prints only the matching line, `--context` lines around it (default 2) and the log's path; exit 0. A `--fail` match (default: the gate's failure words) exits 1 the same way; the `--run` command dying first exits 1 with its status and first failure line; `--timeout` (default 20m) exits 124 with the log's last line. `--run` starts the command in its own session with its output under `<git-dir>/wf-wait/<n>.log`, and leaves it running after a match; `--resume` re-attaches to the last one after a dropped session. A `[wait.<name>]` preset supplies any of the flags. Needs no network. |
| `wf logread PATH [--match RE] [--exclude RE] [--context N] [--last N] [--raw] [--xml]` | A log cut to what an agent acts on: built-in noise (Unity's batch chatter) dropped, runs of one line collapsed to `(×N)`, a stack trace already shown collapsed to its first line, the last 200 lines by default, and a final line saying how much was hidden. A `.xml` path (or `--xml`) is NUnit 3 or JUnit results: the tally and one block per failed case, exit 1 if any failed. Needs no config or network. |
| `wf usage <agent-id>… [--edits REV]`, `wf usage --sessions [--since TIME] [--until TIME]` | The diagnostic, and the fallback without the ledger: an implementer subagent's model, turns and kTokens from its Claude Code transcript, and the matching `wf merged` flags; with `--edits REV`, its file reads outside what changed since `REV` in this checkout. `--sessions` sums every main session started in this checkout instead. Needs no config or network. |
| `wf rebuild [--dry-run]` | Rewrites Status, Size and Deploy from issue state and labels: closed → Done, open with `backlog` → Backlog, other open → Ready; `effort:*` → Size; `deploy:*` → Deploy. Writes only fields that differ, one PATCH per item. Use after a field's options were rewritten. In progress / Blocked / In review are lost and re-set by hand. |

## What `wf` keeps under `.git`

`wf` writes working files under the git common dir and prunes its own folders as it writes, silently: `wf-map/`
keeps the 10 most recently used maps; `wf-dispatch/` drops run folders untouched for 14 days (never the one being
dispatched); `wf-review/` drops review files untouched for 30 days (never the PR being reviewed, so `--since last`
on a PR idle for a month needs `--since <sha>`). The spend ledger (`wf-ledger/`) is never pruned.

## Recording metrics

The board's metric fields are filled at merge with `wf merged`, which reads
them from the **spend ledger**: `<git-common-dir>/wf-ledger/<branch>.jsonl`,
one JSON line per event, written as the work happens.

| Line | Written by | When |
|---|---|---|
| `agent` | `wf hook` (SubagentStop), `wf dispatch` | An implementer stops (it is measured from its transcript there and then), or an OpenCode run ends. Read-only agent types (Explore, Plan, …) are skipped; a fix round stops the same agent again, and its last line wins. |
| `session` | `wf hook` (Stop) | A main session stops a turn. Measured later, by `wf spend`. |
| `review` | `wf review` | Every run, failed ones too, on the PR's head branch. Its `tokens` (input + output + cache writes) and `cache_read` are the two numbers the run prints as `(Nk, +Mk cache)`. |

The hooks come with the `wf-setup` plugin (`plugins/wf-setup/hooks/hooks.json`):
enable it and they are on, provided `wf` is on the PATH Claude Code starts
hooks with (`uv tool install`, `pipx install`). A line goes to the branch
checked out where the work ran: the agent's own worktree, as its transcript
records it, else the session's directory.

Each number on the board has one source:

| Field | Source |
|---|---|
| Model | The `agent` lines of the PR's head branch; `--model` when they name more than one. The OpenCode seat records as `opencode-<family>` (e.g. `opencode-opus`); a full id such as `opencode-anthropic/claude-opus-5-5` is accepted as `--model` and recorded as `opencode-opus`. |
| Agent kTokens, Cache kTokens, Turns | Summed over the same `agent` lines. kTokens is input + output + cache writes; cache reads are Cache kTokens. |
| Review kTokens | The branch's `review` lines for the PR, kept apart so a model's kTokens are its own work. |
| Review findings (`--findings`) | Real findings the `wf review` gate raised on the PR, fixed or not; one declined in writing as wrong does not count. |
| Fix rounds (`--rounds`) | Times the implementer was sent back after opening the PR; 0 when the first PR merged. |

`--model`, `--ktokens`, `--cache-ktokens` and `--turns` given by hand win
over the ledger. A checkout without the hooks passes `--model` and
`--ktokens` by hand, from `wf usage <agent-id>…`; with no ledger and no
`--ktokens`, `wf merged` refuses before any write.

```sh
wf merged app#12 --findings 2 --rounds 0
# app#12: sonnet, findings 2, rounds 0, kTokens 212, cache kTokens 4402, turns 60, review kTokens 95 over 2 run(s) (app#31) [agents a1b2c3d4e5f6; reviews builtin, lean-fix] → Done, closed
```

The orchestrator's own spend (dispatch, rulings, the owner's questions)
belongs to no one issue, so it goes on the sprint record instead, at sprint
close: `wf spend app#50` measures the `session` lines of the base branch
within the record's lifetime. `wf report sprint-3` then gives the per-model
table for a sprint, with the orchestrator as its last row.

Limits:
- The ledger is per machine, in the checkout's git directory: run `wf merged`
  and `wf spend` in the checkout that did the work.
- The hooks see only sessions after the plugin is enabled; earlier work is
  recorded by hand.
- Transcripts record a response's output tokens as they stood when it began
  streaming, so output is undercounted; it is a small share of kTokens.

### Reads outside the edit set

Whether the code map (`wf map`) pays depends on one number the board does
not hold: how many files an implementer read that it did not then change.
`wf dispatch` prints it after each run (`reads 37 over 21 files; 9 reads of 6
files outside the edit set (6 files edited)`) and keeps it in the run's record (`wf dispatch` and `wf usage --edits` list at most 20 of the files, with `files_outside_edit_total` for the count; the run's `state.json` keeps them all); for a Claude
agent, `wf usage <id> --edits origin/dev` counts the same thing from its
transcript against what changed since that ref in the current checkout. A
read is the Read tool, or `cat`, `head`, `tail`, `bat` or `sed -n` from the
shell; searches are not reads. Compare runs of the same issue with and
without the map on `reads_outside_edit`, not on kTokens alone.

## Reviews

This repo is also a Claude Code plugin marketplace. Its `wf-review` plugin is
a lean pull-request review:
- **Prep:** a script gathers the PR, diff, linked issue, CLAUDE.md files and
  file history over REST, with no agents.
- **Lanes:** parallel Sonnet agents (by default correctness, spec, reuse and
  history; [custom lanes](#custom-lanes) change them) with Read, Grep, Glob
  and Bash, told to read only and to use REST.
- **Scoring:** the orchestrating session scores each finding for severity and
  likelihood separately.
- **Output:** one comment with Report and Noted findings.

```sh
claude plugin marketplace add Taykes-Gaming/agent-workflow
claude plugin install wf-review@agent-workflow
```

Run `/wf-review:review <pr>` from a session that did not write the change.
`--no-post --out FILE` writes the review to a file instead of posting it.
The recipe allows GraphQL reads (`gh pr view`, `gh pr diff`, `gh issue view`)
only in `graphql` mode; `--api rest|graphql` overrides `api` in
`.workflow.toml`, and `wf review` passes the mode explicitly.
A standalone run picks its tier, and so its lanes, like `wf review`:
`--tier light|standard|deep`, else with `tiered = true` the linked issue's
size label or the diff's changed lines, else standard. `--config PATH` names
the config file (default: `WF_CONFIG`, else the first `.workflow.toml` up from
the current directory); `wf review` passes the one it read.

### `wf review <pr>`

The review gate as one command. It fetches the PR's head into a throwaway
worktree, runs the built-in `/code-review <pr> high` there from a fresh
`claude -p` session (so the review sees only the PR, never the author's
context), caps spend and wall clock (45 minutes per run), has the run post its own findings, and prints one line:

```
#12: review posted, $2.38 (90k, +1865k cache)
```

Run it from a checkout of the PR's repo, from any session **other than the
one that wrote the change**. Options: `--no-post` prints the findings
instead; `--budget USD`, `--model M`, `--level low|medium|high|xhigh|max`;
`--lean` also runs the `wf-review` plugin (install it first) for L/XL slices
or ones touching an invariant surface. The review session loads no settings files (user, project or local) and no MCP
servers. wf enables only the wf-review plugin, which must be installed for
`--lean` and `--since`. User-level `env` and `apiKeyHelper` do not apply. Defaults can live in the config:

```toml
[review]            # all optional
level = "high"
model = "opus"
budget_usd = 6
```

`(Nk, +Mk cache)` is the run's tokens (input + output + cache writes) and its
cache reads, the same two numbers its spend-ledger line records.

"review posted" means a new review or comment is on the PR: after the run wf
looks for a conversation comment, inline comment or review newer than the
run's start (at most three single-page requests), and exits 1 if none landed
(a headless run that is denied a tool still reports success), naming any
denied commands. Each run's
result is kept in `.git/wf-review/pr-<n>-<variant>.md` (and `.json`), so a
failed post is sent with `gh pr comment <n> --body-file <that file>` instead
of a rerun. The check is wf's own API calls; the review session's prompt and
tools are unchanged, so it costs no extra model tokens.

The review session may read the checkout and run `gh` and read-only `git`
and `ast-grep` commands; deny rules stop `git … --output=<file>` and
ast-grep's rewrite flags, which would otherwise write files.

The cost shown is API-equivalent (what the tokens would cost at list price);
on a subscription it draws on the plan's allowance.

#### Spending less: size tiers and fix rounds

**Tiers** scale the review to the slice. `--tier light|standard|deep` picks
one; with `tiered = true` under `[review]`, wf reads the size label
(`effort:XS`…) on the issue the PR closes, or goes by the diff's changed lines
when there is none (up to 150 light, up to 800 standard, more deep):

| Tier | Sizes | Claude engine | default lanes |
|---|---|---|---|
| light | XS, S | `/code-review` at `medium` | correctness, reuse |
| standard | M | at the configured level (`high`) | correctness, spec when there is an issue or CLAUDE.md, reuse, history when there is history |
| deep | L, XL | at `xhigh` | correctness, spec when there is an issue or CLAUDE.md, reuse, history |

`--level` still wins over the tier. Without `--tier` or `tiered`, wf behaves
as before (standard).

**Custom lanes.** The lanes the wf-review recipe runs (under opencode, with
`--lean`, in a fix round, or as `/wf-review:review`) are configurable per
tier. Each lane is a subagent run and adds to every review's spend; there is
no cap.

```toml
[review.lanes.security]     # a new lane, or a built-in name to override any of its keys
brief = "find injection, authorization and secret-handling bugs the change introduces"
when = "always"             # always | issue_or_claude_md | history
model = "opus"              # optional: the Claude recipe's lane model (sonnet | opus | haiku | fable)
opencode_model = "anthropic/claude-opus-5-5"  # optional: the opencode engine's provider/model

[review.tiers]              # each lane is a subagent run and adds to every review's spend
light = ["correctness", "reuse"]
standard = ["correctness", "spec?", "reuse", "history?"]
deep = ["correctness", "spec?", "reuse", "history", "security"]
fix = ["correctness"]
```

- The built-in lanes are `correctness` and `reuse` (`when = "always"`), `spec`
  (`issue_or_claude_md`: the PR links an issue or a CLAUDE.md applies) and
  `history` (`history`: the touched files have history, or the issue says the
  change replaces code). A `[review.lanes.<built-in>]` table overrides only
  the keys it sets; a new lane needs a `brief`.
- An entry `name?` runs only when its lane's `when` gate holds; a bare `name`
  always runs. A fix round reads no history, so the `history` gate is false
  there and a lane gated on it never runs as `name?` in `fix`. The lists
  above are the defaults, and a tier left out keeps
  its default.
- Two model keys, because the engines name models differently: `model` for
  the Claude recipe's lane agent, `opencode_model` (`provider/model`) for
  opencode, where each distinct one gets its own lane agent
  (`wf-review-lane-1`, …). A lane without one uses the default lane model.

Every `wf` command, and the prep script, rejects a bad lane config, listing
each error as `<type>: <where>: <message>`:

| Type | When |
|---|---|
| `unknown_lane` | a tier entry (without its `?`) is neither built in nor in `[review.lanes]` |
| `missing_brief` | a lane that is not built in has no `brief`, or `brief` is not a non-empty string |
| `bad_when` | `when` is not `always`, `issue_or_claude_md` or `history` |
| `bad_model` | `model` is not `sonnet`, `opus`, `haiku` or `fable`, or `opencode_model` is not `provider/model` |
| `bad_tier` | a `[review.tiers]` key is not `light`, `standard`, `deep` or `fix`, or its value is not a list of strings |
| `empty_tier` | a tier list is empty |
| `duplicate_lane` | a tier list names the same lane twice (ignoring `?`) |
| `unknown_key` | a lane table has a key other than `brief`, `when`, `model`, `opencode_model`, or `[review.lanes].<name>` is not a table |

**Fix rounds** review only what changed since the last review:

```sh
wf review 12                 # first review; wf records the head it saw
wf review 12 --since last    # after the fixes: only the new commits
```

`--since <sha>` names the commit instead. The round's diff is just the new
commits (the whole PR stays available as context), its lanes are the `fix`
list (correctness by default), and the review opens with **Previous findings**: each earlier
finding marked resolved, still open or declined. It always runs the
`wf-review` recipe (under opencode, or the plugin under Claude, which must be
installed): the built-in `/code-review` takes no commit range, and when it
can't read its diff it reports nothing. If nothing was pushed since, wf says
so and runs nothing. On this repo's PR #20 the round's diff was 90 lines
against 971 for the whole PR.

**Reuse** is a quality gate next to the bug hunt: agents tend to rewrite a
helper the codebase already has. The reuse lane (and an instruction to the
built-in) reports new code that duplicates an existing function, type,
constant or block, with a link to the existing one; findings land under
**Reuse** and are fixed or declined like any other. When `ast-grep` is
installed, the lanes use `ast-grep run -p <pattern>` to find definitions and
call sites instead of reading whole files.

#### `--engine opencode` (optional)

Runs the `wf-review` recipe under [opencode](https://opencode.ai) instead of
Claude Code, so the review spends whatever provider opencode is logged in to
(an Anthropic Console key, or any other) rather than the Claude plan, and the
lanes can use a cheaper model than the orchestrator:

```sh
wf review 12 --engine opencode --lane-model anthropic/claude-haiku-4-5
# #12: wf-review posted (opencode anthropic/claude-sonnet-5-5, lanes anthropic/claude-haiku-4-5), $0.69 (62k, +1100k cache)
```

- Needs opencode 2.1 or later; wf checks before it starts.
- Same worktree at the PR head as the Claude engine. wf runs the prep script
  itself, then `opencode run --standalone` with its agents passed inline
  (nothing is written to the checkout): `wf-review`, which launches the lanes
  and scores, and `wf-review-lane`, the plugin's lane prompt (plus
  `wf-review-lane-1`, … for each other lane `opencode_model`). The run uses a
  private server because opencode's shared background service ignores
  inline config.
- The lanes run in the foreground, in parallel. A private server ends with
  the run, so a lane launched in the background would end before it reports.
  If the orchestrator backgrounds one anyway, the review fails and posts
  nothing; run it again.
- The agents may read the checkout and run `gh api` (reads only: writes are
  denied, and so is raw `gh api graphql`; `api = "graphql"` adds `gh pr view`,
  `gh pr diff` and `gh issue view`), `git log`, `git blame`, `git show`, `git diff` (not
  `--output`) and `ast-grep run` (not `-U` or `-i`); nothing else, and no
  edits. The repo's own `AGENTS.md` loads, or its root `CLAUDE.md` when it
  has none (wf passes it as an instruction, since opencode 2 reads only
  `AGENTS.md`). Your Claude skills do not load. Your opencode plugins do,
  since opencode 2.1 has no `--pure`.
- wf posts the review itself and keeps it under `.git/wf-review/pr-<n>-opencode.*`
  as for the other engines. A run that ends without a review fails and posts
  nothing.
- The cost includes the lanes (read from `opencode session export`) and is what the
  provider bills. opencode has no budget cap, so `--budget` does not apply;
  each agent's turns are capped instead. `--level` and `--lean` are for the
  Claude engine.
- Models are `provider/model` as `opencode models` lists them. `--model` sets
  the orchestrator (default `anthropic/claude-sonnet-5-5`); the lanes use it
  too unless `--lane-model` is given. Config:

```toml
[review]
tiered = true                            # pick the tier from the issue's size
engine = "opencode"                      # default "claude"
opencode_model = "anthropic/claude-sonnet-5-5"
lane_model = "anthropic/claude-haiku-4-5"
```

`tools/review-compare.sh <pr> plugin builtin lean` is for **measuring**: it
runs several review recipes blind on one PR and records what each cost. Runs land in
`docs/reviews/runs/`, with a summary table printed at the end.

## Exit codes

| Code | When |
|---|---|
| 0 | Success. |
| 1 | GitHub refused a call — stderr is `wf: <GitHub's message>`, e.g. `wf: API rate limit exceeded for user ID …` — or the config is missing, malformed, or names a field or option the board does not have. |
| 2 | Usage error: unknown command or verb, malformed ref, bad argument. Nothing was sent to GitHub. |

## Adopting it in a project

`/wf-setup:setup` does all of this. By hand:

1. Create the board, its fields and the labels
   ([`board-setup.md`](plugins/wf-setup/skills/setup/board-setup.md)),
   install `wf`, and commit a `.workflow.toml`
   ([template](plugins/wf-setup/templates/workflow.toml)). `wf board` checks
   the board and every field name; option names are checked on first use.
2. Copy the templates from
   [`plugins/wf-setup/templates/`](plugins/wf-setup/templates/) and fill in
   their `{{PLACEHOLDERS}}`:

   | Template | Goes to | Is |
   |---|---|---|
   | `docs/issue-workflow.md` | `docs/issue-workflow.md` | The implementer's contract: pickup, gate, blocking, PR, comments. |
   | `docs/sprint-workflow.md` | `docs/sprint-workflow.md` | The architect's guide: planning, model choice, dispatch, the merge gate, metrics, board fields and what "done" means. |
   | `agents/implementer.md` | `.claude/agents/implementer.md` | The six-tool implementer subagent. |
   | `github/PULL_REQUEST_TEMPLATE.md` | `.github/PULL_REQUEST_TEMPLATE.md` | The sections the architect gates on. |
   | `github/ISSUE_TEMPLATE/slice.md`, `sprint-record.md` | `.github/ISSUE_TEMPLATE/` | A dispatch-ready issue; a sprint's record. |

3. Optionally, install the `wf-review` plugin ([Reviews](#reviews)).

Then the architect writes the first issue from the Slice template, sizes it
(`wf size`), moves it to Ready (`wf move`) and dispatches it.
[`docs/dispatch.md`](docs/dispatch.md) explains how to launch implementers as
subagents (each with its model set at launch and its own worktree) and what
their starting context costs.

## License

[Apache License 2.0](LICENSE).
