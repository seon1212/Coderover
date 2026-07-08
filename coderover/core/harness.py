"""
Adaptive Harness: 闭环调度器，控制 执行 -> 验证 -> 反思 -> 重试 循环。

状态机：
    INIT  → VERIFY ─(all pass)──────────────→ SUCCESS
                  │
                  └──(has errors)
                      │
                      ├── filter severe_errors
                      │
                      ├──(none)────────────────→ SUCCESS  (剩下都是低危，跳过)
                      │
                      └──(some)
                          │
                          ├── REFLECT ─(no plans/low conf)→ FAILED
                          │
                          └── FIX ─→ next VERIFY

每轮输出采用统一的 ascii 表格格式，兼容 GBK/UTF-8 终端。
"""
from pathlib import Path
from typing import Dict, Any, List

from coderover.agent import Agent
from coderover.verifier import verify
from coderover.agents import reflect


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Error types treated as "low severity" — auto-fix not required.
IGNORE_MYPY_TYPES = {"override", "var-annotated", "no-untyped-def"}
IGNORE_RUFF_CODES = {"F401", "F841", "E402", "E501", "W291"}

BANNER = "=" * 70
SUB_BANNER = "-" * 70


class AdaptiveHarness:
    """Adaptive runtime orchestrating Agent / Verifier / Reflector."""

    def __init__(self, llm, tools, max_retries: int = 3, aggressive: bool = False):
        self.llm = llm
        self.tools = tools
        self.max_retries = max_retries
        self.aggressive = aggressive
        self.agent = Agent(llm, tools)

    # -----------------------------------------------------------------------
    # Public entry
    # -----------------------------------------------------------------------
    def run(self, task: str, repo_path: Path | str) -> Dict[str, Any]:
        repo_path = Path(repo_path).resolve()
        history: List[List[str]] = []
        actual_attempts = 0
        modified_files: List[str] = []


        # Reset agent context for a fresh run
        self.agent.reset()

        self._header(repo_path)
        self._step("INIT", f"task = {task[:80]!r}")
        self._step("INIT", f"max_retries = {self.max_retries}, aggressive = {self.aggressive}")
        self._sep()

        # First attempt happens outside the retry loop
        self._phase_start(1)
        self.agent.chat(task)
        actual_attempts += 1
        self._phase_end(actual_attempts)

        for attempt in range(1, self.max_retries + 1):
            verify_result = verify(str(repo_path))
            self._render_verification(verify_result)

            # Case 1: everything passed
            if verify_result.passed:
                self._sep()
                self._ok("ALL CHECKS PASSED")
                return self._success_result(verify_result, actual_attempts, modified_files)

            # Case 2: out of retries
            if attempt >= self.max_retries:
                self._warn(f"Reached max_retries={self.max_retries}, stopping")
                return self._failed_result(verify_result, actual_attempts, modified_files)

            # Filter: only act on severe errors
            severe_errors = self._filter_severe_errors(verify_result.errors)
            skipped_count = len(verify_result.errors) - len(severe_errors)

            # Case 3: no severe errors -> only low-severity warnings remain.
            #         From the user's standpoint, nothing requires fixing.
            if not severe_errors:
                self._info(
                    f"No severe errors found ({skipped_count} low-severity skipped). "
                    f"Treating run as SUCCESS."
                )
                self._sep()
                self._ok("LOW-SEVERITY-ONLY — no fixes required")
                return self._success_result(verify_result, actual_attempts, modified_files,
                                            skipped=skipped_count)

            # Loop detection: identical errors between two rounds => no progress
            current_errors = [e.message for e in severe_errors]
            if history and current_errors == history[-1]:
                self._warn("Loop detected (errors identical to previous round). Stopping.")
                return self._failed_result(verify_result, actual_attempts, modified_files)
            history.append(current_errors)

            self._step(
                "REFLECT",
                f"{len(severe_errors)} severe error(s) "
                f"({skipped_count} low-severity skipped)",
            )
            #reflector_result = reflect(severe_errors, repo_path)

            from coderover.memory import FailureLibrary, OUTCOME_SUCCESS

            # 检索相似的成功案例
            lib = FailureLibrary()
            similar_cases = lib.find_similar(
                severe_errors,
                top_k=3,
                same_outcome=OUTCOME_SUCCESS   # 只取成功案例
            )

            extra_context = ""
            if similar_cases:
                lines = []
                for i, case in enumerate(similar_cases, 1):
                    lines.append(f"--- Previous successful fix #{i} ---")
                    lines.append(f"Root cause: {case.root_cause}")
                    if case.fix_plan:
                        lines.append(f"Fix plan: {case.fix_plan.explanation}")
                    lines.append(f"Outcome: {case.outcome}")
                    lines.append("")
                extra_context = "\n".join(lines)

            # 调用 Reflector，传入 extra_context
            reflector_result = reflect(
                severe_errors,
                repo_path,
                extra_context=extra_context
            )

            self._render_reflector(reflector_result)

            # Low confidence -> refuse to act on shaky plan
            if reflector_result.confidence < 0.3:
                self._warn(f"Reflector confidence too low ({reflector_result.confidence:.2f} < 0.3). Stopping.")
                return self._failed_result(verify_result, actual_attempts, modified_files)

            fix_plans = reflector_result.fix_plans
            if not fix_plans:
                self._warn("Reflector produced no fix plans. Stopping.")
                return self._failed_result(verify_result, actual_attempts, modified_files)

            # Apply fixes via Agent
            fix_prompt = self._format_fix_plans(fix_plans)
            self._phase_start(attempt + 1)
            self.agent.chat(fix_prompt)
            actual_attempts += 1
            self._phase_end(actual_attempts)
            modified_files.extend(self._collect_modified_files(reflector_result))

        return self._failed_result(verify_result, actual_attempts, modified_files)

    # -----------------------------------------------------------------------
    # Output helpers — all ASCII-safe
    # -----------------------------------------------------------------------
    def _header(self, repo_path: Path | str) -> None:
        print()
        print(BANNER)
        print("  CodeRover Adaptive Harness")
        print(f"  target = {repo_path}")
        print(BANNER)

    def _sep(self) -> None:
        print(SUB_BANNER)

    def _step(self, stage: str, msg: str) -> None:
        print(f"  [{stage:^8}]  {msg}")

    def _ok(self, msg: str) -> None:
        print(f"  [   OK   ]  {msg}")

    def _info(self, msg: str) -> None:
        print(f"  [  INFO  ]  {msg}")

    def _warn(self, msg: str) -> None:
        print(f"  [  WARN  ]  {msg}")

    def _phase_start(self, idx: int) -> None:
        print()
        print(f"  >> Phase {idx}: agent is working ...")

    def _phase_end(self, idx: int) -> None:
        print(f"  << Phase {idx} complete")

    def _render_verification(self, vr) -> None:
        self._sep()
        print("  Verification summary")
        print(f"    status       : {'PASS' if vr.passed else 'FAIL'}")
        print(f"    total errors : {len(vr.errors)}")
        # Group by tool
        by_tool: Dict[str, int] = {}
        for e in vr.errors:
            by_tool[e.tool] = by_tool.get(e.tool, 0) + 1
        if by_tool:
            for tool, n in sorted(by_tool.items()):
                print(f"      - {tool:8s}: {n}")
        print(f"    summary      : {vr.summary}")
        self._sep()

    def _render_reflector(self, rr) -> None:
        print(f"    root_cause  : {rr.root_cause[:140]}")
        print(f"    reasoning   : {rr.reasoning[:140]}")
        print(f"    confidence  : {rr.confidence:.2f}")
        print(f"    fix_plans   : {len(rr.fix_plans)}")
        for idx, p in enumerate(rr.fix_plans, 1):
            print(f"      {idx}. {p.file}:{p.line}")
            print(f"         why: {p.explanation[:120]}")

    # -----------------------------------------------------------------------
    # Filtering & formatting
    # -----------------------------------------------------------------------
    def _filter_severe_errors(self, errors: List) -> List:
        """Keep only errors that genuinely require Agent intervention."""
        severe: List = []
        for err in errors:
            if err.tool == "pytest":
                # pytest failures are always actionable
                severe.append(err)
                continue
            if err.tool == "mypy":
                if self.aggressive:
                    severe.append(err)
                    continue
                if err.error_type not in IGNORE_MYPY_TYPES:
                    severe.append(err)
                continue
            if err.tool == "ruff":
                # Skip by default, unless aggressive
                if self.aggressive or err.error_type not in IGNORE_RUFF_CODES:
                    severe.append(err)
                continue
            # Unknown tool -> conservatively skip
        return severe

    def _format_fix_plans(self, plans: List) -> str:
        if not plans:
            return "No modifications required."
        prompt = "Apply the following precise fix plans using the edit tool:\n"
        for idx, p in enumerate(plans, 1):
            prompt += f"\n--- Plan {idx} ---\n"
            prompt += f"file: {p.file}\n"
            prompt += f"line: {p.line}\n"
            prompt += "old code:\n```\n"
            prompt += f"{p.old_code}\n"
            prompt += "```\n"
            prompt += "new code:\n```\n"
            prompt += f"{p.new_code}\n"
            prompt += "```\n"
            prompt += f"reason: {p.explanation}\n"
        prompt += "\nApply ALL of the above plans exactly as specified."
        return prompt

    @staticmethod
    def _collect_modified_files(reflector_result) -> List[str]:
        seen = []
        for p in reflector_result.fix_plans:
            f = getattr(p, "file", None)
            if f and f not in seen:
                seen.append(f)
        return seen

    # -----------------------------------------------------------------------
    # Result builders
    # -----------------------------------------------------------------------
    @staticmethod
    def _success_result(verify_result, attempts: int, modified_files: List[str],
                       skipped: int = 0) -> Dict[str, Any]:
        return {
            "status": "success",
            "attempts": attempts,
            "modified_files": modified_files,
            "skipped_low_severity": skipped,
            "result": verify_result,
        }

    @staticmethod
    def _failed_result(verify_result, attempts: int, modified_files: List[str]) -> Dict[str, Any]:
        return {
            "status": "failed",
            "attempts": attempts,
            "modified_files": modified_files,
            "skipped_low_severity": 0,
            "result": verify_result,
        }
