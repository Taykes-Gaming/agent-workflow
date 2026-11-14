"""`.workflow.toml`: which board, which repos, which field and option names.

Only names live here. Field and option ids are looked up from the board once
per run (`board.Board`), never written down: rewriting a single-select field's
options regenerates every option id.
"""

import functools
import importlib.util
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .gh import WfError

FILENAME = ".workflow.toml"
STATUS_VERBS = ("backlog", "ready", "progress", "blocked", "review", "done")
REQUIRED_FIELDS = ("status", "size", "model", "findings", "rounds", "ktokens")
OPTIONAL_FIELDS = ("turns", "cache_ktokens", "review_ktokens", "deploy", "release")
LEVELS = ("low", "medium", "high", "xhigh", "max")  # wf review --level
APIS = ("rest", "graphql")  # the top-level `api`
ENGINES = ("claude", "opencode")  # wf review --engine
OPENCODE_KEYS = ("model", "variant", "base", "contract")  # [opencode], for wf dispatch
GATE_KEYS = ("steps", "when", "base")  # [gate], for wf gate
MAP_KEYS = ("budget", "scip", "index", "languages", "base", "env", "common")  # [map], for wf map / wf index
WAIT_KEYS = ("until", "fail", "timeout", "run", "file", "context")  # a [wait.<name>] preset, for wf wait
STEP_KEYS = ("run", "name", "xml")  # a [gate] step written as a table
ASSETS = Path(__file__).resolve().parent / "review_assets"  # wf-review's prompts and prep script, shipped with wf
_REF = re.compile(r"^(?:(?P<alias>[A-Za-z0-9_.-]+)|(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))#(?P<n>[0-9]+)$")


class UsageError(Exception):
    """A malformed command line; reported as `wf: <message>`, exit 2."""


@dataclass(frozen=True)
class Ref:
    repo: str  # owner/name
    number: int


@dataclass
class Config:
    owner: str
    owner_type: str
    number: int
    repos: dict  # alias → owner/name
    fields: dict  # key → board field name
    status: dict  # verb → option name
    labels: dict
    path: Path
    api: str = "rest"  # which GitHub API the agents may use: "rest" or "graphql" (lifts the GraphQL bans)
    review: dict = field(default_factory=dict)  # [review]: level, model, budget_usd, engine, opencode_model, lane_model, tiered, lanes, tiers; defaults in commands.review
    opencode: dict = None  # [opencode]: model, variant, base, contract; defaults in commands.dispatch
    gate: dict = None  # [gate], normalised: {steps: [step], when: {glob: [step]}, base}; step = {run, name, xml}
    map: dict = None  # [map]: budget, scip, index, languages, base, env, common; defaults in commands.map
    wait: dict = None  # [wait]: name → {until, fail, timeout, run, file, context}; defaults in commands.wait

    @property
    def project_path(self):
        kind = "orgs" if self.owner_type == "org" else "users"
        return f"/{kind}/{self.owner}/projectsV2/{self.number}"

    def parse_ref(self, text):
        m = _REF.match(text)
        if not m:
            raise UsageError(f"bad issue reference '{text}': want <alias>#<n> or <owner>/<name>#<n>")
        if m["alias"]:
            if m["alias"] not in self.repos:
                raise UsageError(f"unknown repo alias '{m['alias']}' in '{text}'; known: {', '.join(self.repos)}")
            return Ref(self.repos[m["alias"]], int(m["n"]))
        return Ref(m["repo"], int(m["n"]))

    def show_ref(self, repo, number):
        for alias, full in self.repos.items():
            if full.lower() == repo.lower():
                return f"{alias}#{number}"
        return f"{repo}#{number}"


@functools.cache
def review_prep():
    """The wf-review plugin's review_prep.py (symlinked into ASSETS) as a module: wf takes its tier and lane rules
    from there, so they live in one file the plugin also runs without wf."""
    spec = importlib.util.spec_from_file_location("wf_review_prep", ASSETS / "review_prep.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find():
    if os.environ.get("WF_CONFIG"):
        return Path(os.environ["WF_CONFIG"])
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / FILENAME).is_file():
            return d / FILENAME
    raise WfError(f"no {FILENAME} in {here} or any parent, and WF_CONFIG is not set")


