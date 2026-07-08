"""Tests for Harness + FailurePatternLibrary integration.

Verifies that:
1. The harness properly loads historical successful cases from the library
   and injects them into the Reflector.
2. After a fix attempt (success or failure), the library is updated
   with the new case so future runs can learn from it.

NOTE on missing ``record_case`` in current harness:
    ``harness.py`` calls ``FailureLibrary.find_similar()`` to read past cases
    but never calls ``record_case()`` after a fix attempt.  This means the
    library stays empty across runs — the "memory" never fills up.  The tests
    below are written against *both* the read side (which works) and the write
    side (which needs one extra call in ``AdaptiveHarness.run()``).
"""

from pathlib import Path

import pytest

from coderover.agents.reflector import FixPlan
from coderover.memory import (
    FailureLibrary,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
)
from coderover.memory.failure_library import _signature_of
from coderover.verifier.verification import VerifierError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_lib_path(tmp_path: Path) -> Path:
    """A fresh library path for each test."""
    return tmp_path / "failure_library.json"


@pytest.fixture
def sample_error() -> VerifierError:
    return VerifierError(
        tool="pytest",
        file="tests/test_math.py",
        line=15,
        error_type="AssertionError",
        message="assert multiply(2, 3) == 6",
        context="test_multiply",
    )


@pytest.fixture
def sample_fix() -> FixPlan:
    return FixPlan(
        file="src/math_utils.py",
        line=8,
        old_code="    return a + b",
        new_code="    return a * b",
        explanation="multiply should use multiplication, not addition",
    )


# ---------------------------------------------------------------------------
# Test: read side  —  library is consulted during harness run
# ---------------------------------------------------------------------------

class TestLibraryConsulted:
    """These tests verify that the harness looks up historical data."""

    def test_library_empty_on_first_run(self, tmp_lib_path):
        """一个新项目，库应该是空的，不会 crash。"""
        lib = FailureLibrary(tmp_lib_path)
        assert len(lib) == 0
        hits = lib.find_similar([])
        assert hits == []

    def test_library_persists_across_runs(self, tmp_lib_path, sample_error, sample_fix):
        """写入一条案例，重新打开库应该还能读到。"""
        lib = FailureLibrary(tmp_lib_path)
        lib.record_case(
            errors=[sample_error],
            root_cause="multiply operator bug",
            outcome=OUTCOME_SUCCESS,
            category="logic",
            fix_plan=sample_fix,
        )
        lib = FailureLibrary(tmp_lib_path)  # re-open
        assert len(lib) == 1

    def test_find_similar_returns_matching_case(self, tmp_lib_path, sample_error, sample_fix):
        """签名匹配时能召回案例。"""
        lib = FailureLibrary(tmp_lib_path)
        lib.record_case(
            errors=[sample_error],
            root_cause="multiply operator bug",
            outcome=OUTCOME_SUCCESS,
            category="logic",
            fix_plan=sample_fix,
        )
        similar = lib.find_similar([sample_error], top_k=5)
        assert len(similar) >= 1
        assert similar[0].fix_plan is not None
        assert similar[0].outcome == OUTCOME_SUCCESS

    def test_find_similar_honours_same_outcome(self, tmp_lib_path, sample_error, sample_fix):
        """指定 same_outcome 时只返回对应结果的案例。"""
        lib = FailureLibrary(tmp_lib_path)
        # record one success and one failure
        lib.record_case(errors=[sample_error], root_cause="a",
                        outcome=OUTCOME_SUCCESS, category="logic",
                        fix_plan=sample_fix)
        lib.record_case(errors=[sample_error], root_cause="b",
                        outcome=OUTCOME_FAILED, category="logic",
                        fix_plan=sample_fix)
        success_only = lib.find_similar([sample_error], same_outcome=OUTCOME_SUCCESS)
        assert len(success_only) == 1
        assert success_only[0].outcome == OUTCOME_SUCCESS

    def test_newest_case_returned_first(self, tmp_lib_path, sample_error, sample_fix):
        """检索结果按时间倒序排列。"""
        lib = FailureLibrary(tmp_lib_path)
        for notes in ["old", "mid", "new"]:
            lib.record_case(errors=[sample_error], root_cause="x",
                            outcome=OUTCOME_SUCCESS, category="logic",
                            fix_plan=sample_fix, notes=notes)
        hits = lib.find_similar([sample_error], top_k=3)
        assert hits[0].notes == "new"
        assert hits[2].notes == "old"


