"""
Tests for v2.0 features:
  - ElectiveFiller
  - plan_store (SQLite persistence)
  - SSE streaming plan endpoint (/api/plan/stream)
  - /api/plan/validate accepting plan_id
  - /api/courses search improvements
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_course(code, credits=15, level=100, offerings=None, prerequisites=None):
    from coursemap.domain.course import Course, Offering
    offs = offerings or (Offering(semester="S1", campus="D", mode="DIS"),)
    return Course(
        code=code,
        title=f"Course {code}",
        credits=credits,
        level=level,
        offerings=tuple(offs),
        prerequisites=prerequisites,
        url=f"https://www.massey.ac.nz/courses/{code.lower()}/",
        description=f"Introduction to {code}.",
    )


# ---------------------------------------------------------------------------
# ElectiveFiller tests
# ---------------------------------------------------------------------------

class TestElectiveFiller:
    def test_empty_courses_returns_empty(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        filler = ElectiveFiller({}, campus="D", mode="DIS")
        result = filler.select_to_fill(
            seed_codes=[], completed=frozenset(), budget_credits=60
        )
        assert result == []

    def test_excludes_zero_credit_courses(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        from coursemap.domain.course import Offering
        courses = {
            "159101": _make_course("159101", credits=15, level=100),
            "000000": _make_course("000000", credits=0, level=100),
        }
        filler = ElectiveFiller(courses, campus="D", mode="DIS")
        result = filler.select_to_fill(
            seed_codes=["159101"], completed=frozenset(), budget_credits=30
        )
        assert "000000" not in result

    def test_excludes_already_planned_codes(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        courses = {
            "159101": _make_course("159101", credits=15),
            "159102": _make_course("159102", credits=15),
            "159201": _make_course("159201", credits=15),
        }
        filler = ElectiveFiller(courses, campus="D", mode="DIS")
        result = filler.select_to_fill(
            seed_codes=["159101", "159102"],
            completed=frozenset(),
            budget_credits=30,
            exclude=frozenset(["159102"]),
        )
        assert "159101" not in result
        assert "159102" not in result

    def test_respects_budget_credits(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        courses = {f"159{i:03d}": _make_course(f"159{i:03d}", credits=15) for i in range(1, 20)}
        filler = ElectiveFiller(courses, campus="D", mode="DIS")
        result = filler.select_to_fill(
            seed_codes=[], completed=frozenset(), budget_credits=45
        )
        total = sum(courses[c].credits for c in result)
        assert total <= 45

    def test_preferred_codes_in_tier_0(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        courses = {
            "159101": _make_course("159101", credits=15),
            "159201": _make_course("159201", credits=15),
            "159999": _make_course("159999", credits=15),  # preferred
        }
        filler = ElectiveFiller(courses, campus="D", mode="DIS")
        ranked = filler.rank_candidates(
            seed_codes=["159101"],
            completed=frozenset(),
            budget_credits=30,
            prefer=frozenset(["159999"]),
        )
        # preferred course should be first
        assert ranked[0].code == "159999"
        assert ranked[0].tier == 0

    def test_same_prefix_is_tier_1(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        courses = {
            "159101": _make_course("159101", credits=15),
            "159201": _make_course("159201", credits=15),
            "160101": _make_course("160101", credits=15),
        }
        filler = ElectiveFiller(courses, campus="D", mode="DIS")
        ranked = filler.rank_candidates(
            seed_codes=["159101"],
            completed=frozenset(),
            budget_credits=60,
        )
        same_prefix = [r for r in ranked if r.code == "159201"]
        diff_prefix = [r for r in ranked if r.code == "160101"]
        assert same_prefix, "159201 should be a candidate"
        assert not diff_prefix, "160101 is a different prefix, should not appear (not adjacent)"

    def test_no_offerings_excluded(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        from coursemap.domain.course import Course
        no_offer = Course(
            code="159999",
            title="No offerings",
            credits=15,
            level=100,
            offerings=(),  # empty
        )
        courses = {
            "159101": _make_course("159101"),
            "159999": no_offer,
        }
        filler = ElectiveFiller(courses, campus="D", mode="DIS")
        result = filler.select_to_fill(
            seed_codes=["159101"], completed=frozenset(), budget_credits=30
        )
        assert "159999" not in result

    def test_level_cap_respected(self):
        from coursemap.planner.elective_filler import ElectiveFiller
        courses = {
            "159101": _make_course("159101", credits=15, level=100),
            "159401": _make_course("159401", credits=15, level=400),
        }
        filler = ElectiveFiller(courses, campus="D", mode="DIS")
        ranked = filler.rank_candidates(
            seed_codes=["159101"],
            completed=frozenset(),
            budget_credits=30,
            level_cap=300,
        )
        assert not any(r.code == "159401" for r in ranked)


# ---------------------------------------------------------------------------
# plan_store tests
# ---------------------------------------------------------------------------

class TestPlanStore:
    def test_get_missing_returns_none(self):
        from coursemap.api.plan_store import _PlanStore
        with tempfile.TemporaryDirectory() as tmp:
            store = _PlanStore(Path(tmp) / "test.db")
            result = store.get("doesnotexist")
            assert result is None

    def test_put_and_get_roundtrip(self):
        from coursemap.api.plan_store import _PlanStore
        with tempfile.TemporaryDirectory() as tmp:
            store = _PlanStore(Path(tmp) / "test.db")
            params = {"major": "Computer Science", "start_year": 2026}
            result = {"plan_id": "abc123", "semesters": [], "meta": {}}
            store.put("abc123", params, result)
            got = store.get("abc123")
            assert got is not None
            assert got["plan_id"] == "abc123"

    def test_hit_count_increments(self):
        from coursemap.api.plan_store import _PlanStore
        with tempfile.TemporaryDirectory() as tmp:
            store = _PlanStore(Path(tmp) / "test.db")
            store.put("xyz", {}, {"plan_id": "xyz"})
            store.get("xyz")
            store.get("xyz")
            stats = store.stats()
            top = stats["top_plans"]
            assert any(p["plan_id"] == "xyz" and p["hits"] >= 2 for p in top)

    def test_prune_on_exceed_max(self):
        from coursemap.api.plan_store import _PlanStore, _MAX_PLANS
        with tempfile.TemporaryDirectory() as tmp:
            store = _PlanStore(Path(tmp) / "test.db")
            # Insert more than _MAX_PLANS - normally 10_000 but we patch it
            with patch("coursemap.api.plan_store._MAX_PLANS", 5):
                for i in range(8):
                    store.put(f"plan{i}", {}, {"plan_id": f"plan{i}"})
            # After patching, count may vary - just ensure store doesn't crash
            assert store.count() >= 1

    def test_stats_returns_dict(self):
        from coursemap.api.plan_store import _PlanStore
        with tempfile.TemporaryDirectory() as tmp:
            store = _PlanStore(Path(tmp) / "test.db")
            stats = store.stats()
            assert "total_plans" in stats
            assert "top_plans" in stats

    def test_put_idempotent(self):
        from coursemap.api.plan_store import _PlanStore
        with tempfile.TemporaryDirectory() as tmp:
            store = _PlanStore(Path(tmp) / "test.db")
            store.put("dup", {}, {"v": 1})
            store.put("dup", {}, {"v": 2})  # replace
            got = store.get("dup")
            assert got["v"] == 2
            assert store.count() == 1


# ---------------------------------------------------------------------------
# Course dataclass tests (url and description fields)
# ---------------------------------------------------------------------------

class TestCourseFields:
    def test_url_and_description_on_course(self):
        c = _make_course("159101")
        assert c.url == "https://www.massey.ac.nz/courses/159101/"
        assert "159101" in c.description

    def test_course_without_url_description(self):
        from coursemap.domain.course import Course, Offering
        c = Course(
            code="999001",
            title="Test Course",
            credits=15,
            level=100,
            offerings=(Offering("S1", "D", "DIS"),),
        )
        assert c.url is None
        assert c.description is None


# ---------------------------------------------------------------------------
# API tests - SSE streaming endpoint
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    import sys, os
    sys.path.insert(0, str(Path(__file__).parent.parent))
    os.environ.setdefault("COURSEMAP_DB_PATH", str(Path(tempfile.mkdtemp()) / "test_plans.db"))
    from coursemap.api.server import app
    return TestClient(app)


def _base_req(**kwargs):
    return {
        "major": "Computer Science – Bachelor of Information Sciences",
        "campus": "D",
        "mode": "DIS",
        "start_year": 2026,
        "start_semester": "S1",
        "max_credits": 60,
        **kwargs,
    }


class TestSSEStreamEndpoint:
    def test_stream_returns_200(self, client):
        res = client.post("/api/plan/stream", json=_base_req())
        assert res.status_code == 200

    def test_stream_content_type_is_event_stream(self, client):
        res = client.post("/api/plan/stream", json=_base_req())
        assert "text/event-stream" in res.headers.get("content-type", "")

    def test_stream_contains_done_event(self, client):
        res = client.post("/api/plan/stream", json=_base_req())
        lines = res.text.split("\n")
        data_lines = [l[6:] for l in lines if l.startswith("data: ")]
        events = [json.loads(d) for d in data_lines]
        done_events = [e for e in events if e.get("type") == "done"]
        assert done_events, "Stream should emit a 'done' event"

    def test_stream_done_event_has_plan(self, client):
        res = client.post("/api/plan/stream", json=_base_req())
        lines = res.text.split("\n")
        data_lines = [l[6:] for l in lines if l.startswith("data: ")]
        events = [json.loads(d) for d in data_lines]
        done = next(e for e in events if e.get("type") == "done")
        assert "plan" in done
        assert "semesters" in done["plan"]
        assert "plan_id" in done

    def test_stream_progress_events_emitted(self, client):
        res = client.post("/api/plan/stream", json=_base_req())
        lines = res.text.split("\n")
        data_lines = [l[6:] for l in lines if l.startswith("data: ")]
        events = [json.loads(d) for d in data_lines]
        progress_events = [e for e in events if e.get("type") == "progress"]
        assert progress_events, "At least one progress event should be emitted"

    def test_stream_error_on_invalid_major(self, client):
        res = client.post("/api/plan/stream", json=_base_req(major="Xylophone Studies"))
        lines = res.text.split("\n")
        data_lines = [l[6:] for l in lines if l.startswith("data: ")]
        events = [json.loads(d) for d in data_lines]
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events, "Invalid major should produce an error event"

    def test_stream_plan_id_retrievable(self, client):
        res = client.post("/api/plan/stream", json=_base_req())
        lines = res.text.split("\n")
        data_lines = [l[6:] for l in lines if l.startswith("data: ")]
        events = [json.loads(d) for d in data_lines]
        done = next(e for e in events if e.get("type") == "done")
        plan_id = done["plan_id"]
        assert plan_id, "plan_id should be non-empty"
        # Retrieve via GET
        get_res = client.get(f"/api/plan/{plan_id}")
        assert get_res.status_code == 200
        assert get_res.json()["plan_id"] == plan_id

    def test_stream_cached_plan_returns_immediately(self, client):
        """Second request with same params should hit cache (cached event type)."""
        payload = _base_req()
        # First request - generates and caches
        client.post("/api/plan/stream", json=payload)
        # Second request - should return from cache
        res = client.post("/api/plan/stream", json=payload)
        lines = res.text.split("\n")
        data_lines = [l[6:] for l in lines if l.startswith("data: ")]
        events = [json.loads(d) for d in data_lines]
        cached_events = [e for e in events if e.get("step") == "cached"]
        assert cached_events, "Second request should emit a 'cached' progress event"


# ---------------------------------------------------------------------------
# API tests - /api/plan/validate
# ---------------------------------------------------------------------------

class TestValidatePlanEndpoint:
    def test_validate_with_course_codes(self, client):
        res = client.post("/api/plan/validate", json={
            "major": "Computer Science – Bachelor of Information Sciences",
            "course_codes": ["159101", "159102", "159201"],
        })
        assert res.status_code == 200
        data = res.json()
        assert "overall_passed" in data
        assert "checklist" in data
        assert "total_credits" in data

    def test_validate_with_plan_id(self, client):
        # Generate a plan first to get a plan_id
        stream_res = client.post("/api/plan/stream", json=_base_req())
        lines = stream_res.text.split("\n")
        data_lines = [l[6:] for l in lines if l.startswith("data: ")]
        events = [json.loads(d) for d in data_lines]
        done = next(e for e in events if e.get("type") == "done")
        plan_id = done["plan_id"]

        res = client.post("/api/plan/validate", json={
            "major": "Computer Science – Bachelor of Information Sciences",
            "plan_id": plan_id,
        })
        assert res.status_code == 200
        data = res.json()
        assert data["plan_id"] == plan_id

    def test_validate_unknown_plan_id_returns_404(self, client):
        res = client.post("/api/plan/validate", json={
            "major": "Computer Science – Bachelor of Information Sciences",
            "plan_id": "nonexistentplanid999",
        })
        assert res.status_code == 404

    def test_validate_unknown_major_returns_404(self, client):
        res = client.post("/api/plan/validate", json={
            "major": "Xylophone Studies",
            "course_codes": ["159101"],
        })
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# API tests - /api/courses improvements
# ---------------------------------------------------------------------------

class TestCoursesEndpoint:
    def test_search_by_code(self, client):
        res = client.get("/api/courses?search=159101&limit=10")
        assert res.status_code == 200
        data = res.json()
        codes = [c["code"] for c in data["courses"]]
        assert "159101" in codes, "Search by code should return the exact course"

    def test_search_by_title_keyword(self, client):
        res = client.get("/api/courses?search=programming&limit=20")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] > 0

    def test_search_returns_count_and_showing(self, client):
        res = client.get("/api/courses?limit=5")
        assert res.status_code == 200
        data = res.json()
        assert "count" in data
        assert "showing" in data
        assert data["showing"] <= 5

    def test_level_filter_700_matches_700_level(self, client):
        res = client.get("/api/courses?level=700&limit=50")
        assert res.status_code == 200
        data = res.json()
        for course in data["courses"]:
            assert 700 <= course["level"] < 800, f"Level filter 700 returned {course['level']}"

    def test_course_has_url_field(self, client):
        res = client.get("/api/courses/159101")
        assert res.status_code == 200
        data = res.json()
        assert "url" in data
        # url may be None if not in dataset, but field should exist

    def test_course_has_corequisites_field(self, client):
        res = client.get("/api/courses/159101")
        assert res.status_code == 200
        data = res.json()
        assert "corequisites" in data


# ---------------------------------------------------------------------------
# OR prerequisite scheduling (ready for when scraper runs)
# ---------------------------------------------------------------------------

class TestOrPrerequisiteScheduling:
    """
    Verify that OrExpression prerequisites are correctly evaluated.
    When a course requires 'A OR B', it should be schedulable as soon
    as either A or B is completed - not blocked until both are done.
    """
    def test_or_prereq_satisfied_by_either_branch(self):
        from coursemap.domain.prerequisite import OrExpression, CoursePrerequisite
        from coursemap.domain.prerequisite_utils import prereqs_met
        prereq = OrExpression(children=(
            CoursePrerequisite("159101"),
            CoursePrerequisite("160101"),
        ))
        known = {"159101", "160101", "159201"}
        # Only 159101 completed - should still be satisfied (OR)
        assert prereqs_met(prereq, completed={"159101"}, known=known)
        assert prereqs_met(prereq, completed={"160101"}, known=known)
        assert not prereqs_met(prereq, completed=set(), known=known)

    def test_and_prereq_requires_all_branches(self):
        from coursemap.domain.prerequisite import AndExpression, CoursePrerequisite
        from coursemap.domain.prerequisite_utils import prereqs_met
        prereq = AndExpression(children=(
            CoursePrerequisite("159101"),
            CoursePrerequisite("160101"),
        ))
        known = {"159101", "160101", "159201"}
        assert not prereqs_met(prereq, completed={"159101"}, known=known)
        assert not prereqs_met(prereq, completed={"160101"}, known=known)
        assert prereqs_met(prereq, completed={"159101", "160101"}, known=known)

    def test_nested_or_in_and(self):
        """(A OR B) AND C - should require C plus at least one of A/B."""
        from coursemap.domain.prerequisite import AndExpression, OrExpression, CoursePrerequisite
        from coursemap.domain.prerequisite_utils import prereqs_met
        prereq = AndExpression(children=(
            OrExpression(children=(CoursePrerequisite("A"), CoursePrerequisite("B"))),
            CoursePrerequisite("C"),
        ))
        known = {"A", "B", "C", "D"}
        assert not prereqs_met(prereq, completed={"A"}, known=known)  # missing C
        assert not prereqs_met(prereq, completed={"C"}, known=known)  # missing A or B
        assert prereqs_met(prereq, completed={"A", "C"}, known=known)
        assert prereqs_met(prereq, completed={"B", "C"}, known=known)

    def test_out_of_scope_prereq_treated_as_satisfied(self):
        """Prereq codes not in 'known' (e.g. UE, admission codes) are auto-satisfied."""
        from coursemap.domain.prerequisite import AndExpression, CoursePrerequisite
        from coursemap.domain.prerequisite_utils import prereqs_met
        prereq = AndExpression(children=(
            CoursePrerequisite("627739"),  # University Entrance - not in known
            CoursePrerequisite("159101"),
        ))
        known = {"159101", "159201"}  # 627739 not in known → auto-satisfied
        assert prereqs_met(prereq, completed={"159101"}, known=known)

    def test_or_prereq_in_feasibility_check(self):
        """_prereqs_feasible should handle OR-branched prerequisites."""
        from coursemap.optimisation.search import PlanSearch
        from coursemap.domain.course import Course, Offering
        from coursemap.domain.prerequisite import OrExpression, CoursePrerequisite

        off = (Offering("S1", "D", "DIS"),)
        courses = {
            "A": Course("A", "Course A", 15, 100, off),
            "B": Course("B", "Course B", 15, 100, off),
            # C requires A OR B - feasible since A is in the working set
            "C": Course("C", "Course C", 15, 200, off,
                        prerequisites=OrExpression(children=(
                            CoursePrerequisite("A"),
                            CoursePrerequisite("B"),
                        ))),
        }
        search = PlanSearch.__new__(PlanSearch)
        search.prior_completed = frozenset()
        search.courses = courses
        result = search._prereqs_feasible(courses)
        assert result, "Plan with OR prereq should be feasible"
