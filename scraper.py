import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# ============================================================
# CONFIG
# ============================================================

HOME_URL = "https://xoilacz.vip/"

OUTPUT_FILE = Path("matches.json")

# Không nên quá cao trên GitHub Actions
MAX_WORKERS = 3

PAGE_TIMEOUT = 15000

WAIT_AFTER_LOAD = 1000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}


# ============================================================
# HOME PAGE
# ============================================================

def get_home_page():

    print(
        "Downloading home page...",
        flush=True
    )

    response = requests.get(
        HOME_URL,
        headers=HEADERS,
        timeout=20
    )

    response.raise_for_status()

    print(
        f"Home page status: {response.status_code}",
        flush=True
    )

    return response.text


# ============================================================
# NORMALIZE URL
# ============================================================

def normalize_match_url(url):

    # /link/0
    # /link/1
    # /link/2
    # ...

    url = re.sub(
        r"/link/\d+/?$",
        "/",
        url
    )

    url = url.rstrip("/") + "/"

    return url


# ============================================================
# FIND EVENTS
# ============================================================

def find_match_urls(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    matches = set()

    for a in soup.select("a[href]"):

        href = a.get("href")

        if not href:
            continue

        if "/truc-tiep/" not in href:
            continue

        full_url = urljoin(
            HOME_URL,
            href
        )

        full_url = normalize_match_url(
            full_url
        )

        matches.add(full_url)

    return sorted(matches)


# ============================================================
# SCRAPE ONE MATCH
# ============================================================

def scrape_one_match(
    match_url,
    index,
    total
):

    print(
        f"[{index}/{total}] START {match_url}",
        flush=True
    )

    try:

        # ----------------------------------------------------
        # Mỗi worker tự tạo Playwright
        # ----------------------------------------------------

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            context = browser.new_context(
                user_agent=HEADERS["User-Agent"]
            )

            page = context.new_page()

            # ------------------------------------------------
            # LOAD PAGE
            # ------------------------------------------------

            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

            page.wait_for_timeout(
                WAIT_AFTER_LOAD
            )

            # ------------------------------------------------
            # TITLE
            # ------------------------------------------------

            title = "Unknown Match"

            try:

                h1 = page.locator(
                    "h1"
                ).first

                if h1.count() > 0:

                    text = h1.inner_text(
                        timeout=2000
                    ).strip()

                    if text:
                        title = text

            except Exception:
                pass

            if title == "Unknown Match":

                try:

                    page_title = page.title()

                    if page_title:
                        title = page_title.strip()

                except Exception:
                    pass

            # ------------------------------------------------
            # PLAYER LINKS
            # ------------------------------------------------

            elements = page.locator(
                "a.player-link"
            ).all()

            players = []

            for player_index, element in enumerate(
                elements
            ):

                try:
                    name = element.inner_text(
                        strip=True
                    )
                except Exception:
                    name = ""

                try:
                    server = element.get_attribute(
                        "title"
                    )
                except Exception:
                    server = ""

                try:
                    data_link = element.get_attribute(
                        "data-link"
                    )
                except Exception:
                    data_link = ""

                try:
                    href = element.get_attribute(
                        "href"
                    )
                except Exception:
                    href = ""

                players.append({
                    "index": player_index,
                    "name": name,
                    "server": server,
                    "data_link": data_link,
                    "href": (
                        urljoin(
                            HOME_URL,
                            href
                        )
                        if href
                        else ""
                    )
                })

            # ------------------------------------------------
            # CLOSE
            # ------------------------------------------------

            context.close()
            browser.close()

            print(
                f"[{index}/{total}] OK "
                f"| players={len(players)} "
                f"| {title}",
                flush=True
            )

            return {
                "title": title,
                "url": match_url,
                "players": players
            }

    except Exception as error:

        print(
            f"[{index}/{total}] ERROR",
            flush=True
        )

        print(
            f"  URL: {match_url}",
            flush=True
        )

        print(
            f"  ERROR: {type(error).__name__}: {error}",
            flush=True
        )

        return None


# ============================================================
# SAVE JSON
# ============================================================

def save_results(results):

    data = {
        "generated_at": int(
            time.time()
        ),
        "match_count": len(results),
        "matches": results
    }

    OUTPUT_FILE.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    html = get_home_page()

    # --------------------------------------------------------
    # EVENTS
    # --------------------------------------------------------

    match_urls = find_match_urls(
        html
    )

    print(
        f"Found {len(match_urls)} unique events",
        flush=True
    )

    if not match_urls:

        print(
            "No events found.",
            flush=True
        )

        return

    # --------------------------------------------------------
    # SCRAPE
    # --------------------------------------------------------

    results = []

    print(
        f"Using {MAX_WORKERS} workers...",
        flush=True
    )

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {}

        for index, match_url in enumerate(
            match_urls,
            1
        ):

            future = executor.submit(
                scrape_one_match,
                match_url,
                index,
                len(match_urls)
            )

            futures[future] = match_url

        for future in as_completed(
            futures
        ):

            try:

                result = future.result()

                if result is not None:

                    results.append(
                        result
                    )

            except Exception as error:

                print(
                    "Worker exception:",
                    error,
                    flush=True
                )

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    results.sort(
        key=lambda x: x["url"]
    )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    save_results(
        results
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    total_players = sum(
        len(match["players"])
        for match in results
    )

    events_with_players = sum(
        1
        for match in results
        if len(match["players"]) > 0
    )

    print()
    print("=" * 70)
    print("SCRAPER COMPLETE")
    print("=" * 70)

    print(
        f"Events found:       {len(match_urls)}"
    )

    print(
        f"Events processed:   {len(results)}"
    )

    print(
        f"Events with player: {events_with_players}"
    )

    print(
        f"Total players:      {total_players}"
    )

    print(
        f"Output file:        {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
