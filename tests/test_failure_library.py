"""Tests for the Failure Pattern Library."""

import json
from pathlib import Path

import pytest

from coderover.agents.reflector import FixPlan
from coderover.memory.failure_library import (
    DEFAULT_DIR,
    DEFAULT_FILE,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    FailureCase,
    FailureLibrary,
    _normalize_msg,
    _signature_of,
    _summary_of,
)
from coderover.verifier.verification import VerifierError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_library(tmp_path: Path) -> FailureLibrary:
    """A library whose on-disk file lives under tmp_path."""
    return FailureLibrary(tmp_path / "library.json")


@pytest.fixture
def sample_errors() -> list:
    return [
        VerifierError(
            tool="pytest", file="src/math_utils.py", line=10,
            error_type="AssertionError",
            message="assert multiply(2, 3) == 6",
            context="tests/test_math_utils.py:9",
        ),
        VerifierError(
            tool="mypy", file="src/math_utils.py", line=10,
            error_type="return-value",
            message="Incompatible return value type",
            context="",
        ),
    ]


@pytest.fixture
def sample_fix() -> FixPlan:
    return FixPlan(
        file="src/math_utils.py",
        line=10,
        old_code="    return a + b  # buggy",
        new_code="    return a * b",
        explanation="multiply should use *",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSignature:
    def test_same_errors_same_signature(self, sample_errors):
        assert _signature_of(sample_errors) == _signature_of(sample_errors)

    def test_signature_stable_under_reordering(self, sample_errors):
        # Swap order — should produce the same hash (errors are sorted).
        flipped = list(reversed(sample_errors))
        assert _signature_of(sample_errors) == _signature_of(flipped)

    def test_signature_collapses_numbers(self):
        e1 = VerifierError(tool="pytest", file="a.py", line=10,
                           error_type="AssertionError",
                           message="assert multiply(2, 3) == 6", context="")
        e2 = VerifierError(tool="pytest", file="a.py", line=42,
                           error_type="AssertionError",
                           message="assert multiply(7, 8) == 999", context="")
        # Both contain digit-only differences; signature should be the same.
        assert _signature_of([e1]) == _signature_of([e2])

    def test_signature_short(self, sample_errors):
        assert len(_signature_of(sample_errors)) == 16

    def test_empty_error_set(self):
        sig = _signature_of([])
        assert isinstance(sig, str)
        assert len(sig) == 16

    def test_different_tool_different_signature(self):
        a = VerifierError(tool="pytest", file="a.py", line=1,
                          error_type="AssertionError",
                          message="X", context="")
        b = VerifierError(tool="mypy", file="a.py", line=1,
                          error_type="type-error",
                          message="X", context="")
        assert _signature_of([a]) != _signature_of([b])


class TestNormalizeMsg:
    def test_digits_become_n(self):
        assert _normalize_msg("assert 1 + 2 == 3") == _normalize_msg(
            "assert 9 + 8 == 7")

    def test_repeated_digits_collapse(self):
        # 99 and 666 should both collapse to "n"
        assert _normalize_msg("aaa 99 bb") == _normalize_msg("aaa 1 bb")
        assert _normalize_msg("aaa 666 bb") == _normalize_msg("aaa 1 bb")

    def test_lowercase(self):
        assert _normalize_msg("ABCdef") == _normalize_msg("abcdef")

    def test_clipped(self):
        assert len(_normalize_msg("x" * 500)) <= 200


class TestSummary:
    def test_empty(self):
        assert _summary_of([]) == "(no errors)"

    def test_three_or_fewer(self, sample_errors):
        s = _summary_of(sample_errors[:1])
        assert "[pytest]" in s
        assert "AssertionError" in s

    def test_more_than_three(self):
        errors = [
            VerifierError(tool="pytest", file=f"f{i}.py", line=i,
                          error_type="AssertionError", message=f"e{i}",
                          context="")
            for i in range(5)
        ]
        s = _summary_of(errors)
        assert "+2 more" in s


class TestLibrary:
    def test_empty_on_new_library(self, tmp_library):
        assert len(tmp_library) == 0
        assert tmp_library.find_similar([]) == []
        assert tmp_library.list_recent() == []
        s = tmp_library.stats()
        assert s["total"] == 0

    def test_record_case_returns_case(self, tmp_library, sample_errors,
                                      sample_fix):
        case = tmp_library.record_case(
            errors=sample_errors,
            root_cause="multiply() returns the wrong arithmetic operator",
            outcome=OUTCOME_SUCCESS,
            category="logic",
            fix_plan=sample_fix,
            attempt_number=1,
        )
        assert isinstance(case, FailureCase)
        assert case.outcome == OUTCOME_SUCCESS
        assert case.error_category == "logic"
        assert case.fix_plan is sample_fix
        assert case.attempt_number == 1
        assert case.case_id
        assert case.timestamp > 0

    def test_record_persists_to_disk(self, tmp_library, sample_errors,
                                     sample_fix, tmp_path):
        tmp_library.record_case(
            errors=sample_errors,
            root_cause="multiply bug",
            outcome=OUTCOME_SUCCESS,
            category="logic",
            fix_plan=sample_fix,
        )
        # Read the file directly
        data = json.loads((tmp_path / "library.json").read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert len(data["cases"]) == 1
        rec = data["cases"][0]
        assert rec["outcome"] == "success"
        assert rec["error_category"] == "logic"
        assert rec["fix_plan"]["file"] == "src/math_utils.py"

    def test_reload_from_disk(self, tmp_library, sample_errors, sample_fix,
                              tmp_path):
        tmp_library.record_case(
            errors=sample_errors, root_cause="r1", outcome=OUTCOME_SUCCESS,
            category="logic", fix_plan=sample_fix, notes="first",
        )
        tmp_library.record_case(
            errors=[], root_cause="no errors", outcome=OUTCOME_SUCCESS,
            category="unknown", notes="second",
        )

        reopened = FailureLibrary(tmp_path / "library.json")
        assert len(reopened) == 2
        cases = reopened.list_recent(10)
        # newest first
        assert cases[0].notes == "second"
        assert cases[1].notes == "first"

    def test_find_similar_matches_same_signature(
        self, tmp_library, sample_errors, sample_fix,
    ):
        tmp_library.record_case(
            errors=sample_errors, root_cause="r", outcome=OUTCOME_SUCCESS,
            category="logic", fix_plan=sample_fix,
        )
        # Build a "similar" error set with shifted line numbers — messages
        # and error types remain unchanged so signatures should match.
        similar = [
            VerifierError(tool=e.tool, file=e.file, line=e.line + 100,
                          error_type=e.error_type,
                          message=e.message,
                          context=e.context)
            for e in sample_errors
        ]
        hits = tmp_library.find_similar(similar)
        assert len(hits) == 1
        assert hits[0].outcome == OUTCOME_SUCCESS

    def test_find_similar_filters_by_outcome(
        self, tmp_library, sample_errors, sample_fix,
    ):
        tmp_library.record_case(
            errors=sample_errors, root_cause="a", outcome=OUTCOME_FAILED,
            category="logic", fix_plan=sample_fix, attempt_number=1,
        )
        tmp_library.record_case(
            errors=sample_errors, root_cause="b", outcome=OUTCOME_SUCCESS,
            category="logic", fix_plan=sample_fix, attempt_number=2,
        )
        success_only = tmp_library.find_similar(
            sample_errors, same_outcome=OUTCOME_SUCCESS)
        assert len(success_only) == 1
        assert success_only[0].outcome == OUTCOME_SUCCESS

        failed_only = tmp_library.find_similar(
            sample_errors, same_outcome=OUTCOME_FAILED)
        assert len(failed_only) == 1
        assert failed_only[0].outcome == OUTCOME_FAILED

        all_hits = tmp_library.find_similar(sample_errors)
        assert len(all_hits) == 2

    def test_find_similar_respects_top_k(
        self, tmp_library, sample_errors, sample_fix,
    ):
        for i in range(5):
            tmp_library.record_case(
                errors=sample_errors, root_cause=f"r{i}",
                outcome=OUTCOME_SUCCESS, category="logic",
                fix_plan=sample_fix, attempt_number=i,
            )
        hits = tmp_library.find_similar(sample_errors, top_k=3)
        assert len(hits) == 3

    def test_list_recent_orders_newest_first(
        self, tmp_library, sample_errors, sample_fix,
    ):
        for i in range(3):
            tmp_library.record_case(
                errors=sample_errors, root_cause=f"r{i}",
                outcome=OUTCOME_SUCCESS, category="logic",
                fix_plan=sample_fix, notes=f"case-{i}",
            )
        recent = tmp_library.list_recent()
        assert [c.notes for c in recent] == ["case-2", "case-1", "case-0"]

    def test_list_recent_with_limit(self, tmp_library, sample_errors,
                                    sample_fix):
        for i in range(10):
            tmp_library.record_case(
                errors=sample_errors, root_cause=f"r{i}",
                outcome=OUTCOME_SUCCESS, category="logic",
                fix_plan=sample_fix,
            )
        assert len(tmp_library.list_recent(3)) == 3

    def test_stats_aggregates(self, tmp_library, sample_errors, sample_fix):
        tmp_library.record_case(
            errors=sample_errors, root_cause="r", outcome=OUTCOME_SUCCESS,
            category="logic", fix_plan=sample_fix,
        )
        tmp_library.record_case(
            errors=[], root_cause="r", outcome=OUTCOME_FAILED,
            category="syntax", fix_plan=None,
        )
        s = tmp_library.stats()
        assert s["total"] == 2
        assert s["by_outcome"] == {"success": 1, "failed": 1}
        assert s["by_category"] == {"logic": 1, "syntax": 1}

    def test_record_case_without_fix(self, tmp_library, sample_errors):
        case = tmp_library.record_case(
            errors=sample_errors, root_cause="unresolved",
            outcome=OUTCOME_FAILED, category="unknown",
        )
        assert case.fix_plan is None

    def test_save_atomic(self, tmp_library, sample_errors, sample_fix,
                         tmp_path):
        tmp_library.record_case(
            errors=sample_errors, root_cause="r", outcome=OUTCOME_SUCCESS,
            category="logic", fix_plan=sample_fix,
        )
        # The atomic-write .tmp file should not linger after save().
        leftover = list(tmp_path.glob("*.tmp"))
        assert leftover == []

    def test_corrupt_file_does_not_crash(self, tmp_path):
        bad = tmp_path / "library.json"
        bad.write_text("{not valid json", encoding="utf-8")
        fl = FailureLibrary(bad)
        assert len(fl) == 0

    def test_default_path_under_home(self):
        # Sanity: the baked-in path lives in ~/.coderover
        assert str(DEFAULT_DIR).replace("\\", "/").endswith(
            ".coderover/failure_library")
        assert "library.json" in str(DEFAULT_FILE)


class TestFailureCaseDataclass:
    def test_minimal_construction(self):
        c = FailureCase(
            case_id="abc", timestamp=0.0,
            error_signature="", error_summary="",
            error_category="unknown", root_cause="",
        )
        assert c.fix_plan is None
        assert c.outcome == OUTCOME_FAILED   # default
        assert c.attempt_number == 1
        assert c.notes == ""

    def test_to_dict_then_back_roundtrip(self, sample_errors, sample_fix):
        from coderover.memory.failure_library import _case_to_dict, _dict_to_case

        original = FailureCase(
            case_id="id1", timestamp=12345.6,
            error_signature="abcd", error_summary="sum",
            error_category="logic", root_cause="causal",
            fix_plan=sample_fix, outcome=OUTCOME_SUCCESS,
            attempt_number=2, notes="n",
        )
        d = _case_to_dict(original)
        d.pop("case_id")  # roundtrip via dict pops won't match -- accept loose
        d.pop("timestamp")
        rebuilt = _dict_to_case({**d, "case_id": "id1", "timestamp": 12345.6})
        assert rebuilt.fix_plan is not None
        assert rebuilt.root_cause == "causal"
        assert rebuilt.outcome == OUTCOME_SUCCESS
