#!/usr/bin/env python3
"""Fill the project and fork tables of a GitHub profile README from the GitHub API.

Only the text between the marker pairs is replaced:

    <!-- PROJECTS:START --> ... <!-- PROJECTS:END -->
    <!-- FORKS:START -->    ... <!-- FORKS:END -->

Everything else in the README is left untouched. Changes are written only with --run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

API_URL = "https://api.github.com"
PROJECTS_MARKER = "PROJECTS"
FORKS_MARKER = "FORKS"

JsonFetcher = Callable[[str], Any]


def make_fetcher(token: str | None) -> JsonFetcher:
    def fetch(path: str) -> Any:
        request = urllib.request.Request(
            API_URL + path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "github-profile-readme",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    return fetch


def list_public_repos(fetch: JsonFetcher, user: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = fetch(f"/users/{user}/repos?type=owner&per_page=100&page={page}")
        repos.extend(batch)
        if len(batch) < 100:
            return repos
        page += 1


def has_own_commits(fetch: JsonFetcher, user: str, repo: dict[str, Any]) -> bool:
    try:
        detail = fetch(f"/repos/{user}/{repo['name']}")
        parent = detail["parent"]
        base = f"{parent['full_name']}/compare/{parent['default_branch']}"
        head = f"{user}:{detail['default_branch']}"
        return fetch(f"/repos/{base}...{head}").get("ahead_by", 0) > 0
    except (urllib.error.URLError, KeyError):
        return False


def upstream_of(fetch: JsonFetcher, user: str, repo: dict[str, Any]) -> str:
    try:
        return fetch(f"/repos/{user}/{repo['name']}")["parent"]["full_name"]
    except (urllib.error.URLError, KeyError):
        return ""


def escape_cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(" --- " for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def repo_link(repo: dict[str, Any]) -> str:
    return f"[{repo['name']}]({repo['html_url']})"


def description_for(repo: dict[str, Any], config: dict[str, Any]) -> str:
    override = config.get("descriptions", {}).get(repo["name"])
    return escape_cell(override or repo.get("description") or "")


def sort_repos(repos: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if config.get("sort", "pushed") == "name":
        return sorted(repos, key=lambda repo: repo["name"].lower())
    return sorted(repos, key=lambda repo: repo.get("pushed_at") or "", reverse=True)


def build_sections(
    fetch: JsonFetcher, user: str, config: dict[str, Any]
) -> dict[str, str]:
    hidden = set(config.get("hide", []))
    forced_forks = set(config.get("forks", []))
    own_rows: list[list[str]] = []
    fork_rows: list[list[str]] = []

    repos = [
        repo
        for repo in list_public_repos(fetch, user)
        if repo["name"] not in hidden
        and not repo.get("archived")
        and repo["name"].lower() != user.lower()
    ]
    for repo in sort_repos(repos, config):
        if not repo.get("fork"):
            own_rows.append(
                [repo_link(repo), description_for(repo, config), (repo.get("pushed_at") or "")[:10]]
            )
        elif repo["name"] in forced_forks or has_own_commits(fetch, user, repo):
            upstream = upstream_of(fetch, user, repo)
            upstream_cell = f"[{upstream}](https://github.com/{upstream})" if upstream else ""
            fork_rows.append([repo_link(repo), description_for(repo, config), upstream_cell])

    return {
        PROJECTS_MARKER: render_table(["Repository", "Description", "Updated"], own_rows),
        FORKS_MARKER: render_table(["Repository", "My modifications", "Upstream"], fork_rows),
    }


def replace_between_markers(text: str, name: str, content: str) -> str:
    start = f"<!-- {name}:START -->"
    end = f"<!-- {name}:END -->"
    start_index = text.find(start)
    end_index = text.find(end)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        raise ValueError(f"README is missing the {start} / {end} markers")
    return text[: start_index + len(start)] + "\n" + content + "\n" + text[end_index:]


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("readme", type=Path, help="profile README.md containing the markers")
    parser.add_argument(
        "--user",
        default=os.environ.get("GITHUB_REPOSITORY_OWNER"),
        help="GitHub login (default: $GITHUB_REPOSITORY_OWNER)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help='JSON file: {"hide": [], "forks": [], "descriptions": {}, "sort": "pushed|name"}',
    )
    parser.add_argument("--run", action="store_true", help="write the README (default: dry run)")
    args = parser.parse_args(argv)
    if not args.user:
        parser.error("--user is required when GITHUB_REPOSITORY_OWNER is not set")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.run:
        print("DRY RUN: README will not be modified. Use --run to apply changes.")

    try:
        text = args.readme.read_text(encoding="utf-8")
        config = load_config(args.config)
        fetch = make_fetcher(os.environ.get("GITHUB_TOKEN"))
        sections = build_sections(fetch, args.user, config)
        updated = text
        for name, content in sections.items():
            updated = replace_between_markers(updated, name, content)
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if updated == text:
        print("README is already up to date.")
    elif args.run:
        args.readme.write_text(updated, encoding="utf-8")
        print(f"Updated {args.readme}")
    else:
        for name, content in sections.items():
            print(f"\n[{name}]\n{content}")
        print("\nDRY RUN: README would change. Use --run to apply changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
