<!--
  Example architect's guide. Copy to your project's docs/sprint-workflow.md
  and replace every {{PLACEHOLDER}} (the same set as issue-workflow.md):

  {{REPO}}            owner/name of the repo issues live in
  {{ALIAS}}           this repo's alias in .workflow.toml ([repos])
  {{BASE_BRANCH}}     the branch PRs target (e.g. main, dev)
  {{GATE}}            the local checks run before every push
  {{CRITICAL_PATHS}}  code where a locally reasonable change is globally wrong
                      (always dispatched on the strongest model), or "none yet"

  Delete this comment when done.
-->

# Running a sprint: the architect's guide

`docs/issue-workflow.md` is the implementer's contract: how one issue is
worked. This is the other half: how a sprint is planned, dispatched, gated
and closed. Amend it as the project learns.

| Seat | Who | Decides | Never does |
|---|---|---|---|
| **Owner** | the human | product rulings, priorities, what a sprint is for | writes issue bodies |
| **Architect** | an interactive Claude Code session | the issue is the specification: every mechanism, number, test and file reference; order; merge | writes implementation code; starts work on owner feedback that has no issue (§1b) |
| **Implementer** | a subagent per issue, in its own worktree (`docs/issue-workflow.md`) | nothing outside the body's stated bounds; a gap is a `needs-ruling` comment | changes the specification |

The rule the whole thing rests on: **a decision is made once, in writing, by
the seat that owns it, before the seat below needs it.**

## 1. Planning a sprint (architect, with the owner)

1. **Rulings first.** Every question whose answer changes the shape of the
   work goes to the owner as a closed question with a recommended option,
   a few at a time, before anything is written. Record each ruling on the
   sprint-record issue.
2. **Issues are written against a named commit** of `{{BASE_BRANCH}}`, in
   dependency order, from the Slice issue template: Why, Design (decided),
   Symbols, Numbers, Tests, Constraints, Docs owed, Size · Model · Depends
   on. Every number has a default; every test a name.
3. **Labels and board.** Give each issue the sprint's milestone
   (`wf move … ready` refuses an issue without one) and label it
   `sprint-<N>` (the label `wf report` groups by). Size each issue (`wf size {{ALIAS}}#<n> M`, which
   also sets the `effort:*` label). An issue stays in Backlog
   (`wf move {{ALIAS}}#<n> backlog`) until every *Depends on* is merged
   and it is groomed to the Slice template, then moves to Ready (`wf move {{ALIAS}}#<n> ready`).
4. **Open questions** the owner has not ruled on get the `needs-ruling`
   label and are never dispatched; they exist so the gap is visible.
5. **The dispatch pass.** Before the sprint starts, re-read every body end to
   end against the others and against `{{BASE_BRANCH}}`, looking for one
   thing: a sentence an implementer would have to choose how to read. Fix
   each in the body.

## 1a. Choosing the model for an issue

- The mid-tier model (e.g. Sonnet) for `XS`/`S`/`M` issues whose body can be
  followed mechanically.
- The strongest model (e.g. Opus) for `L`, and for anything architecturally
  novel: a new invariant, a schema change, a first use of a mechanism.
- **Critical paths**, which go to the strongest model regardless of Size:
  {{CRITICAL_PATHS}}
- **`XL` is over budget:** split it into two issues before dispatch.

`wf report` shows findings, fix rounds and tokens per model over past
sprints; let it move these lines.

## 1b. Owner feedback

Owner feedback, a bug report or a change request becomes **one issue per item**,
Status Backlog, label `backlog` (`wf move {{ALIAS}}#<n> backlog` sets both), written by
the architect in the same turn. The architect replies with the issue number and
does not start the work. The issue is groomed to the Slice template at the next
planning pass (§1), like any other issue. Two exceptions:

- (a) the owner says in words to fix it now: this skips the wait for the planning
  pass, not the issue; the architect files it, grooms it and gives it a milestone
  and Size per §1 in the same turn, and dispatches it as soon as it is Ready;
- (b) the feedback is a ruling on an issue already In progress, Blocked or In
  review: it is recorded in that issue's body per §2, not filed as a new issue.

## 2. Dispatching

Dispatch an issue only when its Status is Ready. Launch the implementer as a
subagent with the `implementer` agent definition, its model set at launch and
`isolation: worktree` (`docs/dispatch.md` in agent-workflow explains why).
The dispatch prompt names the issue, the model and `docs/issue-workflow.md`,
and nothing else: the body carries the rest. **Keep the agent id the launch
returns**; it is how the metrics are measured at merge (§3).

