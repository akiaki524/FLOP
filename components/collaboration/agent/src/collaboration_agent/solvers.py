"""Closed registry: task data cannot select modules, commands, URLs or templates."""

from dataclasses import dataclass
import math
import re
from types import MappingProxyType

from .empirical import agent_fields, normal_nim, quoted_limit, validation_pair
from .bounded import inline_table, integer_reference, shortest_path

from .model import (check, citation, document_value, fields, outcome,
                    source_for)


def exact(task):
    params = task["params"]
    fields(params, "candidate reference")
    a = source_for(task, params["candidate"])
    b = source_for(task, params["reference"])
    check(a["id"] != b["id"], "distinct_sources_required")
    matched = a["text"] == b["text"]
    return outcome("COMPLETED", "exact_comparison", value={"equal": matched},
                   verdict="MATCH" if matched else "MISMATCH",
                   evidence=[citation(s, {"kind": "text"}, s["text"]) for s in (a, b)])


def extract(task):
    params = task["params"]
    fields(params, "source pointer")
    source = source_for(task, params["source"])
    value = document_value(source["text"].encode("utf-8"), params["pointer"])
    return outcome("COMPLETED", "structured_extraction", value=value,
                   verdict="NOT_APPLICABLE", evidence=[citation(source,
                   {"kind": "json_pointer", "pointer": params["pointer"]}, value)])


def lines(task):
    params = task["params"]
    fields(params, "source first last")
    source = source_for(task, params["source"])
    first, last = params["first"], params["last"]
    rows = source["text"].splitlines()
    check(type(first) is int and type(last) is int and 1 <= first <= last <= len(rows),
          "invalid_line_range")
    quote = "\n".join(rows[first - 1:last])
    return outcome("COMPLETED", "literal_lines", value=quote, verdict="NOT_APPLICABLE",
                   evidence=[citation(source, {"kind": "lines", "first": first, "last": last}, quote)])


def gcd_lcm(task):
    fields(task["params"], "source")
    source = source_for(task, task["params"]["source"])
    # Full grammar only: no substring routing, eval, arbitrary math or added instructions.
    number = r"(0|[1-9][0-9]{0,99})"
    match = re.fullmatch(r"Compute gcd\(" + number + r", " + number +
                         r"\) and lcm\(" + number + r", " + number + r"\)\.", source["text"])
    if not match:
        return outcome("UNKNOWN", "unsupported_math_template")
    a, b, c, d = map(int, match.groups())
    check((a, b) == (c, d), "inconsistent_operands")
    value = f"gcd={math.gcd(a, b)} lcm={math.lcm(a, b)}"
    return outcome("COMPLETED", "integer_gcd_lcm", value=value, verdict="NOT_APPLICABLE",
                   evidence=[citation(source, {"kind": "text"}, source["text"])])


@dataclass(frozen=True)
class Solver:
    name: str
    version: int
    solve: object


REGISTRY = MappingProxyType({
    "exact.match": Solver("exact.match", 1, exact),
    "json.extract": Solver("json.extract", 2, extract),
    "text.lines": Solver("text.lines", 1, lines),
    "math.gcd_lcm": Solver("math.gcd_lcm", 1, gcd_lcm),
    "math.normal_nim": Solver("math.normal_nim", 1, normal_nim),
    "docs.quoted_limit": Solver("docs.quoted_limit", 1, quoted_limit),
    "docs.agent_fields": Solver("docs.agent_fields", 1, agent_fields),
    "public.validation": Solver("validation.ordered_seq_pair", 1, validation_pair),
    "validation.integer_reference": Solver("validation.integer_reference", 1, integer_reference),
    "tables.inline": Solver("tables.inline", 1, inline_table),
    "math.shortest_path": Solver("math.shortest_path", 1, shortest_path),
})


def classify(task):
    return REGISTRY.get(task["family"])
