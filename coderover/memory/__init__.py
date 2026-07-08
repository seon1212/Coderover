"""Memory layer for CodeRover — long-lived stores beyond a single session.

Currently exposes the :class:`FailureLibrary`, an append-mostly log of past
fix attempts that the Reflector can prime from on subsequent runs.
"""

from .failure_library import (
    DEFAULT_DIR,
    DEFAULT_FILE,
    FailureCase,
    FailureLibrary,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
)

__all__ = [
    "DEFAULT_DIR",
    "DEFAULT_FILE",
    "FailureCase",
    "FailureLibrary",
    "OUTCOME_FAILED",
    "OUTCOME_SUCCESS",
]
