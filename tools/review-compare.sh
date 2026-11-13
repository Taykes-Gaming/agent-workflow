#!/usr/bin/env bash
#
# review-compare.sh <pr> <variant>…
#
# Runs each review variant headless on one PR and records what it cost.
# builtin and lean post nothing; plugin always posts (see below). Run it from a checkout of the PR's repo: each variant
# runs in a throwaway detached worktree at the PR's head commit, so it sees
# neither later commits nor anything written after the PR (this repo's
# review notes included). Compare variants blind: on a PR whose outcome is
# not yet known.
#
#   builtin   /code-review <pr> high            (Claude Code's built-in review)
#   lean      /wf-review:review <pr> --no-post  (this repo's plugin)
#   plugin    /code-review:code-review <pr>     (POSTS to the PR: the plugin
#             always comments, so run it only on a PR that should get a review)
#
# Each run writes docs/reviews/runs/<owner>-<name>-<pr>-<variant>.json (the
# `claude -p --output-format json` result) next to this repo's docs; a summary
# table of the runs is printed at the end. REVIEW_BUDGET_USD caps each run
# (default 15); REVIEW_MODEL sets the session model (default: the user's) and
# REVIEW_TAG is appended to the run name.

set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# -ge 2 && "$1" =~ ^[0-9]+$ ]] || { sed -n '3,/^$/p' "${BASH_SOURCE[0]}"; exit 2; }
pr="$1"; shift
repo="$(git remote get-url origin | sed -E 's#.*github\.com[:/]##; s#\.git$##')"
runs="${here}/docs/reviews/runs"
mkdir -p "${runs}"
outs=()
git fetch -q origin "pull/${pr}/head"
head="$(git rev-parse FETCH_HEAD)"
scratch="$(mktemp -d)"
tree="${scratch}/pr-${pr}"
git worktree add -q --detach "${tree}" "${head}"
trap 'git worktree remove --force "${tree}"; rm -rf "${scratch}"' EXIT

for variant in "$@"; do
    extra=()
    case "${variant}" in
        builtin) prompt="/code-review ${pr} high. Also read the issue the PR closes (gh api repos/${repo}/issues/<n>) and any CLAUDE.md files, and check the change against them." ;;
        lean)    prompt="/wf-review:review ${pr} --no-post --force --out .wf-review-lean.md"
                 extra=("Write") ;;
        plugin)  prompt="/code-review:code-review ${pr}"
                 extra=("Bash(gh pr view:*)" "Bash(gh pr diff:*)" "Bash(gh pr comment:*)" "Bash(gh pr list:*)"
                        "Bash(gh issue view:*)" "Bash(gh issue list:*)" "Bash(gh search:*)") ;;
        *) echo "unknown variant '${variant}'" >&2; exit 2 ;;
    esac
    name="${repo//\//-}-${pr}-${variant}${REVIEW_TAG:+-${REVIEW_TAG}}"
    out="${runs}/${name}.json"
    ( cd "${tree}" && claude -p "${prompt}" \
        --output-format json \
        --no-session-persistence \
        --plugin-dir "${here}/plugins/wf-review" \
        --max-budget-usd "${REVIEW_BUDGET_USD:-15}" \
        ${REVIEW_MODEL:+--model "${REVIEW_MODEL}"} \
        --disallowedTools "Bash(git * --output*)" "Bash(ast-grep * -U*)" "Bash(ast-grep * --update-all*)" \
        --allowedTools "Read" "Grep" "Glob" "Agent" "Task" ${extra[@]+"${extra[@]}"} \
            "Bash(gh api:*)" "Bash(git log:*)" "Bash(git blame:*)" "Bash(git show:*)" "Bash(git diff:*)" "Bash(ast-grep run:*)" "Bash(python3:*)" \
        > "${out}" ) || true
    [[ "${variant}" == lean && -f "${tree}/.wf-review-lean.md" ]] && mv "${tree}/.wf-review-lean.md" "${runs}/${name}.md"
    outs+=("${out}")
done
python3 "${here}/tools/review_summary.py" "${outs[@]}"
