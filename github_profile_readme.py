#!/usr/bin/env python3
"""Fill the project and fork tables of a GitHub profile README from the GitHub API.

Only the text between the marker pairs is replaced:

    <!-- PROJECTS:START --> ... <!-- PROJECTS:END -->
    <!-- FORKS:START -->    ... <!-- FORKS:END -->

Collecting data and rendering are separate steps: with --data, raw API data is cached in a
JSON file and the README is rendered from it, so layout and config changes need no API calls.
With --only REPO [REPO ...], only the named repositories are re-fetched; the rest come from --data.
A dot before each project name shows code size in three shades of one colour from code bytes reported by /languages
(config "size": {"limits_kb": [...], "exclude_languages": [...]}); it is not a quality measure.
Everything else in the README is left untouched. Files are written only with --run.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

API_URL = "https://api.github.com"
PROJECTS_MARKER = "PROJECTS"
FORKS_MARKER = "FORKS"
CONTRIBUTIONS_MARKER = "CONTRIBUTIONS"  # optional section
MONTH_NAMES = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
ACTIVITY_COLORS = [(1, "brightgreen"), (3, "green"), (6, "yellow"), (12, "orange")]
SETTLED_COLOR = "blue"
DEFAULT_MIN_COMMITS = 3
DEFAULT_MIN_LINES = 100
SIZE_LEVELS = ["Small", "Medium", "Large"]  # dots in assets/size-*.svg, light to dark
DEFAULT_MAIN_LANGUAGES_SHARE = 0.8
DEFAULT_BYTES_PER_LINE = 35
DEFAULT_SIZE_LIMITS_KB = [300, 2048]
DEFAULT_SIZE_EXCLUDED = ["HTML", "Jupyter Notebook", "CSS", "SCSS", "TeX"]
NAME_CHAR_EM = 0.51
HEADER_CHAR_EM = 0.63
NBSP_EM = 0.28
REPO_FIELDS = ("name", "html_url", "description", "fork", "archived", "pushed_at")

JsonFetcher = Callable[[str], Any]
Sleeper = Callable[[float], None]


@dataclass
class Activity:
    month: str
    commits: int
    additions: int
    deletions: int


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


def fetch_or_none(fetch: JsonFetcher, path: str) -> Any:
    try:
        return fetch(path)
    except urllib.error.URLError:
        return None


def list_public_repos(fetch: JsonFetcher, user: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = fetch(f"/users/{user}/repos?type=owner&per_page=100&page={page}")
        repos.extend(batch)
        if len(batch) < 100:
            return repos
        page += 1


def fork_info(fetch: JsonFetcher, user: str, name: str) -> tuple[str | None, bool]:
    """Return the upstream and whether the fork has commits authored by the user.

    Being ahead is not enough: a fork can carry other people's commits from its network.
    """
    detail = fetch_or_none(fetch, f"/repos/{user}/{name}")
    if not detail or "parent" not in detail:
        return None, False
    parent = detail["parent"]
    comparison = fetch_or_none(
        fetch,
        f"/repos/{parent['full_name']}/compare/{parent['default_branch']}"
        f"...{user}:{detail['default_branch']}",
    )
    own = any(
        ((commit.get("author") or {}).get("login") or "").lower() == user.lower()
        for commit in (comparison or {}).get("commits", [])
    )
    return parent["full_name"], own


def monthly_stats(
    fetch: JsonFetcher, user: str, name: str, attempts: int = 6, delay: float = 3.0, sleep: Sleeper = time.sleep
) -> dict[str, list[int]] | None:
    """Return {"YYYY-MM": [commits, additions, deletions]}; GitHub answers 202 while computing."""
    for attempt in range(attempts):
        contributors = fetch_or_none(fetch, f"/repos/{user}/{name}/stats/contributors")
        if isinstance(contributors, list):
            months: dict[str, list[int]] = {}
            for contributor in contributors:
                for week in contributor.get("weeks", []):
                    if not (week["c"] or week["a"] or week["d"]):
                        continue
                    month = datetime.fromtimestamp(week["w"], timezone.utc).strftime("%Y-%m")
                    totals = months.setdefault(month, [0, 0, 0])
                    totals[0] += week["c"]
                    totals[1] += week["a"]
                    totals[2] += week["d"]
            return dict(sorted(months.items()))
        if attempt < attempts - 1:
            sleep(delay)
    return None


def collect(
    fetch: JsonFetcher,
    user: str,
    previous: dict[str, Any] | None = None,
    sleep: Sleeper = time.sleep,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch raw data for all public repositories, independent of config and layout.

    With `only`, the repository list is still fetched, but per-repository details are fetched
    only for the named repositories and for ones missing from `previous`; the rest are reused.
    """
    old = {repo["name"]: repo for repo in (previous or {}).get("repos", [])}
    repos = [{field: repo.get(field) for field in REPO_FIELDS} for repo in list_public_repos(fetch, user)]
    stale: list[dict[str, Any]] = []
    for repo in repos:
        if only is None or repo["name"] in only or repo["name"] not in old:
            stale.append(repo)
        else:
            repo.update({key: value for key, value in old[repo["name"]].items() if key not in REPO_FIELDS})
    for repo in stale:
        if not repo["fork"]:
            fetch_or_none(fetch, f"/repos/{user}/{repo['name']}/stats/contributors")
    for repo in stale:
        name = repo["name"]
        if repo["fork"]:
            repo["upstream"], repo["own_commits"] = fork_info(fetch, user, name)
            continue
        monthly = monthly_stats(fetch, user, name, sleep=sleep)
        repo["monthly"] = monthly if monthly is not None else old.get(name, {}).get("monthly")
        repo["languages"] = fetch_or_none(fetch, f"/repos/{user}/{name}/languages") or {}
    private = private_summary(fetch, user)
    if private is None:
        private = (previous or {}).get("private")
        print("Private repositories: token cannot list them, keeping cached totals.")
    else:
        print(f"Private repositories: refreshed ({private['count']}).")
    contributions = collect_contributions(fetch, user)
    if contributions is None:
        contributions = (previous or {}).get("contributions", [])
    return {
        "user": user,
        "repos": sorted(repos, key=lambda repo: repo["name"].lower()),
        "private": private,
        "contributions": contributions,
    }


