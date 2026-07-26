#!/usr/bin/env python3
"""Generate repository-local profile statistics cards.

The commit count is intentionally different from GitHub's contribution graph:
it scans every branch of every repository owned by the authenticated user,
follows API pagination, and counts each commit SHA once per repository.
"""

from __future__ import annotations

import html
import json
import os
import ssl
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()


API_ROOT = "https://api.github.com/"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
USERNAME = os.environ.get("GITHUB_USERNAME", "")
OUTPUT_STATS = "profile/stats.svg"
OUTPUT_LANGS = "profile/top-langs.svg"

LANGUAGE_COLORS = {
    "C": "#555555",
    "C++": "#f34b7d",
    "CMake": "#064f8c",
    "Cuda": "#3a4e3a",
    "CSS": "#563d7c",
    "Fortran": "#4d41b1",
    "HTML": "#e34c26",
    "Java": "#b07219",
    "JavaScript": "#f1e05a",
    "Python": "#3572A5",
    "Prolog": "#74283c",
    "Rust": "#dea584",
    "Shell": "#89e051",
    "TypeScript": "#3178c6",
}
HIDDEN_LANGUAGES = {"CMake", "Makefile"}


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GitHubAPI:
    def __init__(self, token: str):
        if not token:
            raise GitHubError(0, "GITHUB_TOKEN is required")
        self.token = token

    def request(self, url: str):
        if not url.startswith("http"):
            url = urljoin(API_ROOT, url.lstrip("/"))

        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "LearningByDoingNow-profile-stats",
            },
        )
        for attempt in range(4):
            try:
                with urlopen(request, context=SSL_CONTEXT, timeout=30) as response:
                    return json.load(response), response.headers
            except HTTPError as error:
                remaining = error.headers.get("X-RateLimit-Remaining")
                if error.code in {202, 429, 500, 502, 503, 504} or (
                    error.code == 403 and remaining == "0"
                ):
                    if attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                try:
                    detail = json.loads(error.read().decode("utf-8"))
                    message = detail.get("message", str(error))
                except (ValueError, UnicodeDecodeError):
                    message = str(error)
                raise GitHubError(error.code, message) from error
            except (URLError, RemoteDisconnected, TimeoutError) as error:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(0, str(error)) from error
        raise GitHubError(0, f"Request failed: {url}")

    @staticmethod
    def next_link(headers):
        link_header = headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                return part[part.find("<") + 1 : part.find(">")]
        return None

    def get(self, path: str, params: dict[str, str | int] | None = None):
        query = urlencode(params or {})
        url = f"{API_ROOT}{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        return self.request(url)[0]

    def get_all(self, path: str, params: dict[str, str | int] | None = None):
        query = urlencode(params or {})
        url = f"{API_ROOT}{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"

        items = []
        while url:
            data, headers = self.request(url)
            if not isinstance(data, list):
                raise GitHubError(0, f"Expected a list from {url}")
            items.extend(data)
            url = self.next_link(headers)
        return items


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def write_svg(path: str, body: str, width: int, height: int, title: str, desc: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">{esc(title)}</title>
  <desc id="desc">{esc(desc)}</desc>
  <style>
    .title {{ font: 700 22px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; fill: #2f80ed; }}
    .label {{ font: 600 14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; fill: #434d58; }}
    .value {{ font: 700 14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; fill: #434d58; }}
    .language {{ font: 400 13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; fill: #434d58; }}
  </style>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="8" fill="#fffefe" stroke="#e4e2e2" />
  {body}
</svg>
'''
    with open(path, "w", encoding="utf-8") as output:
        output.write(svg)


def stats_svg(stats: dict[str, int]):
    rows = [
        ("Unique commits (all branches)", stats["commits"]),
        ("Repositories scanned", stats["repositories"]),
        ("Branches scanned", stats["branches"]),
        ("Total stars", stats["stars"]),
        ("Followers", stats["followers"]),
    ]
    body = [
        '<text x="25" y="36" class="title">GitHub Repository Stats</text>',
    ]
    for index, (label, value) in enumerate(rows):
        y = 65 + index * 27
        body.append(f'<text x="30" y="{y}" class="label">{esc(label)}:</text>')
        body.append(f'<text x="370" y="{y}" class="value">{esc(value)}</text>')
    return "".join(body)


def languages_svg(languages: Counter[str]):
    visible = [
        (name, size)
        for name, size in languages.most_common()
        if name not in HIDDEN_LANGUAGES
    ][:8]
    total = sum(languages.values()) or 1
    rows = []
    for index, (name, size) in enumerate(visible):
        column = index // 4
        row = index % 4
        x = 30 + column * 245
        y = 105 + row * 25
        percentage = size / total * 100
        color = LANGUAGE_COLORS.get(name, "#858585")
        rows.append(
            f'<circle cx="{x}" cy="{y - 5}" r="7" fill="{color}" />'
            f'<text x="{x + 16}" y="{y}" class="language">{esc(name)} {percentage:.2f}%</text>'
        )

    bar_x, bar_y, bar_width, bar_height = 30, 67, 440, 10
    segments = []
    offset = bar_x
    for name, size in visible:
        segment_width = bar_width * size / total
        segments.append(
            f'<rect x="{offset:.2f}" y="{bar_y}" width="{segment_width:.2f}" height="{bar_height}" fill="{LANGUAGE_COLORS.get(name, "#858585")}" />'
        )
        offset += segment_width

    body = [
        '<text x="25" y="36" class="title">Most Used Languages</text>',
        f'<clipPath id="language-bar"><rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" rx="5" /></clipPath>',
        f'<g clip-path="url(#language-bar)">{"".join(segments)}</g>',
        "".join(rows),
    ]
    return "".join(body)


def collect_data(api: GitHubAPI):
    user = api.get("/user")
    if user.get("login", "").casefold() != USERNAME.casefold():
        raise GitHubError(0, "GITHUB_USERNAME does not match the authenticated user")

    repositories = api.get_all(
        "/user/repos",
        {"visibility": "all", "affiliation": "owner", "per_page": 100},
    )
    repositories = [
        repo
        for repo in repositories
        if repo.get("owner", {}).get("login", "").casefold() == USERNAME.casefold()
    ]

    language_sizes = Counter()
    branch_tasks = []

    for repository in repositories:
        full_name = repository["full_name"]
        if not repository.get("fork", False):
            language_sizes.update(api.get(f"/repos/{full_name}/languages"))
        branches = api.get_all(
            f"/repos/{full_name}/branches", {"per_page": 100}
        )
        branch_tasks.extend((full_name, branch["name"]) for branch in branches)

    def scan_branch(task):
        full_name, branch_name = task
        try:
            commits = api.get_all(
                f"/repos/{full_name}/commits",
                {"sha": branch_name, "author": USERNAME, "per_page": 100},
            )
        except GitHubError as error:
            if error.status == 409:
                return set()
            raise
        return {(full_name, commit["sha"]) for commit in commits}

    commit_keys = set()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(scan_branch, task) for task in branch_tasks]
        for future in as_completed(futures):
            commit_keys.update(future.result())

    stats = {
        "commits": len(commit_keys),
        "repositories": len(repositories),
        "branches": len(branch_tasks),
        "stars": sum(repo.get("stargazers_count", 0) for repo in repositories),
        "followers": user.get("followers", 0),
    }
    return stats, language_sizes


def main():
    api = GitHubAPI(TOKEN)
    stats, languages = collect_data(api)
    write_svg(
        OUTPUT_STATS,
        stats_svg(stats),
        500,
        205,
        "GitHub Repository Stats",
        "Unique commits per repository across all branches of all owned repositories.",
    )
    write_svg(
        OUTPUT_LANGS,
        languages_svg(languages),
        500,
        205,
        "Most Used Languages",
        "Language distribution across owned non-fork repositories, excluding CMake and Makefile.",
    )
    print(
        f"Generated cards: {stats['commits']} unique commits, "
        f"{stats['repositories']} repositories, {stats['branches']} branches."
    )


if __name__ == "__main__":
    main()
