<!--
  Example workflow contract. Copy to your project's docs/issue-workflow.md
  and replace every {{PLACEHOLDER}}:

  {{REPO}}          owner/name of the repo issues live in
  {{BOARD_URL}}     the Projects v2 board's URL
  {{ALIAS}}         this repo's alias in .workflow.toml ([repos])
  {{BASE_BRANCH}}   the branch PRs target (e.g. main, dev)
  {{GATE}}          the local checks run before every push
  {{HOUSE_RULES}}   the few rules every change must keep (or point to CLAUDE.md)

  <angle-bracket> fields in the comment templates are filled in per issue
  by the implementer; leave them. Delete this comment when done.
-->

# Working an issue: the implementer's guide

Issues in `{{REPO}}` are the specification, tracked on the board
(<{{BOARD_URL}}>). The architect writes them and dispatches one to you, the
implementer. This guide is the contract.

**The issue is the specification, and you do not change it.** If it is
wrong, silent or impossible, stop and say so on the issue (§3). Never
resolve it by picking an answer.

## 1. Picking up an issue

1. Take only an issue whose board Status is **Ready**. `Backlog` means not
   dispatch-ready (not yet groomed, or a dependency is unmerged); do not start
   it, ask on the issue.
2. Read the body, then the cited symbols and line ranges; run `wf map
   <symbol or path>…` (or `wf map --text <file>` on the body) on them first. Read
   a whole file only when the body says to edit most of it. A drifted
   reference is not a blocker; note the new location in the pickup comment.
3. Read the cited doc **sections** only, never a whole document. `CLAUDE.md`
   is read once, if the repository has one.
4. Post the **pickup comment** (§6) and move Status to **In progress**
   (`wf move {{ALIAS}}#<n> progress`).
5. Branch from `{{BASE_BRANCH}}`: `claude/<n>-<slug>`, in your own worktree
   (`git fetch && git checkout -B <branch> origin/{{BASE_BRANCH}}`; worktrees
   start stale). One issue, one branch, one PR.

You move Status Ready → In progress → In review, and → Blocked
(`wf move`), and add only the label `needs-ruling`. Size and the metric
fields are the architect's; never set or estimate them.

## 2. While working

- **House rules** are not restated in issue bodies: {{HOUSE_RULES}}
- **Commits**: one intent each, subject a sentence, body the *why*.
- **Never rebase or force-push a pushed branch.** Bring `{{BASE_BRANCH}}` in
  with `git merge origin/{{BASE_BRANCH}}`; resolve conflicts in the merge
  commit.
- **Comment on the issue only at** pickup, checkpoint, blocked, PR opened
  and done. No "still working".
- **Keep a checkpoint note.** After each phase of the pickup plan, run
  `wf checkpoint "<phase> done; next <phase>"`. If you are resumed or
  restarted, `wf checkpoint` is your first read: it says where you are, so do
  not re-derive it from git and scratch files.
- **Scope is fixed.** Anything the body did not ask for is a follow-up (§5),
  never a commit here.
- **Diagnose before you spend.** Before any step that costs money, more
  than a few minutes, or a large diff (a batch, a capture, a migration, a
  paid build), post a checkpoint comment: the diagnosis, the one lever you
  are about to pull, and what it should cost against the body's *Spend*.
  Pull one lever at a time. If the cost would exceed *Spend*, that is a
  blocker (§3).
- **Long commands, logs, gate.** Start anything that takes minutes with
  `wf wait --run "<cmd>" --until "<regex>"` (`--resume` after a dropped
  session); read a log with `wf logread <path>`, never `cat`. Run {{GATE}}
  unfiltered before every push (`wf gate` when `.workflow.toml` has a
  `[gate]`), no step omitted, no failure trimmed, and paste its summary
  lines; `wf gate --watch` while fixing. Exit 1 is a failure for `wait` and
  `gate`, 124 a `wait` timeout; other flags: the wf README command table.

