"""GitHub REST API client — create Pull Requests from CodeRover results."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests


API_BASE = "https://api.github.com"
_ENV_TOKEN = "GITHUB_TOKEN"


class GitHubClient:
    """Thin wrapper around GitHub REST API for PR creation.

    Usage::

        client = GitHubClient()                  # reads GITHUB_TOKEN env var
        client = GitHubClient(token="ghp_...")   # explicit token

        pr = client.create_pr(
            repo="owner/repo",
            title="[CodeRover] Fix bug",
            body="...",
            head="coderover-fix",
            base="main",
        )
        print(pr["html_url"])
    """

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token or os.environ.get(_ENV_TOKEN) or ""
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "CodeRover/1.0",
        })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def create_pr(
        self,
        repo: str,
        title: str,
        body: str = "",
        head: str = "coderover-fix",
        base: str = "main",
    ) -> Dict[str, Any]:
        """Create a Pull Request on *repo*.

        Args:
            repo: ``"owner/repo"`` (e.g. ``"seon1212/Coderover"``).
            title: PR title.
            body: PR description.
            head: Source branch name.
            base: Target branch name.

        Returns:
            The JSON response from GitHub (contains ``html_url``, ``number``,
            ``state``, etc.).

        Raises:
            ValueError: Token is empty.
            requests.RequestException: Network / API error.
        """
        if not self.token:
            raise ValueError(
                f"GitHub token not found — set the {_ENV_TOKEN} environment variable."
            )

        url = f"{API_BASE}/repos/{repo}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
        resp = self.session.post(url, json=payload)
        if resp.status_code == 422:
            # Likely the branch does not exist.  Surface the API error.
            detail = resp.json().get("errors", [{"message": resp.json().get("message", "")}])
            raise ValueError(f"GitHub API error (422): {detail[0]['message']}")
        resp.raise_for_status()
        return resp.json()