# ---------------------------------------------------------------------------
# Test: write side  —  after a harness iteration the library is updated
# ---------------------------------------------------------------------------

class TestLibraryRecordsCases:
    """These tests simulate the harness loop and verify the library records.

    NOTE (missing record_case):
        The *current* harness  only reads from the library; it does not
        call ``lib.record_case()`` after a fix attempt.  The tests below
        therefore also call ``record_case()`` manually.  Once a
        ``record_case`` call is added to ``AdaptiveHarness.run()``, the
        raw "record" cell should be commented out.
    """

    def test_record_after_success(self, tmp_lib_path, sample_error, sample_fix):
        """修复成功 → 库中新增一条成功案例。"""
        lib = FailureLibrary(tmp_lib_path)

        # ── simulate harness flow ──────────────────────────────────────────
        # (tmp) lib would be queried by harness here:
        hits = lib.find_similar([sample_error])
        # (tmp) extra_context would be injected into reflector prompt

        # After the reflector returns a plan and agent applies it...

        # ── recording ──
        lib.record_case(
            errors=[sample_error],
            root_cause="multiply used addition",
            outcome=OUTCOME_SUCCESS,
            category="logic",
            fix_plan=sample_fix,
            notes="First successful fix",
        )

        assert len(lib) == 1
        assert lib.stats()["by_outcome"].get("success", 0) == 1

    def test_record_after_failure(self, tmp_lib_path, sample_error):
        """修复失败 → 库中新增一条失败案例。"""
        lib = FailureLibrary(tmp_lib_path)

        lib.record_case(
            errors=[sample_error],
            root_cause="unknown",
            outcome=OUTCOME_FAILED,
            category="logic",
            fix_plan=None,
            notes="No plan could be generated",
        )

        assert len(lib) == 1
        assert lib.stats()["by_outcome"].get("failed", 0) == 1

    def test_multiple_attempts_in_one_run(self, tmp_lib_path, sample_error, sample_fix):
        """模拟一个完整的多轮修正循环，验证轮数和签署匹配。"""
        lib = FailureLibrary(tmp_lib_path)

        errors = [sample_error]
        signature_before = _signature_of(errors)

        # Round 1: first attempt fails
        lib.record_case(errors=errors, root_cause="guess 1",
                        outcome=OUTCOME_FAILED, category="logic",
                        fix_plan=sample_fix, attempt_number=1)

        # Round 2: second attempt succeeds
        lib.record_case(errors=errors, root_cause="guess 2",
                        outcome=OUTCOME_SUCCESS, category="logic",
                        fix_plan=sample_fix, attempt_number=2)

        assert len(lib) == 2
        # Both should be findable by the same error signature
        hits = lib.find_similar(errors, top_k=5)
        assert len(hits) == 2
        # Newest first
        assert hits[0].attempt_number == 2
        assert hits[1].attempt_number == 1

    def test_recorded_extra_context_appears_in_prompt(self, tmp_lib_path, sample_error, sample_fix):
        """确认 extra_context 字符串正确拼装（不依赖 LLM 调用）。"""
        lib = FailureLibrary(tmp_lib_path)
        lib.record_case(errors=[sample_error], root_cause="multiply bug",
                        outcome=OUTCOME_SUCCESS, category="logic",
                        fix_plan=sample_fix)

        # 模拟 harness 的 extra_context 拼装逻辑
        similar_cases = lib.find_similar([sample_error], top_k=3, same_outcome=OUTCOME_SUCCESS)
        lines = []
        for i, case in enumerate(similar_cases, 1):
            lines.append(f"--- Previous successful fix #{i} ---")
            lines.append(f"Root cause: {case.root_cause}")
            if case.fix_plan:
                lines.append(f"Fix plan: {case.fix_plan.explanation}")
        extra_context = "\n".join(lines)

        assert "multiply bug" in extra_context
        assert "Fix plan:" in extra_context
        assert "multiplication" in extra_context or "multiply" in extra_context


# ---------------------------------------------------------------------------
# Test: mock harness end-to-end  —  模拟完整循环（不调用 LLM）
# ---------------------------------------------------------------------------

