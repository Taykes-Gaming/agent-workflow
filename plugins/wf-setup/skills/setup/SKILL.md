---
name: setup
description: Set up the agent-workflow (architect → implementer subagents → fresh-session review, tracked on a GitHub Projects board with wf) in the current repository. Use when the user wants to adopt agent-workflow, set up wf, create the workflow board, or add the issue-workflow/sprint-workflow docs to a project.
---

# Set up agent-workflow in this repository

You are adopting agent-workflow in the repository in the current directory.
The templates are in `${CLAUDE_PLUGIN_ROOT}/templates/`; the board recipe is
`${CLAUDE_PLUGIN_ROOT}/skills/setup/board-setup.md`. Read the recipe before
step 3.

Work through the steps in order. Anything that writes to GitHub (creating a
board, fields, labels; pushing) is shown to the user and done only after
they say yes. Local files are written on a new branch, and nothing is pushed
without asking.

## 1. Check the tools

Run these together:
- `gh auth status`: logged in, scopes include `project` and `repo`. If not,
  ask the user to run `gh auth refresh -s project,repo` themselves (it
  opens a browser).
- `wf --help`: if missing, install it with
  `uv tool install git+https://github.com/Taykes-Gaming/agent-workflow`
  (needs `uv`; the repo may be private, so the user needs access to it).
- `git remote get-url origin` and the default branch
  (`gh api repos/<owner>/<repo> --jq .default_branch`).

## 2. Gather the answers

Infer what you can before asking:
- **Repo, owner, owner type**: from the remote (`gh api users/<owner>
  --jq .type` gives `User` or `Organization`).
- **Base branch**: whether a `dev` branch exists (`git ls-remote --heads
  origin dev`), else the default branch.
- **Gate**: the project's own check commands, from `package.json` scripts,
  `Makefile`, `pyproject.toml`, `Cargo.toml`, `*.csproj`, CI workflow files
  or `CLAUDE.md`. Write it as the exact commands, both into `{{GATE}}` and
  as `[gate].steps` in `.workflow.toml` (steps that only apply under one
  directory go in `[gate.when]`; an NUnit results file goes in `xml`).
- **House rules and critical paths**: from `CLAUDE.md` and `docs/`, if
  present. Keep house rules to the few every change must keep.
- **Existing setup**: `.workflow.toml`, `docs/issue-workflow.md`,
  `docs/sprint-workflow.md`, `.github/PULL_REQUEST_TEMPLATE.md`,
  `.github/ISSUE_TEMPLATE/`, `.claude/agents/implementer.md`. Never
  overwrite one; show the difference and ask.

Then ask the user, in one AskUserQuestion call, only what is still open:
new board or existing (and its URL); any other repos on the same board, with
short aliases; whether to track deploys; and confirmation of the inferred
gate and base branch.

## 3. The board

Follow `board-setup.md`, step by step, with the user's values. Before
running it, show the user the board name, owner, fields and labels it will
create, and ask. On an existing board, `wf fields --create` adds only the
missing fields; ask before replacing Status options on a
board that has items (it clears their Status; `wf rebuild` restores most of
it).

## 4. The files

Create a branch (`git checkout -b chore/agent-workflow`), then write each
file from its template, replacing every `{{PLACEHOLDER}}` and deleting the
template's header comment:

| Template | Goes to |
|---|---|
| `templates/workflow.toml` | `.workflow.toml` |
| `templates/docs/issue-workflow.md` | `docs/issue-workflow.md` |
| `templates/docs/sprint-workflow.md` | `docs/sprint-workflow.md` |
| `templates/agents/implementer.md` | `.claude/agents/implementer.md` |
| `templates/github/PULL_REQUEST_TEMPLATE.md` | `.github/PULL_REQUEST_TEMPLATE.md` |
| `templates/github/ISSUE_TEMPLATE/slice.md` | `.github/ISSUE_TEMPLATE/slice.md` |
| `templates/github/ISSUE_TEMPLATE/sprint-record.md` | `.github/ISSUE_TEMPLATE/sprint-record.md` |

`{{HOUSE_RULES}}` and `{{CRITICAL_PATHS}}` may be "none yet" for a new
project. Writing under `.claude/` asks the user for permission even when
other edits are allowed; that is expected, so say why before the write. Afterwards, `grep -rn '{{' ` over the written files must find
nothing.

If the project has a `CLAUDE.md`, offer to add a short section:

```markdown
## Workflow
Issues are worked per `docs/issue-workflow.md` (implementers) and
`docs/sprint-workflow.md` (architect). The board is driven with `wf`.
```

## 5. The spend ledger

`wf merged` and `wf spend` read what each agent and session spent from a
ledger the plugin's hooks write (`wf hook`, on SubagentStop and Stop), so
the numbers need no transcript hunting. Tell the user:

- With this plugin enabled, the hooks are on; they record sessions from
  then on, not before. They run `wf`, so it must be on the PATH Claude Code
  starts hooks with: `uv tool install` (step 1) or `pipx install` puts it
  there.
- The ledger lives in the repo's git directory, per machine: record an
  issue with `wf merged` in the checkout that did the work.
- On a board set up before these fields existed, add the `cache_ktokens`
  and `review_ktokens` keys under `[fields]` and run `wf fields --create`;
  `wf board` warns while they are missing.
- Give each sprint record the `sprint-<N>` label as well as
  `sprint-record`: `wf spend` records the orchestrator's sessions on it and
  `wf report sprint-<N>` prints them as its own row.

## 6. Check and hand over

- `wf board` must exit 0.
- Offer to install the review plugin:
  `claude plugin install wf-review@agent-workflow`.
- Show `git status` and the diff summary, then ask whether to commit and
  open a PR.
- Tell the user that `.claude/agents/implementer.md` loads at the next
  session start, and that the first issue is written from the Slice
  template, sized with `wf size`, and moved to Ready with `wf move`.
