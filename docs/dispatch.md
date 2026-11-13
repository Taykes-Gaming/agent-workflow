# Dispatching implementers as subagents

How an architect session launches an implementer so it starts on the right
model with the smallest context, and what that context costs. The numbers
were measured on a Unity game project in October 2026. Yours will differ with your plugins, MCP servers and skills,
but the proportions should hold.

## Why not chips

A desktop chip starts a new session on the Code tab's remembered model, not
the one the prompt names. When the architect runs on a dear model, every chip
opens on it and reads its whole starting context before the owner can switch
it; and a switch made from outside (`set_session_model`) applies only from the
session's *next turn*, which for an implementer that never blocks is after
the issue is done. Chips therefore cost one full issue at the wrong price
whenever the owner is not watching.

A subagent launched with the Agent tool takes its model at launch, runs in
its own worktree (`isolation: worktree`), and keeps its transcript under the
architect session's `subagents/` folder, so the issue's record survives.
Rulings work as before: the agent stops with its blocked comment and
returns; the architect rules and resumes it by name with its context intact.
`wf review` still runs from inside the agent as its own fresh `claude -p`
session, so the review's tokens never enter the agent's context.

## What the context costs

Measured on that project, with its `.claude/settings.json` already disabling
the iOS plugins it did not use:

| Launch | Starting context (one turn, "reply OK") |
|---|---|
| `general-purpose` subagent, full tool set | **54.9k** |
| fresh desktop session (for comparison) | 47.9k |
| `implementer` project agent, six tools (below), Opus | **9.4k** |

The six-tool agent starts at about a sixth of the full tool set and a fifth of a fresh desktop session; the agent's first `usage` line in its `subagents/` transcript is where to read it.

The starting context is paid again on every turn (as a cache read), so a
270-turn issue pays it 270 times. Two things move the bill, in this order.

**1. Turns.** Issue every tool call that does not depend on another in the
same response; make edits with one `Edit` or one Bash heredoc rather than a
read-then-edit pair; read the sections a body cites rather than whole files.
The measured project's implementer transcripts ran 80–324 turns per issue; the metrics
record turns per issue so the number is visible.

**2. Tool schemas.** Every tool the agent may use is described in its prompt.
In the measured session, the schemas for browser, simulator, Unity MCP,
Serena, Context7 and Chrome tools made up most of the 54.9k; check which
servers your own sessions load. A project agent definition lists only what an implementer uses:

```markdown
---
name: implementer
description: Implements one issue end to end in its own worktree per docs/issue-workflow.md.
tools: Bash, Read, Edit, Write, Grep, Glob
---
```

(`plugins/wf-setup/templates/agents/implementer.md` is the full file; copy it to the project's
`.claude/agents/` and adapt its project lines. Agent definitions load at
session start.)

Smaller, and the owner's to change: user-level skills in `~/.claude/skills`
are listed by name and description in every session in every repo (about 3k
tokens for a dozen); move them into the projects that use them. A user-level
SessionStart hook that injects text (e.g. one installed by a plugin)
costs its length in every session; subagents do not run SessionStart hooks,
so the switch removes that one by itself.

Not worth trimming: `CLAUDE.md` (2–3k) and the issue body (3–6k). They are
the specification, and a shorter one costs rulings.

**The map.** The turns before the pickup comment are mostly the agent
finding what the issue names. `wf dispatch` appends a `## Map` section to
the instruction file, `wf map --text` over the issue, and the contract
(`docs/issue-workflow.md` §1) tells the implementer to run `wf map` itself
([map.md](map.md)). The metrics record turns per issue; the claim to check
is fewer of them before pickup.

## The dispatch prompt