def load(path=None):
    path = Path(path) if path else find()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise WfError(f"cannot read config {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise WfError(f"{path}: {e}") from None

    def need(table, key, where):
        if key not in table:
            raise WfError(f"{path}: missing {where}.{key}")
        return table[key]

    def section(name, required=True):
        """A top-level table whose values are all strings (or [board], checked below)."""
        if name not in raw:
            if required:
                raise WfError(f"{path}: missing [{name}]")
            return {}
        value = raw[name]
        if not isinstance(value, dict):
            raise WfError(f"{path}: [{name}] must be a table, not {type(value).__name__}")
        if name != "board":
            for k, v in value.items():
                if not isinstance(v, str):
                    raise WfError(f"{path}: [{name}].{k} must be a string, not {type(v).__name__}")
        return dict(value)

    api = raw.get("api", "rest")
    if not isinstance(api, str) or api not in APIS:
        raise WfError(f'{path}: api must be "rest" or "graphql", not {api!r}')
    board = section("board")
    owner_type = need(board, "owner_type", "[board]")
    if owner_type not in ("user", "org"):
        raise WfError(f"{path}: [board].owner_type must be \"user\" or \"org\", not {owner_type!r}")
    fields = section("fields")
    for key in REQUIRED_FIELDS:
        need(fields, key, "[fields]")
    unknown = set(fields) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise WfError(f"{path}: unknown [fields] keys: {', '.join(sorted(unknown))}")
    status = section("status")
    for verb in STATUS_VERBS:
        need(status, verb, "[status]")
    labels = section("labels")
    for key in ("backlog", "size_prefix", "deploy_prefix"):
        if not need(labels, key, "[labels]"):
            raise WfError(f"{path}: [labels].{key} must not be empty")
    owner = need(board, "owner", "[board]")
    if not isinstance(owner, str) or not owner:
        raise WfError(f"{path}: [board].owner must be a login string, not {owner!r}")
    number = need(board, "number", "[board]")
    if isinstance(number, str) and number.isdigit():
        number = int(number)  # "2" was accepted before; keep accepting it
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise WfError(f"{path}: [board].number must be a positive integer, not {number!r}")
    if not isinstance(raw.get("review", {}), dict):
        raise WfError(f"{path}: [review] must be a table")
    review = dict(raw.get("review") or {})
    unknown = set(review) - {"level", "model", "budget_usd", "engine", "opencode_model", "lane_model", "tiered",
                             "lanes", "tiers"}
    if unknown:
        raise WfError(f"{path}: unknown [review] keys: {', '.join(sorted(unknown))}")
    if "budget_usd" in review and not _number(review["budget_usd"], above=0):
        raise WfError(f"{path}: [review].budget_usd must be a positive number")
    for key in ("level", "model", "engine", "opencode_model", "lane_model"):
        if key in review and not isinstance(review[key], str):
            raise WfError(f"{path}: [review].{key} must be a string")
    if "level" in review and review["level"] not in LEVELS:
        raise WfError(f"{path}: [review].level must be one of {', '.join(LEVELS)}")
    if not isinstance(review.get("tiered", False), bool):
        raise WfError(f"{path}: [review].tiered must be true or false")
    if review.get("engine", "claude") not in ENGINES:
        raise WfError(f"{path}: [review].engine must be claude or opencode")
    if "lanes" in review or "tiers" in review:
        errors = review_prep().lane_config_errors(review)
        if errors:
            raise WfError("\n".join(f"{path}: {e}" for e in errors))
    if not isinstance(raw.get("opencode", {}), dict):
        raise WfError(f"{path}: [opencode] must be a table")
    opencode = section("opencode", required=False)
    unknown = set(opencode) - set(OPENCODE_KEYS)
    if unknown:
        raise WfError(f"{path}: unknown [opencode] keys: {', '.join(sorted(unknown))}")
    gate = _gate(raw.get("gate"), path) if "gate" in raw else None
    map_ = _map(raw.get("map"), path) if "map" in raw else None
    wait = _wait(raw.get("wait"), path) if "wait" in raw else None
    return Config(
        owner=owner,
        owner_type=owner_type,
        number=number,
        repos=section("repos", required=False),
        fields=fields,
        status=status,
        labels=labels,
        path=path,
        api=api,
        review=review,
        opencode=opencode,
        gate=gate,
        map=map_,
        wait=wait,
    )


def _number(value, above=None, at_least=None, at_most=None, integer=False):
    """True for a number (an int when `integer`, never a bool) inside the given bounds."""
    kinds = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds):
        return False
    return ((above is None or value > above) and (at_least is None or value >= at_least)
            and (at_most is None or value <= at_most))


def _commands(value, where, keys, path):
    """A list of commands, each a string or a table with run and some of `keys`, all non-empty strings;
    returns tables."""
    if not isinstance(value, list):
        raise WfError(f"{path}: {where} must be a list of commands")
    out = []
    for i, c in enumerate(value):
        at = f"{where}[{i}]"
        if isinstance(c, str):
            c = {"run": c}
        if not isinstance(c, dict):
            raise WfError(f"{path}: {at} must be a command or a table with run")
        bad = set(c) - set(keys)
        if bad:
            raise WfError(f"{path}: unknown keys in {at}: {', '.join(sorted(bad))}")
        for key in keys:
            if key in c and not (isinstance(c[key], str) and c[key].strip()):
                raise WfError(f"{path}: {at}.{key} must be a non-empty string")
        if "run" not in c:
            raise WfError(f"{path}: {at} needs run")
        out.append(c)
    return out


