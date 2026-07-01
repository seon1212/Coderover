"""
Adaptive Harness: 闭环调度器，控制 执行→验证→反思→重试 循环。
"""

from pathlib import Path
from typing import Dict, Any, List

from coderover.agent import Agent
from coderover.verifier import verify
from coderover.agents import reflect


class AdaptiveHarness:
    """自适应运行时，负责调度 Agent、Verifier、Reflector 形成闭环。"""

    def __init__(self, llm, tools, max_retries: int = 3):
        self.llm = llm
        self.tools = tools
        self.max_retries = max_retries
        self.agent = Agent(llm, tools)

    def run(self, task: str, repo_path: str) -> Dict[str, Any]:
        repo_path = Path(repo_path).resolve()
        history = []
        actual_attempts = 0

        # Reset agent context for a fresh run
        self.agent.reset()

        print("\n[LOOP] === Attempt 1 ===")
        self.agent.chat(task)
        actual_attempts += 1

        for attempt in range(1, self.max_retries + 1):
            verify_result = verify(repo_path)
            print(f"  Verification: {'[PASS]' if verify_result.passed else '[FAIL]'}")

            if verify_result.passed:
                return {"status": "success", "attempts": actual_attempts, "result": verify_result}

            if attempt >= self.max_retries:
                break

            current_errors = [e.message for e in verify_result.errors]
            # Loop detection: check if errors are identical to the previous round
            if history and current_errors == history[-1]:
                print("  [WARN] Loop detected (errors identical to previous round), stopping retries.")
                break
            history.append(current_errors)

            print(f"  Found {len(verify_result.errors)} error(s), calling Reflector...")
            reflector_result = reflect(verify_result.errors, repo_path)
            #下面两句测试用。
            print(f"  Reflector root_cause: {reflector_result.root_cause[:100]}...")
            print(f"  Reflector reasoning: {reflector_result.reasoning[:100]}...")
            fix_plans = reflector_result.fix_plans
            print(f"  Reflector generated {len(fix_plans)} fix plan(s), confidence: {reflector_result.confidence:.2f}")

            # Confidence check: stop if Reflector is not confident
            if reflector_result.confidence < 0.3:
                print("  [WARN] Low confidence (< 0.3), stopping retries.")
                return {"status": "failed", "attempts": actual_attempts, "result": verify_result}

            if not fix_plans:
                print("  [WARN] No fix plans generated, stopping retries.")
                break

            fix_prompt = self._format_fix_plans(fix_plans)
            print(f"\n[LOOP] === Attempt {attempt + 1} ===")
            self.agent.chat(fix_prompt)
            actual_attempts += 1

        return {"status": "failed", "attempts": actual_attempts, "result": verify_result}

    def _format_fix_plans(self, plans: List) -> str:
        if not plans:
            return "没有需要修改的方案。"
        prompt = "请根据以下精确的修复方案，使用 edit 工具逐一修改代码：\n"
        for idx, p in enumerate(plans, 1):
            prompt += f"\n--- 修复方案 {idx} ---\n"
            prompt += f"文件：{p.file}\n"
            prompt += f"行号（参考）：{p.line}\n"
            prompt += f"需要替换的旧代码段：\n```\n{p.old_code}\n```\n"
            prompt += f"替换为的新代码段：\n```\n{p.new_code}\n```\n"
            prompt += f"修改原因：{p.explanation}\n"
        prompt += "\n请严格按照上述方案执行，只改动指定的代码段。"
        return prompt