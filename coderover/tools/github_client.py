"""GitHub REST API client — create Pull Requests and read Issues."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional, Tuple

import requests


API_BASE = "https://api.github.com"
_ENV_TOKEN = "GITHUB_TOKEN"

# GitHub Issue URL pattern:
#   https://github.com/{owner}/{repo}/issues/{number}
_ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)\s*$"
)


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

    # ------------------------------------------------------------------
    # Issue API
    # ------------------------------------------------------------------
    def get_issue(self, issue_url: str) -> Dict[str, Any]:
        """Fetch a GitHub Issue by URL.

        Args:
            issue_url: Full URL like ``https://github.com/owner/repo/issues/42``.

        Returns:
            A dict with at least ``title``, ``body``, ``html_url``, ``number``,
            and ``state`` keys.

        Raises:
            ValueError: URL format is invalid.
            requests.RequestException: Network / API error.
        """
        owner, repo, number = parse_issue_url(issue_url)
        url = f"{API_BASE}/repos/{owner}/{repo}/issues/{number}"
        resp = self.session.get(url)
        if resp.status_code == 404:
            raise ValueError(
                f"Issue not found: {owner}/{repo}#{number}. "
                "Check that the URL and repository are correct."
            )
        if resp.status_code == 403:
            raise ValueError(
                f"GitHub API rate limit (403). "
                f"Set the {_ENV_TOKEN} environment variable for a higher limit."
            )
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title", ""),
            "body": data.get("body", "") or "",
            "html_url": data.get("html_url", issue_url),
            "number": data.get("number", number),
            "state": data.get("state", "open"),
        }


def parse_issue_url(url: str) -> Tuple[str, str, int]:
    """Parse a GitHub Issue URL into ``(owner, repo, issue_number)``.

    Args:
        url: Full URL like ``https://github.com/owner/repo/issues/42``.

    Returns:
        ``(owner, repo, issue_number)``.

    Raises:
        ValueError: If the URL does not match the expected pattern.
    """
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(
            f"Invalid GitHub Issue URL: {url!r}\n"
            "Expected format: https://github.com/{owner}/{repo}/issues/{number}"
        )
    return m.group(1), m.group(2), int(m.group(3))
