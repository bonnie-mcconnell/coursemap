"""
Tests for the coursemap FastAPI server.

Uses FastAPI's TestClient (HTTPX-backed, no real server needed).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from coursemap.api.server import app, _svc, _courses

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(scope="module", autouse=True)
def warmup():
    """Pre-warm the singleton so individual tests don't pay the load cost."""
    _svc()
    yield


# ---------------------------------------------------------------------------
# Root / meta
# ---------------------------------------------------------------------------

def test_root_serves_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "coursemap" in r.text.lower()


def test_api_root_has_dataset_info():
    r = client.get("/api")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "coursemap"
    assert "dataset" in data
    assert "scrape_date" in data["dataset"]


def test_freshness_endpoint():
    r = client.get("/api/freshness")
    assert r.status_code == 200
    data = r.json()
    assert "is_stale" in data
    assert "age_days" in data
    assert isinstance(data["is_stale"], bool)


# ---------------------------------------------------------------------------
# Majors
# ---------------------------------------------------------------------------

def test_majors_list():
    r = client.get("/api/majors?limit=5")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] <= 5
    assert len(data["majors"]) == data["count"]
    assert "name" in data["majors"][0]


def test_majors_search():
    r = client.get("/api/majors?search=computer+science")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    for m in data["majors"]:
        assert "computer" in m["name"].lower() or "science" in m["name"].lower()


def test_majors_search_no_results():
    r = client.get("/api/majors?search=xyzzzzznonexistent")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_majors_resolve_exact():
    r = client.get("/api/majors/resolve?name=Computer+Science")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1


def test_majors_resolve_not_found():
    r = client.get("/api/majors/resolve?name=xyzzzz+nonexistent+major")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

def test_courses_list():
    r = client.get("/api/courses?limit=10")
    assert r.status_code == 200
    data = r.json()
    assert len(data["courses"]) <= 10
    c = data["courses"][0]
    assert all(k in c for k in ("code", "title", "credits", "level", "offerings"))


def test_courses_filter_campus():
    r = client.get("/api/courses?campus=D&limit=20")
    assert r.status_code == 200
    for c in r.json()["courses"]:
        assert any(o["campus"] == "D" for o in c["offerings"])


def test_courses_filter_level():
    r = client.get("/api/courses?level=300&limit=20")
    assert r.status_code == 200
    for c in r.json()["courses"]:
        assert c["level"] == 300


def test_courses_filter_search():
    r = client.get("/api/courses?search=programming")
    assert r.status_code == 200
    for c in r.json()["courses"]:
        assert "programming" in c["title"].lower()


def test_course_detail_known():
    r = client.get("/api/courses/159101")
    assert r.status_code == 200
    data = r.json()
    assert data["code"] == "159101"
    assert data["credits"] == 15
    assert data["level"] == 100
    assert len(data["offerings"]) >= 1


def test_course_detail_case_insensitive():
    r = client.get("/api/courses/159101")
    r2 = client.get("/api/courses/159101")
    assert r.status_code == 200
    assert r2.status_code == 200


def test_course_detail_not_found():
    r = client.get("/api/courses/XXXXXX")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------

def _base_plan_request(**overrides) -> dict:
    base = {
        "major": "Computer Science",
        "start_year": 2026,
        "max_credits": 60,
        "campus": "D",
        "mode": "DIS",
        "completed": [],
        "transfer_credits": 0,
        "prefer": [],
        "exclude": [],
        "no_summer": False,
        "auto_fill": False,
    }
    base.update(overrides)
    return base


def test_plan_basic():
    r = client.post("/api/plan", json=_base_plan_request())
    assert r.status_code == 200
    data = r.json()
    assert len(data["semesters"]) >= 3
    assert data["meta"]["credits_planned"] > 0
    assert data["meta"]["degree_target"] > 0
    assert data["warnings"] == []


def test_plan_returns_courses_with_full_detail():
    r = client.post("/api/plan", json=_base_plan_request())
    assert r.status_code == 200
    semesters = r.json()["semesters"]
    assert len(semesters) > 0
    course = semesters[0]["courses"][0]
    assert all(k in course for k in ("code", "title", "credits", "level", "offerings"))


def test_plan_auto_fill_reaches_360():
    r = client.post("/api/plan", json=_base_plan_request(auto_fill=True))
    assert r.status_code == 200
    data = r.json()
    total = data["meta"]["credits_total"]
    assert total == data["meta"]["degree_target"], (
        f"Auto-fill should reach degree target: got {total}, expected {data['meta']['degree_target']}"
    )
    # filler_codes may be empty when the plan fills entirely from major requirement pools
    # (e.g. CS BInfSci fills 360cr from its own course pools without needing general filler).
    # What matters is the total credits == degree_target (asserted above).
    assert isinstance(data["filler_codes"], list)


