"""Failure Pattern Library - persistent memory of past repair attempts.

When CodeRover runs the verify -> reflect -> fix loop, every iteration is a
small lesson: *which* error class came up, *what* root cause the reflector
guessed, and *whether* the resulting fix actually worked.  This module is
where those lessons are stashed so that future runs can prime the reflector
(or the human reader) with what worked before.

Library layout on disk::

    ~/.coderover/failure_library/
        library.json     # append-only list of FailureCase records

The on-disk format is a single JSON document, hand-shaped rather than
free-form so any reader / dumper pair agrees.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from coderover.agents.reflector import FixPlan
from coderover.verifier.verification import VerifierError


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------

DEFAULT_DIR = Path.home() / ".coderover" / "failure_library"
DEFAULT_FILE = DEFAULT_DIR / "library.json"

# Record outcomes.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"

# Hard cap so a runaway session does not silently produce a multi-MB file.
MAX_CASES = 5_000


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FailureCase:
    """A single fix attempt: what broke, what we tried, whether it worked."""

    case_id: str
    timestamp: float
    # Deterministic signature of the input error set (see ``_signature_of``).
    error_signature: str
    # Human-readable, single-line rendering of the error set.
    error_summary: str
    error_category: str          # "syntax" | "type" | "logic" | "style" | "unknown"
    root_cause: str
    # The fix plan we applied (if any).  May be ``None`` for purely diagnostic
    # recordings.
    fix_plan: Optional[FixPlan] = None
    # Either "success" or "failed".
    outcome: str = OUTCOME_FAILED
    # Which retry attempt this was within one harness run (1-based).
    attempt_number: int = 1
    notes: str = ""


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

class FailureLibrary:
    """Persistent, append-mostly store of ``FailureCase`` records."""

    def __init__(self, path: str | Path = DEFAULT_FILE) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cases: List[FailureCase] = []
        self._load()

    # ----------------------------------------------------------------------
    # Recording
    # ----------------------------------------------------------------------
    def record_case(
        self,
        errors: Iterable[VerifierError],
        root_cause: str,
        outcome: str,
        category: str,
        fix_plan: Optional[FixPlan] = None,
        attempt_number: int = 1,
        notes: str = "",
    ) -> FailureCase:
        """Persist one fix attempt and return the stored case."""
        err_list = list(errors)
        case = FailureCase(
            case_id=uuid.uuid4().hex[:12],
            timestamp=time.time(),
            error_signature=_signature_of(err_list),
            error_summary=_summary_of(err_list),
            error_category=category,
            root_cause=root_cause,
            fix_plan=fix_plan,
            outcome=outcome,
            attempt_number=attempt_number,
            notes=notes,
        )
        self._cases.append(case)
        if len(self._cases) > MAX_CASES:
            # Drop oldest, never the freshest few hundred.
            self._cases = self._cases[-MAX_CASES:]
        self.save()
        return case

    # ----------------------------------------------------------------------
    # Retrieval
    # ----------------------------------------------------------------------
    def find_similar(
        self,
        errors: Iterable[VerifierError],
        top_k: int = 5,
        same_outcome: Optional[str] = None,
    ) -> List[FailureCase]:
        """Look up prior cases whose ``error_signature`` matches ``errors``.

        Args:
            errors: The error set the Agent is staring at right now.
            top_k: Maximum results to return.
            same_outcome: If provided (e.g. ``OUTCOME_SUCCESS``), only return
                cases with that outcome — useful for "how did we fix this
                last time?" queries.
        """
        target = _signature_of(list(errors))
        pool = [c for c in self._cases if c.error_signature == target
                and (same_outcome is None or c.outcome == same_outcome)]
        # Newest first — recent experience is more relevant than ancient.
        pool.sort(key=lambda c: c.timestamp, reverse=True)
        return pool[:top_k]

    def list_recent(self, n: int = 20) -> List[FailureCase]:
        """Return the ``n`` most recently recorded cases, newest first."""
        return list(reversed(self._cases[-n:]))

    def stats(self) -> Dict[str, Any]:
        """Aggregate counts for quick overviews."""
        if not self._cases:
            return {"total": 0, "by_outcome": {}, "by_category": {}}
        by_outcome: Dict[str, int] = {}
        by_category: Dict[str, int] = {}
        for c in self._cases:
            by_outcome[c.outcome] = by_outcome.get(c.outcome, 0) + 1
            by_category[c.error_category] = by_category.get(c.error_category, 0) + 1
        return {
            "total": len(self._cases),
            "by_outcome": by_outcome,
            "by_category": by_category,
        }

    # ----------------------------------------------------------------------
    # Compatibility API  —  spec requires add() and retrieve()
    # ----------------------------------------------------------------------
    def add(self, error_summary: dict, fix_plan: FixPlan, success: bool) -> None:
        """Add a new case from a plain dict.

        This is a convenience wrapper around ``record_case()`` for callers
        (such as the acceptance test) that pass a dict rather than
        ``VerifierError`` objects.

        The dict must contain at least ``"error_type"``;  ``"message"`` is
        optional.
        """
        errors = [
            VerifierError(
                tool=error_summary.get("tool", "unknown"),
                file=error_summary.get("file", ""),
                line=error_summary.get("line", 0),
                error_type=error_summary.get("error_type", "unknown"),
                message=error_summary.get("message", ""),
                context=error_summary.get("context", ""),
            )
        ]
        self.record_case(
            errors=errors,
            root_cause=error_summary.get("root_cause", ""),
            outcome=OUTCOME_SUCCESS if success else OUTCOME_FAILED,
            category=error_summary.get("error_category", "unknown"),
            fix_plan=fix_plan,
        )

    def retrieve(self, error_summary: dict, top_k: int = 3) -> List[dict]:
        """Return the *top_k* most similar past cases as plain dicts.

        This is the dict-oriented counterpart of ``find_similar()``.  It
        creates a temporary ``VerifierError`` from *error_summary*, computes
        the signature, and returns matched records as JSON-safe dicts.
        """
        needle = VerifierError(
            tool=error_summary.get("tool", "unknown"),
            file=error_summary.get("file", ""),
            line=error_summary.get("line", 0),
            error_type=error_summary.get("error_type", "unknown"),
            message=error_summary.get("message", ""),
            context="",
        )
        cases = self.find_similar([needle], top_k=top_k)
        return [_case_to_dict(c) for c in cases]

    # ----------------------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------------------
    def save(self) -> None:
        """Rewrite the on-disk JSON file with the current state."""
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "cases": [_case_to_dict(c) for c in self._cases],
        }
        # Atomic write: dump to temp, then rename.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt file — start fresh rather than crash the harness.
            return
        self._cases = [_dict_to_case(d) for d in data.get("cases", [])
                       if isinstance(d, dict)]

    def __len__(self) -> int:
        return len(self._cases)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signature_of(errors: List[VerifierError]) -> str:
    """Deterministic hash of a (possibly empty) error set."""
    norm = sorted(
        (e.tool, e.file, e.error_type, _normalize_msg(e.message))
        for e in errors
    )
    blob = json.dumps(norm, ensure_ascii=False, sort_keys=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _normalize_msg(msg: str) -> str:
    """Drop volatile bits (line numbers, addresses) so the same logical error
    collapses to one signature.

    Consecutive digits collapse into a single ``n`` placeholder so that
    ``assert 2 == 6`` and ``assert 99 == 666`` normalize to the same string.
    """
    out: List[str] = []
    in_digit = False
    for ch in msg:
        if ch.isdigit():
            if not in_digit:
                out.append("n")
                in_digit = True
        else:
            in_digit = False
            out.append(ch.lower())
    return "".join(out).strip()[:200]


def _summary_of(errors: List[VerifierError]) -> str:
    if not errors:
        return "(no errors)"
    parts = [f"[{e.tool}] {e.error_type}: {e.message[:80]}" for e in errors[:3]]
    if len(errors) > 3:
        parts.append(f"... +{len(errors) - 3} more")
    return " | ".join(parts)


def _case_to_dict(c: FailureCase) -> Dict[str, Any]:
    d = asdict(c)
    if c.fix_plan is not None:
        # FixPlan is a pydantic model — use .model_dump(), asdict() won't work.
        d["fix_plan"] = c.fix_plan.model_dump()
    return d


def _dict_to_case(d: Dict[str, Any]) -> FailureCase:
    plan_d = d.pop("fix_plan", None)
    fix_plan = FixPlan(**plan_d) if isinstance(plan_d, dict) else None
    return FailureCase(fix_plan=fix_plan, **d)
