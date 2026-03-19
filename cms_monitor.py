#!/usr/bin/env python3
"""
CMS Innovation Model Page Monitor
Tracks changes to CMS innovation model pages and generates an HTML dashboard.
"""

import hashlib
import json
import os
import re
import sys
import difflib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────

# NOTE: The "description" field is a fallback only. The monitor extracts the
# actual page title from each CMS page at runtime (see extract_page_title()).
# This prevents stale or invented names from appearing on the dashboard.
MODELS = {
    "ACCESS": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/access",
        "description": "ACCESS Model",
    },
    "ASM": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/asm",
        "description": "Ambulatory Specialty Model (ASM)",
    },
    "LEAD": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/lead",
        "description": "LEAD Model",
    },
    "MAHA ELEVATE": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/maha-elevate",
        "description": "MAHA ELEVATE Model",
    },
    "AHEAD": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/ahead",
        "description": "AHEAD Model",
    },
    "TEAM": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/team-model",
        "description": "Transforming Episode Accountability Model (TEAM)",
    },
    "ACO Primary Care Flex": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/aco-primary-care-flex-model",
        "description": "ACO Primary Care Flex Model",
    },
    "ACO REACH": {
        "url": "https://www.cms.gov/priorities/innovation/innovation-models/aco-reach",
        "description": "ACO REACH Model",
    },
}

# Where to store snapshots and output
SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR / "data"
DASHBOARD_PATH = SCRIPT_DIR / "index.html"
CHANGE_LOG_PATH = SCRIPT_DIR / "change_log.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CMSModelMonitor/1.0; policy-research)"
}
TIMEOUT = 30


# ── Fetching & Parsing ────────────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    """Fetch a CMS page and return the parsed soup object."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ERROR fetching {url}: {e}")
        return None
    return BeautifulSoup(resp.text, "html.parser")


def extract_page_title(soup: BeautifulSoup) -> str | None:
    """Extract the official page title/heading from a CMS model page.

    This pulls the title directly from the page so we never invent or
    hardcode model names. Falls back through several selectors."""
    # Try the main h1 heading first
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
        if title and len(title) < 200:
            return title

    # Try the <title> tag, stripping the " | CMS" suffix
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        title = re.sub(r"\s*\|.*$", "", title).strip()
        if title:
            return title

    # Try og:title meta tag
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()

    return None


