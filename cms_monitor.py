#!/usr/bin/env python3
"""
CMS Innovation Model Page Monitor
Tracks changes to CMS innovation model pages and generates an HTML dashboard.
Extracts and highlights resources, fact sheets, FAQs, and documents specifically.
"""

import hashlib
import json
import os
import re
import sys
import difflib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

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

# Resource type classification patterns
RESOURCE_PATTERNS = {
    "fact_sheet": re.compile(r"fact\s*sheet|factsheet|fact-sheet|fs\.pdf", re.I),
    "faq": re.compile(r"faq|frequently\s+asked|questions\s+and\s+answers", re.I),
    "overview": re.compile(r"overview|summary|at\s+a\s+glance", re.I),
    "webinar": re.compile(r"webinar|slide|presentation|transcript|recording", re.I),
    "announcement": re.compile(r"announce|innovation\s+insight|press|release|notice", re.I),
    "guidance": re.compile(r"guidance|rule|regulation|request\s+for|rfi|rfa|nprm|final\s+rule", re.I),
    "report": re.compile(r"report|evaluation|findings|results|data", re.I),
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

CMS_BASE = "https://www.cms.gov"


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
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
        if title and len(title) < 200:
            return title

    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        title = re.sub(r"\s*\|.*$", "", title).strip()
        if title:
            return title

    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()

    return None


def extract_page_text(soup: BeautifulSoup) -> str:
    """Extract the main text content from a parsed CMS page."""
    soup_copy = BeautifulSoup(str(soup), "html.parser")

    for tag in soup_copy(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    main = soup_copy.find("main") or soup_copy.find("article") or soup_copy.find("div", {"role": "main"})
    if main is None:
        main = soup_copy.body or soup_copy

    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def classify_resource(title: str, url: str) -> str:
    """Classify a resource link into a type based on its title and URL."""
    combined = f"{title} {url}"
    for rtype, pattern in RESOURCE_PATTERNS.items():
        if pattern.search(combined):
            return rtype
    if url.lower().endswith(".pdf"):
        return "document"
    return "resource"


def extract_resources(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Extract resources, documents, fact sheets, FAQs from a CMS page.

    Only looks at links inside the main content area (not nav/footer/sidebar).
    Focuses on PDFs, FAQ pages, fact sheets, webinars, and other model-specific
    resources. Deduplicates by URL."""
    resources = []
    seen_urls = set()

    # First, isolate the main content area to avoid nav/sidebar/footer links
    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", {"role": "main"})
        or soup.find("div", class_=re.compile(r"content|field--name-body", re.I))
    )
    if main_content is None:
        # Last resort: use body but strip nav/footer
        main_content = BeautifulSoup(str(soup.body or soup), "html.parser")
        for tag in main_content.find_all(["nav", "footer", "header"]):
            tag.decompose()

    # Only search for links within the main content
    for a_tag in main_content.find_all("a", href=True):
        href = a_tag["href"].strip()
        title = a_tag.get_text(strip=True)

        if not title or len(title) < 3:
            continue

        # Resolve relative URLs
        full_url = urljoin(page_url, href)

        # Skip navigation, anchor-only, mailto, tel links
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue

        # Skip links that are clearly site navigation (not model-specific)
        # These are broad CMS pages, not model resources
        nav_patterns = [
            r"/priorities/innovation/?$",
            r"/priorities/?$",
            r"^https?://www\.cms\.gov/?$",
            r"/about-cms",
            r"/newsroom/?$",
            r"/regulations-and-guidance/?$",
            r"/medicare/?$",
            r"/medicaid/?$",
            r"/data-research/?$",
            r"/outreach-and-education/?$",
        ]
        is_nav = False
        for nav_pat in nav_patterns:
            if re.search(nav_pat, href):
                is_nav = True
                break
        if is_nav:
            continue

        # Is this a resource-like link?
        is_resource = False

        # PDFs are always resources
        if href.lower().endswith(".pdf"):
            is_resource = True

        # Links with resource-like text in the link title
        resource_title_keywords = re.compile(
            r"fact\s*sheet|faq|frequently|webinar|slide|transcript|recording|"
            r"overview\s+(document|pdf)|announcement|innovation\s+insight|"
            r"guidance|rule|regulation|request\s+for|report|evaluation|"
            r"participant\s+resource|application|toolkit|template|"
            r"listserv|email\s+update|sign\s+up",
            re.I
        )
        if resource_title_keywords.search(title):
            is_resource = True

        # Links to CMS innovation subpages (FAQs, participant resources, etc.)
        if re.search(r"/priorities/innovation/.+/(faq|resource|participant|data|report)", href, re.I):
            is_resource = True

        # Links to govdelivery (listservs/email signups)
        if "govdelivery.com" in href:
            is_resource = True

        if not is_resource:
            continue

        # Deduplicate
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        rtype = classify_resource(title, full_url)
        resources.append({
            "title": title,
            "url": full_url,
            "type": rtype,
        })

    return resources


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Snapshot Management ───────────────────────────────────────────────────

def load_snapshot(model_key: str) -> dict | None:
    path = DATA_DIR / f"{model_key.lower().replace(' ', '_')}_snapshot.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def save_snapshot(model_key: str, text: str, content_hash: str, resources: list[dict]):
    path = DATA_DIR / f"{model_key.lower().replace(' ', '_')}_snapshot.json"
    snapshot = {
        "hash": content_hash,
        "text": text,
        "resources": resources,
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
            "preview": added[:5],
            "full": added,  # complete list for expandable diff view
        })
    if removed:
        changes.append({
            "type": "removed",
            "count": len(removed),
            "preview": removed[:5],
            "full": removed,
        })

    return changes


def detect_resource_changes(old_resources: list[dict], new_resources: list[dict]) -> dict:
    """Compare old and new resource lists. Return added, removed, and unchanged."""
    old_urls = {r["url"] for r in old_resources}
    new_urls = {r["url"] for r in new_resources}

    added_urls = new_urls - old_urls
    removed_urls = old_urls - new_urls

    added = [r for r in new_resources if r["url"] in added_urls]
    removed = [r for r in old_resources if r["url"] in removed_urls]

    return {
        "added": added,
        "removed": removed,
        "total_current": len(new_resources),
    }


# ── Change Log ────────────────────────────────────────────────────────────

def load_change_log() -> list[dict]:
    if CHANGE_LOG_PATH.exists():
        with open(CHANGE_LOG_PATH) as f:
            return json.load(f)
    return []


def save_change_log(log: list[dict]):
    with open(CHANGE_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def add_to_change_log(model_key: str, changes: list[dict], resource_changes: dict | None = None):
    log = load_change_log()
    entry = {
        "model": model_key,
        "url": MODELS[model_key]["url"],
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    }
    if resource_changes:
        entry["resource_changes"] = resource_changes
    log.insert(0, entry)
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

RESOURCE_TYPE_LABELS = {
    "fact_sheet": ("Fact Sheet", "#e53e3e", "📄"),
    "faq": ("FAQ", "#d69e2e", "❓"),
    "overview": ("Overview", "#3182ce", "📋"),
    "webinar": ("Webinar", "#805ad5", "🎥"),
    "announcement": ("Announcement", "#dd6b20", "📢"),
    "guidance": ("Guidance", "#2f855a", "⚖️"),
    "report": ("Report", "#4a5568", "📊"),
    "document": ("Document", "#718096", "📎"),
    "resource": ("Resource", "#a0aec0", "🔗"),
}


def build_resource_html(resources: list[dict], new_urls: set | None = None) -> str:
    """Build HTML for a resource list, highlighting new items."""
    if not resources:
        return ""

    # Group by type
    by_type = {}
    for r in resources:
        rtype = r.get("type", "resource")
        by_type.setdefault(rtype, []).append(r)

    # Priority order for display
    type_order = ["fact_sheet", "faq", "guidance", "announcement", "overview",
                  "webinar", "report", "document", "resource"]

    items_html = ""
    for rtype in type_order:
        type_resources = by_type.get(rtype, [])
        if not type_resources:
            continue
        label, color, icon = RESOURCE_TYPE_LABELS.get(rtype, ("Resource", "#a0aec0", "🔗"))
        for r in type_resources:
            is_new = new_urls and r["url"] in new_urls
            new_badge = ' <span class="new-badge">NEW</span>' if is_new else ""
            row_class = "resource-row new-resource" if is_new else "resource-row"
            items_html += f"""
            <div class="{row_class}">
                <span class="resource-type-tag" style="background: {color}15; color: {color}; border: 1px solid {color}40;">{icon} {label}</span>
                <a href="{escape_html(r['url'])}" target="_blank">{escape_html(r['title'][:100])}</a>{new_badge}
            </div>"""

    if not items_html:
        return ""

    return f"""
    <div class="resources-section">
        <h4>Resources & Documents ({len(resources)})</h4>
        <div class="resources-list">{items_html}
        </div>
    </div>"""


def get_recent_changes_for_model(model_key: str, change_log: list[dict], days: int = 7) -> list[dict]:
    """Get all change log entries for a model within the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for entry in change_log:
        if entry["model"] != model_key:
            continue
        try:
            detected = datetime.fromisoformat(entry["detected_at"])
            if detected >= cutoff:
                recent.append(entry)
        except (ValueError, KeyError):
            continue
    return recent


def days_ago_label(iso_timestamp: str) -> str:
    """Convert an ISO timestamp to a human-readable 'X days ago' string."""
    try:
        detected = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(timezone.utc)
        delta = now - detected
        if delta.days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                return "just now"
            return f"{hours}h ago"
        elif delta.days == 1:
            return "1 day ago"
        else:
            return f"{delta.days} days ago"
    except (ValueError, KeyError):
        return ""


def generate_dashboard(results: list[dict], news: dict[str, list[dict]]):
    """Generate an HTML dashboard from monitoring results.

    Uses the change log to show changes from the last 7 days in each model
    card, not just the most recent run."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    change_log = load_change_log()

    total_resources = sum(len(r.get("resources", [])) for r in results)

    # Count models with recent changes (last 7 days) from the log
    models_with_recent_changes = set()
    recent_new_resource_count = 0
    for entry in change_log:
        try:
            detected = datetime.fromisoformat(entry["detected_at"])
            if datetime.now(timezone.utc) - detected <= timedelta(days=7):
                models_with_recent_changes.add(entry["model"])
                rc = entry.get("resource_changes", {})
                recent_new_resource_count += len(rc.get("added", []))
        except (ValueError, KeyError):
            continue

    # Build model cards
    model_cards_html = ""
    for r in results:
        model_key = r["model"]
        recent_changes = get_recent_changes_for_model(model_key, change_log, days=7)
        has_recent = len(recent_changes) > 0

        # Status is based on 7-day window, not just latest run
        if r["status"] == "error":
            status_class = "error"
            status_label = "Fetch Error"
            status_icon = "⚠️"
        elif r["status"] == "new_baseline":
            status_class = "new"
            status_label = "First Scan (Baseline)"
            status_icon = "🟡"
        elif has_recent:
            most_recent_ts = recent_changes[0]["detected_at"]
            ago = days_ago_label(most_recent_ts)
            status_class = "changed"
            status_label = f"Changed {ago}"
            status_icon = "🔴"
        else:
            status_class = "unchanged"
            status_label = "No Recent Changes"
            status_icon = "🟢"

        # Build recent changes section from the 7-day log
        recent_changes_html = ""
        if has_recent:
            for entry in recent_changes:
                entry_date = entry["detected_at"][:10]
                entry_ago = days_ago_label(entry["detected_at"])

                # Resource changes
                rc = entry.get("resource_changes", {})
                if rc.get("added"):
                    added_items = "".join(
                        f'<li><a href="{escape_html(res["url"])}" target="_blank">{escape_html(res["title"][:100])}</a> '
                        f'<span class="resource-type-inline">{RESOURCE_TYPE_LABELS.get(res.get("type", "resource"), ("Resource","",""))[0]}</span></li>'
                        for res in rc["added"]
                    )
                    recent_changes_html += f"""
                    <div class="resource-alert added">
                        <strong>New Resources ({entry_date}, {entry_ago})</strong>
                        <ul>{added_items}</ul>
                    </div>"""

                if rc.get("removed"):
                    removed_items = "".join(
                        f'<li>{escape_html(res["title"][:100])}</li>' for res in rc["removed"]
                    )
                    recent_changes_html += f"""
                    <div class="resource-alert removed">
                        <strong>Resources Removed ({entry_date}, {entry_ago})</strong>
                        <ul>{removed_items}</ul>
                    </div>"""

                # Text changes
                for c in entry.get("changes", []):
                    full_lines = c.get("full", c.get("preview", []))
                    preview_lines = full_lines[:3]
                    remaining_lines = full_lines[3:]

                    preview_items = "".join(
                        f'<li>{escape_html(line[:200])}</li>' for line in preview_lines
                    )

                    # Build expandable full diff if there are more lines
                    expand_html = ""
                    if remaining_lines:
                        full_items = "".join(
                            f'<li>{escape_html(line[:300])}</li>' for line in remaining_lines
                        )
                        expand_html = f"""
                        <details class="full-diff">
                            <summary>Show all {len(full_lines)} lines</summary>
                            <ul class="change-preview full-diff-content">{full_items}</ul>
                        </details>"""

                    recent_changes_html += f"""
                    <div class="change-detail">
                        <span class="change-date">{entry_date}</span>
                        <span class="change-type {c['type']}">{c['type'].upper()}</span>
                        <span class="change-count">{c['count']} lines</span>
                        <ul class="change-preview">{preview_items}</ul>
                        {expand_html}
                    </div>
                    """

        # Resources section
        resources = r.get("resources", [])
        # Collect all new resource URLs from the 7-day window
        recent_new_urls = set()
        for entry in recent_changes:
            rc = entry.get("resource_changes", {})
            for res in rc.get("added", []):
                recent_new_urls.add(res["url"])
        resources_html = build_resource_html(resources, recent_new_urls if recent_new_urls else None)

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
            {recent_changes_html}
            {resources_html}
            {news_html}
        </div>
        """

    # Full change history timeline
    timeline_html = ""
    for entry in change_log[:30]:
        ts = entry["detected_at"][:10]
        ago = days_ago_label(entry["detected_at"])
        model = entry["model"]
        summary_parts = []
        for c in entry.get("changes", []):
            summary_parts.append(f'{c["count"]} lines {c["type"]}')
        rc = entry.get("resource_changes", {})
        if rc.get("added"):
            summary_parts.append(f'{len(rc["added"])} new resource(s)')
        if rc.get("removed"):
            summary_parts.append(f'{len(rc["removed"])} resource(s) removed')
        summary = ", ".join(summary_parts) if summary_parts else "changes detected"

        has_resources = bool(rc.get("added") or rc.get("removed"))
        entry_class = "timeline-entry has-resource-change" if has_resources else "timeline-entry"
        timeline_html += f"""
        <div class="{entry_class}">
            <span class="timeline-date">{ts}</span>
            <span class="timeline-ago">{ago}</span>
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
    min-width: 120px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}}
.summary-stat .stat-value {{ font-size: 28px; font-weight: 700; }}
.summary-stat .stat-label {{ font-size: 13px; color: #666; margin-top: 2px; }}
.summary-stat.has-changes .stat-value {{ color: #c53030; }}
.summary-stat.all-clear .stat-value {{ color: #276749; }}
.summary-stat.has-new-resources .stat-value {{ color: #d69e2e; }}

.model-card {{
    background: white;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 16px;
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

/* Resource alerts */
.resource-alert {{
    margin-top: 12px;
    padding: 12px 14px;
    border-radius: 8px;
    font-size: 13px;
}}
.resource-alert.added {{
    background: #f0fff4;
    border: 1px solid #9ae6b4;
}}
.resource-alert.added strong {{ color: #276749; }}
.resource-alert.removed {{
    background: #fff5f5;
    border: 1px solid #feb2b2;
}}
.resource-alert.removed strong {{ color: #9b2c2c; }}
.resource-alert ul {{
    margin-top: 6px;
    padding-left: 20px;
    color: #4a5568;
}}
.resource-alert li {{ margin-bottom: 2px; }}

/* Resources section */
.resources-section {{
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid #e2e8f0;
}}
.resources-section h4 {{
    font-size: 13px;
    font-weight: 600;
    color: #4a5568;
    margin-bottom: 8px;
}}
.resources-list {{
    display: flex;
    flex-direction: column;
    gap: 5px;
}}
.resource-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    padding: 4px 0;
}}
.resource-row.new-resource {{
    background: #fffff0;
    padding: 4px 8px;
    border-radius: 4px;
    border: 1px solid #fefcbf;
}}
.resource-type-tag {{
    font-size: 10px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 3px;
    white-space: nowrap;
    flex-shrink: 0;
}}
.resource-row a {{ color: #2b6cb0; text-decoration: none; }}
.resource-row a:hover {{ text-decoration: underline; }}
.new-badge {{
    background: #f6e05e;
    color: #744210;
    font-size: 10px;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 3px;
    margin-left: 4px;
}}

/* Page content changes */
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
.change-date {{ font-size: 11px; color: #999; margin-right: 4px; }}
.change-count {{ font-size: 12px; color: #666; margin-left: 8px; }}
.more-changes {{ font-size: 11px; color: #999; font-style: italic; }}
.change-preview {{
    margin-top: 8px;
    padding-left: 18px;
    font-size: 12px;
    color: #4a5568;
}}
.change-preview li {{ margin-bottom: 2px; }}

/* Expandable full diff */
.full-diff {{
    margin-top: 8px;
}}
.full-diff summary {{
    font-size: 12px;
    color: #2b6cb0;
    cursor: pointer;
    font-weight: 600;
    padding: 4px 0;
}}
.full-diff summary:hover {{ text-decoration: underline; }}
.full-diff-content {{
    max-height: 400px;
    overflow-y: auto;
    background: #f7fafc;
    border: 1px solid #e2e8f0;
    border-radius: 4px;
    padding: 8px 8px 8px 24px;
    margin-top: 6px;
    font-size: 12px;
    line-height: 1.6;
}}
.resource-type-inline {{
    font-size: 10px;
    color: #999;
    font-style: italic;
}}

/* News */
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
.timeline-entry.has-resource-change {{
    border-left: 3px solid #d69e2e;
}}
.timeline-date {{ color: #999; white-space: nowrap; min-width: 85px; }}
.timeline-ago {{ color: #b0b0b0; font-size: 12px; white-space: nowrap; min-width: 80px; }}
.timeline-model {{ font-weight: 600; min-width: 160px; }}
.timeline-summary {{ color: #666; }}
.no-history {{ color: #999; font-size: 14px; padding: 16px 0; }}

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
    <div class="subtitle">Tracking changes to CMMI model pages, resources, and news</div>
    <div class="timestamp">Last checked: {now}</div>
</div>

<div class="summary-bar">
    <div class="summary-stat">
        <div class="stat-value">{len(results)}</div>
        <div class="stat-label">Models Tracked</div>
    </div>
    <div class="summary-stat {'has-changes' if len(models_with_recent_changes) > 0 else 'all-clear'}">
        <div class="stat-value">{len(models_with_recent_changes)}</div>
        <div class="stat-label">Changed (7 days)</div>
    </div>
    <div class="summary-stat {'has-new-resources' if recent_new_resource_count > 0 else ''}">
        <div class="stat-value">{recent_new_resource_count}</div>
        <div class="stat-label">New Resources (7 days)</div>
    </div>
    <div class="summary-stat">
        <div class="stat-value">{total_resources}</div>
        <div class="stat-label">Total Resources</div>
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
                "resources": [],
                "resource_changes": {},
            })
            continue

        # Pull the official title from the page itself, never invent one
        page_title = extract_page_title(soup)
        description = page_title if page_title else fallback_description
        print(f"  Title from page: {description}")

        # Extract page text and resources
        text = extract_page_text(soup)
        resources = extract_resources(soup, url)
        print(f"  Found {len(resources)} resources")

        new_hash = compute_hash(text)
        old_snapshot = load_snapshot(model_key)

        # Compare resources
        old_resources = old_snapshot.get("resources", []) if old_snapshot else []
        resource_changes = detect_resource_changes(old_resources, resources)

        if resource_changes["added"]:
            print(f"  NEW RESOURCES: {len(resource_changes['added'])}")
            for r in resource_changes["added"]:
                print(f"    + {r['title']} [{r['type']}]")
        if resource_changes["removed"]:
            print(f"  REMOVED RESOURCES: {len(resource_changes['removed'])}")

        if old_snapshot is None:
            print("  First scan - saving baseline")
            save_snapshot(model_key, text, new_hash, resources)
            results.append({
                "model": model_key,
                "url": url,
                "description": description,
                "status": "new_baseline",
                "changes": [],
                "resources": resources,
                "resource_changes": {},
            })
        elif old_snapshot["hash"] != new_hash:
            changes = detect_changes(old_snapshot["text"], text)
            print(f"  PAGE CHANGES DETECTED ({len(changes)} change groups)")
            add_to_change_log(model_key, changes, resource_changes)
            save_snapshot(model_key, text, new_hash, resources)
            results.append({
                "model": model_key,
                "url": url,
                "description": description,
                "status": "changed",
                "changes": changes,
                "resources": resources,
                "resource_changes": resource_changes,
            })
        elif resource_changes["added"] or resource_changes["removed"]:
            # Resources changed but main text hash didn't (unlikely but possible)
            add_to_change_log(model_key, [], resource_changes)
            save_snapshot(model_key, text, new_hash, resources)
            results.append({
                "model": model_key,
                "url": url,
                "description": description,
                "status": "changed",
                "changes": [],
                "resources": resources,
                "resource_changes": resource_changes,
            })
        else:
            print("  No changes")
            results.append({
                "model": model_key,
                "url": url,
                "description": description,
                "status": "unchanged",
                "changes": [],
                "resources": resources,
                "resource_changes": {},
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
    total_new_resources = sum(
        len(r.get("resource_changes", {}).get("added", []))
        for r in results
    )
    if changed:
        print(f"\n{changed} model(s) have changes!")
    if total_new_resources:
        print(f"{total_new_resources} new resource(s) found across all models!")
    if errors:
        print(f"{errors} model(s) had fetch errors")
    if not changed and not errors:
        print("\nAll models unchanged.")

    # Write a markdown summary for GitHub Issues notification
    # Only created when there are actual changes (not on first baseline scan)
    write_changes_summary(results)


def write_changes_summary(results: list[dict]):
    """Write a changes_summary.md file if any models have changes.

    This file is picked up by GitHub Actions to create an Issue notification.
    Only written when real changes are detected (not first-time baselines)."""
    changed_results = [r for r in results if r["status"] == "changed"]
    if not changed_results:
        # No summary file = no issue created
        summary_path = SCRIPT_DIR / "changes_summary.md"
        if summary_path.exists():
            summary_path.unlink()
        return

    lines = []
    lines.append("Changes were detected on the following CMS Innovation Model pages:\n")
    lines.append(f"[View the full dashboard](https://lisabbari.github.io/cms-tracker/)\n")

    for r in changed_results:
        lines.append(f"## [{r['model']}]({r['url']})\n")

        # Resource changes (most important)
        rc = r.get("resource_changes", {})
        if rc.get("added"):
            lines.append(f"**New Resources ({len(rc['added'])}):**\n")
            for res in rc["added"]:
                rtype = RESOURCE_TYPE_LABELS.get(res.get("type", "resource"), ("Resource", "", ""))[0]
                lines.append(f"- [{res['title']}]({res['url']}) ({rtype})")
            lines.append("")

        if rc.get("removed"):
            lines.append(f"**Removed Resources ({len(rc['removed'])}):**\n")
            for res in rc["removed"]:
                lines.append(f"- {res['title']}")
            lines.append("")

        # Page content changes
        if r.get("changes"):
            for c in r["changes"]:
                lines.append(f"**Page content: {c['count']} lines {c['type']}**\n")
                for preview_line in c.get("preview", [])[:3]:
                    lines.append(f"> {preview_line[:120]}")
                lines.append("")

    summary_path = SCRIPT_DIR / "changes_summary.md"
    with open(summary_path, "w") as f:
        f.write("\n".join(lines))
    print(f"Changes summary written to {summary_path}")


if __name__ == "__main__":
    run_check()