The implementer returns with the hand-back (`docs/issue-workflow.md` §6)
and stops; its fields are the answers, so do not ask follow-ups the template
already covers. An agent still running after its hand-back is stopped.

**Repetitive slices** (migrations, re-pins, captures, goldens): the body
names a manifest of items, a per-item state (todo, done, stopped, flagged),
a `--dry`, `--resume` and `--redo` on the script that walks it, and one
summary table at the end. The implementer reads the table and only the
stopped items, never the full log; the body names the `wf wait` preset (or
the `--until` line) that says the walk is done, so the wait is one call.

Two implementers may run at once only when neither depends on the other and
they touch different files.

When the implementer returns with a `needs-ruling` question, rule on it (in
the issue body, so the spec stays the single source), then resume the
**same** agent by message. A fresh agent loses the context and splits the
issue's metrics. If an agent died (a network drop), start the replacement with
the output of `wf checkpoint` run on its branch, so it resumes from the note.

## 3. The gate (architect, per PR)

Check the PR template's sections **in order**, refusing on the first miss:

1. `Closes #<n>` and *What changed* listing every difference from the body.
2. *Gate*: {{GATE}} (or the `wf gate` summary lines), pasted, all green or
   explained. Re-run `wf gate` yourself rather than re-reading the output.
3. *Review*: `wf review <pr>` was run on the open PR (a fresh-session review,
   posted on the PR) and its comment linked; every finding is fixed in its
   own commit or declined in writing. A review by the implementer's own
   session does not count.
4. *Docs amended* per the body's Docs owed.

A miss goes back to the same implementer agent by message; each send-back is
one **fix round**.

Then merge into `{{BASE_BRANCH}}` and, **in the checkout that did the work**,
record the metrics. A number reconstructed later is a guess:

```sh
wf merged {{ALIAS}}#<n> --findings <n> --rounds <n>
```

`wf merged` refuses until a merged PR references the issue, and closes the
issue itself: `Closes #n` is honoured only on a merge to the default branch.

- Model, kTokens, Cache kTokens and Turns are summed from the spend ledger
  of the PR's branch: every implementer that stopped there (the wf-setup
  plugin's hooks record it) or ran under `wf dispatch`. Review kTokens is
  its `wf review` runs, kept apart. Without the hooks, pass `--model` and
  `--ktokens` (and `--cache-ktokens`, `--turns`) by hand from `wf usage`.
- `--findings`: findings the review gate raised that were real, whether
  fixed or not; a finding declined in writing as wrong does not count.
- `--rounds`: how many times the implementer was sent back (0 means the
  first PR merged).

## 4. Closing a sprint

1. **Sweep** open issues: shipped, superseded, folded, or kept with notes.
   Record the result on the sprint-record issue.
2. **Audit** (optional, a fresh read-only session): `/code-review` over the
   sprint's commit range, plus the project's own invariant checks. Fix the
   findings in a follow-up batch or file them as issues.
3. **Metrics**: `wf spend {{ALIAS}}#<record>`, in the checkout the
   architect ran in, records the orchestrator's own spend on the record:
   the sessions on `{{BASE_BRANCH}}` in the ledger, from the record's
   creation. The record carries the `sprint-<N>` label too. Then paste
   `wf report sprint-<N>` into the record. Never write it by hand.
4. **Escalations** for the owner, and the closing commit on `{{BASE_BRANCH}}`.
5. The next sprint's architect session starts by reading the record issue.

## 5. Board fields and done

| Field | Who sets it | Values |
|---|---|---|
| Status | implementer: Ready → In progress → In review, and → Blocked; architect: Backlog → Ready, Blocked → Ready; merge → Done (`wf merged`) | `Backlog` `Ready` `In progress` `Blocked` `In review` `Done` |
| Size | architect (`wf size`); mirrored by an `effort:*` label | `XS`–`XL`; if badly wrong, say so in the done comment |
| Labels | architect; the implementer adds only `needs-ruling` | `sprint-<N>` `effort:*` `backlog` `needs-ruling`, plus the project's own |
| Model, Review Findings, Fix Rounds, Agent kTokens, Turns | architect, at merge, via `wf merged` | never set or estimated by the implementer |

Labels mirror the board (`backlog` ↔ Status Backlog, `effort:*` ↔ Size) so
`wf rebuild` can restore it after a field's options are rewritten.

### What "done" means

The PR is merged into `{{BASE_BRANCH}}`; every acceptance criterion is met or
explained; the gate is green; every review finding is fixed or declined in
writing. Not before, and nothing "to follow".