def _gate(raw, path):
    """[gate] checked and normalised: every step becomes {run, name, xml}, `when` maps glob → steps."""
    if not isinstance(raw, dict):
        raise WfError(f"{path}: [gate] must be a table")
    unknown = set(raw) - set(GATE_KEYS)
    if unknown:
        raise WfError(f"{path}: unknown [gate] keys: {', '.join(sorted(unknown))}")

    def steps(value, where):
        return [{"run": c["run"], "name": c.get("name") or c["run"], "xml": c.get("xml")}
                for c in _commands(value, where, STEP_KEYS, path)]

    when = raw.get("when", {})
    if not isinstance(when, dict):
        raise WfError(f"{path}: [gate.when] must be a table of glob = [commands]")
    if "base" in raw and not isinstance(raw["base"], str):
        raise WfError(f"{path}: [gate].base must be a string")
    gate = {"steps": steps(raw.get("steps", []), "[gate].steps"),
            "when": {glob: steps(v, f'[gate.when]."{glob}"') for glob, v in when.items()},
            "base": raw.get("base")}
    if not gate["steps"] and not gate["when"]:
        raise WfError(f"{path}: [gate] has no steps")
    return gate


def _map(raw, path):
    """[map] checked and normalised: budget a positive int; scip a path or a list of them (always a list after);
    index a list of commands or {run, out} tables (always tables after); languages a list of strings; env a
    table of strings."""
    from .codemap import LANGUAGES
    if not isinstance(raw, dict):
        raise WfError(f"{path}: [map] must be a table")
    unknown = set(raw) - set(MAP_KEYS)
    if unknown:
        raise WfError(f"{path}: unknown [map] keys: {', '.join(sorted(unknown))}")
    if "budget" in raw and not _number(raw["budget"], at_least=50, integer=True):
        raise WfError(f"{path}: [map].budget must be an integer of at least 50")
    if "base" in raw and not (isinstance(raw["base"], str) and raw["base"].strip()):
        raise WfError(f"{path}: [map].base must be a non-empty string")
    if "common" in raw and not _number(raw["common"], above=0, at_most=1):
        raise WfError(f"{path}: [map].common must be a share of files between 0 and 1")
    out = dict(raw)
    if "scip" in raw:
        scip = [raw["scip"]] if isinstance(raw["scip"], str) else raw["scip"]
        if not (isinstance(scip, list) and scip and all(isinstance(x, str) and x.strip() for x in scip)):
            raise WfError(f"{path}: [map].scip must be an index path or a list of them")
        out["scip"] = scip
    if "languages" in raw and not (isinstance(raw["languages"], list)
                                   and all(isinstance(x, str) and x.strip() for x in raw["languages"])):
        raise WfError(f"{path}: [map].languages must be a list of non-empty strings")
    if "index" in raw:
        out["index"] = [{"run": c["run"], "out": c.get("out")}
                        for c in _commands(raw["index"], "[map].index", ("run", "out"), path)]
    if "env" in raw and not (isinstance(raw["env"], dict) and all(isinstance(v, str) for v in raw["env"].values())):
        raise WfError(f"{path}: [map].env must be a table of strings")
    bad = set(raw.get("languages", [])) - set(LANGUAGES)
    if bad:
        raise WfError(f"{path}: unknown [map].languages: {', '.join(sorted(bad))}; known: {', '.join(LANGUAGES)}")
    return out


def _wait(raw, path):
    """[wait] checked: a table of presets, each with `until` (a regex) and some of fail, timeout, run, file,
    context; at most one of run and file."""
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        raise WfError(f"{path}: [wait] must hold [wait.<name>] tables")
    out = {}
    for name, preset in raw.items():
        where = f"[wait.{name}]"
        unknown = set(preset) - set(WAIT_KEYS)
        if unknown:
            raise WfError(f"{path}: unknown {where} keys: {', '.join(sorted(unknown))}")
        if "until" not in preset:
            raise WfError(f"{path}: {where} needs until")
        for key in ("until", "fail", "timeout", "run", "file"):
            if key in preset and not (isinstance(preset[key], str) and preset[key].strip()):
                raise WfError(f"{path}: {where}.{key} must be a non-empty string")
        for key in ("until", "fail"):
            if key in preset:
                try:
                    re.compile(preset[key])
                except re.error as e:
                    raise WfError(f"{path}: {where}.{key} is not a regex: {e}") from None
        if "context" in preset and (isinstance(preset["context"], bool) or not isinstance(preset["context"], int)
                                    or preset["context"] < 0):
            raise WfError(f"{path}: {where}.context must be a non-negative integer")
        if "run" in preset and "file" in preset:
            raise WfError(f"{path}: {where} names both run and file; pick one")
        out[name] = dict(preset)
    return out
