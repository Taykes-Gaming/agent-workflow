"""wf merged <ref> --findings N --rounds N [--model M] [--ktokens N] [--cache-ktokens N] [--turns N]
                [--deploy na|pending] [--pr REF].

The spend comes from the ledger (`wf hook`, `wf dispatch`, `wf review`) of the
merged PR's head branch, in this checkout's git directory: Model, kTokens,
Cache kTokens and Turns summed over its `agent` lines (one per agent, the last
line of a fix round winning), Review kTokens over its `review` lines, kept
apart so a model's kTokens are its own work. A flag given by hand wins over
what the ledger says; a checkout without the hook passes `--model` and
`--ktokens` by hand.

Refuses unless a merged PR references the issue (`--pr` names it; otherwise
the issue's timeline is searched, preferring a PR whose body closes the issue
over one that only mentions it), and refuses with no spend to record, both
before any write. Then the merge-time fields, Status Done, Deploy (default
`pending` when a deploy field is configured), mirroring the backlog and
deploy:* labels, and last the issue is closed as completed: GitHub honours
`Closes #n` only on a merge to the default branch, so a PR merged to `dev`
leaves it open. All over REST; `gh issue close` is GraphQL.
"""

from .. import ledger
from . import DEPLOY, WfError, board_model, closing_refs, need_field, set_deploy, usage


def _count(text):
    n = int(text)
    if n < 0:
        raise ValueError(text)
    return n


def add_parser(sub, parents):
    p = sub.add_parser("merged", parents=parents, help="record the merge-time fields and move to Done")
    p.add_argument("ref")
    p.add_argument("--model", type=board_model,
                   help="a Model option; opencode-<model> for the OpenCode seat (default: from the ledger)")
    p.add_argument("--findings", type=_count, required=True)
    p.add_argument("--rounds", type=_count, required=True)
    p.add_argument("--ktokens", type=_count, help="by hand, instead of the ledger's agents")
    p.add_argument("--cache-ktokens", type=_count)
    p.add_argument("--turns", type=_count)
    p.add_argument("--deploy", choices=("na", "pending"))
    p.add_argument("--pr", metavar="REF", help="the merged PR (<alias>#<n>); default: a merged PR on the issue's timeline")


def merged_pr(gh, cfg, ref, pr):
    """The merged PR for the issue, as (owner/name, number, head branch); a WfError when there is none."""
    if pr is None:
        # A PR that only mentions the issue must not win over the one that closes it.
        merged = []
        for event in gh.get_all(f"/repos/{ref.repo}/issues/{ref.number}/timeline?per_page=100"):
            source = (event.get("source") or {}).get("issue") or {}
            if event.get("event") == "cross-referenced" and (source.get("pull_request") or {}).get("merged_at"):
                merged.append(source)
                if _closes(source, ref):
                    break
        if not merged:
            raise WfError(f"no merged PR references {cfg.show_ref(ref.repo, ref.number)}; "
                          "merge it first, or name it with --pr")
        source = merged[-1] if _closes(merged[-1], ref) else merged[0]
        repo, number = (source.get("repository") or {}).get("full_name") or ref.repo, source["number"]
    else:
        repo, number = pr.repo, pr.number
    data = gh.get(f"/repos/{repo}/pulls/{number}")
    if not data.get("merged_at"):
        raise WfError(f"{cfg.show_ref(repo, number)} is not merged")
    return repo, number, (data.get("head") or {}).get("ref")


def _closes(source, ref):
    """Whether a PR's body closes the issue (`Closes #n` from its own repo, `Fixes owner/name#n` or the
    issue's URL from any); repo names compare without case, as GitHub's do."""
    own = (source.get("repository") or {}).get("full_name") or ref.repo
    return any(n == ref.number and (repo or own).lower() == ref.repo.lower()
               for repo, n in closing_refs(source.get("body")))


def _common():
    try:
        return ledger.where()[0]
    except (WfError, OSError):
        return None


