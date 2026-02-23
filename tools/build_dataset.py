"""
NDSS 2026 Personal Agenda – data pipeline
==========================================

Usage
-----
Generate JSON from cached HTML (default):
    python tools/build_dataset.py

Refresh cache from the live NDSS site and regenerate JSON:
    python tools/build_dataset.py --refresh

Serve locally after generating the JSON:
    python -m http.server 8000
    # then open http://localhost:8000/

Deploy to GitHub Pages:
    Commit data/ndss2026.program.json (and raw HTML) and push to the
    branch that is configured as the GitHub Pages source.

Requirements
------------
Python 3.11+.  Only stdlib + beautifulsoup4 are used.
    pip install beautifulsoup4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Try to import BeautifulSoup; give a clear error if missing.
# ---------------------------------------------------------------------------
try:
    from bs4 import BeautifulSoup, Tag
except ImportError:  # pragma: no cover
    sys.exit(
        "beautifulsoup4 is required.  Install it with:  pip install beautifulsoup4"
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_HTML_PATH = DATA_DIR / "ndss2026.raw.html"
JSON_PATH = DATA_DIR / "ndss2026.program.json"

SOURCE_URL = "https://www.ndss-symposium.org/ndss-program/symposium-2026/"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable_id(*parts: str) -> str:
    """Return a short stable hex ID derived from the concatenated parts."""
    raw = "\x00".join(p.strip().lower() for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _norm(text: str | None) -> str:
    """Strip and normalise whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _absolute_url(href: str | None) -> str:
    """Make sure a URL is absolute, using SOURCE_URL as base."""
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/"):
        from urllib.parse import urlparse
        parsed = urlparse(SOURCE_URL)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return href


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    """Download *url* and return the response body as a string."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ndss-agenda-builder/1.0 (+https://github.com)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

# Patterns that recognise common NDSS-style time strings, e.g.
# "8:30 AM – 10:00 AM",  "08:30-10:00",  "9:00am-11:30am"
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*(?:AM|PM)?(?:\s*[–\-]\s*)(\d{1,2}:\d{2})\s*(?:AM|PM)?",
    re.IGNORECASE,
)
_TIME_SINGLE_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(?:AM|PM)?\b", re.IGNORECASE)

# Recognise dates such as "February 24, 2026",  "24 February 2026",
# "Mon, Feb 24",  "Tuesday, 24 February"  (day may come before or after month)
_DATE_RE = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*[\s,]*)?(?:(\d{1,2})\s+)?"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+(\d{1,2}))?(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _parse_date(text: str) -> str:
    """Return ISO date string (YYYY-MM-DD) or empty string."""
    m = _DATE_RE.search(text)
    if not m:
        return ""
    day_prefix, month_name, day_suffix, year = m.groups()
    day = day_prefix or day_suffix
    if not day:
        return ""
    month = _MONTH_MAP.get(month_name.lower(), "00")
    yr = year or "2026"
    try:
        return f"{int(yr):04d}-{int(month):02d}-{int(day):02d}"
    except (ValueError, TypeError):
        return ""


def _parse_times(text: str) -> tuple[str, str]:
    """Return (start, end) as 'HH:MM' 24-h strings, or ('', '')."""
    m = _TIME_RANGE_RE.search(text)
    if m:
        return _to24(m.group(1), text, "start"), _to24(m.group(2), text, "end")
    singles = _TIME_SINGLE_RE.findall(text)
    if len(singles) >= 2:
        return _to24(singles[0], text, "start"), _to24(singles[1], text, "end")
    if len(singles) == 1:
        return _to24(singles[0], text, "start"), ""
    return "", ""


def _to24(time_str: str, context: str, role: str) -> str:
    """Convert a possibly-AM/PM time string to HH:MM (24h).

    We look for AM/PM in *context* near the time_str to determine period.
    """
    h, m = map(int, time_str.split(":"))
    # Find the AM/PM marker that immediately follows the time value in context
    pattern = re.escape(time_str) + r"\s*(AM|PM)"
    match = re.search(pattern, context, re.IGNORECASE)
    if match:
        period = match.group(1).upper()
        if period == "PM" and h != 12:
            h += 12
        elif period == "AM" and h == 12:
            h = 0
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def _get_text(tag: Tag) -> str:
    return _norm(tag.get_text(" ", strip=True)) if tag else ""


def parse_program(html: str) -> dict:
    """Parse NDSS program HTML into the canonical JSON schema."""
    soup = BeautifulSoup(html, "html.parser")

    # -----------------------------------------------------------------------
    # Primary strategy: the NDSS site uses a Bootstrap card layout where each
    # conference day is a <div class="card"> block containing an <h3> day
    # label and collapsible session rows.  Co-located event days (Monday /
    # Friday) contain only workshop links; we skip them.
    # -----------------------------------------------------------------------

    days_out = _parse_ndss_card_layout(soup)

    # -----------------------------------------------------------------------
    # Fallback A: <div class="...day..."> / <section class="...day...">
    # -----------------------------------------------------------------------
    if not days_out:
        day_containers = soup.find_all(
            lambda t: t.name in ("div", "section", "article")
            and any(
                kw in " ".join(t.get("class", [])).lower()
                for kw in ("day", "schedule-day", "program-day")
            )
        )
        for day_el in day_containers:
            day_dict = _parse_day(day_el)
            if day_dict["sessions"]:
                days_out.append(day_dict)

    # -----------------------------------------------------------------------
    # Fallback B: day-level h2/h3 headings
    # -----------------------------------------------------------------------
    if not days_out:
        for day_el in _group_by_heading(soup):
            day_dict = _parse_day(day_el)
            if day_dict["sessions"]:
                days_out.append(day_dict)

    # -----------------------------------------------------------------------
    # Fallback C: treat whole body as one unnamed day
    # -----------------------------------------------------------------------
    if not days_out:
        day_dict = _parse_day(soup.body or soup)
        if day_dict["sessions"]:
            days_out.append(day_dict)

    # If we still got nothing meaningful, emit a single placeholder day so
    # the JSON is at least structurally valid.
    if not days_out:
        days_out = [{"day_id": _stable_id("unknown"), "label": "Unknown", "date": "", "sessions": []}]

    return days_out


def _parse_ndss_card_layout(soup: BeautifulSoup) -> list[dict]:
    """
    Parse the Bootstrap card layout used on www.ndss-symposium.org.

    Each conference day is a  <div class="card …">  block whose header
    contains an <h3> with the day label.  Three types of schedule items
    appear inside each card (in document order):

    * <li  class="… card-subheading-session …">  – misc items (Registration,
      Breakfast, Welcome, Breaks).  No href.
    * <a   class="… card-subheading-workshop …">  – workshops (Mon/Fri) and
      keynotes (Tue–Thu).  Carry a real href.
    * <a   class="… card-subheading-session …">  – paper sessions.  No href;
      followed by a <ul class="list-group-session …"> paper list.
    """
    cards = soup.find_all("div", class_="card")
    if not cards:
        return []

    days_out: list[dict] = []

    for card in cards:
        header = card.find("div", class_="card-header")
        if not header:
            continue
        h3 = header.find("h3")
        if not h3:
            continue

        label = _norm(h3.get_text(" ", strip=True))
        date_str = _parse_date(label)
        day_id = _stable_id(label or "unknown", date_str)

        sessions: list[dict] = []

        # Iterate all schedule items in document order so the daily timeline
        # is preserved across the three element types.
        for el in card.find_all(
            lambda t: t.name in ("a", "li")
            and any(
                cls in " ".join(t.get("class", []))
                for cls in ("card-subheading-session", "card-subheading-workshop")
            )
        ):
            session = _parse_ndss_item(el)
            if session:
                sessions.append(session)

        if sessions:
            days_out.append({
                "day_id": day_id,
                "label": label,
                "date": date_str,
                "sessions": sessions,
            })

    return days_out


def _parse_ndss_item(el: Tag) -> dict | None:
    """
    Extract one schedule item from any of the three NDSS card item types:

    * <li  class="… card-subheading-session">   – misc (Registration, Breaks)
    * <a   class="… card-subheading-workshop">  – workshop / keynote (has href)
    * <a   class="… card-subheading-session">   – paper session (followed by paper <ul>)
    """
    el_classes = " ".join(el.get("class", []))

    # Time is in the first col-2 div
    time_div = el.find("div", class_="col-2")
    time_text = _norm(time_div.get_text(" ", strip=True)) if time_div else ""
    start_time, end_time = _parse_times(time_text)

    # Title is in col-8; sessions wrap it in <strong>, misc items do not
    col8 = el.find("div", class_="col-8")
    if not col8:
        return None
    strong = col8.find("strong")
    if strong:
        parts = [t.strip() for t in strong.strings if t.strip()]
        title = parts[0] if parts else _norm(col8.get_text(" ", strip=True))
    else:
        title = _norm(col8.get_text(" ", strip=True))

    if not title:
        return None

    # Room is in the text-right div
    room_div = el.find("div", class_="text-right")
    room = _norm(room_div.get_text(" ", strip=True)) if room_div else ""

    # Track badge (e.g. "1A" from "Session 1A: …")
    track = ""
    track_m = re.search(r"\bSession\s+([A-Z0-9]+[A-Z][A-Z0-9]*|[0-9]+[A-Z])\b", title)
    if track_m:
        track = track_m.group(1)

    # URL: workshop/keynote <a> elements carry a real href
    url = _absolute_url(el.get("href")) if el.name == "a" else ""

    session_id = _stable_id(title, start_time, end_time, track, room, url)

    # Papers are in the next sibling <ul class="list-group-session"> (paper sessions only)
    items: list[dict] = []
    if "card-subheading-session" in el_classes and el.name == "a":
        paper_ul = el.find_next_sibling("ul")
        if paper_ul and "list-group-session" in " ".join(paper_ul.get("class", [])):
            order = 0
            for li in paper_ul.find_all("li", class_="list-group-item"):
                paper_a = li.find("a", href=True)
                if not paper_a:
                    continue
                item_title = _norm(paper_a.get_text(" ", strip=True))
                if not item_title:
                    continue
                item_url = _absolute_url(paper_a["href"])
                authors_tag = li.find("i")
                authors = _norm(authors_tag.get_text(" ", strip=True)) if authors_tag else ""
                order += 1
                item_id = _stable_id(item_title, item_url, str(order))
                items.append({
                    "item_id": item_id,
                    "title": item_title,
                    "url": item_url,
                    "authors": authors,
                    "order": order,
                })

    return {
        "session_id": session_id,
        "start": start_time,
        "end": end_time,
        "track": track,
        "room": room,
        "title": title,
        "url": url,
        "items": items,
    }


def _group_by_heading(soup: BeautifulSoup) -> list[Tag]:
    """
    Find day-level <h2>/<h3> tags that contain a day/date hint and group
    the following siblings into a synthetic container element.
    Returns a list of Tag-like objects (we reuse BeautifulSoup Tag by
    wrapping in a new soup fragment).
    """
    from bs4 import BeautifulSoup as BS

    day_headers: list[Tag] = []
    for tag in soup.find_all(["h2", "h3"]):
        txt = _get_text(tag).lower()
        if any(w in txt for w in ("monday", "tuesday", "wednesday", "thursday", "friday",
                                   "saturday", "sunday", "february", "march", "day ")):
            day_headers.append(tag)

    if not day_headers:
        return []

    results = []
    for i, header in enumerate(day_headers):
        # Collect siblings until next header
        frag = BS("<div></div>", "html.parser")
        container = frag.div
        container.append(BS(str(header), "html.parser").find(header.name))
        sibling = header.next_sibling
        stop_tags = {h.name for h in day_headers}
        while sibling:
            if isinstance(sibling, Tag) and sibling.name in stop_tags:
                # Stop if we hit another header of the same type
                if sibling in day_headers[i + 1:]:
                    break
            try:
                container.append(BS(str(sibling), "html.parser"))
            except Exception:
                pass
            sibling = sibling.next_sibling
        results.append(container)
    return results


def _parse_day(day_el: Tag) -> dict:
    """Extract day label, date, and sessions from a day container element."""
    # Try to get the day label from first heading
    heading = day_el.find(["h1", "h2", "h3", "h4"])
    label = _get_text(heading) if heading else _get_text(day_el)[:60]
    date_str = _parse_date(label) or _parse_date(_get_text(day_el)[:200])

    day_id = _stable_id(label or "unknown", date_str)

    sessions = _parse_sessions(day_el)

    return {
        "day_id": day_id,
        "label": label,
        "date": date_str,
        "sessions": sessions,
    }


def _parse_sessions(container: Tag) -> list[dict]:
    """
    Try several structural patterns to extract sessions from a container.
    A 'session' is a block with a title + list of items (talks/papers).
    """
    sessions: list[dict] = []

    # Pattern 1: look for elements with class containing 'session'
    session_els = container.find_all(
        lambda t: t.name in ("div", "article", "section", "li")
        and any("session" in c.lower() for c in t.get("class", []))
    )

    if not session_els:
        # Pattern 2: look for <h3>/<h4> inside container as session headers
        session_els = _group_sessions_by_heading(container)

    for sel in session_els:
        s = _extract_session(sel)
        if s:
            sessions.append(s)

    return sessions


def _group_sessions_by_heading(container: Tag) -> list[Tag]:
    """Group content by h3/h4 headings as session boundaries."""
    from bs4 import BeautifulSoup as BS

    headers = container.find_all(["h3", "h4", "h5"])
    if not headers:
        return []

    results = []
    for i, header in enumerate(headers):
        frag = BS("<div class='session'></div>", "html.parser")
        c = frag.div
        c.append(BS(str(header), "html.parser").find(header.name))
        sibling = header.next_sibling
        stop_set = set(id(h) for h in headers[i + 1:])
        while sibling:
            if isinstance(sibling, Tag) and id(sibling) in stop_set:
                break
            try:
                c.append(BS(str(sibling), "html.parser"))
            except Exception:
                pass
            sibling = sibling.next_sibling
        results.append(c)
    return results


def _extract_session(sel: Tag) -> dict | None:
    """Extract session metadata and items from a session element."""
    # Session title
    title_tag = sel.find(["h2", "h3", "h4", "h5", "strong", "b"])
    title = _get_text(title_tag) if title_tag else _get_text(sel)[:120]
    if not title:
        return None

    # Session URL
    link_tag = sel.find("a", href=True)
    session_url = _absolute_url(link_tag["href"]) if link_tag else ""

    # Time / track info – look in the element text and in common wrapper attrs
    full_text = _get_text(sel)
    start_time, end_time = _parse_times(full_text)

    # Track label – e.g. "Session 1A", "Track 2B"
    # Require at least one letter so we don't accidentally capture time digits.
    track = ""
    track_m = re.search(r"\b(?:Session|Track)\s+([A-Z0-9]*[A-Z][A-Z0-9]*)\b", full_text)
    if track_m:
        track = track_m.group(1)

    # Room
    room = ""
    room_m = re.search(r"\b(?:Room|Hall|Salon)\s+([A-Z0-9 &]+?)(?:\s*[,.]|$)", full_text, re.IGNORECASE)
    if room_m:
        room = _norm(room_m.group(0))

    session_id = _stable_id(title, start_time, end_time, track, room, session_url)

    items = _extract_items(sel, title_tag)

    # A session without items is still valid (e.g. keynote / breaks)
    return {
        "session_id": session_id,
        "start": start_time,
        "end": end_time,
        "track": track,
        "room": room,
        "title": title,
        "url": session_url,
        "items": items,
    }


def _extract_items(sel: Tag, session_title_tag: Tag | None) -> list[dict]:
    """Extract talk/paper items from inside a session element."""
    items: list[dict] = []

    # Look for list items or paper-like divs
    candidates = sel.find_all(
        lambda t: t.name in ("li", "div", "article")
        and any(
            kw in " ".join(t.get("class", [])).lower()
            for kw in ("paper", "talk", "item", "entry", "presentation")
        )
    )

    if not candidates:
        # Fall back to all <li> elements
        candidates = sel.find_all("li")

    # Filter out the session heading itself
    session_title_text = _get_text(session_title_tag) if session_title_tag else ""

    order = 0
    for cand in candidates:
        item_title_tag = cand.find(["strong", "b", "h5", "h6", "a"])
        item_title = _get_text(item_title_tag) if item_title_tag else _get_text(cand)[:200]
        item_title = _norm(item_title)
        if not item_title or item_title == session_title_text:
            continue

        item_link = cand.find("a", href=True)
        item_url = _absolute_url(item_link["href"]) if item_link else ""

        # Authors: text after title, often in <em>/<i> or a separate span
        authors_tag = cand.find(["em", "i", "span"])
        authors = _get_text(authors_tag) if authors_tag else ""
        if authors == item_title:
            authors = ""

        order += 1
        item_id = _stable_id(item_title, item_url, str(order))
        items.append({
            "item_id": item_id,
            "title": item_title,
            "url": item_url,
            "authors": authors,
            "order": order,
        })

    return items


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Build NDSS 2026 program JSON dataset.")
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch the NDSS program HTML before (re)generating JSON.",
    )
    ap.add_argument(
        "--output",
        default=str(JSON_PATH),
        help="Path to write the output JSON (default: data/ndss2026.program.json).",
    )
    ap.add_argument(
        "--raw",
        default=str(RAW_HTML_PATH),
        help="Path to the cached raw HTML (default: data/ndss2026.raw.html).",
    )
    args = ap.parse_args()

    raw_path = Path(args.raw)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Step 1 – obtain HTML
    # ------------------------------------------------------------------
    if args.refresh or not raw_path.exists():
        if args.refresh:
            print(f"[refresh] Fetching {SOURCE_URL} …")
        else:
            print(f"[init] No cached HTML found; fetching {SOURCE_URL} …")
        html = fetch_html(SOURCE_URL)
        raw_path.write_text(html, encoding="utf-8")
        print(f"[ok] HTML cached to {raw_path} ({len(html):,} bytes)")
    else:
        html = raw_path.read_text(encoding="utf-8")
        print(f"[cache] Using cached HTML from {raw_path} ({len(html):,} bytes)")

    # ------------------------------------------------------------------
    # Step 2 – parse
    # ------------------------------------------------------------------
    print("[parse] Parsing HTML …")
    days = parse_program(html)

    raw_sha = hashlib.sha256(html.encode()).hexdigest()

    output = {
        "meta": {
            "source_url": SOURCE_URL,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "raw_html_sha256": raw_sha,
        },
        "days": days,
    }

    # ------------------------------------------------------------------
    # Step 3 – write
    # ------------------------------------------------------------------
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    total_sessions = sum(len(d["sessions"]) for d in days)
    total_items = sum(len(s["items"]) for d in days for s in d["sessions"])
    print(
        f"[ok] Wrote {out_path}  "
        f"({len(days)} days, {total_sessions} sessions, {total_items} items)"
    )


if __name__ == "__main__":
    main()