def extract_page_text(soup: BeautifulSoup) -> str:
    """Extract the main text content from a parsed CMS page."""
    # Work on a copy so we don't mutate the original
    soup_copy = BeautifulSoup(str(soup), "html.parser")

    # Remove script/style/nav/footer noise
    for tag in soup_copy(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    # Try to grab the main content area
    main = soup_copy.find("main") or soup_copy.find("article") or soup_copy.find("div", {"role": "main"})
    if main is None:
        main = soup_copy.body or soup_copy

    text = main.get_text(separator="\n", strip=True)
    # Collapse blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def extract_sections(text: str) -> dict[str, str]:
    """Split page text into rough sections for smarter diffing."""
    sections = {}
    current_section = "Header"
    current_lines = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            current_lines.append("")
            continue
        # Heuristic: lines that are short, capitalized, or look like headers
        if (len(stripped) < 80 and stripped == stripped.title() and
                not stripped.endswith(".") and len(stripped.split()) <= 8):
            if current_lines:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(stripped)

    if current_lines:
        sections[current_section] = "\n".join(current_lines).strip()
    return sections


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Snapshot Management ───────────────────────────────────────────────────

def load_snapshot(model_key: str) -> dict | None:
    path = DATA_DIR / f"{model_key.lower().replace(' ', '_')}_snapshot.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def save_snapshot(model_key: str, text: str, content_hash: str):
    path = DATA_DIR / f"{model_key.lower().replace(' ', '_')}_snapshot.json"
    snapshot = {
        "hash": content_hash,
        "text": text,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)


# ── Change Detection ─────────────────────────────────────────────────────

def detect_changes(old_text: str, new_text: str) -> list[dict]:
    """Return a list of change summaries between old and new text."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    differ = difflib.unified_diff(old_lines, new_lines, lineterm="", n=2)
    diff_lines = list(differ)

    if not diff_lines:
        return []

    changes = []
    added = []
    removed = []

    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            content = line[1:].strip()
            if content:
                added.append(content)
        elif line.startswith("-"):
            content = line[1:].strip()
            if content:
                removed.append(content)

    if added:
        changes.append({
            "type": "added",
            "count": len(added),
            "preview": added[:5],  # first 5 lines
        })
    if removed:
        changes.append({
            "type": "removed",
            "count": len(removed),
            "preview": removed[:5],
        })

    return changes


# ── Change Log ────────────────────────────────────────────────────────────

def load_change_log() -> list[dict]:
    if CHANGE_LOG_PATH.exists():
        with open(CHANGE_LOG_PATH) as f:
            return json.load(f)
    return []


def save_change_log(log: list[dict]):
    with open(CHANGE_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def add_to_change_log(model_key: str, changes: list[dict]):
    log = load_change_log()
    log.insert(0, {
        "model": model_key,
        "url": MODELS[model_key]["url"],
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    })
    # Keep last 200 entries
    save_change_log(log[:200])


# ── News Search ───────────────────────────────────────────────────────────

def search_news(model_name: str) -> list[dict]:
    """Search for recent news about a CMS model using Google News RSS."""
    query = f"CMS {model_name} innovation model"
    rss_url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"

    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")
        items = soup.find_all("item", limit=5)

        results = []
        for item in items:
            title = item.find("title")
            link = item.find("link")
            pub_date = item.find("pubDate")
            source = item.find("source")
            if title and link:
                results.append({
                    "title": title.text.strip(),
                    "url": link.text.strip() if link.text else (link.next_sibling or "").strip(),
                    "date": pub_date.text.strip() if pub_date else "",
                    "source": source.text.strip() if source else "",
                })
        return results
    except Exception as e:
        print(f"  News search error for {model_name}: {e}")
        return []


# ── Dashboard Generation ─────────────────────────────────────────────────

def generate_dashboard(results: list[dict], news: dict[str, list[dict]]):
    """Generate an HTML dashboard from monitoring results."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    change_log = load_change_log()

    # Build model cards
    model_cards_html = ""
    for r in results:
        status_class = "changed" if r["status"] == "changed" else (
            "new" if r["status"] == "new_baseline" else (
                "error" if r["status"] == "error" else "unchanged"
            )
        )
        status_label = {
            "changed": "Changes Detected",
            "new_baseline": "First Scan (Baseline)",
            "unchanged": "No Changes",
            "error": "Fetch Error",
        }.get(r["status"], r["status"])

        status_icon = {
            "changed": "🔴",
            "new_baseline": "🟡",
            "unchanged": "🟢",
            "error": "⚠️",
        }.get(r["status"], "")

        changes_html = ""
        if r.get("changes"):
            for c in r["changes"]:
                preview_items = "".join(
                    f'<li>{escape_html(line[:120])}</li>' for line in c.get("preview", [])
                )
                changes_html += f"""
                <div class="change-detail">
                    <span class="change-type {c['type']}">{c['type'].upper()}</span>
                    <span class="change-count">{c['count']} lines</span>
                    <ul class="change-preview">{preview_items}</ul>
                </div>
                """

        # News for this model
        model_news = news.get(r["model"], [])
        news_html = ""
        if model_news:
            news_items = ""
            for n in model_news[:3]:
                source_tag = f' <span class="news-source">{escape_html(n["source"])}</span>' if n.get("source") else ""
                date_tag = f' <span class="news-date">{escape_html(n["date"][:16])}</span>' if n.get("date") else ""
                news_items += f'<li><a href="{escape_html(n["url"])}" target="_blank">{escape_html(n["title"][:100])}</a>{source_tag}{date_tag}</li>'
            news_html = f'<div class="news-section"><h4>Recent News</h4><ul>{news_items}</ul></div>'

        model_cards_html += f"""
        <div class="model-card {status_class}">
            <div class="card-header">
                <div>
                    <h3><a href="{r['url']}" target="_blank">{escape_html(r['model'])}</a></h3>
                    <p class="description">{escape_html(r['description'])}</p>
                </div>
                <div class="status-badge {status_class}">
                    {status_icon} {status_label}
                </div>
            </div>
            {changes_html}
            {news_html}
        </div>
        """

    # Recent changes timeline
    timeline_html = ""
    for entry in change_log[:15]:
        ts = entry["detected_at"][:10]
        model = entry["model"]
        summary_parts = []
        for c in entry.get("changes", []):
            summary_parts.append(f'{c["count"]} lines {c["type"]}')
        summary = ", ".join(summary_parts) if summary_parts else "changes detected"
        timeline_html += f"""
        <div class="timeline-entry">
            <span class="timeline-date">{ts}</span>
            <span class="timeline-model">{escape_html(model)}</span>
            <span class="timeline-summary">{escape_html(summary)}</span>
        </div>
        """

    if not timeline_html:
        timeline_html = '<p class="no-history">No changes recorded yet. History will build over time as the monitor runs daily.</p>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CMS Innovation Model Monitor</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    line-height: 1.5;
}}
.container {{ max-width: 1000px; margin: 0 auto; padding: 24px; }}

