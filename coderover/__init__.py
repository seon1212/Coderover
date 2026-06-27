"""CoreCoder - Minimal AI coding agent inspired by Claude Code's architecture."""

__version__ = "0.3.0"

from coderover.agent import Agent
from coderover.llm import LLM
from coderover.config import Config
from coderover.tools import ALL_TOOLS

__all__ = ["Agent", "LLM", "Config", "ALL_TOOLS", "__version__"]