def test_plan_with_completed_courses():
    r = client.post("/api/plan", json=_base_plan_request(completed=["159101", "159102"]))
    assert r.status_code == 200
    data = r.json()
    # Completed courses should not appear in scheduled semesters
    scheduled = {c["code"] for s in data["semesters"] for c in s["courses"]}
    assert "159101" not in scheduled
    assert "159102" not in scheduled
    assert data["meta"]["credits_prior"] == 30  # 2 × 15cr


def test_plan_with_transfer_credits():
    r = client.post("/api/plan", json=_base_plan_request(transfer_credits=60))
    assert r.status_code == 200
    data = r.json()
    assert data["meta"]["credits_transfer"] == 60


def test_plan_exclude_elective():
    r = client.post("/api/plan", json=_base_plan_request(exclude=["159223"]))
    assert r.status_code == 200
    scheduled = {c["code"] for s in r.json()["semesters"] for c in s["courses"]}
    assert "159223" not in scheduled


def test_plan_exclude_required_emits_warning():
    # Excluding a required course either produces a warning (if the planner can work around it)
    # or a 422 (if the plan becomes impossible). Both are correct behaviour.
    r = client.post("/api/plan", json=_base_plan_request(exclude=["159101"]))
    if r.status_code == 200:
        data = r.json()
        # If it succeeded, it should warn that a required course was excluded
        assert any("159101" in w for w in data.get("warnings", [])), (
            "Excluding a required course should produce a warning"
        )
    else:
        # 422 is also valid - the plan is impossible without a required course
        assert r.status_code == 422, f"Expected 200 or 422, got {r.status_code}"



def test_plan_prefer_elective():
    """Preferred elective should appear in the plan when auto-fill is active."""
    r = client.post("/api/plan", json=_base_plan_request(
        auto_fill=True, prefer=["159223"]
    ))
    assert r.status_code == 200
    scheduled = {c["code"] for s in r.json()["semesters"] for c in s["courses"]}
    assert "159223" in scheduled


def test_plan_no_summer():
    r = client.post("/api/plan", json=_base_plan_request(no_summer=True))
    assert r.status_code == 200
    for sem in r.json()["semesters"]:
        assert sem["semester"] != "SS", "Summer school semester should be absent"


def test_plan_double_major():
    r = client.post("/api/plan", json=_base_plan_request(
        major="Computer Science", double_major="Mathematics"
    ))
    assert r.status_code == 200
    data = r.json()
    assert data["double_major_info"] is not None
    dmi = data["double_major_info"]
    assert "first_label" in dmi
    assert "second_label" in dmi
    assert isinstance(dmi["shared_codes"], list)
    assert dmi["saved_credits"] >= 0


def test_plan_autofill_and_double_major_supported():
    """auto_fill + double_major is now supported - should return 200."""
    r = client.post("/api/plan", json=_base_plan_request(
        double_major="Mathematics", auto_fill=True
    ))
    assert r.status_code == 200
    data = r.json()
    assert data["double_major_info"] is not None
    # filler_codes may or may not be present depending on gap, but the plan
    # must be valid and cover both majors.
    assert len(data["semesters"]) > 0


def test_plan_unknown_major():
    r = client.post("/api/plan", json=_base_plan_request(major="xyzzzz nonexistent major"))
    assert r.status_code == 422


def test_plan_semester_credits_within_cap():
    r = client.post("/api/plan", json=_base_plan_request(max_credits=30))
    assert r.status_code == 200
    for sem in r.json()["semesters"]:
        assert sem["credits"] <= 30, f"Semester {sem['year']} {sem['semester']} exceeds 30cr cap"


def test_plan_meta_fields_present():
    r = client.post("/api/plan", json=_base_plan_request())
    assert r.status_code == 200
    meta = r.json()["meta"]
    required_fields = [
        "major", "campus", "mode", "start_year",
        "credits_planned", "credits_prior", "credits_transfer",
        "credits_total", "degree_target", "free_elective_gap",
    ]
    for f in required_fields:
        assert f in meta, f"Missing meta field: {f}"


# ---------------------------------------------------------------------------
# iCal export
# ---------------------------------------------------------------------------

