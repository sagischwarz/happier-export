#!/usr/bin/env python3
"""
Export meditation history from Ten Percent Happier (Happier Meditation).

Authenticates via the web login form, fetches your full meditation history,
and writes it to JSON and CSV files.

Usage:
    python export_history.py --email you@example.com --password yourpassword
    python export_history.py   # will prompt interactively

Requirements:
    pip install requests beautifulsoup4 lxml
"""

import argparse
import csv
import getpass
import http.cookiejar
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://my.meditatehappier.com"
SIGN_IN_PATH = "/v2/sign_in"
HISTORY_PATH = "/v2/history"
COOKIE_FILE = os.path.join(Path.home(), ".happier_session_cookies")


def _set_base_url(url):
    global BASE_URL
    BASE_URL = url


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def create_session():
    """Create a requests session with browser-like defaults."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def save_cookies(session, path=COOKIE_FILE):
    """Save session cookies to a file for reuse across runs."""
    jar = http.cookiejar.MozillaCookieJar(path)
    for cookie in session.cookies:
        jar.set_cookie(cookie)
    jar.save(ignore_discard=True, ignore_expires=True)
    os.chmod(path, 0o600)


def load_cookies(session, path=COOKIE_FILE):
    """Load saved cookies into the session. Returns True if cookies were loaded."""
    if not os.path.exists(path):
        return False
    jar = http.cookiejar.MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError, Exception):
        return False
    for cookie in jar:
        session.cookies.set_cookie(cookie)
    return True


def session_is_valid(session):
    """Check whether the saved session is still authenticated."""
    resp = session.get(
        urljoin(BASE_URL, HISTORY_PATH),
        headers={"Accept": "text/html,application/xhtml+xml"},
        allow_redirects=False,
    )
    # Authenticated: 200. Expired: 302 to /sign_in or 401.
    return resp.status_code == 200


def clear_cookies(path=COOKIE_FILE):
    """Remove the saved cookie file."""
    if os.path.exists(path):
        os.remove(path)


def login(session, email, password):
    """
    Log in via the Rails form at /v2/sign_in.

    Returns True on success, raises on failure.
    """
    print("Fetching sign-in page...")
    resp = session.get(
        urljoin(BASE_URL, SIGN_IN_PATH),
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    # Extract the CSRF token from the hidden form field
    csrf_input = soup.find("input", attrs={"name": "authenticity_token"})
    if not csrf_input or not csrf_input.get("value"):
        csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
        if not csrf_meta or not csrf_meta.get("content"):
            raise RuntimeError("Could not find CSRF token on the sign-in page.")
        csrf_token = csrf_meta["content"]
    else:
        csrf_token = csrf_input["value"]

    print("Logging in...")
    resp = session.post(
        urljoin(BASE_URL, SIGN_IN_PATH),
        data={
            "authenticity_token": csrf_token,
            "session[email]": email,
            "session[password]": password,
            "commit": "Sign In",
        },
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        allow_redirects=True,
    )

    if "/sign_in" in resp.url:
        error_soup = BeautifulSoup(resp.text, "lxml")
        error_div = error_soup.find(attrs={"role": "alert"})
        error_msg = error_div.get_text(strip=True) if error_div else "Unknown error"
        raise RuntimeError(f"Login failed: {error_msg}")

    print(f"Logged in successfully (redirected to {resp.url})")
    return True


# ---------------------------------------------------------------------------
# Parsing history entries from the server-rendered HTML
#
# Each entry is a <div> with these data attributes on the row element:
#   data-history-deletion-uuid-value       - session UUID
#   data-history-deletion-source-value     - teacher / source name
#   data-history-deletion-start-date-value - ISO 8601 start timestamp
#   data-history-deletion-end-date-value   - ISO 8601 end timestamp
#
# The visible text has:
#   <p class="text-base font-bold ...">Title</p>     (e.g. "Meditation")
#   <p class="text-sm ...">Subtitle</p>               (e.g. "Plum Village")
#   An optional <a href="/v2/singles/..."> wrapper
#
# Pagination is cursor-based infinite scroll:
#   <div id="history-sentinel" data-history-infinite-scroll-url-value="/v2/history?before=...">
# ---------------------------------------------------------------------------


def parse_history_page(html):
    """
    Parse a history page and return (entries, next_url).

    entries: list of dicts with session data
    next_url: relative URL for the next page, or None if this is the last page
    """
    soup = BeautifulSoup(html, "lxml")
    entries = []

    for row in soup.select("[data-history-deletion-uuid-value]"):
        entry = parse_entry(row)
        if entry:
            entries.append(entry)

    # Extract the infinite-scroll cursor for the next page
    sentinel = soup.select_one(
        "[data-history-infinite-scroll-url-value]"
    )
    next_url = None
    if sentinel:
        next_url = sentinel.get("data-history-infinite-scroll-url-value")

    return entries, next_url


def parse_entry(row):
    """Extract a meditation session from a history row element."""
    uuid = row.get("data-history-deletion-uuid-value")
    source = row.get("data-history-deletion-source-value", "")
    start_str = row.get("data-history-deletion-start-date-value", "")
    end_str = row.get("data-history-deletion-end-date-value", "")

    # Parse timestamps and compute duration
    start_dt = parse_iso(start_str)
    end_dt = parse_iso(end_str)

    duration_minutes = None
    if start_dt and end_dt:
        delta = (end_dt - start_dt).total_seconds()
        duration_minutes = round(delta / 60, 1)

    # Title and subtitle from the visible content area
    content = row.select_one("[data-swipe-row-target='content']") or row
    title_el = content.select_one("p.font-bold")
    subtitle_el = content.select_one("p.text-sm")
    title = title_el.get_text(strip=True) if title_el else ""
    subtitle = subtitle_el.get_text(strip=True) if subtitle_el else ""

    # Determine content type from link href
    content_type = classify_type(content, title)

    # Link to the content (if any)
    link = content.select_one("a[href*='/v2/']")
    link_href = link.get("href", "") if link else ""

    return {
        "uuid": uuid,
        "date": start_str,
        "date_end": end_str,
        "title": title or subtitle or source,
        "source": source,
        "duration_minutes": duration_minutes,
        "type": content_type,
        "link": link_href,
    }


def parse_iso(s):
    """Parse an ISO 8601 timestamp string, tolerant of missing timezone."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_type(content_el, title):
    """Guess the content type from the link href or title text."""
    link = content_el.select_one("a[href]")
    href = link.get("href", "") if link else ""

    if "/unguided_timer" in href or "unguided" in title.lower():
        return "unguided"
    if "/course_session" in href:
        return "course"
    if "/sleep" in href or "sleep" in title.lower():
        return "sleep"
    if "/podcast" in href:
        return "podcast"
    if "/practice_in_action" in href:
        return "practice_in_action"
    if "/wisdom_clip" in href:
        return "wisdom_clip"
    if "/singles/" in href or "/meditation" in href:
        return "meditation"

    # Default — most entries are meditations
    t = title.lower()
    if "timer" in t:
        return "unguided"
    if "sleep" in t:
        return "sleep"
    return "meditation"


