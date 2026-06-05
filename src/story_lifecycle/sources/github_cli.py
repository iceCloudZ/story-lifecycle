"""gh CLI wrapper — all GitHub API calls go through subprocess.run(["gh", ...])."""

from __future__ import annotations

import json
import logging
import re
import subprocess

log = logging.getLogger(__name__)


class GithubCliError(Exception):
    """Unified error for all gh CLI failures."""


class GithubCli:
    def __init__(self, repo: str):
        self.repo = repo

    def _run(self, args: list[str]) -> str:
        result = subprocess.run(
            ["gh", *args, "-R", self.repo],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise GithubCliError(
                f"gh command failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def list_issues(self, state: str = "open", label: str | None = None) -> list[dict]:
        args = [
            "issue",
            "list",
            "--state",
            state,
            "--json",
            "number,title,labels,body,assignees,state,milestone",
        ]
        if label:
            args.extend(["--label", label])
        output = self._run(args)
        return json.loads(output) if output else []

    def get_issue(self, number: int) -> dict:
        output = self._run(
            [
                "issue",
                "view",
                str(number),
                "--json",
                "number,title,body,labels,assignees,state,milestone",
            ]
        )
        return json.loads(output)

    def create_issue(
        self, title: str, body: str, label: list[str] | None = None
    ) -> int:
        args = ["issue", "create", "--title", title, "--body", body]
        if label:
            for lb in label:
                args.extend(["--label", lb])
        output = self._run(args)
        match = re.search(r"/issues/(\d+)", output)
        if not match:
            raise GithubCliError(f"Could not parse issue number from: {output}")
        return int(match.group(1))

    def close_issue(self, number: int) -> None:
        self._run(["issue", "close", str(number)])

    def add_label(self, number: int, label: str) -> None:
        self._run(["issue", "edit", str(number), "--add-label", label])

    def remove_label(self, number: int, label: str) -> None:
        self._run(["issue", "edit", str(number), "--remove-label", label])

    def comment_issue(self, number: int, body: str) -> None:
        self._run(["issue", "comment", str(number), "--body", body])

    def test_auth(self) -> bool:
        try:
            subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return True
        except Exception:
            return False