def spend(args, entries, pr_number, where):
    """{model, ktokens, cache_ktokens, turns, agents, review}: the hand-given flags over the ledger's sums.
    `where` names the ledger in the refusal when it has no agent and no --ktokens was given."""
    agents = ledger.latest(entries, "agent")
    reviews = [e for e in ledger.of_kind(entries, "review") if e.get("pr") in (None, pr_number)]
    review = None
    if reviews:
        review = {"runs": len(reviews), "variants": [e.get("variant") for e in reviews],
                  **{k: sum(e.get(k) or 0 for e in reviews) for k in ("tokens", "cache_read", "turns")},
                  "cost_usd": round(sum(e.get("cost_usd") or 0 for e in reviews), 2)}
    if args.ktokens is None and not agents:
        raise WfError(f"the ledger has no agent for {where}: this checkout has no `wf hook` record of the work. "
                      "Pass --model and --ktokens by hand (and --cache-ktokens, --turns)")
    model = args.model
    if model is None and not agents:
        raise WfError("--ktokens by hand needs --model: the ledger has no agent to take it from")
    if model is None:
        families = list(dict.fromkeys(f for e in agents for f in str(e.get("model") or "").split("/") if f))
        if len(families) != 1:
            raise WfError(f"the agents ran on {'/'.join(families) or 'no known model'}; pass --model")
        model = families[0]

    def total(key):
        return sum(e.get(key) or 0 for e in agents) if agents else None

    cache, turns = total("cache_read"), total("turns")
    return {"model": model,
            "ktokens": args.ktokens if args.ktokens is not None else usage.kilo(total("tokens")),
            "cache_ktokens": args.cache_ktokens if args.cache_ktokens is not None else
            (usage.kilo(cache) if cache is not None else None),
            "turns": args.turns if args.turns is not None else turns,
            "agents": [e.get("id") for e in agents] if args.ktokens is None else [],
            "review": review}


def run(args, cfg, board):
    if args.turns is not None:
        need_field(cfg, "turns", "--turns")
    if args.deploy is not None:
        need_field(cfg, "deploy", "--deploy")
    deploy = args.deploy or ("pending" if "deploy" in cfg.fields else None)
    ref = cfg.parse_ref(args.ref)
    pr = cfg.parse_ref(args.pr) if args.pr else None
    common = _common()

    pr_repo, pr_number, branch = merged_pr(board.gh, cfg, ref, pr)
    shown_pr = cfg.show_ref(pr_repo, pr_number)
    if common is None:
        entries, where = [], "this checkout (not a git checkout)"
    elif branch is None:
        entries, where = [], f"{shown_pr} (its head branch is unknown)"
    else:
        entries, where = ledger.read(common, branch), f"{branch} ({ledger.path(common, branch)})"
    s = spend(args, entries, pr_number, where)
    model_name = s["model"]

    try:
        model = board.option_id("model", model_name)
    except WfError as e:
        if not model_name.startswith("opencode-"):
            raise
        raise WfError(f"{e}. Add '{model_name}' to the Model field in the board's field settings first "
                      "(the board UI keeps existing values; rewriting the options over the API does not)") from None
    done_name = cfg.status["done"]
    done = board.option_id("status", done_name)
    deploy_option = board.option_id("deploy", DEPLOY[deploy]) if deploy else None

    review = s["review"] if "review_ktokens" in cfg.fields else None
    cache = s["cache_ktokens"] if "cache_ktokens" in cfg.fields else None
    turns = s["turns"] if "turns" in cfg.fields else None
    item = board.item(ref)
    values = {"model": model, "findings": args.findings, "rounds": args.rounds, "ktokens": s["ktokens"]}
    if cache is not None:
        values["cache_ktokens"] = cache
    if turns is not None:
        values["turns"] = turns
    if review:
        values["review_ktokens"] = usage.kilo(review["tokens"])
    values["status"] = done
    if deploy:
        set_deploy(cfg, board, item, deploy, deploy_option, values)
    else:
        board.set_fields(item, values)
    board.set_label(item, cfg.labels["backlog"], False)
    issue = board.gh.get(f"/repos/{ref.repo}/issues/{ref.number}")
    closed_now = issue.get("state") != "closed"
    if closed_now:
        board.gh.patch(f"/repos/{ref.repo}/issues/{ref.number}", {"state": "closed", "state_reason": "completed"})
        item.state = "closed"

    shown = cfg.show_ref(ref.repo, ref.number)
    if "review_ktokens" not in cfg.fields:
        reviewed = ""
    elif review:
        reviewed = f", review kTokens {usage.kilo(review['tokens'])} over {review['runs']} run(s)"
    else:
        reviewed = ", no wf review run in the ledger"
    counted = ([f"agents {', '.join(map(str, s['agents']))}"] if s["agents"] else []) + (
        [f"reviews {', '.join(map(str, review['variants']))}"] if review else [])
    tail = (f", Deploy {DEPLOY[deploy]}" if deploy else "") + (", closed" if closed_now else "")
    line = (f"{shown}: {model_name}, findings {args.findings}, rounds {args.rounds}, kTokens {s['ktokens']}"
            + (f", cache kTokens {cache}" if cache is not None else "")
            + (f", turns {turns}" if turns is not None else "")
            + f"{reviewed} ({shown_pr})" + (f" [{'; '.join(counted)}]" if counted else "")
            + f" → {done_name}{tail}")
    yield line, {
        "ref": shown, "model": model_name, "findings": args.findings, "rounds": args.rounds,
        "ktokens": s["ktokens"], "cache_ktokens": cache, "turns": turns, "review": review,
        "agents": s["agents"], "branch": branch, "status": done_name,
        "deploy": DEPLOY[deploy] if deploy else None, "pr": shown_pr, "closed": closed_now,
    }