# ---------------------------------------------------------------------------
# Fetching all history pages
# ---------------------------------------------------------------------------


def fetch_page(session, url):
    """Fetch a single history page and return (entries, next_url)."""
    resp = session.get(
        urljoin(BASE_URL, url),
        headers={"Accept": "text/html,application/xhtml+xml"},
        allow_redirects=False,
    )

    if resp.status_code in (301, 302):
        location = resp.headers.get("location", "")
        if "/sign_in" in location:
            raise RuntimeError("Session expired or not authenticated.")
        return [], None

    if resp.status_code != 200:
        return [], None

    return parse_history_page(resp.text)


def export_all_history(session):
    """Fetch all pages of meditation history using cursor-based pagination."""
    all_sessions = []
    url = HISTORY_PATH
    page = 1

    print("\nFetching meditation history...")

    while url:
        print(f"  Page {page}...", end=" ", flush=True)

        entries, next_url = fetch_page(session, url)
        print(f"{len(entries)} sessions")

        if not entries:
            if page == 1:
                print("  No sessions found.")
            break

        all_sessions.extend(entries)

        if not next_url:
            print("  Reached end of history.")
            break

        url = next_url
        page += 1

    return all_sessions


# ---------------------------------------------------------------------------
# Export to files
# ---------------------------------------------------------------------------


