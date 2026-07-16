"""Reflector - error analysis and fix plan generator.

The Reflector receives structured errors from the Verifier and produces
structured fix plans by analyzing the root cause and generating concrete
code changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from pydantic import BaseModel, ValidationError

from coderover.config import Config
from coderover.llm import LLM, LiteLLM


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class FixPlan(BaseModel):
    """A single file fix plan.

    Attributes:
        file: Path to the file to modify.
        line: Line number of the code to modify (or starting line for multi-line changes).
        old_code: Original code snippet (or the code segment to replace).
        new_code: Modified code snippet (the replacement).
        explanation: Why this change is necessary.
    """

    file: str
    line: int
    old_code: str
    new_code: str
    explanation: str


class ReflectorResult(BaseModel):
    """Complete output from the Reflector.

    Attributes:
        root_cause: Human-readable analysis of the error's root cause.
        error_category: One of "syntax", "type", "logic", "style", "unknown".
        priority: 0=highest (blocking), 1=high, 2=medium, 3=low.
        fix_plans: List of concrete fix plans, one per file modification.
        reasoning: Step-by-step reasoning process (useful for debugging).
        confidence: Confidence level in the fix (0.0-1.0).
    """

    root_cause: str
    error_category: str
    priority: int
    fix_plans: List[FixPlan] = []
    reasoning: str = ""
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_error(error_type: str, tool: str) -> str:
    """Classify an error into a category.

    Args:
        error_type: The error type (e.g., "AssertionError", "override").
        tool: The tool that reported the error ("pytest", "mypy", "ruff").

    Returns:
        One of "syntax", "type", "logic", "style", "unknown".
    """
    error_type_lower = error_type.lower()

    # Syntax errors
    if "syntax" in error_type_lower or "indentation" in error_type_lower:
        return "syntax"

    # Logic errors (pytest failures)
    if "assertion" in error_type_lower or "failed" in error_type_lower:
        return "logic"

    # Type errors (mypy)
    if tool == "mypy" or "override" in error_type_lower or "assignment" in error_type_lower:
        return "type"

    # Style errors (ruff)
    if tool == "ruff":
        return "style"

    return "unknown"


def _assign_priority(category: str, tool: str) -> int:
    """Assign priority level based on error category and tool.

    Args:
        category: The error category from _classify_error.
        tool: The tool that reported the error.

    Returns:
        0=highest (blocking), 1=high, 2=medium, 3=low.
    """
    # Pytest failures are always highest priority
    if tool == "pytest":
        return 0

    # Syntax errors block execution
    if category == "syntax":
        return 0

    # Type errors should be fixed
    if category == "type":
        return 1

    # Logic errors need investigation
    if category == "logic":
        return 1

    # Style errors can wait
    if category == "style":
        return 2

    return 3


# ---------------------------------------------------------------------------
# Code context extraction
# ---------------------------------------------------------------------------


def _get_code_context(file_path: str, line: int, context_lines: int = 5) -> str:
    """Extract code context around the error line.

    Args:
        file_path: Path to the file with the error.
        line: The line number where the error occurred (1-indexed).
        context_lines: Number of lines to include before and after the error.

    Returns:
        A string with the code context, or empty string if the file cannot be read.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            return ""

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

        # Convert 1-indexed line to 0-indexed
        start = max(0, line - context_lines - 1)
        end = min(len(lines), line + context_lines)

        context_lines_list = []
        for i in range(start, end):
            line_num = i + 1
            marker = ">" if line_num == line else " "
            context_lines_list.append(f"{line_num:4d}{marker} {lines[i]}")

        return "\n".join(context_lines_list)
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------
# LLM integration
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are a code error analysis expert. Your task is to:

1. Analyze the error's root cause (don't just repeat the error message — infer the deeper issue).
2. Determine the error category (syntax/type/logic/style).
3. Generate specific fix code (show old_code -> new_code comparison).
4. Output ONLY valid JSON, no other text.

The JSON structure must be:
{{
  "root_cause": "Brief explanation of the root cause",
  "error_category": "syntax|type|logic|style|unknown",
  "priority": 0|1|2|3,
  "fix_plans": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "old_code": "the original code",
      "new_code": "the fixed code",
      "explanation": "why this fix works"
    }}
  ],
  "reasoning": "step-by-step analysis",
  "confidence": 0.0-1.0
}}

Error information:
{error_summary}

Code context:
{code_context}

Similar successful fixes from the past:
{extra_context}

