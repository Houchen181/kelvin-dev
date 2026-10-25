"""Recipes: a measurement in stages that checks its own results, kept so that it can be run again.

    recipe: find-dirac-point
    description: Coarse, then fine gate sweep around the Dirac point.
    values: {span: 2}                        # numbers the stages use; run_recipe can give others
    stages:
      - name: coarse
        plan: |                              # a plan as run_plan takes it, with {{values}} and {{arithmetic}}
          plan: coarse_gate
          sweep: {smu.V: {from: {{-span}}, to: {{span}}, points: 81}}
          measure: [lockin.X]
        pass: max(lockin.X) > 3 * median(lockin.X)   # over the run's columns, as compute takes it
        keep: {dirac: "at_max(lockin.X, smu.V)"}     # values for the later stages, from this stage's run
        retries: 1                                   # how often to try again when the check fails
        retry: {span: span * 2}                      # what changes before each new try
      - name: fine
        plan: |
          plan: fine_gate
          sweep: {smu.V: {from: {{dirac - 0.2}}, to: {{dirac + 0.2}}, points: 101}}
          measure: [lockin.X]

Each stage runs through the safety gate like any plan (limits, approvals, device protection). A stage whose check
still fails after its retries, or a run that stops (a trip, a declined approval, the stop button), ends the recipe.
Expressions are expr.py's, so nothing else in Python is reachable from a recipe."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from . import expr
from .yamlio import load_yaml

MAX_STAGES = 10
MAX_RETRIES = 5
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_SLOT = re.compile(r"\{\{(.+?)\}\}")


class RecipeError(ValueError):
    pass


@dataclass
class Stage:
    name: str
    plan: str
    passes: str = ""                                          # the check on the stage's run ("pass" in the file)
    keep: Dict[str, str] = field(default_factory=dict)       # value name: expression over the run's columns
    retries: int = 0
    retry: Dict[str, str] = field(default_factory=dict)      # value name: expression over the values


@dataclass
class Recipe:
    name: str
    description: str
    values: Dict[str, float]
    stages: List[Stage]


def slug(text: Any) -> str:
    """A name for a recipe or a skill: lower case, digits and dashes."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower())).strip("-")[:48]


def _names(mapping: Any, what: str) -> Dict[str, str]:
    if mapping in (None, ""):
        return {}
    if not isinstance(mapping, dict):
        raise RecipeError("%s must be a mapping of names to expressions" % what)
    out = {}
    for k, v in mapping.items():
        if not _VALUE.match(str(k)):
            raise RecipeError("%s: %r is not a value name (letters, digits, _); put an expression with a comma in "
                              "quotes" % (what, k))
        out[str(k)] = str(v).strip()
    return out


def parse(source: Any) -> Recipe:
    """A recipe from its YAML text or mapping. RecipeError: what is wrong with it."""
    try:
        data = load_yaml(source) if isinstance(source, str) else source
    except yaml.YAMLError as e:
        raise RecipeError("not valid YAML: %s" % str(e).splitlines()[0])
    if not isinstance(data, dict):
        raise RecipeError("a recipe is a mapping with recipe, description, values and stages")
    unknown = set(data) - {"recipe", "description", "values", "stages"}
    if unknown:
        raise RecipeError("unknown key %s (a recipe has recipe, description, values, stages)" % ", ".join(sorted(unknown)))
    name = slug(data.get("recipe"))
    if not NAME.match(name):
        raise RecipeError("recipe: a name is required (letters, digits, dashes)")
    values: Dict[str, float] = {}
    for k, v in (data.get("values") or {}).items():
        if not _VALUE.match(str(k)) or isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RecipeError("values: %s must be a name with a number" % k)
        values[str(k)] = float(v)
    stages_in = data.get("stages")
    if not isinstance(stages_in, list) or not 1 <= len(stages_in) <= MAX_STAGES:
        raise RecipeError("stages: a list of 1 to %d stages" % MAX_STAGES)
    stages = []
    for n, s in enumerate(stages_in, 1):
        if not isinstance(s, dict):
            raise RecipeError("stage %d is not a mapping" % n)
        unknown = set(s) - {"name", "plan", "pass", "keep", "retries", "retry"}
        if unknown:
            raise RecipeError("stage %d: unknown key %s" % (n, ", ".join(sorted(unknown))))
        if not isinstance(s.get("plan"), str) or not s["plan"].strip():
            raise RecipeError("stage %d: plan is the plan's text (a YAML block, as run_plan takes it)" % n)
        try:
            retries = int(s.get("retries") or 0)
        except (TypeError, ValueError):
            raise RecipeError("stage %d: retries is a whole number" % n)
        if not 0 <= retries <= MAX_RETRIES:
            raise RecipeError("stage %d: retries is 0 to %d" % (n, MAX_RETRIES))
        stages.append(Stage(name=str(s.get("name") or "stage %d" % n)[:48], plan=s["plan"].strip() + "\n",
                            passes=str(s.get("pass") or "").strip(), keep=_names(s.get("keep"), "stage %d: keep" % n),
                            retries=retries, retry=_names(s.get("retry"), "stage %d: retry" % n)))
    recipe = Recipe(name, " ".join(str(data.get("description") or "").split())[:300], values, stages)
    check(recipe)
    return recipe


