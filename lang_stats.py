#!/usr/bin/env python3
"""Monthly language share of lines added across your own (non-fork) GitHub repos.

Usage:
    python3 lang_stats.py                      # last 365 days, chart to assets/languages-monthly.png
    python3 lang_stats.py --out /tmp/foo       # other folder for the PNG
    python3 lang_stats.py --since 2025-11-26   # fixed start date
    python3 lang_stats.py --no-fetch           # reuse cached clones, skip git fetch

Requires: gh (logged in), git, python3 with matplotlib.
Clones are cached in ~/.cache/lang-stats/clones and updated on every run.
Outputs: languages-monthly.png and languages-monthly-dark.png (transparent) in the output folder (default: assets/ next to this script, as the README
expects) and the intermediate languages_monthly.csv in ~/.cache/lang-stats.
"""
import argparse
import collections
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys

CACHE = os.path.expanduser("~/.cache/lang-stats")
CLONES = os.path.join(CACHE, "clones")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
GH_USER = None  # None = author from `gh api user`

EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".rs": "Rust", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".cu": "C++", ".cuh": "C++",
    ".lean": "Lean", ".svelte": "Svelte", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".go": "Go", ".sh": "Shell", ".bash": "Shell", ".tex": "TeX", ".java": "Java", ".rb": "Ruby",
    ".vue": "Vue", ".jinja": "Jinja", ".j2": "Jinja", ".sql": "SQL",
}
# Data / config / docs / binary: not counted as code.
DATA_EXT = {".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".csv", ".lock", ".svg", ".png", ".jpg",
            ".xml", ".ini", ".cfg", ".log", ".jsonl", ".patch", ".diff", ".rst"}