Remember: Output ONLY the JSON, no markdown, no explanations."""


def _parse_llm_response(content: str) -> dict[str, Any]:
    """Parse LLM response, handling various JSON formats.

    Args:
        content: Raw content from the LLM.

    Returns:
        Parsed dictionary, or empty dict if parsing fails.
    """
    # Try direct JSON parsing
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from markdown code blocks
    for prefix in ["```json", "```JSON", "```"]:
        if content.startswith(prefix):
            content = content[len(prefix) :].strip()
            if content.endswith("```"):
                content = content[:-3].strip()
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue

    # Try to find JSON-like patterns
    start = content.find("{")
    if start != -1:
        end = content.rfind("}")
        if end != -1 and end > start:
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                pass

    return {}


def _create_fallback_result(error: Any) -> ReflectorResult:
    """Create a fallback result when LLM fails.

    Args:
        error: The error that caused the failure.

    Returns:
        A ReflectorResult with confidence=0.0 and minimal information.
    """
    return ReflectorResult(
        root_cause=f"LLM analysis failed: {error}",
        error_category="unknown",
        priority=3,
        fix_plans=[],
        reasoning=f"Could not generate fix plan due to LLM error: {error}",
        confidence=0.0,
    )


# ---------------------------------------------------------------------------
# Main reflect function
# ---------------------------------------------------------------------------


def reflect(
    error_summary: List[Any], repo_path: Path | str,
    config: Config | None = None, extra_context: str = ""
) -> ReflectorResult:
    """Analyze errors and generate fix plans.

    This function processes a list of VerifierError objects, classifies them,
    assigns priorities, and uses an LLM to generate concrete fix plans.

    Args:
        error_summary: List of VerifierError objects from the Verifier.
        repo_path: Path to the repository root (for context extraction).
        config: Optional Config object for LLM initialization. If not provided,
            defaults to Config.from_env().

    Returns:
        A ReflectorResult containing the analysis and fix plans. If LLM fails,
        returns a fallback result with confidence=0.0.
    """
    # Convert repo_path to Path
    repo_path = Path(repo_path).resolve()

    # Use default config if not provided
    if config is None:
        config = Config.from_env()

    # Sort errors by priority (highest first)
    sorted_errors = sorted(
        error_summary,
        key=lambda e: (
            _assign_priority(_classify_error(e.error_type, e.tool), e.tool),
            e.line if e.line > 0 else 999999,
        ),
    )

    if not sorted_errors:
        return ReflectorResult(
            root_cause="No errors to analyze",
            error_category="unknown",
            priority=3,
            fix_plans=[],
            reasoning="No errors were provided for analysis.",
            confidence=1.0,
        )

    # Build error summary for the LLM
    error_text = "\n".join(
        [
            f"- {e.tool}:{e.file}:{e.line} — {e.error_type}: {e.message}"
            for e in sorted_errors
        ]
    )

    # Collect code context for all errors
    context_parts: List[str] = []
    for err in sorted_errors:
        if err.file:
            ctx = _get_code_context(err.file, err.line)
            if ctx:
                context_parts.append(f"\n### {err.file}:{err.line}\n{ctx}")

    code_context = "\n".join(context_parts) if context_parts else "No context available"


    # Create LLM instance
    llm: LLM | LiteLLM
    try:
        if config.provider == "litellm":
            llm = LiteLLM(
                model=config.model,
                api_key=config.api_key or None,
                base_url=config.base_url,
                temperature=0.0,
                max_tokens=8192,
            )
        else:
            llm = LLM(
                model=config.model,
                api_key=config.api_key,
                base_url=config.base_url,
                temperature=0.0,
                max_tokens=8192,
            )
    except Exception as e:
        return _create_fallback_result(e)

    # Prepare the prompt
    prompt = _SYSTEM_PROMPT.format(
        error_summary=error_text, code_context=code_context,
        extra_context=extra_context
    )

    # Call the LLM
    try:
        response = llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": "You are a code error analysis expert who outputs only JSON.",
                },
                {"role": "user", "content": prompt},
            ]
        )
        content = response.content.strip()
    except Exception as e:
        return _create_fallback_result(e)

    
    # Parse the LLM response
    parsed = _parse_llm_response(content)

    # Validate and convert to ReflectorResult
    try:
        # Extract fix_plans as list of dicts
        fix_plans_data = parsed.get("fix_plans", [])
        fix_plans: List[FixPlan] = []

        for plan in fix_plans_data:
            if isinstance(plan, dict):
                fix_plans.append(
                    FixPlan(
                        file=str(plan.get("file", "")),
                        line=int(plan.get("line", 0)),
                        old_code=str(plan.get("old_code", "")),
                        new_code=str(plan.get("new_code", "")),
                        explanation=str(plan.get("explanation", "")),
                    )
                )

        result = ReflectorResult(
            root_cause=str(parsed.get("root_cause", "No root cause provided")),
            error_category=str(parsed.get("error_category", "unknown")),
            priority=int(parsed.get("priority", 3)),
            fix_plans=fix_plans,
            reasoning=str(parsed.get("reasoning", "")),
            confidence=float(parsed.get("confidence", 0.0)),
        )

        # Validate the result
        if not 0.0 <= result.confidence <= 1.0:
            result.confidence = 0.5

        if result.error_category not in ["syntax", "type", "logic", "style", "unknown"]:
            result.error_category = "unknown"

        if result.priority not in [0, 1, 2, 3]:
            result.priority = 3

        return result

    except (ValidationError, ValueError, KeyError, TypeError) as e:
        # If validation fails, create a degraded result
        return ReflectorResult(
            root_cause=f"Failed to parse LLM response: {e}",
            error_category="unknown",
            priority=3,
            fix_plans=[],
            reasoning=f"LLM response could not be validated: {content[:500]}",
            confidence=0.0,
        )