def dump(recipe: Recipe) -> str:
    """The recipe as YAML, its plans as blocks."""
    class Dumper(yaml.SafeDumper):
        pass

    def text(dumper: Any, value: str) -> Any:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)
    Dumper.add_representer(str, text)
    stages = []
    for s in recipe.stages:
        one: Dict[str, Any] = {"name": s.name, "plan": s.plan}
        if s.passes:
            one["pass"] = s.passes
        if s.keep:
            one["keep"] = dict(s.keep)
        if s.retries:
            one["retries"] = s.retries
        if s.retry:
            one["retry"] = dict(s.retry)
        stages.append(one)
    data = {"recipe": recipe.name, "description": recipe.description, "values": dict(recipe.values),
            "stages": stages}
    return yaml.dump(data, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=120)


def calc(expression: str, values: Dict[str, float]) -> float:
    """An expression over the recipe's values (no run's columns): one number."""
    import numpy as np

    def value(key: str) -> Any:
        if key not in values:
            raise KeyError("no value %r in this recipe (it has %s)" % (key, ", ".join(sorted(values)) or "none"))
        return np.float64(values[key])
    try:
        out = np.asarray(expr.evaluate(expression, value, np), dtype=float)
    except ValueError as e:
        raise RecipeError("%s: %s" % (expression, e))
    if out.ndim:
        raise RecipeError("%s is not one number" % expression)
    return float(out)


def render(text: str, values: Dict[str, float]) -> str:
    """The text with each {{expression}} replaced by its number."""
    return _SLOT.sub(lambda m: "%.12g" % calc(m.group(1), values), text)


def check(recipe: Recipe) -> None:
    """Every {{...}}, retry and later stage uses only values the recipe has by then; every plan is YAML."""
    known = {k: 1.0 for k in recipe.values}
    for s in recipe.stages:
        try:
            for k, e in s.retry.items():
                if k not in recipe.values and k not in known:
                    raise RecipeError("retry changes %s, which is not one of the recipe's values" % k)
                calc(e, known)
            plan = load_yaml(render(s.plan, known))
            import numpy as np
            for e in [s.passes] + list(s.keep.values()):
                if e:   # its form (any column name will do here: the run's own are checked when it runs)
                    expr.evaluate(render(e, known), lambda key: np.ones(4), np)
        except RecipeError as e:
            raise RecipeError("stage %s: %s" % (s.name, e))
        except ValueError as e:
            raise RecipeError("stage %s: %s" % (s.name, e))
        except yaml.YAMLError as e:
            raise RecipeError("stage %s: its plan is not valid YAML once filled in: %s" % (
                s.name, str(e).splitlines()[0]))
        if not isinstance(plan, dict) or "plan" not in plan:
            raise RecipeError("stage %s: its plan needs a name (plan: ...), a sweep and what to measure" % s.name)
        for k in s.keep:
            known[k] = 1.0


def start_values(recipe: Recipe, given: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """The recipe's values with the ones given for this run (only names the recipe has)."""
    values = dict(recipe.values)
    for k, v in (given or {}).items():
        if k not in values:
            raise RecipeError("%s is not one of this recipe's values (%s)" % (k, ", ".join(sorted(values)) or "none"))
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RecipeError("%s must be a number" % k)
        values[k] = float(v)
    return values


def shown(values: Dict[str, float]) -> str:
    return ", ".join("%s = %.6g" % kv for kv in values.items()) or "none"
