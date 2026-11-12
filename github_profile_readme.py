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
import math
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

API_URL = "https://api.github.com"
PROJECTS_MARKER = "PROJECTS"
FORKS_MARKER = "FORKS"
MONTH_NAMES = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
ACTIVITY_COLORS = [(1, "brightgreen"), (3, "green"), (6, "yellow"), (12, "orange")]
SETTLED_COLOR = "blue"
DEFAULT_MIN_COMMITS = 3
DEFAULT_MIN_LINES = 100

JsonFetcher = Callable[[str], Any]


@dataclass
class Activity:
    month: str
    commits: int
    additions: int | None
    deletions: int | None


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
            if response.status in (202, 204):
                return None
            body = response.read()
        return json.loads(body) if body else None

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
    """Being ahead is not enough: a fork can carry other people's commits from its network."""
    try:
        detail = fetch(f"/repos/{user}/{repo['name']}")
        parent = detail["parent"]
        base = f"{parent['full_name']}/compare/{parent['default_branch']}"
        head = f"{user}:{detail['default_branch']}"
        comparison = fetch(f"/repos/{base}...{head}")
    except (urllib.error.URLError, KeyError):
        return False
    return any(
        ((commit.get("author") or {}).get("login") or "").lower() == user.lower()
        for commit in comparison.get("commits", [])
    )


def upstream_of(fetch: JsonFetcher, user: str, repo: dict[str, Any]) -> str:
    try:
        return fetch(f"/repos/{user}/{repo['name']}")["parent"]["full_name"]
    except (urllib.error.URLError, KeyError):
        return ""


def monthly_lines(
    fetch: JsonFetcher,
    user: str,
    name: str,
    attempts: int = 6,
    delay: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, tuple[int, int]] | None:
    """Return {"YYYY-MM": (additions, deletions)}; GitHub answers 202 while it computes stats."""
    for attempt in range(attempts):
        try:
            weeks = fetch(f"/repos/{user}/{name}/stats/code_frequency")
        except urllib.error.URLError:
            return None
        if isinstance(weeks, list):
            months: dict[str, tuple[int, int]] = {}
            for timestamp, additions, deletions in weeks:
                month = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")
                added, deleted = months.get(month, (0, 0))
                months[month] = (added + additions, deleted - deletions)
            return months
        if attempt < attempts - 1:
            sleep(delay)
    return None


def last_active_month(
    fetch: JsonFetcher,
    user: str,
    name: str,
    config: dict[str, Any],
    sleep: Callable[[float], None] = time.sleep,
) -> Activity | None:
    """Find the newest month with enough commits and changed lines to count as real work."""
    settings = config.get("activity", {})
    min_commits = settings.get("min_commits", DEFAULT_MIN_COMMITS)
    min_lines = settings.get("min_lines", DEFAULT_MIN_LINES)
    lines = monthly_lines(fetch, user, name, sleep=sleep)

    def activity(month: str) -> Activity:
        added, deleted = lines.get(month, (0, 0)) if lines is not None else (None, None)
        return Activity(month, commits[month], added, deleted)

    def qualifies(month: str) -> bool:
        if commits[month] < min_commits:
            return False
        return lines is None or sum(lines.get(month, (0, 0))) >= min_lines

    commits: dict[str, int] = {}
    page = 1
    while True:
        try:
            batch = fetch(f"/repos/{user}/{name}/commits?per_page=100&page={page}") or []
        except urllib.error.URLError:
            batch = []
        for commit in batch:
            month = commit["commit"]["committer"]["date"][:7]
            commits[month] = commits.get(month, 0) + 1
        finished = len(batch) < 100
        oldest_seen = min(commits) if commits else ""
        for month in sorted(commits, reverse=True):
            if not finished and month <= oldest_seen:
                break
            if qualifies(month):
                return activity(month)
        if finished:
            break
        page += 1

    if not commits:
        return None
    if lines is not None:
        busiest = max(commits, key=lambda month: (sum(lines.get(month, (0, 0))), month))
    else:
        busiest = max(commits, key=lambda month: (commits[month], month))
    return activity(busiest)


def months_between(month: str, today: date) -> int:
    year, number = (int(part) for part in month.split("-"))
    return (today.year * 12 + today.month) - (year * 12 + number)