def write_json(sessions, path="meditation_history.json"):
    """Write sessions to a JSON file."""
    output = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_sessions": len(sessions),
        "sessions": sessions,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"  JSON: {path} ({len(sessions)} sessions)")


def write_csv(sessions, path="meditation_history.csv"):
    """Write sessions to a CSV file."""
    fieldnames = [
        "date", "date_end", "title", "source",
        "duration_minutes", "type", "uuid", "link",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for s in sessions:
            writer.writerow({k: s.get(k) for k in fieldnames})

    print(f"  CSV:  {path} ({len(sessions)} rows)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Export your Ten Percent Happier meditation history."
    )
    parser.add_argument("--email", help="Account email address")
    parser.add_argument("--password", help="Account password")
    parser.add_argument(
        "--output", "-o",
        default="meditation_history",
        help="Output filename prefix (default: meditation_history)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"Base URL (default: {BASE_URL})",
    )
    parser.add_argument(
        "--min-duration",
        type=float,
        default=1.0,
        help="Minimum session duration in minutes (default: 1.0, use 0 to include all)",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Clear saved session and exit",
    )
    args = parser.parse_args()

    if args.base_url:
        _set_base_url(args.base_url.rstrip("/"))

    if args.logout:
        clear_cookies()
        print("Saved session cleared.")
        sys.exit(0)

    session = create_session()

    # Try reusing a saved session first
    authenticated = False
    if load_cookies(session):
        print("Loaded saved session, checking validity...")
        if session_is_valid(session):
            print("Session is still valid.")
            authenticated = True
        else:
            print("Saved session has expired.")
            session.cookies.clear()
            clear_cookies()

    if not authenticated:
        email = args.email or input("Email: ")
        password = args.password or getpass.getpass("Password: ")

        if not email or not password:
            print("Error: email and password are required.", file=sys.stderr)
            sys.exit(1)

        try:
            login(session, email, password)
            save_cookies(session)
            print(f"Session saved to {COOKIE_FILE}")
        except RuntimeError as e:
            print(f"\nError: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        sessions = export_all_history(session)
    except RuntimeError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    if not sessions:
        print("\nNo meditation history found.")
        sys.exit(0)

    total_fetched = len(sessions)
    if args.min_duration > 0:
        sessions = [
            s for s in sessions
            if (s.get("duration_minutes") or 0) >= args.min_duration
        ]
        skipped = total_fetched - len(sessions)
        if skipped:
            print(f"\nFiltered out {skipped} sessions shorter than {args.min_duration} min.")

    if not sessions:
        print("\nNo sessions match the minimum duration filter.")
        sys.exit(0)

    print(f"\nFound {len(sessions)} sessions. Writing files...")
    json_path = f"{args.output}.json"
    csv_path = f"{args.output}.csv"

    write_json(sessions, json_path)
    write_csv(sessions, csv_path)

    # Update saved cookies (server may have rotated the session)
    save_cookies(session)

    # Quick summary
    dates = [s["date"] for s in sessions if s.get("date")]
    durations = [s["duration_minutes"] for s in sessions if s.get("duration_minutes")]
    if dates:
        print(f"\n  Date range: {dates[-1][:10]} → {dates[0][:10]}")
    if durations:
        print(f"  Total meditation time: {sum(durations):.0f} minutes ({sum(durations)/60:.1f} hours)")

    print("\nDone!")


if __name__ == "__main__":
    main()
