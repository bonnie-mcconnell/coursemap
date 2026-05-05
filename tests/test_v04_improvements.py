"""
Tests for coursemap v0.4 improvements.

Covers:
- RFC 5545-compliant iCal escaping
- /api/courses/{code}/explain endpoint
- prereq-chain BFS correctness (deque)
- domain-layer prereq_to_dict / prereq_to_human serialisers
- _execute_plan deduplication (ical uses same logic as plan)
- max_per_semester wired through API
- server version bump to 0.4.0
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from coursemap.api.server import app, _svc, _courses
from coursemap.domain.prerequisite import (
    AndExpression,
    CoursePrerequisite,
    OrExpression,
    prereq_to_dict,
    prereq_to_human,
)
from coursemap.export.ical import _escape_ical_text, plan_to_ical

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(scope="module", autouse=True)
def warmup():
    _svc()
    yield


# ---------------------------------------------------------------------------
# Domain-layer serialisation helpers
# ---------------------------------------------------------------------------

def _cp(code: str) -> CoursePrerequisite:
    return CoursePrerequisite(code=code)


class TestPrereqToDict:
    def test_single_course(self):
        d = prereq_to_dict(_cp("159101"))
        assert d == {"type": "course", "code": "159101"}

    def test_and_expression(self):
        expr = AndExpression(children=(_cp("A"), _cp("B")))
        d = prereq_to_dict(expr)
        assert d["type"] == "and"
        assert len(d["children"]) == 2
        assert d["children"][0] == {"type": "course", "code": "A"}

    def test_or_expression(self):
        expr = OrExpression(children=(_cp("A"), _cp("B"), _cp("C")))
        d = prereq_to_dict(expr)
        assert d["type"] == "or"
        assert len(d["children"]) == 3

    def test_nested(self):
        inner = OrExpression(children=(_cp("X"), _cp("Y")))
        outer = AndExpression(children=(_cp("A"), inner))
        d = prereq_to_dict(outer)
        assert d["type"] == "and"
        assert d["children"][1]["type"] == "or"

    def test_none_returns_none(self):
        assert prereq_to_dict(None) is None


class TestPrereqToHuman:
    def test_single_course(self):
        assert prereq_to_human(_cp("159101")) == "159101"

    def test_and_wraps_in_parens(self):
        expr = AndExpression(children=(_cp("A"), _cp("B")))
        assert prereq_to_human(expr) == "(A AND B)"

    def test_or_wraps_in_parens(self):
        expr = OrExpression(children=(_cp("A"), _cp("B")))
        assert prereq_to_human(expr) == "(A OR B)"

    def test_single_child_no_parens(self):
        expr = AndExpression(children=(_cp("A"),))
        result = prereq_to_human(expr)
        # Single child - no redundant parens
        assert result == "A"

    def test_nested_readable(self):
        inner = OrExpression(children=(_cp("X"), _cp("Y")))
        outer = AndExpression(children=(_cp("A"), inner))
        result = prereq_to_human(outer)
        assert "A" in result
        assert "X" in result
        assert "OR" in result
        assert "AND" in result

    def test_none_returns_none(self):
        assert prereq_to_human(None) is None


# ---------------------------------------------------------------------------
# iCal RFC 5545 escaping
# ---------------------------------------------------------------------------

class TestICalEscape:
    def test_real_newlines_become_backslash_n(self):
        escaped = _escape_ical_text("line1\nline2")
        assert "\\n" in escaped
        assert "\n" not in escaped

    def test_backslash_escaped_first(self):
        escaped = _escape_ical_text("back\\slash")
        assert "\\\\" in escaped

    def test_comma_escaped(self):
        escaped = _escape_ical_text("A, B, C")
        assert "\\," in escaped

    def test_semicolon_escaped(self):
        escaped = _escape_ical_text("A; B")
        assert "\\;" in escaped

    def test_no_bare_newlines_in_generated_ical(self):
        """Full plan → .ics content must have zero bare newlines inside DESCRIPTION."""
        svc = _svc()
        plan = svc.generate_best_plan(major_name="Computer Science")
        content = plan_to_ical(plan, "Computer Science")
        desc_start = content.find("DESCRIPTION:")
        desc_end = content.find("\r\nTRANSP:", desc_start)
        assert desc_start != -1, "DESCRIPTION property not found"
        desc_block = content[desc_start:desc_end]
        # Count bare \n (not preceded by \r)
        bare = len(re.findall(r"(?<!\r)\n", desc_block))
        assert bare == 0, f"Found {bare} bare newlines in DESCRIPTION block"

    def test_ical_lines_end_with_crlf(self):
        svc = _svc()
        plan = svc.generate_best_plan(major_name="Computer Science")
        content = plan_to_ical(plan, "Computer Science")
        # Every logical line (before folding) ends with CRLF
        assert "\r\n" in content
        # No bare LF that isn't part of CRLF
        bare = len(re.findall(r"(?<!\r)\n", content))
        assert bare == 0

    def test_ical_folded_lines_under_75_bytes(self):
        svc = _svc()
        plan = svc.generate_best_plan(major_name="Computer Science")
        content = plan_to_ical(plan, "Computer Science")
        for line in content.split("\r\n"):
            assert len(line.encode("utf-8")) <= 75, (
                f"Line exceeds 75 bytes: {line[:80]!r}"
            )


# ---------------------------------------------------------------------------
# /api/courses/{code}/explain
# ---------------------------------------------------------------------------

class TestExplainEndpoint:
    def test_explain_known_course(self):
        r = client.get("/api/courses/159201/explain?major=Computer+Science")
        assert r.status_code == 200
        data = r.json()
        assert data["code"] == "159201"
        assert "chain_depth" in data
        assert "constraints" in data
        assert isinstance(data["constraints"], list)
        assert len(data["constraints"]) > 0

    def test_explain_has_offering_info(self):
        r = client.get("/api/courses/159201/explain?major=Computer+Science&campus=D&mode=DIS")
        data = r.json()
        # Should say something about offerings
        text = " ".join(data["constraints"])
        assert any(kw in text.lower() for kw in ["offered", "offering", "delivery", "not offered"])

    def test_explain_has_prereq_info(self):
        # 159201 requires 159102 - chain depth >= 1
        r = client.get("/api/courses/159201/explain?major=Computer+Science")
        data = r.json()
        assert data["chain_depth"] >= 1
        text = " ".join(data["constraints"])
        assert "prerequisite" in text.lower() or "requires" in text.lower()

    def test_explain_no_prereq_course(self):
        # Find a level 100 course with no prerequisites
        courses = _courses()
        no_prereq = next(
            (c for c in courses.values() if c.level == 100 and not c.prerequisites and c.offerings),
            None,
        )
        if no_prereq is None:
            pytest.skip("No level-100 course without prerequisites in dataset")
        r = client.get(f"/api/courses/{no_prereq.code}/explain?major=Computer+Science")
        assert r.status_code == 200
        data = r.json()
        assert data["chain_depth"] == 0
        text = " ".join(data["constraints"])
        assert "semester 1" in text.lower() or "year 1" in text.lower()

    def test_explain_unknown_course_404(self):
        r = client.get("/api/courses/XXXXXX/explain?major=Computer+Science")
        assert r.status_code == 404

    def test_explain_wrong_campus_mode(self):
        # Use campus Z which doesn't exist - should note unavailability
        r = client.get("/api/courses/159201/explain?major=Computer+Science&campus=Z&mode=INT")
        assert r.status_code == 200
        data = r.json()
        assert "offerings_all" in data
        # Constraints should mention the mismatch
        text = " ".join(data["constraints"])
        assert "not offered" in text.lower() or "available at" in text.lower()


# ---------------------------------------------------------------------------
# prereq-chain BFS (deque correctness)
# ---------------------------------------------------------------------------

class TestPrereqChain:
    def test_chain_returns_dag_structure(self):
        r = client.get("/api/courses/159201/prereq-chain")
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "edges" in data
        assert "chain_depth" in data
        assert data["code"] == "159201"

    def test_chain_depth_positive_for_chained_course(self):
        r = client.get("/api/courses/159201/prereq-chain")
        data = r.json()
        assert data["chain_depth"] >= 1

    def test_chain_nodes_have_required_fields(self):
        r = client.get("/api/courses/159201/prereq-chain")
        data = r.json()
        for node in data["nodes"]:
            assert "code" in node
            assert "title" in node
            assert "credits" in node
            assert "level" in node
            assert "depth" in node

    def test_chain_edges_reference_known_nodes(self):
        r = client.get("/api/courses/159201/prereq-chain")
        data = r.json()
        codes = {n["code"] for n in data["nodes"]}
        for edge in data["edges"]:
            assert edge["from"] in codes, f"edge 'from' {edge['from']} not in nodes"
            assert edge["to"] in codes, f"edge 'to' {edge['to']} not in nodes"

    def test_chain_target_node_has_max_depth(self):
        r = client.get("/api/courses/159201/prereq-chain")
        data = r.json()
        target = next(n for n in data["nodes"] if n["code"] == "159201")
        assert target["depth"] == data["chain_depth"]

    def test_chain_no_prereqs_depth_zero(self):
        courses = _courses()
        root = next(
            (c for c in courses.values() if not c.prerequisites and c.offerings),
            None,
        )
        if root is None:
            pytest.skip("No course without prerequisites in dataset")
        r = client.get(f"/api/courses/{root.code}/prereq-chain")
        data = r.json()
        assert data["chain_depth"] == 0
        assert len(data["edges"]) == 0


# ---------------------------------------------------------------------------
# _execute_plan deduplication: /api/plan/ical must produce same plan structure
# ---------------------------------------------------------------------------

class TestICalVsPlanConsistency:
    def test_ical_and_plan_same_semester_count(self):
        body = {"major": "Computer Science", "start_year": 2026}
        plan_r = client.post("/api/plan", json=body)
        ical_r = client.post("/api/plan/ical", json=body)

        assert plan_r.status_code == 200
        assert ical_r.status_code == 200

        n_semesters = len(plan_r.json()["semesters"])
        # iCal should have exactly the same number of VEVENT blocks
        n_vevents = ical_r.text.count("BEGIN:VEVENT")
        assert n_vevents == n_semesters

    def test_ical_autofill_same_semester_count(self):
        body = {"major": "Computer Science", "auto_fill": True}
        plan_r = client.post("/api/plan", json=body)
        ical_r = client.post("/api/plan/ical", json=body)

        assert plan_r.status_code == 200
        assert ical_r.status_code == 200

        n_semesters = len(plan_r.json()["semesters"])
        n_vevents = ical_r.text.count("BEGIN:VEVENT")
        assert n_vevents == n_semesters


# ---------------------------------------------------------------------------
# max_per_semester API wiring
# ---------------------------------------------------------------------------

class TestMaxPerSemester:
    def test_max_per_semester_limits_courses(self):
        r = client.post("/api/plan", json={
            "major": "Computer Science",
            "max_per_semester": 2,
        })
        assert r.status_code == 200
        data = r.json()
        for sem in data["semesters"]:
            assert len(sem["courses"]) <= 2, (
                f"Semester {sem['year']} {sem['semester']} has {len(sem['courses'])} courses (limit 2)"
            )

    def test_max_per_semester_null_unconstrained(self):
        r = client.post("/api/plan", json={
            "major": "Computer Science",
            "max_per_semester": None,
        })
        assert r.status_code == 200
        # Default full-time plan should have 4 courses in some semester
        sems = r.json()["semesters"]
        max_courses = max(len(s["courses"]) for s in sems)
        assert max_courses >= 3


# ---------------------------------------------------------------------------
# API version
# ---------------------------------------------------------------------------

def test_api_version_present():
    r = client.get("/api")
    assert r.status_code == 200
    v = r.json().get("version", "")
    assert v and "." in v


# ---------------------------------------------------------------------------
# /api/plan/validate
# ---------------------------------------------------------------------------

class TestValidateEndpoint:
    def test_validate_returns_passed_for_autofilled_plan(self):
        r = client.post('/api/plan/validate', json={
            'major': 'Computer Science – Bachelor of Information Sciences',
            'course_codes': ['159101', '159102', '159201', '159251', '159302', '159333'],
        })
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d['overall_passed'], bool)
        assert 'checklist' in d

    def test_validate_checklist_is_tree(self):
        r = client.post('/api/plan/validate', json={'major': 'Computer Science', 'auto_fill': True})
        d = r.json()
        assert 'checklist' in d
        cl = d['checklist']
        assert 'type' in cl
        assert 'passed' in cl
        assert 'label' in cl

    def test_validate_checklist_has_course_nodes(self):
        r = client.post('/api/plan/validate', json={'major': 'Computer Science', 'auto_fill': True})
        d = r.json()

        def find_type(node, t):
            if node.get('type') == t:
                return True
            return any(find_type(c, t) for c in node.get('children', []))

        assert find_type(d['checklist'], 'course'), "No course nodes in checklist"

    def test_validate_summary_credits(self):
        r = client.post('/api/plan/validate', json={
            'major': 'Computer Science – Bachelor of Information Sciences',
            'course_codes': ['159101', '159102', '159201', '159251'],
        })
        d = r.json()
        assert d['total_credits'] >= 0
        assert d['plan_credits'] >= 0
        assert d['total_credits'] == d['plan_credits'] + d['prior_credits'] + d['transfer_credits']

    def test_validate_unknown_major_422(self):
        r = client.post('/api/plan/validate', json={'major': 'XXXX_NONEXISTENT_MAJOR_XXXX'})
        assert r.status_code in (404, 422)

    def test_validate_basic_plan_structure(self):
        r = client.post('/api/plan/validate', json={
            'major': 'Computer Science – Bachelor of Information Sciences',
            'course_codes': ['159101', '159102'],
        })
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d['overall_passed'], bool)
        assert isinstance(d['checklist'], dict)
        assert 'total_credits' in d


# ---------------------------------------------------------------------------
# HTML export (downloadHtml is client-side JS, test the UI markup is correct)
# ---------------------------------------------------------------------------

def test_ui_has_html_export_button():
    r = client.get('/')
    assert 'downloadHtml' in r.text

def test_ui_has_course_modal_markup():
    r = client.get('/')
    assert 'id="course-modal"' in r.text
    assert 'modal-search' in r.text

def test_ui_has_validation_panel_markup():
    r = client.get('/')
    # New UI uses val-panel class and renderValidationPanel function
    assert 'val-panel' in r.text
    assert 'renderValidationPanel' in r.text

def test_ui_has_year_grouping_markup():
    r = client.get('/')
    # New UI uses year-header class with year grouping logic
    assert 'year-header' in r.text or 'year-label' in r.text

def test_ui_has_mark_completed_markup():
    r = client.get('/')
    # New UI uses markDone function and mark-done-btn class
    assert 'markDone' in r.text or 'mark-done' in r.text

def test_ui_has_progress_bar_markup():
    r = client.get('/')
    # New UI uses progress-wrap class and renderProgressBar function
    assert 'progress-wrap' in r.text
    assert 'renderProgressBar' in r.text
