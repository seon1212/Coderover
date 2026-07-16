"""
Adaptive Harness: 闭环调度器，控制 执行 -> 验证 -> 反思 -> 重试 循环。

状态机：
    INIT → [Issue Analyzer] → [Localization Ranking] → Phase 1 (agent works)
         ↓
    VERIFY ─(pass + modified + !issue)──→ SUCCESS
         │
         ├─(pass + modified + issue)──→ 反思 Issue ─(fixed)──→ SUCCESS
         │                                              └(unfixed) → 合成错误 → REFLECTOR
         │
         ├─(pass + no modified + issue) → 强制 Reflector（兜底）
         │
         ├─(pass + no modified + !issue)──→ SKIPPED
         │
         ├─(has errors) ──→ severe_errors
         │                     ├──(none)──→ SUCCESS
         │                     └──(some)──→ REFLECT ─→ FIX ─→ next VERIFY
         │
         └─(synthetic errors from issue review / force reflect) → REFLECT (复用现有循环)

每轮输出采用统一的 ascii 格式，兼容 GBK/UTF-8 终端。
"""
from pathlib import Path
from typing import Dict, Any, List
import time
import json
import subprocess

from coderover.agent import Agent
from coderover.verifier import verify
from coderover.agents import reflect



# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IGNORE_MYPY_TYPES = {"override", "var-annotated", "no-untyped-def"}
IGNORE_RUFF_CODES = {"F401", "F841", "E402", "E501", "W291"}

BANNER = "=" * 70
SUB_BANNER = "-" * 70


class _SyntheticError:
    """用于"issue 未修复"反思的合成错误，与 VerifierError 接口兼容。"""
    def __init__(self, message: str):
        self.tool = "issue_review"
        self.file = ""
        self.line = 0
        self.error_type = "issue_not_resolved"
        self.message = message


