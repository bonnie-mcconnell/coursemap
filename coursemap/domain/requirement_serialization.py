"""
Serialize and deserialize requirement trees to/from JSON-friendly dicts.
Dataset format: each node is a dict with "type" and type-specific fields.
"""
from __future__ import annotations

from .requirement_nodes import (
    AllOfRequirement,
    AnyOfRequirement,
    ChooseCreditsRequirement,
    ChooseNRequirement,
    CourseRequirement,
    MajorRequirement,
    MaxLevelCreditsRequirement,
    MinLevelCreditsFromRequirement,
    MinLevelCreditsRequirement,
    RequirementNode,
    TotalCreditsRequirement,
)
from .requirement_utils import collect_course_codes


def requirement_to_dict(node: RequirementNode) -> dict:
    """Convert a RequirementNode to a JSON-serializable dict."""
    if isinstance(node, CourseRequirement):
        return {"type": "COURSE", "course_code": node.course_code}
    if isinstance(node, AllOfRequirement):
        return {
            "type": "ALL_OF",
            "children": [requirement_to_dict(c) for c in node.children],
        }
    if isinstance(node, AnyOfRequirement):
        return {
            "type": "ANY_OF",
            "children": [requirement_to_dict(c) for c in node.children],
        }
    if isinstance(node, ChooseCreditsRequirement):
        return {
            "type": "CHOOSE_CREDITS",
            "credits": node.credits,
            "course_codes": list(node.course_codes),
            "open_pool": node.open_pool,
        }
    if isinstance(node, ChooseNRequirement):
        return {
            "type": "CHOOSE_N",
            "n": node.n,
            "course_codes": list(node.course_codes),
        }
    if isinstance(node, MinLevelCreditsRequirement):
        return {
            "type": "MIN_LEVEL_CREDITS",
            "level": node.level,
            "min_credits": node.min_credits,
        }
    if isinstance(node, MinLevelCreditsFromRequirement):
        return {
            "type": "MIN_LEVEL_CREDITS_FROM",
            "level": node.level,
            "min_credits": node.min_credits,
            "course_codes": list(node.course_codes),
        }
    if isinstance(node, MaxLevelCreditsRequirement):
        return {
            "type": "MAX_LEVEL_CREDITS",
            "level": node.level,
            "max_credits": node.max_credits,
        }
    if isinstance(node, TotalCreditsRequirement):
        return {"type": "TOTAL_CREDITS", "required_credits": node.required_credits}
    if isinstance(node, MajorRequirement):
        return {
            "type": "MAJOR",
            "name": node.name,
            "requirement": requirement_to_dict(node.requirement),
        }
    raise ValueError(f"Unknown requirement node type: {type(node)}")


def _sanitize_code(raw: str) -> str:
    """Strip the 'Course code:' prefix that the scraper sometimes produces."""
    s = raw.strip()
    if s.lower().startswith("course code:"):
        return s[len("course code:"):].strip()
    return s


def requirement_from_dict(data: dict) -> RequirementNode:
    """Parse a dict (e.g. from JSON) into a RequirementNode."""
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError("Requirement dict must have 'type' key")
    typ = data["type"]
    if typ == "COURSE":
        return CourseRequirement(course_code=_sanitize_code(data["course_code"]))
    if typ == "ALL_OF":
        raw_children = data.get("children", [])
        if not raw_children:
            raise ValueError("ALL_OF requirement must have at least one child")
        children = [requirement_from_dict(c) for c in raw_children]
        return AllOfRequirement(children=tuple(children))
    if typ == "ANY_OF":
        raw_children = data.get("children", [])
        if not raw_children:
            raise ValueError("ANY_OF requirement must have at least one child")
        children = [requirement_from_dict(c) for c in raw_children]
        return AnyOfRequirement(children=tuple(children))
    if typ == "CHOOSE_CREDITS":
        credits = int(data["credits"])
        if credits < 0:
            raise ValueError(f"CHOOSE_CREDITS credits must be non-negative, got {credits}")
        codes = tuple(_sanitize_code(c) for c in data.get("course_codes", []))
        # An empty course_codes list with positive credits is unambiguous in
        # the source data: it always means "choose freely," never "no courses
        # qualify" (a genuinely-impossible pool wouldn't be authored at all).
        open_pool = data.get("open_pool", not codes and credits > 0)
        return ChooseCreditsRequirement(credits=credits, course_codes=codes, open_pool=open_pool)
    if typ == "CHOOSE_N":
        n = int(data["n"])
        if n < 1:
            raise ValueError(f"CHOOSE_N n must be at least 1, got {n}")
        codes = tuple(_sanitize_code(c) for c in data.get("course_codes", []))
        return ChooseNRequirement(n=n, course_codes=codes)
    if typ == "MIN_LEVEL_CREDITS":
        return MinLevelCreditsRequirement(
            level=int(data["level"]),
            min_credits=int(data["min_credits"]),
        )
    if typ == "MIN_LEVEL_CREDITS_FROM":
        return MinLevelCreditsFromRequirement(
            level=int(data["level"]),
            min_credits=int(data["min_credits"]),
            course_codes=tuple(data["course_codes"]),
        )
    if typ == "MAX_LEVEL_CREDITS":
        return MaxLevelCreditsRequirement(
            level=int(data["level"]),
            max_credits=int(data["max_credits"]),
        )
    if typ == "TOTAL_CREDITS":
        return TotalCreditsRequirement(required_credits=int(data["required_credits"]))
    if typ == "MAJOR":
        if "name" not in data:
            raise ValueError("MAJOR requirement dict missing 'name' field")
        if "requirement" not in data:
            raise ValueError("MAJOR requirement dict missing 'requirement' field")
        return MajorRequirement(
            name=data["name"],
            requirement=requirement_from_dict(data["requirement"]),
        )
    raise ValueError(f"Unknown requirement type: {typ!r}")

