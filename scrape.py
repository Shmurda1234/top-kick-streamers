#!/usr/bin/env python3
"""
StreamerPlus — Kick Streamers Scraper
=====================================

Scrapes the top 20 most-followed Kick streamers from streamscharts.com/top-channels?platform=kick

Output: data/top-kick-streamers.json

Design notes:
- Uses Playwright (Streams Charts is a Vue.js SPA, plain requests/BeautifulSoup won't work)
- Anonymous access (no login) — gives us the top 20 reliably
- Polite delays + realistic user-agent + waits for content to render
- Validation gate refuses to overwrite good data with bad data
- Diagnostic HTML dump on failure for debugging
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

URL = "https://streamscharts.com/top-channels?platform=kick"
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "top-kick-streamers.json"
DIAGNOSTIC_HTML_PATH = Path(__file__).parent.parent / "data" / "last-debug.html"

TOP_N = 20

# Validation gates — refuse to write if scrape looks broken
MIN_ROWS = 15  # Should be 20, but allow some slack if Streams Charts is having issues
MIN_TOP_CHANNEL_FOLLOWERS = 1_000_000  # Top streamer should have >1M followers (sanity check)

# Realistic user agent — Streams Charts may sniff for headless markers
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15"
)

PAGE_LOAD_TIMEOUT_MS = 30_000
TABLE_WAIT_TIMEOUT_MS = 15_000

# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------

@dataclass
class KickChannel:
    rank: int
    name: str
    slug: str
    url: str
    avatar: Optional[str]
    country: Optional[str]
    country_code: Optional[str]
    language: Optional[str]
    primary_game: Optional[str]
    primary_game_icon: Optional[str]
    followers: Optional[int]
    peak_viewers: Optional[int]
    peak_viewers_date: Optional[str]
    latest_stream: Optional[str]


@dataclass
class ScrapeResult:
    scraped_at: str
    source: str
    channels: list[KickChannel] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def parse_followers(raw: str) -> Optional[int]:
    """
    Streams Charts uses non-breaking spaces (\xa0) and regular spaces as
    thousand separators: '3 876 484' or '3\xa0876\xa0484' -> 3876484.
    """
    if not raw or raw.strip() in ("", "--", "—", "-"):
        return None
    # Strip all whitespace (including non-breaking spaces) and commas
    cleaned = re.sub(r"[\s,]+", "", raw.strip())
    if not cleaned.isdigit():
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_peak_with_date(raw: str) -> tuple[Optional[int], Optional[str]]:
    """
    Streams Charts shows peak viewers as 'Oct 19, 2025  4 001 560' (date + number).
    We want to split these into peak_viewers=4001560, peak_viewers_date='Oct 19, 2025'.
    """
    if not raw or raw.strip() in ("", "--", "—", "-"):
        return (None, None)

    # Try to find a date pattern at the start: "Mon DD, YYYY"
    date_match = re.match(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}",
        raw.strip(),
    )
    if date_match:
        date_str = date_match.group(0)
        remainder = raw.strip()[len(date_str):].strip()
        viewers = parse_followers(remainder)
        return (viewers, date_str)

    # No date — just try parsing the whole thing as a number
    viewers = parse_followers(raw)
    return (viewers, None)


def is_locked_cell(cell_html: str) -> bool:
    """Detect 'PRO users only' lock icons in cells."""
    return "lock-fill" in cell_html or "available for" in cell_html


def extract_alt_text(img_html: str) -> Optional[str]:
    """Extract alt attribute from an img tag."""
    match = re.search(r'alt="([^"]*)"', img_html)
    return match.group(1) if match else None


# -----------------------------------------------------------------------------
# Main scraper
# -----------------------------------------------------------------------------

def scrape() -> ScrapeResult:
    result = ScrapeResult(
        scraped_at=datetime.now(timezone.utc).isoformat(),
        source="streamscharts.com",
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        # Anti-detection: hide webdriver flag
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        print(f"[scrape] Navigating to {URL}", flush=True)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print(f"[scrape] WARN: page load timeout, continuing anyway", flush=True)

        # Wait for the table to render. Streams Charts uses Vue.js so the table
        # rows appear after JS runs. We wait for actual data rows.
        print("[scrape] Waiting for table rows to render...", flush=True)
        try:
            page.wait_for_selector("table tbody tr", timeout=TABLE_WAIT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[scrape] WARN: table didn't render within timeout, dumping HTML", flush=True)
            DIAGNOSTIC_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
            DIAGNOSTIC_HTML_PATH.write_text(page.content(), encoding="utf-8")
            browser.close()
            return result

        # Give Vue.js a moment to finish populating cells with data
        time.sleep(3)

        # Save full HTML for debugging
        DIAGNOSTIC_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
        DIAGNOSTIC_HTML_PATH.write_text(page.content(), encoding="utf-8")

        # Extract rows using JS evaluation — more reliable than BeautifulSoup
        # because we can target the actual rendered DOM with structured queries
        print("[scrape] Extracting rows from DOM...", flush=True)

        rows_data = page.evaluate("""
            () => {
                const rows = Array.from(document.querySelectorAll('table tbody tr'));
                return rows.map(row => {
                    const cells = Array.from(row.querySelectorAll('td'));
                    // Get HTML and text for each cell so the Python side can parse smartly
                    return cells.map(cell => ({
                        text: cell.innerText.trim(),
                        html: cell.innerHTML
                    }));
                });
            }
        """)

        print(f"[scrape] Got {len(rows_data)} raw rows from DOM", flush=True)

        for idx, cells in enumerate(rows_data, start=1):
            if idx > TOP_N:
                break

            # Skip rows that are blank/locked (rows 21+ for anonymous users)
            # These rows have empty text content or just a rank number
            non_empty_cells = [c for c in cells if c["text"].strip()]
            if len(non_empty_cells) < 4:
                # This is a locked/empty row — skip silently
                continue

            try:
                channel = parse_row(idx, cells)
                if channel:
                    result.channels.append(channel)
            except Exception as exc:
                print(f"[scrape] Failed to parse row {idx}: {exc}", flush=True)
                continue

        browser.close()

    return result


def parse_row(rank: int, cells: list[dict]) -> Optional[KickChannel]:
    """
    Parse one row of cell data into a KickChannel.

    Column layout observed (15 cells total):
      0: rank number
      1: avatar (img)
      2: channel name + language tag  (e.g. "westcol ES")
      3: verification/partner icon
      4: gender icon (or lock for PRO-only)
      5: country flag (or lock for PRO-only)
      6: language code (e.g. "es")
      7: primary game icon + link
      8: (empty / spacer)
      9: followers count
     10: peak viewers (with date)
     11: latest stream (relative time)
     12: streams on other platforms
     13: popular games list

    We only care about: 1 (avatar), 2 (name+lang), 5 (country), 6 (language),
    7 (primary game), 9 (followers), 10 (peak), 11 (latest stream).
    """
    if len(cells) < 12:
        # Not enough cells — locked or malformed row
        return None

    # --- Avatar (cell 1) ---
    avatar_match = re.search(r'<img[^>]+src="([^"]+)"', cells[1]["html"])
    avatar = avatar_match.group(1) if avatar_match else None

    # --- Channel name + slug + language tag (cell 2) ---
    # Example: '<a href="/channels/westcol?platform=kick" title="westcol">westcol</a> ES'
    name_link_match = re.search(
        r'<a href="/channels/([^?"]+)\?platform=kick"[^>]*>([^<]+)</a>',
        cells[2]["html"],
    )
    if not name_link_match:
        return None
    slug = name_link_match.group(1)
    name = name_link_match.group(2).strip()

    # --- Country (cell 5) ---
    country = None
    country_code = None
    if not is_locked_cell(cells[5]["html"]):
        # The img tag may have attributes in any order, so look for src and alt separately
        src_match = re.search(r'src="[^"]*flags/([a-z]+)\.svg', cells[5]["html"])
        alt_match = re.search(r'alt="([^"]+)"', cells[5]["html"])
        if src_match:
            country_code = src_match.group(1).upper()
        if alt_match:
            country = alt_match.group(1)

    # --- Language (cell 6) ---
    language = cells[6]["text"].strip().upper() if cells[6]["text"].strip() else None

    # --- Primary game (cell 7) ---
    primary_game = None
    primary_game_icon = None
    # The img tag may have src and alt in any order
    src_match = re.search(r'<img[^>]+src="([^"]+)"', cells[7]["html"])
    alt_match = re.search(r'<img[^>]+alt="([^"]+)"', cells[7]["html"])
    if alt_match:
        primary_game = alt_match.group(1)
    if src_match:
        primary_game_icon = src_match.group(1)

    # --- Followers (cell 9) ---
    followers = parse_followers(cells[9]["text"])

    # --- Peak viewers (cell 10) — text format: "Oct 19, 2025  4 001 560" ---
    peak_viewers, peak_viewers_date = parse_peak_with_date(cells[10]["text"])

    # --- Latest stream (cell 11) ---
    latest_stream = cells[11]["text"].strip() or None

    return KickChannel(
        rank=rank,
        name=name,
        slug=slug,
        url=f"https://kick.com/{slug}",
        avatar=avatar,
        country=country,
        country_code=country_code,
        language=language,
        primary_game=primary_game,
        primary_game_icon=primary_game_icon,
        followers=followers,
        peak_viewers=peak_viewers,
        peak_viewers_date=peak_viewers_date,
        latest_stream=latest_stream,
    )


# -----------------------------------------------------------------------------
# Validation + write
# -----------------------------------------------------------------------------

def validate(result: ScrapeResult) -> tuple[bool, str]:
    """Sanity-check the scrape before overwriting prior good data."""
    if len(result.channels) < MIN_ROWS:
        return (False, f"only {len(result.channels)} channels (min {MIN_ROWS})")

    top = result.channels[0]
    if top.followers is None or top.followers < MIN_TOP_CHANNEL_FOLLOWERS:
        return (
            False,
            f"top channel {top.name} has {top.followers} followers "
            f"(expected ≥{MIN_TOP_CHANNEL_FOLLOWERS:,})",
        )

    # Sanity: ranks should be 1..N with no gaps
    expected_ranks = list(range(1, len(result.channels) + 1))
    actual_ranks = [c.rank for c in result.channels]
    if actual_ranks != expected_ranks:
        return (False, f"ranks aren't sequential: {actual_ranks}")

    return (True, "ok")


def write_json(result: ScrapeResult) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scraped_at": result.scraped_at,
        "source": result.source,
        "channels": [asdict(c) for c in result.channels],
    }
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[scrape] Wrote {len(result.channels)} channels to {OUTPUT_PATH}", flush=True)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> int:
    print(f"[scrape] Starting kick-streamers scrape at {datetime.now(timezone.utc).isoformat()}", flush=True)

    result = scrape()
    print(f"[scrape] Captured {len(result.channels)} channels", flush=True)

    ok, reason = validate(result)
    if not ok:
        print(f"[scrape] VALIDATION FAILED: {reason}", flush=True)
        print(f"[scrape] Refusing to overwrite existing data. Check {DIAGNOSTIC_HTML_PATH}", flush=True)
        return 1

    write_json(result)
    print(f"[scrape] Done. Top channel: {result.channels[0].name} "
          f"({result.channels[0].followers:,} followers)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