def activity_badge(activity: Activity | None, today: date) -> str:
    if activity is None:
        return ""
    year, number = (int(part) for part in activity.month.split("-"))
    label = MONTH_NAMES[number - 1] + ("" if year == today.year else f" {year}")
    age = months_between(activity.month, today)
    color = next((color for limit, color in ACTIVITY_COLORS if age <= limit), SETTLED_COLOR)
    details = f"{activity.commits} commits"
    if activity.additions is not None:
        details += f", +{activity.additions}/-{activity.deletions} lines"
    url = f"https://img.shields.io/badge/{label.replace(' ', '_')}-{color}"
    return f'![{label}]({url} "Last active month: {details}")'


def escape_cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(" --- " for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def nowrap(text: str) -> str:
    return text.replace("-", "‑").replace("/", "/⁠")


def repo_link(repo: dict[str, Any]) -> str:
    return f"[{nowrap(repo['name'])}]({repo['html_url']})"


def name_header(names: list[str], title: str = "Repository") -> str:
    """Pad the header with non-breaking spaces so name columns of all tables share one width."""
    longest = max((len(name) for name in names), default=0)
    padding = max(0, math.ceil((longest * 0.6 - len(title) * 0.63) / 0.28))
    left = "&nbsp;" * (padding // 2)
    return left + title + "&nbsp;" * (padding - padding // 2)


def description_for(repo: dict[str, Any], config: dict[str, Any]) -> str:
    override = config.get("descriptions", {}).get(repo["name"])
    return escape_cell(override or repo.get("description") or "")


def sort_repos(
    repos: list[dict[str, Any]],
    config: dict[str, Any],
    activities: dict[str, Activity | None],
) -> list[dict[str, Any]]:
    order = config.get("sort", "activity")
    if order == "name":
        return sorted(repos, key=lambda repo: repo["name"].lower())
    if order == "activity":
        def key(repo: dict[str, Any]) -> tuple[str, str]:
            activity = activities.get(repo["name"])
            return (activity.month if activity else "", repo.get("pushed_at") or "")

        return sorted(repos, key=key, reverse=True)
    return sorted(repos, key=lambda repo: repo.get("pushed_at") or "", reverse=True)


def build_sections(
    fetch: JsonFetcher,
    user: str,
    config: dict[str, Any],
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    today = today or date.today()
    hidden = set(config.get("hide", []))
    forced_forks = set(config.get("forks", []))
    repos = [
        repo
        for repo in list_public_repos(fetch, user)
        if repo["name"] not in hidden
        and not repo.get("archived")
        and repo["name"].lower() != user.lower()
    ]
    external_forks: dict[str, str] = config.get("external_forks", {})
    own = [repo for repo in repos if not repo.get("fork") and repo["name"] not in external_forks]
    forks = [repo for repo in repos if repo.get("fork") or repo["name"] in external_forks]

    for repo in own:
        try:
            fetch(f"/repos/{user}/{repo['name']}/stats/code_frequency")
        except urllib.error.URLError:
            pass
    activities = {
        repo["name"]: last_active_month(fetch, user, repo["name"], config, sleep) for repo in own
    }

    shown_forks: list[tuple[dict[str, Any], str]] = []
    for repo in sort_repos(forks, {"sort": "pushed"}, {}):
        if repo["name"] in external_forks:
            url = external_forks[repo["name"]].rstrip("/").removesuffix(".git")
            label = "/".join(url.split("/")[-2:])
            shown_forks.append((repo, f"[{nowrap(label)}]({url})"))
        elif repo["name"] in forced_forks or has_own_commits(fetch, user, repo):
            upstream = upstream_of(fetch, user, repo)
            cell = f"[{nowrap(upstream)}](https://github.com/{upstream})" if upstream else ""
            shown_forks.append((repo, cell))

    header = name_header([repo["name"] for repo in own] + [repo["name"] for repo, _ in shown_forks])
    own_rows = [
        [repo_link(repo), description_for(repo, config), activity_badge(activities[repo["name"]], today)]
        for repo in sort_repos(own, config, activities)
    ]
    fork_rows = [[repo_link(repo), description_for(repo, config), cell] for repo, cell in shown_forks]
    return {
        PROJECTS_MARKER: render_table([header, "Description", "Activity"], own_rows),
        FORKS_MARKER: render_table([header, "My modifications", "Upstream"], fork_rows),
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
        help="JSON file with hide, forks, descriptions, sort and activity settings",
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
