"""One module per `wf` command.

Each module has `add_parser(sub, parents)` and `run(args, cfg, board)`, a
generator yielding one `(line, record)` per action so output appears as each
action lands. Everything a command can reject without the network (verbs,
refs, configured fields) is checked before its first call.
"""

import re

from .. import reads
from ..config import UsageError
from ..gh import WfError

DEFAULT_BASE = "dev"  # the base branch when neither a flag nor the config names one
MAX_LINES = 40  # lines a listing command prints before "… and N more"

SIZES = ("XS", "S", "M", "L", "XL")
# Deploy verb → option name, as on every board that has a Deploy field.
DEPLOY = {"na": "n/a", "pending": "Pending", "staging": "Staging", "production": "Production"}


def refs(cfg, texts):
    return [cfg.parse_ref(t) for t in texts]


def need_field(cfg, key, why):
    if key not in cfg.fields:
        raise WfError(f"{why} needs [fields].{key} in {cfg.path}")


def set_deploy(cfg, board, item, verb, option, also=None):
    """Sets Deploy to an already-resolved option, with the fields of `also` in the same PATCH,
    and mirrors the `deploy:*` label."""
    board.set_fields(item, {**(also or {}), "deploy": option})
    prefix = cfg.labels["deploy_prefix"]
    board.swap_label(item, prefix, prefix + verb)


MODEL_FAMILY = re.compile(r"claude-([a-z]+)")


# GitHub's closing keywords, before `#n`, `owner/name#n` or an issue URL.
_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
                      r"(?:https://github\.com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)(\d+)\b", re.I)


def closing_refs(body):
    """The issues a PR body closes, in order, as (owner/name or None for the PR's own repo, number)."""
    return [(m[1] or m[2], int(m[3])) for m in _CLOSING.finditer(body or "")]


def board_model(name):
    """The Model option a seat records under: `opus` for a Claude implementer, `opencode-<family>` for the
    OpenCode seat. `opencode-anthropic/claude-opus-5-5` and `opencode-opus` both give `opencode-opus`, so
    the full provider/model id `wf dispatch` ran with can be passed as it is, `#variant` and all."""
    if not name.startswith("opencode-"):
        return name
    rest = name.removeprefix("opencode-")
    model = rest.split("/", 1)[-1].split("#", 1)[0]
    family = MODEL_FAMILY.search(model)
    return "opencode-" + (family[1] if family else model)


def frontmatter_stripped(text):
    """`text` without its leading `---` YAML block."""
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def base_branch(cfg, section=None, override=None):
    """The branch work is measured against or cut from: an explicit value, the section's `base`,
    `[opencode].base`, then the default. One rule for gate and map; dispatch applies the same order in `settings`."""
    return (override or (section or {}).get("base") or ((cfg.opencode if cfg else None) or {}).get("base")
            or DEFAULT_BASE)


def changed(runner, base, fallback=False):
    """Files changed against origin/<base> plus uncommitted ones (renames by their new name), or None when
    git cannot say. With `fallback`, a base git cannot resolve as `origin/<base>` is tried as a plain ref."""
    files = reads.edited_files(runner.git, f"origin/{base}...HEAD")
    if files is None and fallback:
        files = reads.edited_files(runner.git, base)
    return files


def truncate(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"


def seconds(s):
    """A duration for a summary line: 12s, 3m05s."""
    return f"{s // 60:.0f}m{s % 60:02.0f}s" if s >= 60 else f"{s:.0f}s"


def regex(text, flag):
    """`text` compiled, None for None; a bad pattern is a usage error naming the flag."""
    if text is None:
        return None
    try:
        return re.compile(text)
    except re.error as e:
        raise UsageError(f"--{flag} is not a regex: {e}") from None


__all__ = ["DEFAULT_BASE", "DEPLOY", "MODEL_FAMILY", "SIZES", "UsageError", "WfError", "base_branch", "board_model",
           "changed", "frontmatter_stripped", "need_field", "refs", "regex", "seconds", "set_deploy", "truncate"]
