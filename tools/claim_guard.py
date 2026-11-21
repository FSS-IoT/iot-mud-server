"""Fail a PR that has not claimed its issue or duplicates another open PR."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

LINK_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
    re.IGNORECASE,
)


def linked_issue(body: str | None) -> int | None:
    """Return the issue number named by a closing keyword, if any."""
    match = LINK_RE.search(body or "")
    return int(match.group(1)) if match else None


def claim_errors(
    issue: int | None,
    author: str,
    assigned: list[str],
    other_prs: list[int],
) -> list[str]:
    """Return the reasons this PR does not hold the claim."""
    if issue is None:
        return ["PR body must link an issue with `Closes #N`."]

    errors: list[str] = []
    if author not in assigned:
        who = ", ".join(assigned) if assigned else "nobody"
        errors.append(
            f"#{issue} is assigned to {who}; {author} must claim it first "
            f"(`gh issue edit {issue} --add-assignee @me`)."
        )
    if other_prs:
        numbers = ", ".join(f"#{number}" for number in other_prs)
        errors.append(f"#{issue} is already claimed by open PR(s) {numbers}.")
    return errors


def _api(path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["PR_NUMBER"])
    author = os.environ["PR_AUTHOR"]
    body = os.environ.get("PR_BODY", "")
    token = os.environ["GITHUB_TOKEN"]

    issue = linked_issue(body)
    if issue is None:
        print("::error::" + claim_errors(None, author, [], [])[0])
        return 1

    try:
        issue_data = _api(f"/repos/{repo}/issues/{issue}", token)
        if "pull_request" in issue_data:
            print(f"::error::#{issue} is a pull request; link an issue instead.")
            return 1
        open_prs = _api(f"/repos/{repo}/pulls?state=open&per_page=100", token)
    except urllib.error.HTTPError as error:
        print(f"::error::GitHub API returned {error.code} for #{issue}: {error.reason}.")
        return 1
    except urllib.error.URLError as error:
        print(f"::error::Could not reach the GitHub API: {error.reason}.")
        return 1

    assigned = [assignee["login"] for assignee in issue_data.get("assignees", [])]
    other_prs = [
        pull["number"]
        for pull in open_prs
        if pull["number"] != pr_number
        and linked_issue(pull.get("body") or "") == issue
    ]

    errors = claim_errors(issue, author, assigned, other_prs)
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