## 3. When you are blocked, or the issue is wrong

Stop. Post the **blocked comment** (§6): what the body says, what contradicts
it or is undecided, the options with trade-offs, and which you would take.
Add label `needs-ruling` and move Status to **Blocked**
(`wf move {{ALIAS}}#<n> blocked`). Continue with whatever does not depend on
the answer; otherwise stop and return.

*Not* blockers (decide, and record in a checkpoint): a drifted line number, a
test name the body guessed wrong, an unnamed helper you need.

*Are* blockers, however small: two incompatible requirements; a "your call"
that changes a public interface, data format or schema; a gate step failing
on `{{BASE_BRANCH}}` before your change; any change the body did not ask for
that you believe its acceptance criteria require.

## 4. Opening the PR

- Base **`{{BASE_BRANCH}}`**. The body follows
  `.github/PULL_REQUEST_TEMPLATE.md`: it opens with `Closes #<n>` and states
  what was built and every difference from the issue, the gate output, and
  each review finding fixed (in its own commit) or declined with a reason.
- `gh pr create --base {{BASE_BRANCH}} --body-file <file>`; never retype gate
  output.
- The review gate is `wf review <PR number>`, run on the open PR. It runs a
  review from a **fresh session** at the PR's head and posts the findings on
  the PR. A review by the session that wrote the change does not satisfy the
  gate.
- Move Status to **In review** (`wf move {{ALIAS}}#<n> review`) and post the
  PR-opened comment on the issue.
- After a fix round, review only the new commits: `wf review <PR number>
  --since last`. It checks the earlier findings off as resolved, still open
  or declined, and reviews what the fixes changed.
- Every review comment gets a commit or a reason, never silence. That
  includes **Reuse** findings: new code that duplicates something the
  codebase already has. Use the existing code, or say why not.
- You never merge.

## 5. Follow-up issues

Open one when the body says to, for a defect outside scope, or for a false
premise in a file you are not editing. Body: file:line, what is wrong, what
correct is, what you did **not** do and why. No Size; the architect sizes it.
Link it from the checkpoint or done comment.

## 6. Comment templates

Prose, not headers; bullets only for lists. Each fact is written **once**:
the issue holds the spec, the PR body holds the gate output, findings and
differences; comments point to them, never restate them.

**Pickup**
> Starting on `claude/<n>-<slug>` from `{{BASE_BRANCH}}` at `<sha>`. Drifted:
> `<file:old → new>` (or "none"). Plan: <one sentence per phase, naming the
> symbols it changes>.

**Checkpoint** (one per decision, two sentences)
> Within bounds: <decision> because <reason>; body said <X>, found <Y>.
> (Follow-up: #<m>.)

**Blocked**
> Blocked on <question>. The body says <quote>. I found <gap, file:line>.
> Options: (a) <…, trade-off>; (b) <…, trade-off>. I would take (a) because
> <reason>. Continuing with <independent part> / stopping.

**PR opened**
> PR #<p> against `{{BASE_BRANCH}}`. Gate green except <none | list>.
> `wf review` found <n>; fixed <i>, declined <j> in the PR. Differs from the
> body at: <list or "nowhere">.

**Hand-back** (your final message to the architect, at PR-opened; one line
per field of at most 200 characters, "none" when empty; no tool call after it)
> **Built:** <one sentence>. PR #<p>.
> **Differs from the body:** <list, or "nowhere">.
> **Gate:** <green, or the failing step and its first failure line>.
> **Review:** `wf review` found <n>; fixed <i>, declined <j>.
> **Spend:** <turns / kTokens / CU / minutes you know of, against the body's *Spend*>.
> **Rulings:** <each `needs-ruling` asked and how it was ruled, or "none">.
> **Follow-ups:** #<m>…, or "none".

**Done** (after merge, if the PR did not close the issue)
> Merged in #<p> as `<sha>`. Follow-ups: #<…>. For the next issue: <what
> changed the plan, or "nothing">.