CONTRIBUTION_QUERIES = {
    "authored": "author:{user} -user:{user}",
    "commented": "commenter:{user} -author:{user} -user:{user}",
}


def collect_contributions(fetch: JsonFetcher, user: str) -> list[dict[str, Any]] | None:
    """Issues and pull requests in other people's repositories that the user opened or commented on."""
    items: list[dict[str, Any]] = []
    for role, query in CONTRIBUTION_QUERIES.items():
        q = urllib.parse.quote(query.format(user=user))
        page = 1
        while True:
            result = fetch_or_none(fetch, f"/search/issues?q={q}&per_page=100&page={page}")
            if not isinstance(result, dict):
                return None
            batch = result.get("items", [])
            for item in batch:
                pull = item.get("pull_request")
                items.append(
                    {
                        "repo": item["repository_url"].split("/repos/", 1)[1],
                        "kind": "pr" if pull else "issue",
                        "role": role,
                        "state": "merged" if pull and pull.get("merged_at") else item["state"],
                        "created": item["created_at"][:10],
                        "title": item["title"],
                        "url": item["html_url"],
                    }
                )
            if len(batch) < 100 or page * 100 >= result.get("total_count", 0):
                break
            page += 1
    return sorted(items, key=lambda item: item["created"], reverse=True)


def private_summary(fetch: JsonFetcher, user: str) -> dict[str, Any] | None:
    """Count the user's own private non-fork repositories and sum their language bytes.

    Only totals are kept, never names. Returns None when the token cannot list the user's
    private repositories (for example the Actions GITHUB_TOKEN), so the caller can keep old data.
    """
    me = fetch_or_none(fetch, "/user")
    if not isinstance(me, dict) or (me.get("login") or "").lower() != user.lower():
        return None
    names: list[str] = []
    page = 1
    while True:
        batch = fetch_or_none(fetch, f"/user/repos?visibility=private&affiliation=owner&per_page=100&page={page}")
        if not isinstance(batch, list):
            return None
        names.extend(repo["full_name"] for repo in batch if not repo.get("fork"))
        if len(batch) < 100:
            break
        page += 1
    languages: dict[str, int] = {}
    for full_name in names:
        for language, size in (fetch_or_none(fetch, f"/repos/{full_name}/languages") or {}).items():
            languages[language] = languages.get(language, 0) + size
    return {"count": len(names), "languages": dict(sorted(languages.items(), key=lambda item: -item[1]))}