Names the issue, the model, `docs/issue-workflow.md` (the project's own
workflow contract; see the README's *Adopting it in a project*), and nothing
else the body already says. The agent returns at a `needs-ruling` comment or at
PR-opened with the hand-back (`docs/issue-workflow.md` §6); it never merges.

Parallel implementers share the architect session's scratchpad, so one that
writes short file names at its root (`e.py`, a PR-body file) overwrites
another's. An implementer writes temporary files only under
`<scratchpad>/<issue-number>/`, or its worktree's untracked `artifacts/`, and
never at the scratchpad root.

## The OpenCode seat

`wf dispatch <ref> --engine opencode` runs an issue on
[opencode](https://opencode.ai) instead of an `implementer` subagent, under
the same contract. Use it when the issue should spend a provider opencode is
logged in to (an Anthropic Console key, or another vendor) rather than the
Claude plan, or to compare seats on like issues. Keep the Agent-tool seat for
issues that need Serena, Context7 or an open Unity editor, and while the
OpenCode seat's numbers are still few. It needs opencode 2.1 or later.

```sh
wf dispatch sk#123 --engine opencode --model anthropic/claude-opus-5-5 --variant medium
# sk#123: worktree …/.claude/worktrees/opencode-123 on claude/123-<slug> from origin/dev at 1a2b3c4
# sk#123: opencode anthropic/claude-opus-5-5 medium running; log …/.git/wf-dispatch/123/run-1.jsonl
# sk#123: opencode exited 0 after 142 turns, $9.80 (690k, cache read 18200k)
# wf merged flags: --model opencode-opus --ktokens 690 --cache-ktokens 18200 --turns 142 (over 1 run(s))
```

What it does, in order:

1. Checks that the checkout's `origin` is the ref's repo, then cuts a
   worktree at `<main checkout>/.claude/worktrees/opencode-<n>` on
   `claude/<n>-<slug>` from `origin/<base>` (fetched first). A worktree that
   is already there is refused unless `--resume` is given.
2. Writes the instruction file `.git/wf-dispatch/<n>/implementer.md`: the
   body of the project's agent definition (`.claude/agents/implementer.md`
   in the new worktree, frontmatter dropped), then the issue, its title, the
   worktree and the branch. That is the text the Claude seat gets plus what
   its dispatch prompt names.
3. Runs `opencode run --standalone --format json --auto` in the worktree
   with an inline agent, `wf-implementer` (`OPENCODE_CONFIG_CONTENT`, so
   nothing is written to the checkout). `--standalone` gives the run a
   private server, because opencode's shared background service ignores
   inline config. The variant goes on the model, `provider/model#variant`.
   The agent's instructions are that file and the worktree's `CLAUDE.md` (or
   its `AGENTS.md`, which opencode reads itself); the user's Claude skills
   are kept out. The agent has opencode's default tools; subagents,
   `gh pr merge`, `git rebase` and force-pushes are denied. The prompt names
   the issue.
4. Streams opencode's JSON events to `.git/wf-dispatch/<n>/run-<k>.jsonl`
   as they arrive (`tail -f` it), stderr beside it, and writes the run's
   cost and tokens to `run-<k>.json` and `state.json`.
5. Appends the run's spend to the issue branch's ledger, exits with
   opencode's status, and prints the run's cost and the `wf merged` flags,
   summed over every run of the issue.

**Rulings.** The seat stops at a `needs-ruling` comment as the subagent
does. After the ruling, `wf dispatch sk#123 --engine opencode --resume
--message "<the ruling>"` continues the same opencode session in the same
worktree, with a new log; the flags it prints cover all runs. The resume
message carries the last 10 lines of the branch's `wf checkpoint` note, so
the session starts from it rather than from git.

**Config** (`.workflow.toml`, all optional; flags win):

```toml
[opencode]
model = "anthropic/claude-opus-5-5"   # default anthropic/claude-sonnet-5-5
variant = "medium"                    # provider reasoning effort; --variant none drops it
base = "dev"                          # default dev
contract = ".claude/agents/implementer.md"
```

**Metrics.** The Model option is `opencode-<family>` (`opencode-opus`), so
`wf report` rows keep the seats apart; add the option to the board's Model
field in the field settings before the first `wf merged` (the UI keeps
existing values). Each run is an `agent` line in the ledger of the issue's
branch (`<git-common-dir>/wf-ledger/<branch>.jsonl`), so `wf merged` sums the
runs itself, in the checkout that dispatched them; the printed flags are for
another checkout. kTokens counts as `wf usage` does: input + output + cache
writes, cache reads as Cache kTokens. Review spend is recorded apart (Review
kTokens), as for the Claude seat; do not add it in.

### MCP servers

opencode does not read Claude Code's `.mcp.json`. It loads servers from the
user's `~/.config/opencode/opencode.json`, then from every `opencode.json`
between the filesystem root and the worktree (so the main checkout's counts
too), then from `.opencode/opencode.json` files. Commit only an `mcp` block
that names no secret: a key goes in as `{env:VAR}`, and OAuth servers
(Scenario's) are signed in once per machine with `opencode mcp auth <name>`,
stored outside the repo.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "scenario": {"type": "remote", "url": "https://mcp.scenario.com/mcp"},
      "unityMCP": {"type": "local", "disabled": true,
                   "command": ["uvx", "--from", "mcpforunityserver==10.2.0", "mcp-for-unity", "--transport", "stdio"]}
    }
  }
}
```

The older shape, with servers directly under `mcp` and `"enabled": false`,
still works in opencode 2, and wf's check reads both.

`wf dispatch` warns, and runs anyway, when the issue's title, body or labels
mention "Scenario MCP" or "Unity MCP" / "MCP for Unity" and no enabled
server's name contains `scenario` or `unity`.

### Limitations

- **No budget cap.** opencode has none; a run ends when the model stops.
  Watch the log, or stop the process.
- **Starting context.** opencode's default agent starts near 25k tokens
  (measured: "reply OK" on Opus 5.5), against 9.4k for the six-tool
  subagent, and every MCP server adds its tool schemas.
- **Unity MCP talks to the open editor**, which is the owner's checkout, not
  the worktree. Keep it disabled for the seat and use `./unity` with the
  editor closed, as the house rules say.
- **No Serena or Context7** unless configured in `opencode.json`; the
  contract's "orient with Serena" lines then fall back to grep.
- **Permissions are open** apart from the denies above (`--auto` approves
  the rest), including paths outside the worktree, as for the subagent.
- The instructions say there are no subagents; Claude-specific lines of the
  contract (the Agent tool) are moot rather than rewritten.
