---
name: process
description: The agent-workflow loop on one screen — roles, the steps from Ready issue to merged PR, and the wf command that answers each step. Use when an agent asks how the process works, what to run next, or which wf command replaces a sleep, a log read or a search.
---

# The loop

**Architect** writes issues on the board and dispatches. **Implementer**
works one issue in its own worktree, from pickup to PR. **Reviewer** is a
fresh session that never saw the author's context. The contract is the
project's `docs/issue-workflow.md`; the architect's side is
`docs/sprint-workflow.md`. This skill is the map; read a section of those
only when a step needs it.

| Step | Who | Run |
|---|---|---|
| Session start: drift and fix streams | architect | `wf board --check`; a non-zero drift line is fixed before anything is dispatched |
| Owner gives feedback | architect | `gh issue create`, `wf move <ref> backlog`, reply with its number (`docs/sprint-workflow.md` §1b) |
| Pick an issue whose Status is Ready | architect | `wf board` |
| Dispatch it | architect | Agent tool with the `implementer` agent, or `wf dispatch <ref> --engine opencode` |
| Pick up: read the body and the files it cites | implementer | `wf map <symbol or path>…` instead of searching; `wf move <ref> progress` |
| Branch `claude/<n>-<slug>` from the base, in a worktree | implementer | `git fetch && git checkout -B <branch> origin/<base>` |
| After each phase | implementer | `wf checkpoint "<phase> done; next <phase>"` (first read on resume) |
| Wait on anything long (a batch, a capture, a build, a job) | implementer | `wf wait --run "<cmd>" --until "<regex>"`, or `wf wait <preset>`; never sleep and reread |
| Read a log or test results | implementer | `wf logread <path>` (`.xml` gives failed cases only); never `cat` the log |
| Fix the gate | implementer | `wf gate --watch` once in the background; read only the flips |
| Before every push | implementer | `wf gate`, summary lines pasted |
| Blocked, or the issue is wrong | implementer | stop; blocked comment on the issue (§3, §6) |
| Open the PR | implementer | PR template, `Closes #<n>`, hand-back comment, then stop |
| Review the PR | a fresh session | `wf review <pr>` (or the `wf-review` plugin); findings fixed in their own commits or declined in writing |
| Merge and record | architect, in the merging session | `wf merged <ref> --findings N --rounds N` (sums the PR branch's ledger: agents, then review spend apart; closes the issue) |

# Spending rules

- The issue is the specification. Never change it; a wrong issue is a
  blocked comment, not a guess.
- Read cited **sections**, not documents. `CLAUDE.md` once.
- Every tool call that does not depend on another goes in the same turn.
- Diagnose before any step that costs money, minutes or a large diff: one
  checkpoint comment, one lever, then pull it.
- Scope is fixed. Anything the body did not ask for is a follow-up issue.
- Never rebase or force-push a pushed branch; merge the base in.