class AdaptiveHarness:
    """Adaptive runtime orchestrating Agent / Verifier / Reflector."""

    def __init__(self, llm, tools, max_retries: int = 3, aggressive: bool = False,
                 max_rounds: int = 50):
        self.llm = llm
        self.tools = tools
        self.max_retries = max_retries
        self.aggressive = aggressive
        self.max_rounds = max_rounds
        self.agent = Agent(llm, tools, max_rounds=max_rounds)

    # -----------------------------------------------------------------------
    # Public entry
    # -----------------------------------------------------------------------
    def run(self, task: str, repo_path: Path | str,
            is_issue_task: bool = False) -> Dict[str, Any]:
        """Run the harness.

        Args:
            task: Task description (issue body or plain text).
            repo_path: Path to the repository to work on.
            is_issue_task: If True, verification pass is followed by an
                           issue-resolution reflection before declaring success.
        """
        repo_path = Path(repo_path).resolve()
        history: List[List[str]] = []
        actual_attempts = 0
        modified_files: List[str] = []
        action_log: List[Dict[str, Any]] = []
        analysis = None
        verify_result = None

        def _log_tool(name: str, kwargs: dict) -> None:
            action_log.append({
                "tool": name,
                "args": {k: str(v)[:120] for k, v in kwargs.items()},
            })

        self.agent.reset()
        self._start_time = time.monotonic()

        self._header(repo_path)
        self._step("INIT", f"task = {task[:80]!r}")
        self._step("INIT", f"max_retries={self.max_retries}, max_rounds={self.max_rounds}, "
                           f"aggressive={self.aggressive}, is_issue_task={is_issue_task}")
        self._sep()

        # ── Issue Analyzer + Localization (is_issue_task only) ──
        phase1_task = task
        analysis = None
        candidates = []
        if is_issue_task:
            self._phase("ISSUE_ANALYSIS")
            analysis = self._analyze_issue(task, repo_path)
            if analysis.get("summary"):
                self._step("ANALYZE", analysis["summary"][:80])

            self._phase("LOCALIZATION")
            if analysis.get("summary"):
                candidates = self._localize_files(analysis, repo_path)
                if candidates:
                    self._step("LOCATE", f"Top: {'; '.join(c['file'] for c in candidates[:3])}")
                else:
                    self._info("No strong localization candidates found.")
            else:
                self._info("Analysis empty, skipping localization.")

            # Issue 模式限制轮次
            self.agent = Agent(self.llm, self.tools, max_rounds=20)
            phase1_task = self._build_constrained_task(task, analysis, candidates)

        # ── PATCH_GENERATION (Phase 1: Agent implements fix) ──
        self._phase("PATCH_GENERATION")
        self._step("TASK", f"len={len(phase1_task)} chars, rounds={self.agent.max_rounds}")
        self.agent.chat(phase1_task, on_tool=_log_tool)
        actual_attempts += 1

        # 从 action_log 提取实际修改
        modified_files = self._collect_modified_files_from_log(action_log)

        # ── 约束检查：Issue 模式下首次 edit 应在 candidate 列表中 ──
        if is_issue_task and candidates and modified_files:
            first_edit = modified_files[0].lower().replace("\\", "/")
            candidate_paths = [c["file"].lower().replace("\\", "/") for c in candidates]
            in_list = any(cp in first_edit for cp in candidate_paths)
            if not in_list:
                self._warn(f"First edit outside candidate list: {modified_files[0]}")
                self._step("CONSTRAINT", f"Candidates were: {'; '.join(candidate_paths[:3])}")

        if modified_files:
            self._step("EDIT", f"{len(modified_files)} file(s) modified")

        # ── 快速放弃：无 tool call ──
        if not action_log:
            self._warn("Agent made no tool calls. Skipping.")
            return {
                "status": "skipped",
                "attempts": 0,
                "modified_files": [],
                "skipped_low_severity": 0,
                "action_log": [],
                "result": "empty",
                "usage": self._build_usage(),
            }

        # ── PATCH_REVIEW ──
        self._phase("PATCH_REVIEW")
        patch_errors: List = []
        if is_issue_task and modified_files and analysis:
            diff_text = self._get_working_diff(repo_path)
            if diff_text:
                review = self._review_patch(analysis, diff_text, modified_files)
                if not review.get("accepted", True):
                    problem = review.get("problem", "none")
                    self._step("PATCH", f"Rejected: {problem} — {review.get('reason', '')[:80]}")
                    patch_errors = [_SyntheticError(
                        f"Patch rejected: {review.get('reason', 'quality issue')}")]
                    if problem in ("wrong_localization", "wrong_root_cause"):
                        self._info("Wrong approach detected. Going directly to Reflector.")
                else:
                    self._step("PATCH", "Accepted")
            else:
                self._step("PATCH", "No diff (skipped)")
        else:
            self._step("PATCH", "Skipped (no issue task or no changes)")

        # ── VERIFICATION loop ──
        self._phase("VERIFICATION")
        for attempt in range(1, self.max_retries + 1):
            # 如果 Patch Review 拒绝了，跳过 Verifier，直接走 Reflector
            if patch_errors:
                severe_errors = patch_errors
                patch_errors = []
                skipped_count = 0
                quality_errors = []
            else:
                verify_result = verify(str(repo_path))
                self._render_verification(verify_result)

                # ── 收集 severe_errors ──────────────────────────────────────
                severe_errors: List = []
                skipped_count = 0

                if verify_result.passed:
                    # 验证通过时，仍可能有低危告警（ruff、低危 mypy）
                    quality_errors = self._filter_severe_errors(verify_result.errors)
                    all_errors = list(verify_result.errors)
                    quality_errors = [e for e in all_errors if e not in quality_errors]
                    self._sep()
                    if is_issue_task and modified_files:
                        # Issue 任务：验证通过后再反思 issue 是否真正解决
                        diff_text = self._get_working_diff(repo_path)
                        review = self._reflect_on_issue(
                            task, action_log, diff=diff_text,
                            verify_result=verify_result,
                            modified_files=modified_files, analysis=analysis)
                        if review.get("is_fixed", False):
                            self._ok(f"ISSUE RESOLVED ({len(modified_files)} files modified)")
                            return self._success_result(verify_result, actual_attempts,
                                                        modified_files, action_log=action_log,
                                                        quality_errors=quality_errors)
                        # issue 没解决 → 按 failure_type 决策
                        ftype = review.get("failure_type", "incomplete_patch")
                        action = review.get("next_action", "reflect_patch")
                        self._info(f"Issue review: [{ftype}] {review.get('reason', 'fix incomplete')}")

                        if action == "rerun_issue_analysis" and analysis:
                            # 重新分析 Issue 并重新生成约束条件
                            self._step("RERUN", "Re-analyzing issue...")
                            analysis = self._analyze_issue(task, repo_path)
                            candidates = self._localize_files(analysis, repo_path) or []
                            phase1_task = self._build_constrained_task(task, analysis, candidates)
                            self.agent.reset()
                            self._phase_start(attempt + 1)
                            self.agent.chat(phase1_task, on_tool=_log_tool)
                            actual_attempts += 1
                            self._phase_end(actual_attempts)
                            modified_files = self._collect_modified_files_from_log(action_log)
                            continue
                        # 默认：合成错误，走 Reflector 循环
                        severe_errors = [_SyntheticError(
                            f"[{ftype}] {review.get('reason', 'fix incomplete')}")]
                    elif modified_files:
                        self._ok(f"ALL CHECKS PASSED ({len(modified_files)} files modified)")
                        return self._success_result(verify_result, actual_attempts,
                                                    modified_files, action_log=action_log,
                                                    quality_errors=quality_errors)
                    else:
                        if is_issue_task:
                            # Issue 任务中 Agent 没改代码 → 强制 Reflector 生成修复方案
                            severe_errors = [_SyntheticError(
                                "Agent did not modify any files. Generate a fix plan based on the issue description.")]
                            self._info("No modifications made. Forcing issue reflection.")
                        else:
                            self._info("No issues found, no modifications needed")
                            return self._skipped_result(verify_result, actual_attempts,
                                                        action_log=action_log)
                else:
                    # 验证失败
                    severe_errors = self._filter_severe_errors(verify_result.errors)
                    skipped_count = len(verify_result.errors) - len(severe_errors)
                    quality_errors = [e for e in verify_result.errors if e not in severe_errors]
                    if not severe_errors:
                        pass  # 交给下面的 "没有严重错误" 分支处理
                    elif attempt >= self.max_retries:
                        self._warn(f"Reached max_retries={self.max_retries}, stopping")
                        return self._failed_result(verify_result, actual_attempts,
                                                   modified_files, action_log=action_log)

            # ── 没有需要修复的错误 → 结束 ──
            if not severe_errors:
                self._info(
                    f"No severe errors found ({skipped_count} low-severity skipped). "
                    f"Treating run as SUCCESS."
                )
                self._sep()
                self._ok("LOW-SEVERITY-ONLY — no fixes required")
                return self._success_result(verify_result, actual_attempts, modified_files,
                                            skipped=skipped_count, action_log=action_log,
                                            quality_errors=quality_errors)

            # ── 循环检测 ──
            current_errors = [e.message for e in severe_errors]
            if history and current_errors == history[-1]:
                self._warn("Loop detected (errors identical). Stopping.")
                return self._failed_result(verify_result, actual_attempts, modified_files,
                                           action_log=action_log)
            history.append(current_errors)

            self._step("REFLECT",
                       f"{len(severe_errors)} error(s) ({skipped_count} low-severity skipped)")

            # ── 检索相似案例 ──
            from coderover.memory import FailureLibrary, OUTCOME_SUCCESS
            lib = FailureLibrary()
            similar_cases = lib.find_similar(severe_errors, top_k=3,
                                             same_outcome=OUTCOME_SUCCESS)
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

            # ── Reflector ──
            reflector_result = reflect(severe_errors, repo_path, extra_context=extra_context)
            self._render_reflector(reflector_result)

            if reflector_result.confidence < 0.3:
                self._warn(f"Confidence too low ({reflector_result.confidence:.2f}). Stopping.")
                return self._failed_result(verify_result, actual_attempts, modified_files,
                                           action_log=action_log)

            fix_plans = reflector_result.fix_plans
            if not fix_plans:
                self._warn("Reflector produced no fix plans. Stopping.")
                return self._failed_result(verify_result, actual_attempts, modified_files,
                                           action_log=action_log)

            # ── 执行修复 ──
            fix_prompt = self._format_fix_plans(fix_plans)
            self._phase_start(attempt + 1)
            self.agent.chat(fix_prompt, on_tool=_log_tool)
            actual_attempts += 1
            self._phase_end(actual_attempts)

            modified_files.extend(self._collect_modified_files(reflector_result))
            seen = set()
            modified_files[:] = [f for f in modified_files if not (f in seen or seen.add(f))]

        return self._failed_result(verify_result, actual_attempts, modified_files,
                                    action_log=action_log)

    # -----------------------------------------------------------------------
    # Phase 1: Issue Analyzer
    # -----------------------------------------------------------------------
    def _analyze_issue(self, task: str, repo_path: Path) -> dict:
        """分析 Issue 文本，生成结构化摘要（空时自动重试）。"""
        try:
            readme = (repo_path / "README.md").read_text(encoding="utf-8", errors="replace")[:2000]
        except FileNotFoundError:
            readme = ""
        try:
            pkg = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
            pkg_context = pkg.get("description", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pkg_context = ""

        for attempt in range(3):
            readable = readme[:1000] if attempt == 0 else ""  # 重试时缩短
            prompt = (
                f"Analyze the following GitHub Issue.\n\n"
                f"Issue:\n{task[:2000]}\n\n"
                f"Respond with this JSON format:\n"
                f'{{"summary":"...",'
                f'"affected_apis":["..."],'
                f'"search_keywords":["..."],'
                f'"search_strategy":"...",'
                f'"constraints":["..."]}}'
            )

            resp = self.llm.chat(messages=[{"role": "user", "content": prompt}])
            raw = (resp.content or "").strip()
            if not raw:
                self._info(f"Issue analyzer attempt {attempt+1} returned empty, retrying...")
                continue
            try:
                s, e = raw.find("{"), raw.rfind("}")
                if s >= 0 and e > s:
                    return json.loads(raw[s:e+1])
            except (json.JSONDecodeError, AttributeError):
                continue
        return {}

    # -----------------------------------------------------------------------
    # Phase 1: Localization Ranking
    # -----------------------------------------------------------------------
    def _localize_files(self, analysis: dict, repo_path: Path) -> list:
        """只读探索阶段，收集 Agent 访问过的文件并排序。"""
        from coderover.tools import ALL_TOOLS
        read_tools = [t for t in ALL_TOOLS if t.name in ("read_file", "glob", "grep", "bash")]
        read_agent = Agent(self.llm, read_tools, max_rounds=8)
        read_agent.reset()

        keywords = analysis.get("search_keywords", [])
        apis = analysis.get("affected_apis", [])
        target = (
            f"Explore the codebase at {repo_path} to understand the project structure. "
            f"Focus on: {' '.join(keywords[:5])}. "
            f"Find files related to: {' '.join(apis[:5])}. "
            f"Do NOT edit any files. Report what you find."
        )

        local_log = []
        def _log(name, kwargs):
            local_log.append({"tool": name, "args": {k: str(v)[:120] for k, v in kwargs.items()}})

        self._step("LOCATE", f"read-only exploration ({len(keywords)} keywords)")
        read_agent.chat(target, on_tool=_log)

        # 评分：每个被访问的文件
        file_scores = {}
        for entry in local_log:
            t, a = entry["tool"], entry["args"]
            fp = ""
            if t == "read_file":
                fp = a.get("file_path", "")
            elif t == "grep":
                fp = a.get("path", "")
            elif t == "glob":
                fp = a.get("path", "")
            if not fp:
                continue
            parts = Path(fp).parts
            rel = str(Path(*parts[-min(4, len(parts)):])).lower()
            score = file_scores.get(rel, {"score": 0, "reads": 0, "greps": 0, "reason": ""})
            score["reads"] += 1 if t == "read_file" else 0
            score["greps"] += 1 if t == "grep" else 0

            base = Path(fp).stem.lower()
            path_lower = str(fp).lower()
            kw_matches = sum(1 for k in keywords if k.lower() in path_lower or k.lower() in base)
            api_matches = sum(1 for a in apis if a.lower() in path_lower or a.lower() in base)

            score["score"] = (kw_matches * 0.3) + (api_matches * 0.3) + (score["reads"] * 0.1) + (score["greps"] * 0.05)
            reasons = []
            if kw_matches: reasons.append(f"keyword({kw_matches})")
            if api_matches: reasons.append(f"api({api_matches})")
            if score["reads"]: reasons.append(f"read({score['reads']})")
            score["reason"] = "+".join(reasons) if reasons else "explored"
            file_scores[rel] = score

        ranked = sorted(
            [{"file": k, "score": min(v["score"], 1.0), "reason": v["reason"]}
             for k, v in file_scores.items() if v["score"] > 0],
            key=lambda x: -x["score"]
        )
        return ranked[:5]

    # -----------------------------------------------------------------------
    # Phase 1: Constrained task builder
    # -----------------------------------------------------------------------
    def _build_constrained_task(self, task: str, analysis: dict, candidates: list) -> str:
        """注入 Issue 分析和候选文件，引导 Agent 精准定位。"""
        parts = [
            task,
            "",
            "===== PRE-ANALYSIS COMPLETE =====",
            "Issue analysis and file localization have been done.",
            "Your role is ONLY to implement the fix.",
            "Do NOT explore the codebase or search for files.",
            "Read the candidate files below and apply the fix.",
            "Do NOT modify files outside the candidate list.",
            "================================",
        ]
        if analysis:
            parts.append(f"Summary: {analysis.get('summary', '')}")
            if analysis.get("search_strategy"):
                parts.append(f"Strategy: {analysis['search_strategy']}")
            if analysis.get("constraints"):
                parts.append(f"Constraints: {'; '.join(analysis['constraints'][:3])}")
        if candidates:
            parts.append("\nCandidate files (edit these):")
            for c in candidates[:3]:
                parts.append(f"  - {c['file']} (score: {c['score']:.0%})")
        parts.append(f"\n=== START FIXING ===")
        return "\n".join(parts)

    # -----------------------------------------------------------------------
    # Phase 2: Patch Review
    # -----------------------------------------------------------------------
    def _get_working_diff(self, repo_path: Path) -> str:
        """获取工作区的 git diff。"""
        try:
            r = subprocess.run(
                ["git", "diff", "--unified=3"],
                cwd=repo_path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            ws = r.stdout or ""
            r2 = subprocess.run(
                ["git", "diff", "--cached", "--unified=3"],
                cwd=repo_path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            ws2 = r2.stdout or ""
            return (ws + "\n" + ws2).strip()
        except Exception:
            return ""

    def _review_patch(self, analysis: dict, diff: str, modified_files: list) -> dict:
        """评审 Patch 质量。"""
        if not diff:
            return {"accepted": True, "reason": "no diff to review"}
        prompt = (
            f"Review this code patch for the issue described below.\n\n"
            f"Issue summary: {analysis.get('summary', '')}\n"
            f"Affected APIs: {', '.join(analysis.get('affected_apis', [])[:4])}\n"
            f"Constraints: {'; '.join(analysis.get('constraints', [])[:3])}\n\n"
            f"--- Files modified ---\n" + "\n".join(str(f) for f in modified_files[:8]) + "\n\n"
            f"--- Diff ---\n{diff[:4000]}\n\n"
            f"Respond with JSON:\n"
            f'{{"accepted":true/false,'
            f'"problem":"wrong_localization | wrong_root_cause | incomplete_patch | too_broad | none",'
            f'"reason":"short explanation"}}'
        )
        resp = self.llm.chat(messages=[{"role": "user", "content": prompt}])
        try:
            raw = resp.content or ""
            s, e = raw.find("{"), raw.rfind("}")
            return json.loads(raw[s:e+1]) if s >= 0 and e > s else {"accepted": True}
        except (json.JSONDecodeError, AttributeError):
            return {"accepted": True}

    # -----------------------------------------------------------------------
    # Phase 3: Enhanced Issue Review
    # -----------------------------------------------------------------------
    def _reflect_on_issue(self, task: str, action_log: list,
                          diff: str = "", verify_result=None,
                          modified_files: list = None,
                          analysis: dict = None) -> dict:
        """增强版 Issue 反思，基于 diff + Verifier + 分析。

        Returns:
            {"is_fixed": bool, "failure_type": str, "reason": str,
             "next_action": "rerun_localization|rerun_issue_analysis|reflect_patch|done"}
        """
        edits = [e for e in action_log if e["tool"] in ("edit_file", "write_file")]
        edit_summary = "\n".join(
            f"- {e['tool']}: {e['args'].get('file_path', '?')}"
            for e in edits
        ) if edits else "(no file changes)"

        verify_info = ""
        if verify_result:
            verify_info = f"Verifier: passed={verify_result.passed}, errors={len(verify_result.errors)}"

        analysis_info = ""
        if analysis:
            analysis_info = (
                f"Issue summary: {analysis.get('summary', '')}\n"
                f"APIs: {', '.join(analysis.get('affected_apis', [])[:4])}\n"
                f"Constraints: {'; '.join(analysis.get('constraints', [])[:3])}\n"
            )

        diff_snippet = (diff or "")[:2000]

        prompt = (
            f"Review whether this code fix resolves the issue.\n\n"
            f"{analysis_info}"
            f"Original issue:\n{task[:1000]}\n\n"
            f"Files modified:\n{edit_summary}\n\n"
            f"--- Diff snippet ---\n{diff_snippet}\n\n"
            f"{verify_info}\n\n"
            f"Decide if the issue is fully resolved. Reply JSON:\n"
            f'{{"is_fixed":true/false,'
            f'"failure_type":"wrong_localization|wrong_root_cause|incomplete_patch|missing_files|none",'
            f'"reason":"short explanation",'
            f'"next_action":"rerun_localization|rerun_issue_analysis|reflect_patch|done"}}'
        )

        resp = self.llm.chat(messages=[
            {"role": "user", "content": prompt},
        ])
        try:
            raw = resp.content or ""
            s, e = raw.find("{"), raw.rfind("}")
            if s >= 0 and e > s:
                j = json.loads(raw[s:e+1])
                j.setdefault("is_fixed", True)
                j.setdefault("failure_type", "none")
                j.setdefault("next_action", "done")
                return j
            return {"is_fixed": True, "failure_type": "none",
                    "reason": "parse error", "next_action": "done"}
        except (json.JSONDecodeError, AttributeError):
            return {"is_fixed": True, "failure_type": "none",
                    "reason": "parse error", "next_action": "done"}

    # -----------------------------------------------------------------------
    # Output helpers
    # -----------------------------------------------------------------------
    def _header(self, repo_path: Path | str) -> None:
        print()
        print(BANNER)
        print("  CodeRover Adaptive Harness")
        print(f"  target = {repo_path}")
        print(BANNER)

    def _sep(self) -> None:
        print(SUB_BANNER)

    def _phase(self, name: str) -> None:
        """命名阶段输出，替代 Phase 1/2/3。"""
        print()
        print(f"  =========== {name} ===========")

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
    @staticmethod
    def _collect_modified_files_from_log(action_log: List[Dict]) -> List[str]:
        files = []
        for entry in action_log:
            if entry["tool"] in ("edit_file", "write_file"):
                fp = entry["args"].get("file_path", "")
                if fp and fp not in files:
                    files.append(fp)
        return files

    def _filter_severe_errors(self, errors: List) -> List:
        severe: List = []
        for err in errors:
            if isinstance(err, _SyntheticError):
                severe.append(err)
                continue
            if err.tool == "pytest":
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
                if self.aggressive or err.error_type not in IGNORE_RUFF_CODES:
                    severe.append(err)
                continue
        return severe

    def _format_fix_plans(self, plans: List) -> str:
        if not plans:
            return "No modifications required."
        prompt = (
            "CRITICAL: You MUST call edit_file for EACH plan below.\n"
            "Do NOT skip any plan. Do NOT claim the fix is done without executing edit_file.\n"
            "Read the file first, then apply the edit.\n\n"
            "Apply the following precise fix plans:\n"
        )
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
        prompt += "\nApply ALL of the above plans now. You must call edit_file."
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
    def _build_usage(self) -> dict:
        return {
            "elapsed_seconds": round(time.monotonic() - self._start_time, 2),
            "prompt_tokens": getattr(self.llm, "total_prompt_tokens", 0),
            "completion_tokens": getattr(self.llm, "total_completion_tokens", 0),
            "estimated_cost": getattr(self.llm, "estimated_cost", None),
        }

    def _success_result(self, verify_result, attempts: int, modified_files: List[str],
                       skipped: int = 0, action_log: list | None = None,
                       quality_errors: list | None = None) -> Dict[str, Any]:
        return {
            "status": "success",
            "attempts": attempts,
            "modified_files": modified_files,
            "skipped_low_severity": skipped,
            "quality_warnings": [
                {"tool": e.tool, "file": e.file, "line": e.line,
                 "type": e.error_type, "message": e.message[:120]}
                for e in (quality_errors or [])
            ],
            "action_log": action_log or [],
            "result": verify_result,
            "usage": self._build_usage(),
        }

    def _skipped_result(self, verify_result, attempts: int,
                       action_log: list | None = None) -> Dict[str, Any]:
        return {
            "status": "skipped",
            "attempts": attempts,
            "modified_files": [],
            "skipped_low_severity": 0,
            "action_log": action_log or [],
            "result": verify_result,
            "usage": self._build_usage(),
        }

    def _failed_result(self, verify_result, attempts: int, modified_files: List[str],
                      action_log: list | None = None) -> Dict[str, Any]:
        return {
            "status": "failed",
            "attempts": attempts,
            "modified_files": modified_files,
            "skipped_low_severity": 0,
            "action_log": action_log or [],
            "result": verify_result,
            "usage": self._build_usage(),
        }
