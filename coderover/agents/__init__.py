"""Agents - specialized components for CodeRover.

This module provides the Reflector, which analyzes verification errors
and generates structured fix plans.
"""

from .reflector import FixPlan, ReflectorResult, reflect

__all__ = ["ReflectorResult", "FixPlan", "reflect"]