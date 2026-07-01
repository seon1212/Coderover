"""Unified verification layer integrating pytest, mypy, and ruff.

Each tool is run independently, its raw output is parsed into structured
VerifierError objects, and results are aggregated into a VerifierResult.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class VerifierError(BaseModel):
    """A single verification error produced by one of the tools."""

    tool: str  # "pytest" | "mypy" | "ruff"
    file: str  # file path where the error occurred
    line: int  # line number (0 if unknown)
    error_type: str  # e.g. AssertionError, SyntaxError, type-arg
    message: str  # human-readable error description
    context: str  # surrounding context (function name, code snippet, etc.)


class VerifierResult(BaseModel):
    """Aggregated result from running one or more verification tools."""

    passed: bool  # True when every tool reports zero errors
    errors: List[VerifierError] = []
    summary: str = ""
    raw_outputs: Dict[str, str] = {}  # tool_name -> raw stdout+stderr

    # Severity icons and labels for priority grouping
    _SEVERITY = {
        "pytest": ("[TESTS]", "P0"),
        "mypy":  ("[TYPES]", "P1"),
        "ruff":  ("[LINT] ", "P2"),
    }
    _TOOL_ORDER = ["pytest", "mypy", "ruff"]

    def __str__(self) -> str:
        """Pretty-print a colour-coded, priority-grouped report."""
        if self.passed and not self.errors:
            return "\n".join([
                "+-------------------------------------------+",
                "|       *** All checks passed! ***          |",
                "+-------------------------------------------+",
                f"  {self.summary}",
            ])

        lines: List[str] = []
        # Header
        status_icon = "[FAIL]" if not self.passed else "[WARN]"
        lines.append("+--------------------------------------------------+")
        lines.append(f"|  {status_icon} — Verification Report  —  \n  {self.summary[:49]:<49} |")
        lines.append("+--------------------------------------------------+")
        lines.append("")

        # Group errors by tool, sorted by priority
        for tool in self._TOOL_ORDER:
            group = [e for e in self.errors if e.tool == tool]
            if not group:
                continue
            tag, priority = self._SEVERITY.get(tool, (tool.upper(), "-"))
            lines.append(f"  {tag}  {priority}  —  {len(group)} issue(s)")
            lines.append("  " + "-" * 48)
            for i, err in enumerate(group, 1):
                loc = f"{err.file}:{err.line}" if err.file else "(n/a)"
                lines.append(f"  [{i}] {loc}")
                lines.append(f"      {err.error_type}: {err.message[:120]}")
                if err.context and err.context != err.message[:120]:
                    ctx = err.context[:100]
                    lines.append(f"      context: {ctx}")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool discovery helpers
# ---------------------------------------------------------------------------

def _find_executable(name: str) -> Optional[str]:
    """Return the full path to *name* if it is on PATH, else None."""
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

def _run_tool(
    repo_path: Path, args: List[str], tool_name: str
) -> Tuple[bool, str]:
    """Run a subprocess inside *repo_path*, return (passed, output).

    *passed* is True when the exit code is 0.  A timeout of 120 s is enforced.
    """
    executable = _find_executable(tool_name)
    if executable is None:
        return False, f"Error: '{tool_name}' not found on PATH"

    try:
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=str(repo_path),
        )
        output = proc.stdout
        if proc.stderr:
            output += "\n" + proc.stderr
        return proc.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, f"Error: '{tool_name}' timed out after 120s"
    except OSError as exc:
        return False, f"Error: failed to run '{tool_name}': {exc}"


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------

def _run_pytest(repo_path: Path) -> Tuple[bool, str]:
    """Execute pytest with short tracebacks, stopping after 5 failures.

    Args:
        repo_path: Root of the repository to test.

    Returns:
        (passed, combined stdout + stderr).
    """
    return _run_tool(
        repo_path,
        ["--tb=short", "--maxfail=5"],
        "pytest",
    )


_PYTEST_LINE_RE = re.compile(
    r"^(?P<file>.+?\.py):(?P<line>\d+):\s+in\s+(?P<context>\S+)"
)
_PYTEST_ERROR_RE = re.compile(r"^E\s+(?P<error_type>\w+(?:Error|Warning|Exception))(?::\s+(?P<message>.*))?")


def _parse_pytest_output(output: str) -> List[VerifierError]:
    """Parse pytest output into structured errors.

    Parsing strategy:
    1. Scan for ``file.py:line: in function`` headers that mark each failure.
    2. Scan the following ``E ...`` lines for the exception type and message.
    3. Collect the nearest code-context line (``> ...``) when available.
    """
    errors: List[VerifierError] = []
    lines = output.splitlines()

    current_file = ""
    current_line = 0
    current_context = ""
    error_type = ""
    message = ""
    code_context = ""
    saw_error_line = False

    for line in lines:
        # Match the failure header: tests/test_math.py:15: in test_add
        m = _PYTEST_LINE_RE.search(line)
        if m and not line.startswith("E"):
            # If we already have a pending error, flush it
            if current_file:
                errors.append(
                    VerifierError(
                        tool="pytest",
                        file=current_file,
                        line=current_line,
                        error_type=error_type or "Failure",
                        message=message or "test failed",
                        context=code_context or current_context,
                    )
                )
            current_file = m.group("file")
            current_line = int(m.group("line"))
            current_context = m.group("context")
            error_type = ""
            message = ""
            code_context = ""
            saw_error_line = False
            continue

        # Capture source code context line: ">       assert add(1, 1) == 3"
        if line.startswith(">") and not saw_error_line:
            code_context = line.strip().lstrip(">").strip()
            continue

        # Match error line: "E   AssertionError: assert 2 == 3"
        if line.startswith("E"):
            em = _PYTEST_ERROR_RE.match(line)
            if em:
                error_type = em.group("error_type")
                message = em.group("message") or ""
                saw_error_line = True
            elif saw_error_line:
                # Continuation lines like "E   assert 2 == 3"
                if message:
                    message += " "
                message += line[1:].strip()

    # Flush last pending error
    if current_file:
        errors.append(
            VerifierError(
                tool="pytest",
                file=current_file,
                line=current_line,
                error_type=error_type or "Failure",
                message=message or "test failed",
                context=code_context or current_context,
            )
        )

    return errors


# ---------------------------------------------------------------------------
# mypy
# ---------------------------------------------------------------------------

def _run_mypy(repo_path: Path) -> Tuple[bool, str]:
    """Execute mypy type checking.

    Args:
        repo_path: Root of the repository to type-check.

    Returns:
        (passed, combined stdout + stderr).
    """
    return _run_tool(
        repo_path,
        [".", "--no-error-summary"],
        "mypy",
    )


_MYPY_LINE_RE = re.compile(
    r"^(?P<file>.+?\.py):(?P<line>\d+):\s+(?P<severity>error|note|warning):\s+"
    r"(?P<message>.+?)(?:\s+\[(?P<error_type>[a-z][\w-]*)\])?$"
)


def _parse_mypy_output(output: str) -> List[VerifierError]:
    """Parse mypy output into structured errors.

    Expected format::

        file.py:42: error: Incompatible return value type [return-value]
        file.py:42: note:     Got "int", expected "str"

    Only ``error`` severity lines are collected; ``note`` lines are folded
    into the preceding error's context or message.
    """
    errors: List[VerifierError] = []
    lines = output.splitlines()

    for line in lines:
        m = _MYPY_LINE_RE.match(line.strip())
        if not m:
            # Multi-line continuation or unrelated output — skip
            continue

        severity = m.group("severity")
        if severity != "error":
            # Attach note to the previous error as extra context
            if errors:
                prev = errors[-1]
                note_text = m.group("message")
                prev.context = (
                    f"{prev.context}; note: {note_text}"
                    if prev.context
                    else f"note: {note_text}"
                )
            continue

        file_path = m.group("file")
        line_num = int(m.group("line"))
        msg = m.group("message")
        etype = m.group("error_type") or "type-error"

        errors.append(
            VerifierError(
                tool="mypy",
                file=file_path,
                line=line_num,
                error_type=etype,
                message=msg,
                context="",
            )
        )

    return errors


# ---------------------------------------------------------------------------
# ruff
# ---------------------------------------------------------------------------

def _run_ruff(repo_path: Path) -> Tuple[bool, str]:
    """Execute ruff linter.

    Args:
        repo_path: Root of the repository to lint.

    Returns:
        (passed, combined stdout + stderr).  ruff exits 0 when there are
        no fixable issues (informational messages also produce exit 0).
    """
    return _run_tool(
        repo_path,
        ["check", ".", "--output-format=concise"],
        "ruff",
    )


_RUFF_LINE_RE = re.compile(
    r"^(?P<file>.+?\.py):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<rule>[A-Z]+\d+)\s+(?P<message>.+)$"
)


def _parse_ruff_output(output: str) -> List[VerifierError]:
    """Parse ruff output into structured errors.

    Expected format::

        file.py:15:1: F401 'os' imported but unused

    Rule codes (F401, E501, …) become *error_type*.
    """
    errors: List[VerifierError] = []

    for line in output.splitlines():
        m = _RUFF_LINE_RE.match(line.strip())
        if not m:
            continue

        errors.append(
            VerifierError(
                tool="ruff",
                file=m.group("file"),
                line=int(m.group("line")),
                error_type=m.group("rule"),
                message=m.group("message"),
                context=f"col {m.group('col')}",
            )
        )

    return errors


# ---------------------------------------------------------------------------
# Unified entry-point
# ---------------------------------------------------------------------------

# Maps tool names to their runner / parser pairs.
_TOOL_REGISTRY: Dict[str, Tuple[Callable, Callable]] = {
    "pytest": (_run_pytest, _parse_pytest_output),
    "mypy": (_run_mypy, _parse_mypy_output),
    "ruff": (_run_ruff, _parse_ruff_output),
}


def verify(
    repo_path: Path,
    tools: Optional[List[str]] = None,
) -> VerifierResult:
    """Run verification tools sequentially and aggregate results.

    Args:
        repo_path: Path to the repository root (a string will be coerced to
            ``Path``).
        tools: List of tool names to run.  Supported values are ``"pytest"``,
            ``"mypy"``, and ``"ruff"``.  Defaults to all three.

    Returns:
        A ``VerifierResult`` with structured errors, per-tool raw output,
        and a human-readable summary.
    """
    if tools is None:
        tools = ["pytest", "mypy", "ruff"]

    repo_path = Path(repo_path).resolve()
    all_errors: List[VerifierError] = []
    raw_outputs: Dict[str, str] = {}
    tool_results: List[Tuple[str, bool, str]] = []

    for name in tools:
        entry = _TOOL_REGISTRY.get(name)
        if entry is None:
            msg = f"Error: unknown tool '{name}' — supported: pytest, mypy, ruff"
            all_errors.append(
                VerifierError(
                    tool=name,
                    file="",
                    line=0,
                    error_type="unknown-tool",
                    message=msg,
                    context="",
                )
            )
            raw_outputs[name] = msg
            tool_results.append((name, False, msg))
            continue

        runner, parser = entry
        assert runner is not None and parser is not None  # guarded by entry is None above

        passed, output = runner(repo_path)
        #测试用
        print(f"\n===== {name} raw output =====")
        print(output)
        print("=============================\n")

        raw_outputs[name] = output

        if not passed and output.startswith("Error:"):
            # Tool not found or timed out — surface as a single error
            all_errors.append(
                VerifierError(
                    tool=name,
                    file="",
                    line=0,
                    error_type="tool-error",
                    message=output,
                    context="",
                )
            )
            tool_results.append((name, False, output))
            continue

        parsed = parser(output)
        #测试用
        print(f"{name}: passed={passed}, parsed_errors={len(parsed)}")
        for e in parsed:
            print(e)

        all_errors.extend(parsed)
        tool_results.append((name, passed and len(parsed) == 0, output))

    passed = all(ok for _, ok, _ in tool_results) and len(all_errors) == 0

    # Build human-readable summary
    summary_parts: List[str] = []
    for name, ok, _ in tool_results:
        tool_errors = [e for e in all_errors if e.tool == name]
        status = "PASSED" if ok else f"{len(tool_errors)} issue(s)"
        summary_parts.append(f"{name}: {status}")

    return VerifierResult(
        passed=passed,
        errors=all_errors,
        summary=" | ".join(summary_parts),
        raw_outputs=raw_outputs,
    )