def last_active_month(monthly: dict[str, list[int]] | None, config: dict[str, Any]) -> Activity | None:
    """Newest month with enough commits and changed lines to count as real work."""
    if not monthly:
        return None
    settings = config.get("activity", {})
    min_commits = settings.get("min_commits", DEFAULT_MIN_COMMITS)
    min_lines = settings.get("min_lines", DEFAULT_MIN_LINES)
    for month in sorted(monthly, reverse=True):
        commits, additions, deletions = monthly[month]
        if commits >= min_commits and additions + deletions >= min_lines:
            return Activity(month, commits, additions, deletions)
    month = max(monthly, key=lambda key: (monthly[key][1] + monthly[key][2], monthly[key][0], key))
    return Activity(month, *monthly[month])


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
    details = f"{activity.commits} commits, +{activity.additions}/-{activity.deletions} lines"
    url = f"https://img.shields.io/badge/{label.replace(' ', '_')}-{color}"
    return f'![{label}]({url} "Last active month: {details}")'


def size_badge(languages: dict[str, int] | None, config: dict[str, Any]) -> str:
    """Three shades of one hue: lighter means a smaller codebase, not a better or worse one."""
    settings = config.get("size", {})
    excluded = set(settings.get("exclude_languages", DEFAULT_SIZE_EXCLUDED))
    limits = settings.get("limits_kb", DEFAULT_SIZE_LIMITS_KB)
    code = {language: size for language, size in (languages or {}).items() if language not in excluded}
    total = sum(code.values())
    index = next((index for index, limit in enumerate(limits) if total / 1024 < limit), len(limits))
    label = SIZE_LEVELS[index]
    return (
        f'<img src="assets/size-{label.lower()}.svg" width="10" height="10" alt="{label}" '
        f'title="{size_tooltip(code, settings)}">'
    )


def size_tooltip(code: dict[str, int], settings: dict[str, Any]) -> str:
    """Leading languages covering most of the code, plus a line estimate from byte counts."""
    total = sum(code.values())
    if not total:
        return "No code detected"
    share = settings.get("main_languages_share", DEFAULT_MAIN_LANGUAGES_SHARE)
    leading: list[str] = []
    covered = 0
    for language, size in sorted(code.items(), key=lambda item: -item[1])[:3]:
        leading.append(language)
        covered += size
        if covered >= share * total:
            break
    return f"{' + '.join(leading)} · {estimate_lines(total, settings)} lines"


def estimate_lines(code_bytes: int, settings: dict[str, Any]) -> str:
    lines = code_bytes / settings.get("bytes_per_line", DEFAULT_BYTES_PER_LINE)
    return f"~{lines / 1000:.0f}k" if lines >= 1000 else f"~{max(10, round(lines, -1)):.0f}"


def private_note(private: dict[str, Any] | None, config: dict[str, Any]) -> str:
    """One line under the projects table: how many private repositories and their code size, no names."""
    if not private or not private.get("count"):
        return ""
    settings = config.get("size", {})
    excluded = set(settings.get("exclude_languages", DEFAULT_SIZE_EXCLUDED))
    code_bytes = sum(size for language, size in private.get("languages", {}).items() if language not in excluded)
    count = private["count"]
    noun = "repository" if count == 1 else "repositories"
    return f"\n\n<sub>Plus {count} private {noun}, {estimate_lines(code_bytes, settings)} lines of code.</sub>"


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


