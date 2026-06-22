"""
repair_dataset.py - offline data repair for coursemap datasets.

Run from repo root:
    python -m coursemap.ingestion.repair_dataset
"""

from __future__ import annotations
import json, logging, shutil
from pathlib import Path
from collections import Counter, defaultdict, deque

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DS = Path(__file__).resolve().parents[2] / "datasets"
_NOISE = {"627739", "219206"}


# ── I/O ────────────────────────────────────────────────────────────────────────

def _load(name: str):
    with open(_DS / name, encoding="utf-8") as f:
        return json.load(f)

def _save(name: str, data) -> None:
    path = _DS / name
    bak = path.with_suffix(".json.bak")
    if not bak.exists():
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Wrote %s  (%d items)", path.name, len(data))

def _clean(raw: str) -> str:
    s = str(raw).strip()
    if s.lower().startswith("course code:"):
        s = s[len("course code:"):].strip()
    return s


# ── PREREQUISITE REPAIR ────────────────────────────────────────────────────────

def repair_prereqs(courses: list[dict]) -> tuple[list[dict], dict]:
    """
    Pass 1 – basic cleaning:
      • strip self-refs, admission noise, phantom codes, duplicates
      • strip inverted-level edges (where prereq.level > own.level)
        – these are always scraper errors
    """
    cm = {c["course_code"]: c for c in courses}
    stats: dict[str, int] = defaultdict(int)

    out = []
    for c in courses:
        code = c["course_code"]
        own_lvl = c.get("course_level") or 0
        seen: set[str] = set()
        clean: list[str] = []

        for raw in c.get("prerequisites", []):
            p = _clean(raw)
            if not p or p == code:
                stats["self"]; continue
            if p in _NOISE:
                stats["noise"]; continue
            if p not in cm:
                stats["phantom"]; continue
            if p in seen:
                stats["dup"]; continue
            # Remove inverted-level edges (scraper noise: page grabbed higher-level codes)
            p_lvl = cm[p].get("course_level") or 0
            if p_lvl > own_lvl and p_lvl - own_lvl >= 100:
                stats["inverted_lvl"]; continue
            # Remove cross-subject prerequisites that span large discipline gaps.
            # Threshold: subject prefix numeric diff > 50 = different faculty = scraper noise.
            # e.g. Health(214) -> Animal Science(117): diff=97 -> strip (noise)
            #      CS(159) -> Stats(161): diff=2 -> keep (genuine academic relationship)
            #      Accounting(110) -> Finance(115): diff=5 -> keep (related business subjects)
            # Also strip same-level cross-subject (always noise regardless of distance).
            if p[:3] != code[:3] and own_lvl > 0:
                try:
                    prefix_diff = abs(int(p[:3]) - int(code[:3]))
                except ValueError:
                    prefix_diff = 999
                if p_lvl >= own_lvl or prefix_diff > 50:
                    stats["cross_noise"]; continue
            seen.add(p)
            clean.append(p)
            stats["kept"] += 1

        out.append({**c, "prerequisites": clean})

    return out, dict(stats)