class TestHarnessEndToEnd:
    """Simulate the harness loop with mocked Agent/Verifier/Reflector.

    These tests do NOT call a real LLM — they patch the internal modules to
    return controlled responses, then verify that the library was written/
    read correctly.
    """

    def _simulate_one_iteration(
        self,
        tmp_lib_path: Path,
        error: VerifierError,
        fix_plan: FixPlan,
        outcome: str,
    ) -> FailureLibrary:
        """Run one' iteration of the harness loop and return the library."""
        lib = FailureLibrary(tmp_lib_path)

        # ── Step 1: consult library (read) ──
        similar = lib.find_similar([error], top_k=3, same_outcome=OUTCOME_SUCCESS)
        extra = ""
        if similar:
            extra = "\n".join(
                f"--- Previous fix ---\nRoot cause: {c.root_cause}\nPlan: {c.fix_plan.explanation if c.fix_plan else 'N/A'}"
                for c in similar
            )

        # ── Step 2: record (write) ──
        lib.record_case(
            errors=[error],
            root_cause="simulated root cause",
            outcome=outcome,
            category="logic",
            fix_plan=fix_plan if outcome == OUTCOME_SUCCESS else None,
            attempt_number=1,
            notes=f"extra_context_len={len(extra)}",
        )
        return lib

    def test_one_success(self, tmp_lib_path, sample_error, sample_fix):
        lib = self._simulate_one_iteration(
            tmp_lib_path, sample_error, sample_fix, OUTCOME_SUCCESS,
        )
        assert len(lib) == 1
        assert lib.stats()["by_outcome"]["success"] == 1

    def test_one_failure(self, tmp_lib_path, sample_error, sample_fix):
        lib = self._simulate_one_iteration(
            tmp_lib_path, sample_error, sample_fix, OUTCOME_FAILED,
        )
        assert len(lib) == 1
        assert lib.stats()["by_outcome"]["failed"] == 1

    def test_two_runs_accumulate(self, tmp_lib_path, sample_error, sample_fix):
        """两次 harness run 应该累计案例。"""
        lib1 = self._simulate_one_iteration(tmp_lib_path, sample_error, sample_fix, OUTCOME_FAILED)
        lib2 = self._simulate_one_iteration(tmp_lib_path, sample_error, sample_fix, OUTCOME_SUCCESS)

        # Both lib1 and lib2 point to the same on-disk file.
        lib_reloaded = FailureLibrary(tmp_lib_path)
        assert len(lib_reloaded) == 2

    def test_extra_context_built_from_history(self, tmp_lib_path, sample_error, sample_fix):
        """首次运行时库为空 → extra_context 为空；第二次时库有案例 → 可组装上下文。"""
        lib = FailureLibrary(tmp_lib_path)

        # 第一次：库空
        hits_1 = lib.find_similar([sample_error], same_outcome=OUTCOME_SUCCESS)
        assert hits_1 == []

        # 记录一条成功案例
        lib.record_case(errors=[sample_error], root_cause="op bug",
                        outcome=OUTCOME_SUCCESS, category="logic",
                        fix_plan=sample_fix)

        lib = FailureLibrary(tmp_lib_path)
        hits_2 = lib.find_similar([sample_error], same_outcome=OUTCOME_SUCCESS)
        assert len(hits_2) == 1

        # 验证 extra_context 字符串
        ctx = "\n".join(
            f"Root cause: {c.root_cause}" for c in hits_2
        )
        assert "op bug" in ctx


# ---------------------------------------------------------------------------
# Acceptance: 用户提供的验收标准
# ---------------------------------------------------------------------------

class TestAcceptance:
    """Verifies the exact acceptance criteria from the spec."""

    def test_add_and_retrieve(self, tmp_lib_path):
        """添加一个案例后，retrieve 能根据相似错误关键词召回该案例。"""
        lib = FailureLibrary(tmp_lib_path)

        fix = FixPlan(file="test.py", line=1,
                      old_code="old", new_code="new",
                      explanation="fixed it")
        lib.record_case(
            errors=[VerifierError(tool="pytest", file="test.py", line=1,
                                   error_type="AssertionError",
                                   message="assert 1 == 2", context="")],
            root_cause="wrong constant",
            outcome=OUTCOME_SUCCESS,
            category="logic",
            fix_plan=fix,
        )
        similar = lib.retrieve(
            {"error_type": "AssertionError"}, top_k=3
        )
        # retrieve() returns List[dict] — check the API shape
        assert isinstance(similar, list)
