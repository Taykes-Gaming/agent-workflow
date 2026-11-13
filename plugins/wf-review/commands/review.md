---
allowed-tools: Bash(python3:*), Bash(gh api:*), Bash(git log:*), Bash(git blame:*), Bash(git show:*), Bash(git diff:*), Bash(ast-grep run:*), Read, Write, Grep, Glob, Agent
argument-hint: <PR number> [--no-post] [--out FILE] [--force] [--since SHA --previous FILE]
description: Lean PR review — scripted prep, 2–4 parallel lanes, scoring in this session, one comment
---

Review pull request $ARGUMENTS with the wf-review recipe. Run it from a session that did **not** write the change; if this session wrote it, say so in the comment's footer.

Be economical: no agents except the lanes in step 2, no re-fetching what prep already wrote, and REST only (`gh api …`; never `gh pr …`, `gh issue …` or `gh api graphql`).

1. **Prep (no agents).** Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_prep.py" <PR>` (append `--force`, `--since SHA` and `--previous FILE` if they were given) and read the `prep.json` it prints.
   - If `stop` is set and `--force` was not given, report the reason in one line and stop.
   - Note `diff`, `issue.path`, `claude_md`, `history_lane`, `lint`, `since`, `tools` and `map` (a `wf map` around the changed files, or null with the reason in `map_skipped`). If `since` is set this is a **fix round**: run only the correctness lane. If `claude_md_incomplete` is true (GitHub truncated the tree), tell the spec lane to find CLAUDE.md files with Glob in the checkout.
2. **Lanes.** In **one** message, launch these `wf-review:review-lane` agents in parallel (correctness only in a fix round). Give each its lane name, the prep directory (`dir` in prep.json), whether `map` is set, and the lane brief below:
   - **correctness**: read `diff.patch` and the changed functions in full; find bugs the change introduces (logic, error paths, state, ordering, resource handling).
   - **spec**: read `issue.md` and the CLAUDE.md files listed in prep; find places where the change contradicts the issue's design, numbers or constraints, misses something it requires, or breaks a house rule a CLAUDE.md states explicitly (quote the rule). Skip this lane if there is no issue and no CLAUDE.md.
   - **history**, only if `history_lane` is true: read the history of the touched files (`git log -p` on them, the review comments on PRs that last touched them via `gh api`), plus any code the issue says this change replaces or ports. Find behaviour the old code or past reviews handled that this change regresses.
   - **reuse**: for each function, type, constant, helper or non-trivial block the diff adds, find an existing equivalent in the codebase that it should have used instead (ast-grep when `tools.ast_grep` is true). Report duplication, not defects.
3. **Score here (no agents).** Collect the lanes' findings and merge duplicates. For each finding, open the cited code yourself and confirm or refute it:
   - **Dropped:** false, pre-existing, a lint or style matter, or an intended behaviour the issue asks for; for reuse, the existing code does not really do the same job. Keep one line saying why.
   - Otherwise keep the lane's severity and likelihood, correcting either if the code shows it wrong.
   - **Report:** `critical` at any likelihood, or `major` and `likely` or `plausible`.
   - **Reuse:** every confirmed reuse finding.
   - **Noted:** every other confirmed finding.
   - In a fix round, also read `since.previous_review` (when set) and `since.review_comments` and, for each earlier finding, check the code at the head: **resolved**, **still open**, or **declined** (the PR body gives a reason).
4. **Write the review** in this exact shape (the Previous findings section only in a fix round):

   ```
   <!-- wf-review -->
   ### Review (wf-review)

   **Previous findings** (<n>)
   - **<title>** — resolved | still open | declined. <one sentence>.

   **Report** (<n>)
   1. **<title>** — <severity>, <likelihood>. <one or two sentences: the failure and why>. <link>

   **Reuse** (<n>)
   - **<what the change adds>** duplicates <existing symbol> (<link to existing>). <one sentence: what to use instead>. <link to new code>

   **Noted** (<n>)
   - **<title>** — <severity>, <likelihood>. <one sentence>. <link>

   <details><summary>Dropped (<n>)</summary>

   - <title> — <why>
   </details>

   Lanes: <lanes run>. Prep: <additions>+/<deletions>- in <n> files.< Fix round since <short sha>.>
   ```

   Links are `https://github.com/<repo>/blob/<head sha>/<path>#L<a>-L<b>`, with the full sha from prep and at least one line of context either side. Write `none` under an empty section. No emojis.
5. **Deliver.**
   - With `--no-post`, write the review to `--out FILE` (default: `review.md` in the prep directory) and print the path.
   - Otherwise post it with `gh api repos/<repo>/issues/<PR>/comments -F body=@<file>` and print the comment URL.