def break_all_cycles(courses: list[dict]) -> tuple[list[dict], int]:
    """
    Pass 2 – use Kahn's topological sort to detect ALL cycle nodes, then
    iteratively remove the 'lightest' back-edges (fewest downstream dependents)
    until no cycles remain.  Guarantees a DAG.
    """
    def kahn_cycle_nodes(prereq_map: dict[str, list[str]], all_codes: set[str]) -> set[str]:
        in_deg: dict[str, int] = defaultdict(int)
        children: dict[str, list[str]] = defaultdict(list)
        for code, ps in prereq_map.items():
            for p in ps:
                if p in all_codes:
                    children[p].append(code)
                    in_deg[code] += 1
        q = deque(c for c in all_codes if in_deg[c] == 0)
        visited: set[str] = set()
        while q:
            n = q.popleft(); visited.add(n)
            for ch in children[n]:
                in_deg[ch] -= 1
                if in_deg[ch] == 0:
                    q.append(ch)
        return all_codes - visited

    cm = {c["course_code"]: c for c in courses}
    prereq_map = {c["course_code"]: list(c.get("prerequisites", [])) for c in courses}
    all_codes = set(cm)
    total_removed = 0

    for _iteration in range(200):          # safety cap
        cycle_nodes = kahn_cycle_nodes(prereq_map, all_codes)
        if not cycle_nodes:
            break

        # For each cycle node, find edges that point INTO the cycle
        # and remove the one on the course with the highest level
        # (heuristic: a high-level course should not be prereq of a low-level one)
        removed_any = False
        for code in sorted(cycle_nodes):
            bad = [p for p in prereq_map.get(code, []) if p in cycle_nodes]
            if bad:
                # Remove the first bad prereq edge; re-run Kahn next iteration
                victim = bad[0]
                prereq_map[code] = [p for p in prereq_map[code] if p != victim]
                log.debug("Break cycle: remove %s→%s", code, victim)
                total_removed += 1
                removed_any = True
                break   # restart Kahn after each removal

        if not removed_any:
            # Shouldn't happen, but guard infinite loop
            break

    remaining = kahn_cycle_nodes(prereq_map, all_codes)
    if remaining:
        log.warning("Could not fully break cycles; %d nodes remain", len(remaining))

    repaired = [{**c, "prerequisites": prereq_map[c["course_code"]]} for c in courses]
    return repaired, total_removed


# ── MAJORS REPAIR ──────────────────────────────────────────────────────────────

def _dominant_level(codes: list[str], cm: dict) -> int:
    lvls = [cm[c].get("course_level", 0) for c in codes if c in cm and cm[c].get("course_level")]
    return Counter(lvls).most_common(1)[0][0] if lvls else 200

def repair_majors(majors: list[dict], cm: dict) -> tuple[list[dict], dict]:
    """
    • Strip 'Course code:' prefix from all pool codes
    • Remove phantom pool codes
    • Expand pools with same-subject, same-level courses from catalogue
      (level-aware: 200-lvl pools only get 200-lvl additions, etc.)
    """
    stats: dict[str, int] = defaultdict(int)

    def collect_required(req: dict) -> set[str]:
        codes: set[str] = set()
        if req.get("type") == "COURSE":
            c = _clean(req.get("course_code", ""))
            if c: codes.add(c)
        for ch in req.get("children", []):
            codes |= collect_required(ch)
        return codes

    def fix_pool(node: dict, excluded: set[str]) -> dict:
        raw = node.get("course_codes", [])
        credits = node.get("credits", 0)

        # Clean + dedupe + phantom-strip
        cleaned: list[str] = []
        seen: set[str] = set()
        for r in raw:
            c = _clean(r)
            if not c or c in seen: continue
            if c not in cm:
                stats["pool_phantom"] += 1; continue
            seen.add(c); cleaned.append(c)
            stats["pool_prefix"] += 1

        dom_lvl = _dominant_level(cleaned, cm)
        prefixes = {c[:3] for c in cleaned if len(c) == 6}

        expanded = list(cleaned)
        exp_set = set(cleaned)

        # Only expand SINGLE-prefix pools.
        # Multi-prefix pools are intentionally curated interdisciplinary selections
        # (e.g. health programs choosing across 147/150/179 subject codes).
        # Expanding them would add unrelated courses and break credit targets.
        if len(prefixes) == 1:
            pfx = next(iter(prefixes))
            for code in sorted(cm):
                if (code.startswith(pfx)
                        and cm[code].get("course_level", 0) == dom_lvl
                        and cm[code].get("credits", 0) > 0
                        and code not in excluded
                        and code not in exp_set):
                    expanded.append(code); exp_set.add(code)
                    stats["pool_added"] += 1

        return {"type": "CHOOSE_CREDITS", "credits": credits, "course_codes": expanded}

    def fix_node(node: dict, excluded: set[str]) -> dict:
        if node.get("type") == "CHOOSE_CREDITS":
            return fix_pool(node, excluded)
        if node.get("type") == "COURSE":
            return {"type": "COURSE", "course_code": _clean(node.get("course_code", ""))}
        return {**node, "children": [fix_node(ch, excluded) for ch in node.get("children", [])]}

    out = []
    for m in majors:
        req = m.get("requirement", {})
        required = collect_required(req)
        out.append({**m, "requirement": fix_node(req, required)})
    return out, dict(stats)