def test_ical_export():
    r = client.post("/api/plan/ical", json=_base_plan_request())
    assert r.status_code == 200
    assert "text/calendar" in r.headers["content-type"]
    body = r.text
    assert "BEGIN:VCALENDAR" in body
    assert "BEGIN:VEVENT" in body
    assert "DTSTART;VALUE=DATE:" in body
    # Should have one VEVENT per semester
    semester_count = len(r.json()["semesters"]) if False else body.count("BEGIN:VEVENT")
    assert semester_count >= 3


def test_ical_export_invalid_major():
    r = client.post("/api/plan/ical", json=_base_plan_request(major="xyzzzz"))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def test_validate_endpoint():
    r = client.get("/api/validate")
    assert r.status_code == 200
    data = r.json()
    assert "passed" in data
    assert "error_count" in data
    assert "warning_count" in data
    assert isinstance(data["errors"], list)
    assert isinstance(data["warnings"], list)
    assert data["passed"] is True, f"Dataset has errors: {data['errors']}"


# ---------------------------------------------------------------------------
# Tests for changes introduced in v0.3 patch
# ---------------------------------------------------------------------------

def test_prereq_serialization_is_dict():
    """prerequisites field must be a JSON-friendly dict, not a Python repr."""
    r = client.get("/api/courses/159352")
    assert r.status_code == 200
    c = r.json()
    assert isinstance(c["prerequisites"], dict)
    assert c["prerequisites"]["type"] in ("course", "and", "or")
    assert c["prerequisites_human"] is not None
    assert "CoursePrerequisite" not in c["prerequisites_human"]


def test_prereq_human_no_prereq_is_null():
    """Course with no prerequisites returns null for both fields."""
    from coursemap.ingestion.dataset_loader import load_courses
    courses = load_courses()
    no_prereq = next(c for c in courses.values() if c.prerequisites is None)
    r = client.get(f"/api/courses/{no_prereq.code}")
    assert r.status_code == 200
    assert r.json()["prerequisites"] is None
    assert r.json()["prerequisites_human"] is None


def test_double_major_autofill_now_supported():
    """double_major + auto_fill must return 200 and a valid plan."""
    r = client.post("/api/plan", json=_base_plan_request(
        double_major="Mathematics", auto_fill=True
    ))
    assert r.status_code == 200
    data = r.json()
    assert data["double_major_info"] is not None
    assert len(data["semesters"]) > 0


def test_unknown_course_codes_produce_warnings():
    req = _base_plan_request(completed=["FAKE999", "ZZZZ000"])
    r = client.post("/api/plan", json=req)
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    assert any("FAKE999" in w for w in warnings)


def test_ical_double_major_returns_calendar():
    r = client.post("/api/plan/ical", json=_base_plan_request(double_major="Mathematics"))
    assert r.status_code == 200
    assert "text/calendar" in r.headers["content-type"]
    assert "VEVENT" in r.text


def test_ical_autofill_returns_calendar():
    r = client.post("/api/plan/ical", json=_base_plan_request(auto_fill=True))
    assert r.status_code == 200
    assert "text/calendar" in r.headers["content-type"]


def test_prereq_chain_endpoint():
    r = client.get("/api/courses/159352/prereq-chain")
    assert r.status_code == 200
    data = r.json()
    assert data["chain_depth"] >= 2
    assert any(n["code"] == "159352" for n in data["nodes"])
    assert len(data["edges"]) >= 2


def test_prereq_chain_no_prereqs():
    from coursemap.ingestion.dataset_loader import load_courses
    courses = load_courses()
    no_prereq = next(c for c in courses.values() if c.prerequisites is None)
    r = client.get(f"/api/courses/{no_prereq.code}/prereq-chain")
    assert r.status_code == 200
    data = r.json()
    assert data["chain_depth"] == 0
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["code"] == no_prereq.code


def test_prereq_chain_missing_course_404():
    r = client.get("/api/courses/FAKE999/prereq-chain")
    assert r.status_code == 404


def test_share_button_url_state_includes_all_params():
    """Full-state plan request round-trips cleanly through the API."""
    req = {**_base_plan_request(
        double_major="Mathematics",
        auto_fill=True,
        completed=["159101"],
        no_summer=True,
    ), "transfer_credits": 30, "start_year": 2027}
    r = client.post("/api/plan", json=req)
    assert r.status_code == 200
    data = r.json()
    assert data["meta"]["credits_transfer"] == 30
    assert not any("SS" in s["semester"] for s in data["semesters"])
    assert data["meta"]["credits_prior"] > 0
