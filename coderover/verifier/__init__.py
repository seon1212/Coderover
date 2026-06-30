"""Verifier - unified automatic verification layer.

Integrates pytest, mypy, and ruff to validate code quality in a single pass.
"""

from .verification import verify, VerifierError, VerifierResult

__all__ = ["verify", "VerifierError", "VerifierResult"]