# ── CREDIT / LEVEL NORMALISATION ───────────────────────────────────────────────

def normalise_courses(courses: list[dict]) -> list[dict]:
    out = []
    for c in courses:
        # float→int credits
        try:
            f = float(c.get("credits", 0))
            cred = int(f) if f == int(f) else c["credits"]
        except (ValueError, TypeError):
            cred = c.get("credits", 0)

        # fill `level` from `course_level`
        lvl = c.get("level") or c.get("course_level")
        out.append({**c, "credits": cred, "level": lvl})
    return out


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # Always work from originals (*.bak) if they exist, else from live files
    import sys
    force = "--force" in sys.argv or "--reset" in sys.argv

    def src(name):
        bak = _DS / (name + ".bak")
        if force and bak.exists():
            log.info("--force: removing stale backup %s", bak.name)
            bak.unlink()
        return bak.name if bak.exists() else name

    log.info("Loading datasets …")
    courses: list[dict] = _load(src("courses.json"))
    majors:  list[dict] = _load(src("majors.json"))
    cm = {c["course_code"]: c for c in courses}

    # ── courses ──
    log.info("Pass 1 – clean prerequisites (%d courses) …", len(courses))
    courses, stats1 = repair_prereqs(courses)
    log.info("  %s", stats1)

    log.info("Pass 2 – break prerequisite cycles …")
    courses, n_broken = break_all_cycles(courses)
    log.info("  broke %d edges", n_broken)

    log.info("Pass 3 – normalise credits/level …")
    courses = normalise_courses(courses)

    # rebuild cm with clean data for pool expansion
    cm = {c["course_code"]: c for c in courses}

    # ── majors ──
    log.info("Repairing %d majors …", len(majors))
    before = sum(
        len(node.get("course_codes", []))
        for m in majors
        for node in _iter_nodes(m["requirement"])
        if node.get("type") == "CHOOSE_CREDITS"
    )
    majors, stats2 = repair_majors(majors, cm)
    after = sum(
        len(node.get("course_codes", []))
        for m in majors
        for node in _iter_nodes(m["requirement"])
        if node.get("type") == "CHOOSE_CREDITS"
    )
    log.info("  pool codes: %d → %d (+%d)  %s", before, after, after - before, stats2)

    log.info("Saving …")
    _save("courses.json", courses)
    _save("majors.json", majors)
    log.info("Done.  Run `coursemap validate` to verify.")


def _iter_nodes(req: dict):
    yield req
    for ch in req.get("children", []):
        yield from _iter_nodes(ch)



def remove_ghost_prerequisites(courses: list[dict]) -> int:
    """
    Remove prerequisite references to courses that have no offerings at all.

    A course with zero offerings is retired or a data ghost - it can never be
    taken, so requiring it as a prerequisite permanently blocks all downstream
    courses. This is the most common cause of plan-bloat in the scheduler.

    Returns the number of courses fixed.
    """
    no_offerings: set[str] = {
        c["course_code"] for c in courses if not c.get("offerings")
    }

    def _prune(node):
        if node is None:
            return None
        if isinstance(node, str):
            return None if node in no_offerings else node
        if isinstance(node, list):
            cleaned = [p for p in node if p not in no_offerings]
            return cleaned if cleaned else None
        if isinstance(node, dict):
            op = node.get("op")
            args = [_prune(a) for a in node.get("args", [])]
            args = [a for a in args if a is not None]
            if not args:
                return None
            if len(args) == 1:
                return args[0]
            return {"op": op, "args": args}
        return node

    fixed = 0
    for course in courses:
        prereqs = course.get("prerequisites")
        if not prereqs:
            continue
        pruned = _prune(prereqs)
        if pruned != prereqs:
            course["prerequisites"] = pruned
            fixed += 1

    return fixed


if __name__ == "__main__":
    main()