/* Header */
.header {{
    background: linear-gradient(135deg, #1a365d 0%, #2a4a7f 100%);
    color: white;
    padding: 32px;
    border-radius: 12px;
    margin-bottom: 24px;
}}
.header h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 4px; }}
.header .subtitle {{ opacity: 0.85; font-size: 14px; }}
.header .timestamp {{ opacity: 0.7; font-size: 13px; margin-top: 8px; }}

/* Summary bar */
.summary-bar {{
    display: flex;
    gap: 12px;
    margin-bottom: 24px;
    flex-wrap: wrap;
}}
.summary-stat {{
    background: white;
    border-radius: 8px;
    padding: 16px 20px;
    flex: 1;
    min-width: 140px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}}
.summary-stat .stat-value {{ font-size: 28px; font-weight: 700; }}
.summary-stat .stat-label {{ font-size: 13px; color: #666; margin-top: 2px; }}
.summary-stat.has-changes .stat-value {{ color: #c53030; }}
.summary-stat.all-clear .stat-value {{ color: #276749; }}

/* Model cards */
.model-card {{
    background: white;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    border-left: 4px solid #cbd5e0;
    transition: box-shadow 0.2s;
}}
.model-card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,0.12); }}
.model-card.changed {{ border-left-color: #c53030; background: #fff5f5; }}
.model-card.new {{ border-left-color: #d69e2e; }}
.model-card.error {{ border-left-color: #ed8936; }}
.model-card.unchanged {{ border-left-color: #48bb78; }}

.card-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
.card-header h3 {{ font-size: 16px; font-weight: 600; }}
.card-header h3 a {{ color: #1a365d; text-decoration: none; }}
.card-header h3 a:hover {{ text-decoration: underline; }}
.description {{ font-size: 13px; color: #666; margin-top: 2px; }}

.status-badge {{
    font-size: 12px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 20px;
    white-space: nowrap;
    flex-shrink: 0;
}}
.status-badge.changed {{ background: #fed7d7; color: #9b2c2c; }}
.status-badge.new {{ background: #fefcbf; color: #975a16; }}
.status-badge.unchanged {{ background: #c6f6d5; color: #276749; }}
.status-badge.error {{ background: #feebc8; color: #9c4221; }}

.change-detail {{
    margin-top: 12px;
    padding: 10px;
    background: #fff;
    border: 1px solid #fed7d7;
    border-radius: 6px;
}}
.change-type {{
    font-size: 11px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 3px;
    text-transform: uppercase;
}}
.change-type.added {{ background: #c6f6d5; color: #276749; }}
.change-type.removed {{ background: #fed7d7; color: #9b2c2c; }}
.change-count {{ font-size: 12px; color: #666; margin-left: 8px; }}
.change-preview {{
    margin-top: 8px;
    padding-left: 18px;
    font-size: 12px;
    color: #4a5568;
}}
.change-preview li {{ margin-bottom: 2px; }}

.news-section {{
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid #e2e8f0;
}}
.news-section h4 {{ font-size: 13px; font-weight: 600; color: #4a5568; margin-bottom: 6px; }}
.news-section ul {{ list-style: none; padding: 0; }}
.news-section li {{ font-size: 13px; margin-bottom: 4px; }}
.news-section a {{ color: #2b6cb0; text-decoration: none; }}
.news-section a:hover {{ text-decoration: underline; }}
.news-source {{ font-size: 11px; color: #999; }}
.news-date {{ font-size: 11px; color: #999; }}

/* Timeline */
.section-title {{ font-size: 18px; font-weight: 700; margin: 28px 0 12px; color: #1a365d; }}
.timeline-entry {{
    display: flex;
    gap: 12px;
    padding: 10px 14px;
    background: white;
    border-radius: 6px;
    margin-bottom: 6px;
    font-size: 13px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.06);
}}
.timeline-date {{ color: #999; white-space: nowrap; min-width: 85px; }}
.timeline-model {{ font-weight: 600; min-width: 140px; }}
.timeline-summary {{ color: #666; }}
.no-history {{ color: #999; font-size: 14px; padding: 16px 0; }}

/* Footer */
.footer {{
    text-align: center;
    font-size: 12px;
    color: #999;
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid #e2e8f0;
}}
</style>
</head>
<body>
<div class="container">

<div class="header">
    <h1>CMS Innovation Model Monitor</h1>
    <div class="subtitle">Tracking changes to CMMI model pages</div>
    <div class="timestamp">Last checked: {now}</div>
</div>

<div class="summary-bar">
    <div class="summary-stat">
        <div class="stat-value">{len(results)}</div>
        <div class="stat-label">Models Tracked</div>
    </div>
    <div class="summary-stat {'has-changes' if any(r['status'] == 'changed' for r in results) else 'all-clear'}">
        <div class="stat-value">{sum(1 for r in results if r['status'] == 'changed')}</div>
        <div class="stat-label">Changes Detected</div>
    </div>
    <div class="summary-stat">
        <div class="stat-value">{sum(1 for r in results if r['status'] == 'error')}</div>
        <div class="stat-label">Fetch Errors</div>
    </div>
    <div class="summary-stat">
        <div class="stat-value">{sum(len(v) for v in news.values())}</div>
        <div class="stat-label">News Items</div>
    </div>
</div>

{model_cards_html}

<h2 class="section-title">Change History</h2>
{timeline_html}

<div class="footer">
    CMS Innovation Model Monitor &middot; Data sourced from cms.gov
</div>

</div>
</body>
</html>"""

    with open(DASHBOARD_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard written to {DASHBOARD_PATH}")


def escape_html(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── Main ──────────────────────────────────────────────────────────────────

def run_check():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    all_news = {}

    print(f"CMS Model Monitor - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Checking {len(MODELS)} models...\n")

    for model_key, model_info in MODELS.items():
        print(f"[{model_key}]")
        url = model_info["url"]
        fallback_description = model_info["description"]

        soup = fetch_page(url)
        if soup is None:
            results.append({
                "model": model_key,
                "url": url,
                "description": fallback_description,
                "status": "error",
                "changes": [],
            })
            continue

        # Pull the official title from the page itself, never invent one
        page_title = extract_page_title(soup)
        description = page_title if page_title else fallback_description
        print(f"  Title from page: {description}")

        text = extract_page_text(soup)
        new_hash = compute_hash(text)
        old_snapshot = load_snapshot(model_key)

        if old_snapshot is None:
            print("  First scan - saving baseline")
            save_snapshot(model_key, text, new_hash)
            results.append({
                "model": model_key,
                "url": url,
                "description": description,
                "status": "new_baseline",
                "changes": [],
            })
        elif old_snapshot["hash"] != new_hash:
            changes = detect_changes(old_snapshot["text"], text)
            print(f"  CHANGES DETECTED ({len(changes)} change groups)")
            add_to_change_log(model_key, changes)
            save_snapshot(model_key, text, new_hash)
            results.append({
                "model": model_key,
                "url": url,
                "description": description,
                "status": "changed",
                "changes": changes,
            })
        else:
            print("  No changes")
            results.append({
                "model": model_key,
                "url": url,
                "description": description,
                "status": "unchanged",
                "changes": [],
            })

        # News search
        print(f"  Searching news...")
        model_news = search_news(model_key)
        if model_news:
            print(f"  Found {len(model_news)} news items")
            all_news[model_key] = model_news
        else:
            print(f"  No news found")

    print(f"\nGenerating dashboard...")
    generate_dashboard(results, all_news)
    print("Done!")

    # Print summary
    changed = sum(1 for r in results if r["status"] == "changed")
    errors = sum(1 for r in results if r["status"] == "error")
    if changed:
        print(f"\n⚠ {changed} model(s) have changes!")
    if errors:
        print(f"\n⚠ {errors} model(s) had fetch errors")


if __name__ == "__main__":
    run_check()