def name_header(names: list[str], config: dict[str, Any], title: str = "Repository") -> str:
    """Pad the header with non-breaking spaces so name columns of all tables share one width."""
    char_em = config.get("layout", {}).get("name_char_em", NAME_CHAR_EM)
    longest = max((len(name) for name in names), default=0)
    padding = max(0, math.ceil((longest * char_em - len(title) * HEADER_CHAR_EM) / NBSP_EM))
    return "&nbsp;" * (padding // 2) + title + "&nbsp;" * (padding - padding // 2)


def description_for(repo: dict[str, Any], config: dict[str, Any]) -> str:
    override = config.get("descriptions", {}).get(repo["name"])
    return escape_cell(override or repo.get("description") or "")


def sort_repos(
    repos: list[dict[str, Any]], config: dict[str, Any], activities: dict[str, Activity | None]
) -> list[dict[str, Any]]:
    """Order by the configured key; archived repositories always go last."""
    order = config.get("sort", "activity")
    if order == "name":
        ordered = sorted(repos, key=lambda repo: repo["name"].lower())
    elif order == "activity":
        def key(repo: dict[str, Any]) -> tuple[str, str]:
            activity = activities.get(repo["name"])
            return (activity.month if activity else "", repo.get("pushed_at") or "")

        ordered = sorted(repos, key=key, reverse=True)
    else:
        ordered = sorted(repos, key=lambda repo: repo.get("pushed_at") or "", reverse=True)
    return sorted(ordered, key=lambda repo: bool(repo.get("archived")))


def archived_badge(activity: Activity | None) -> str:
    tooltip = "No longer developed"
    if activity is not None:
        year, number = (int(part) for part in activity.month.split("-"))
        tooltip += f"; last active {MONTH_NAMES[number - 1]} {year}"
    return f'![Archived](https://img.shields.io/badge/Archived-lightgrey "{tooltip}")'


def render(data: dict[str, Any], config: dict[str, Any], today: date | None = None) -> dict[str, str]:
    today = today or date.today()
    user = data["user"]
    hidden = set(config.get("hide", []))
    forced_forks = set(config.get("forks", []))
    external_forks: dict[str, str] = config.get("external_forks", {})
    repos = [
        repo
        for repo in data["repos"]
        if repo["name"] not in hidden and repo["name"].lower() != user.lower()
    ]

    own = [repo for repo in repos if not repo["fork"] and repo["name"] not in external_forks]
    activities = {repo["name"]: last_active_month(repo.get("monthly"), config) for repo in own}

    shown_forks: list[tuple[dict[str, Any], str]] = []
    for repo in sort_repos([repo for repo in repos if repo not in own], {"sort": "pushed"}, {}):
        if repo["name"] in external_forks:
            url = external_forks[repo["name"]].rstrip("/").removesuffix(".git")
            shown_forks.append((repo, f"[{nowrap('/'.join(url.split('/')[-2:]))}]({url})"))
        elif repo["name"] in forced_forks or repo.get("own_commits"):
            upstream = repo.get("upstream")
            cell = f"[{nowrap(upstream)}](https://github.com/{upstream})" if upstream else ""
            shown_forks.append((repo, cell))

    # The size dot before own project names takes about two characters of width.
    names = [repo["name"] + "xx" for repo in own] + [repo["name"] for repo, _ in shown_forks]
    header = name_header(names, config)
    own_rows = [
        [
            size_badge(repo.get("languages"), config) + "&nbsp;" + repo_link(repo),
            description_for(repo, config),
            archived_badge(activities[repo["name"]])
            if repo.get("archived")
            else activity_badge(activities[repo["name"]], today),
        ]
        for repo in sort_repos(own, config, activities)
    ]
    fork_rows = [[repo_link(repo), description_for(repo, config), cell] for repo, cell in shown_forks]
    return {
        PROJECTS_MARKER: render_table([header, "Description", "Activity"], own_rows)
        + private_note(data.get("private"), config),
        FORKS_MARKER: render_table([header, "My modifications", "Upstream"], fork_rows),
        CONTRIBUTIONS_MARKER: render_contributions(data.get("contributions", []), config),
    }


PR_STATE_COLORS = {"merged": "8250df", "open": "1f883d", "closed": "lightgrey"}


def render_contributions(items: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """Pull requests as a table, then one row per project with years and kinds of involvement."""
    hidden = set(config.get("hide_contributions", []))
    items = [item for item in items if item["repo"] not in hidden]
    parts: list[str] = []

    pulls = [item for item in items if item["kind"] == "pr" and item["role"] == "authored"]
    if pulls:
        rows = []
        newest_first = sorted(pulls, key=lambda item: item["created"], reverse=True)
        for item in sorted(newest_first, key=lambda item: item["state"] != "merged"):
            state = item["state"]
            badge = f"![{state}](https://img.shields.io/badge/{state}-{PR_STATE_COLORS.get(state, 'lightgrey')})"
            repo = f"[{nowrap(item['repo'])}](https://github.com/{item['repo']})"
            rows.append([repo, f"[{escape_cell(item['title'])}]({item['url']})", badge])
        parts.append(render_table(["Project", "Pull request", "Status"], rows))

    projects: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        projects.setdefault(item["repo"], []).append(item)
    if projects:
        def summary(entries: list[dict[str, Any]]) -> str:
            counts = {
                "PR": sum(e["kind"] == "pr" and e["role"] == "authored" for e in entries),
                "issue": sum(e["kind"] == "issue" and e["role"] == "authored" for e in entries),
                "discussion": sum(e["role"] == "commented" for e in entries),
            }
            return ", ".join(f"{count} {noun}{'s' if count > 1 else ''}" for noun, count in counts.items() if count)

        def years(entries: list[dict[str, Any]]) -> str:
            first = min(e["created"][:4] for e in entries)
            last = max(e["created"][:4] for e in entries)
            return first if first == last else f"{first}–{last}"

        ordered = sorted(projects.items(), key=lambda pair: max(e["created"] for e in pair[1]), reverse=True)
        rows = [
            [f"[{nowrap(repo)}](https://github.com/{repo})", years(entries), summary(entries)]
            for repo, entries in ordered
        ]
        parts.append(
            "Opened pull requests or issues, or joined a discussion:\n\n"
            + render_table(["Project", "Years", "Involvement"], rows)
        )
    return "\n\n".join(parts)


def has_markers(text: str, name: str) -> bool:
    return f"<!-- {name}:START -->" in text and f"<!-- {name}:END -->" in text


def replace_between_markers(text: str, name: str, content: str) -> str:
    start = f"<!-- {name}:START -->"
    end = f"<!-- {name}:END -->"
    start_index = text.find(start)
    end_index = text.find(end)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        raise ValueError(f"README is missing the {start} / {end} markers")
    return text[: start_index + len(start)] + "\n" + content + "\n" + text[end_index:]


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
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
    parser.add_argument("--config", type=Path, help="JSON file with hide, forks, descriptions and layout settings")
    parser.add_argument("--data", type=Path, help="JSON cache of raw API data; render from it without API calls")
    parser.add_argument("--refresh", action="store_true", help="fetch from the API even if --data exists")
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="REPO",
        help="refresh details of these repositories only and reuse --data for the rest (implies --refresh)",
    )
    parser.add_argument("--run", action="store_true", help="write the README and data (default: dry run)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.run:
        print("DRY RUN: no files will be modified. Use --run to apply changes.")

    try:
        text = args.readme.read_text(encoding="utf-8")
        config = load_json(args.config)
        cached = load_json(args.data)
        if cached and not args.refresh and not args.only:
            data = cached
            print(f"Rendering from {args.data}; use --refresh or --only to fetch from the API.")
        else:
            if not args.user:
                print("Error: --user is required when GITHUB_REPOSITORY_OWNER is not set", file=sys.stderr)
                return 2
            only = set(args.only) if args.only else None
            data = collect(make_fetcher(os.environ.get("GITHUB_TOKEN")), args.user, cached, only=only)
            missing = sorted((only or set()) - {repo["name"] for repo in data["repos"]})
            if missing:
                print(f"Warning: not public repositories of {args.user}: {', '.join(missing)}", file=sys.stderr)
        sections = render(data, config)
        updated = text
        for name, content in sections.items():
            if name == CONTRIBUTIONS_MARKER and not has_markers(updated, name):
                continue
            updated = replace_between_markers(updated, name, content)
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    new_data = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    if args.data and data is not cached and args.run:
        args.data.write_text(new_data, encoding="utf-8")
        print(f"Saved {args.data}")
    elif args.data and data is not cached:
        print(f"DRY RUN: would save {args.data}")

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
