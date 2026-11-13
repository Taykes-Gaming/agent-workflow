# Setting up the board

Creates a GitHub Projects v2 board with the fields `wf` expects, links the
repos, and creates the labels. Every step uses `gh`; the token needs the
`project` and `repo` scopes:

```sh
gh auth refresh -s project,repo
```

Set these once. `OWNER` is the board owner's login, user or org; not `@me`,
which `gh project link` rejects alongside an `owner/name` repo:

```sh
OWNER=your-org
REPOS="your-org/app your-org/api"
```

## 1. The board

Skip this step to reuse an existing board; its number is the last part of
its URL (`…/projects/<number>`).

```sh
gh project create --owner "$OWNER" --title "My project" --format json
N=<the "number" in the output>
for r in $REPOS; do gh project link "$N" --owner "$OWNER" --repo "$r"; done
```

## 2. Fields

Write `.workflow.toml` first (the `[fields]` names and `[status]` options),
then let `wf` create whatever the board lacks, over REST:

```sh
wf fields            # lists the configured fields the board lacks
wf fields --create   # creates them, with the types and options wf expects
```

It creates only missing fields and never alters an existing one: rewriting a
single-select's options over the API regenerates every option id and clears
the column on every item. Model starts with the lowercase families `opus`,
`sonnet`, `haiku`, `fable`, which is what the ledger records; add a family
(or the OpenCode seat's `opencode-<family>`, e.g. `opencode-opus`) in the
board's field settings, which keeps existing values. Re-run `wf fields
--create` after a `wf` upgrade that adds fields.

## 3. Status options

Every board has a built-in Status field, with `Todo`, `In Progress` and
`Done` options; `gh` cannot edit them, so this step is one GraphQL call. It
**replaces** the options: on a board that already has items, each item's
Status is cleared, and `wf rebuild` restores Done/Ready/Backlog from issue
state afterwards. To keep an existing option (and the items set to it),
add its `id` (from `gh project field-list "$N" --owner "$OWNER" --format
json`) to that option's entry.

```sh
STATUS=$(gh project field-list "$N" --owner "$OWNER" --format json \
  --jq '.fields[] | select(.name == "Status") | .id')
gh api graphql -f id="$STATUS" -f query='
mutation($id: ID!) {
  updateProjectV2Field(input: {fieldId: $id, singleSelectOptions: [
    {name: "Backlog",     color: GRAY,   description: "Not dispatch-ready: not yet groomed, or a dependency is unmerged"},
    {name: "Ready",       color: BLUE,   description: "Dispatchable"},
    {name: "In progress", color: YELLOW, description: "An implementer has it"},
    {name: "Blocked",     color: RED,    description: "needs-ruling"},
    {name: "In review",   color: PURPLE, description: "PR open"},
    {name: "Done",        color: GREEN,  description: "Merged"}
  ]}) { projectV2Field { ... on ProjectV2SingleSelectField { options { name } } } }
}'
```

On a board created in step 1, the built-in workflows that set Status are
off. On an older board, open its **Workflows** page in the browser: "Item
added", "Item closed" and the pull-request workflows name a Status option and
may now point at one that no longer exists. Set "Item closed" to Done and
turn the others off, since `wf` sets Status.

## 4. Labels

```sh
for r in $REPOS; do
  gh label create backlog      -R "$r" --force --color BFD4F2 --description "Not dispatch-ready: not yet groomed, or a dependency is unmerged"
  gh label create needs-ruling -R "$r" --force --color D93F0B --description "Blocked on a decision"
  for s in XS S M L XL; do
    gh label create "effort:$s" -R "$r" --force --color C5DEF5 --description "Size $s"
  done
done
```

With deploys:

```sh
for r in $REPOS; do
  for d in na pending staging production; do
    gh label create "deploy:$d" -R "$r" --force --color FEF2C0 --description "Deploy: $d"
  done
done
```

Sprint labels (`sprint-1`, …) are made as sprints open. A label `wf` adds
that does not exist yet is created by GitHub in a default colour, so a
missing one never fails a command.

## 5. Check

With `.workflow.toml` written, `wf board` from the repo reads the board and
resolves every configured field name; an empty board prints nothing and
exits 0. Option names are checked the first time a command sets one.