SKIP_NAMES = {"package-lock.json", "Cargo.lock", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock"}
# Vendored, generated, fixture and experiment-output paths.
BAD = re.compile(r"(^|/)(vendor|node_modules|dist|build|fixtures?|campaigns?|results?|data|outputs?|generated|"
                 r"third_party|\.venv|venv)(/|$)|\.min\.js$|-results?/")
FAMILY = {"Python": "Python", "Rust": "Rust", "JavaScript": "JS/TS", "TypeScript": "JS/TS"}
FAMILY_ORDER = ["Python", "Rust", "JS/TS", "Other"]
THEMES = {
    "light": {
        "bg": "#ffffff", "ink": "#191919", "ink2": "#666666", "grid": "#e7e7e7", "axis": "#c2c2c2",
        "colors": {"Python": "#2f5d8a", "Rust": "#c8643c", "JS/TS": "#7bb8a8", "Other": "#cfd2d8"},
        "label": {"Python": "white", "Rust": "white", "JS/TS": "#191919", "Other": "#191919"},
        "text": {"Python": "#2f5d8a", "Rust": "#b0532d", "JS/TS": "#3f8a78", "Other": "#6b6f78"},
    },
    # Transparent, so it blends into any dark GitHub theme (default, dimmed, high contrast).
    "dark": {
        "bg": "none", "ink": "#e6edf3", "ink2": "#9198a1", "grid": "#ffffff1a", "axis": "#ffffff33",
        "colors": {"Python": "#6a9fd8", "Rust": "#e08f66", "JS/TS": "#7bb8a8", "Other": "#3b414d"},
        "label": {"Python": "#0d1117", "Rust": "#0d1117", "JS/TS": "#0d1117", "Other": "#e6edf3"},
        "text": {"Python": "#6a9fd8", "Rust": "#e08f66", "JS/TS": "#7bb8a8", "Other": "#9198a1"},
    },
}


def run(cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def die(msg):
    print("error: " + msg, file=sys.stderr)
    sys.exit(1)


def author_login():
    if GH_USER:
        return GH_USER
    out = run(["gh", "api", "user", "-q", ".login"])
    if out.returncode != 0:
        die("gh api user failed: " + out.stderr.strip())
    return out.stdout.strip()


def list_repos(since):
    out = run(["gh", "repo", "list", "--fork=false", "--limit", "500",
               "--json", "nameWithOwner,pushedAt"])
    if out.returncode != 0:
        die("gh repo list failed: " + out.stderr.strip())
    repos = json.loads(out.stdout)
    # Keep repos pushed on or after the window start; older ones cannot have new commits in it.
    return [r["nameWithOwner"] for r in repos if (r.get("pushedAt") or "")[:10] >= since]


def sync_clone(full, fetch):
    name = full.split("/")[1]
    d = os.path.join(CLONES, name + ".git")
    if not os.path.isdir(d):
        out = run(["git", "clone", "--bare", "--quiet", f"https://github.com/{full}.git", d])
        if out.returncode != 0:
            return None, "clone failed: " + out.stderr.strip()[:200]
    elif fetch:
        out = run(["git", "-C", d, "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*"])
        if out.returncode != 0:
            return d, "fetch failed (using cached copy): " + out.stderr.strip()[:200]
    return d, ""


def collect(d, since, author):
    """Yield (sha, month, path, added) for each non-merge commit by the author."""
    out = run(["git", "-C", d, "log", "--all", "--no-merges", f"--since={since}",
               f"--author={author}", "--numstat", "--date=format:%Y-%m",
               "--format=C%x09%H%x09%ad"])
    if out.returncode != 0:
        return None
    seen, cur, cur_month = set(), None, None
    rows = []
    for line in out.stdout.split("\n"):
        if line.startswith("C\t"):
            _, sha, month = line.split("\t")
            if sha in seen:
                cur = None
                continue
            seen.add(sha)
            cur, cur_month = sha, month
            continue
        if cur is None or not line.strip():
            continue
        p = line.split("\t", 2)
        if len(p) != 3:
            continue
        a, _, path = p
        if a == "-":
            continue
        rows.append((cur, cur_month, path, int(a)))
    return rows


def norm_path(p):
    """Turn git rename notation ('old => new' or 'dir/{a => b}/x') into the new path."""
    if "=>" in p:
        if "{" in p:
            pre, rest = p.split("{", 1)
            mid, post = rest.split("}", 1)
            new = mid.split("=>")[1].strip()
            p = (pre + new + post).replace("//", "/")
        else:
            p = p.split("=>")[1].strip()
    return p


def classify(path):
    """Return a family name, or None to skip the file."""
    if BAD.search(path):
        return None
    base = os.path.basename(path)
    if base in SKIP_NAMES:
        return None
    ext = os.path.splitext(base)[1].lower()
    if ext in DATA_EXT or ext == "":
        return None
    lang = EXT.get(ext, "Other-code")
    return FAMILY.get(lang, "Other")


def aggregate(all_rows):
    monthly = collections.defaultdict(collections.Counter)
    for repo, rows in all_rows:
        for _sha, month, path, added in rows:
            fam = classify(norm_path(path))
            if fam is None:
                continue
            monthly[month][fam] += added
    return monthly


def write_csv(monthly, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month"] + FAMILY_ORDER + ["total_lines"])
        for m in sorted(monthly):
            c = monthly[m]
            total = sum(c.values())
            shares = [round(100 * c[k] / total, 1) if total else 0.0 for k in FAMILY_ORDER]
            w.writerow([m] + shares + [total])
    return path


def fmt_total(n):
    n = float(n)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k" if n < 1e4 else f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_label(ym, first):
    y, m = ym.split("-")
    name = MONTHS[int(m) - 1]
    return f"{name}\n{y}" if first or m == "01" else name


def spread(centers, min_gap, lo=0.0, hi=100.0):
    """Nudge label positions apart so direct labels on the last bar do not overlap."""
    order = sorted(range(len(centers)), key=lambda i: centers[i])
    pos = {i: centers[i] for i in order}
    for a, b in zip(order, order[1:]):
        if pos[b] - pos[a] < min_gap:
            pos[b] = pos[a] + min_gap
    over = pos[order[-1]] - hi
    if over > 0:
        for i in order:
            pos[i] -= over
    return [pos[i] for i in range(len(centers))]


def draw_chart(csv_path, png_path, since, theme="light"):
    T = THEMES[theme]
    INK, INK2 = T["ink"], T["ink2"]
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        die("matplotlib is not installed (pip install matplotlib)")
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("no data, chart skipped")
        return None
    n = len(rows)
    fig, ax = plt.subplots(figsize=(11, 6.4), facecolor=T["bg"])
    ax.set_facecolor(T["bg"])
    x = list(range(n))
    bottom = [0.0] * n
    last_centers = {}
    for key in FAMILY_ORDER:
        vals = [float(r[key]) for r in rows]
        gap = 0.5  # visual gap between segments, in percentage points (no outline, so it works on transparent)
        ax.bar(x, [max(v - gap, 0) for v in vals], bottom=[b + gap / 2 for b in bottom],
               color=T["colors"][key], width=0.62, linewidth=0, zorder=3)
        for i, v in enumerate(vals):
            if v >= 6:
                ax.text(i, bottom[i] + v / 2, f"{v:.0f}%", ha="center", va="center",
                        fontsize=9, color=T["label"][key], zorder=4)
        last_centers[key] = bottom[-1] + vals[-1] / 2
        bottom = [b + v for b, v in zip(bottom, vals)]

    # Total lines added, above each bar: gives the scale the percentages hide.
    for i, r in enumerate(rows):
        ax.text(i, 102, fmt_total(r["total_lines"]), ha="center", va="bottom",
                fontsize=8.5, color=INK2, zorder=4)

    # Direct labels next to the last bar instead of a legend.
    ys = spread([last_centers[k] for k in FAMILY_ORDER], min_gap=5.5)
    for key, y in zip(FAMILY_ORDER, ys):
        ax.text(n - 1 + 0.46, y, key, ha="left", va="center", fontsize=10,
                fontweight="bold", color=T["text"][key], zorder=4)

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(T["axis"])
    ax.yaxis.grid(True, color=T["grid"], linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(length=0, colors=INK2, labelsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels([month_label(r["month"], i == 0) for i, r in enumerate(rows)])
    ax.set_xlim(-0.6, n - 1 + 1.4)
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels([f"{t}%" for t in [0, 25, 50, 75, 100]])
    fig.text(0.02, 0.955, "Share of added lines by language, per month",
             fontsize=14, fontweight="bold", color=INK, ha="left")
    fig.text(0.02, 0.905, "Own non-fork repos. Number above each bar = total lines added that month", fontsize=10, color=INK2, ha="left")
    fig.text(0.02, 0.02, f"Data since {since}. Data, lock, log, vendor and generated files excluded.",
             fontsize=8, color=INK2)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.84, bottom=0.12)
    fig.savefig(png_path, dpi=200, facecolor=T["bg"], transparent=(T["bg"] == "none"))
    plt.close(fig)
    return png_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"folder for the PNG (default: {DEFAULT_OUT})")
    ap.add_argument("--since", help="window start YYYY-MM-DD (default: one year ago today)")
    ap.add_argument("--no-fetch", action="store_true", help="do not fetch existing clones")
    args = ap.parse_args()

    since = args.since or (dt.date.today() - dt.timedelta(days=365)).isoformat()
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(CLONES, exist_ok=True)

    author = author_login()
    repos = list_repos(since)
    print(f"author={author} since={since} repos={len(repos)} (non-fork, pushed since {since})")

    all_rows, problems = [], []
    for full in repos:
        d, err = sync_clone(full, fetch=not args.no_fetch)
        if err:
            problems.append(f"{full}: {err}")
        if d is None:
            continue
        rows = collect(d, since, author)
        if rows is None:
            problems.append(f"{full}: git log failed")
            continue
        all_rows.append((full, rows))

    monthly = aggregate(all_rows)
    csv_path = write_csv(monthly, os.path.join(CACHE, "languages_monthly.csv"))
    print(f"wrote {csv_path}")
    for theme, name in (("light", "languages-monthly.png"), ("dark", "languages-monthly-dark.png")):
        png_path = draw_chart(csv_path, os.path.join(args.out, name), since, theme)
        if png_path:
            print(f"wrote {png_path}")
    if problems:
        print("warnings:")
        for p in problems:
            print("  " + p)


if __name__ == "__main__":
    main()
