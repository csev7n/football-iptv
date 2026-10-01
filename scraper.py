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

# Số trang chạy đồng thời
MAX_WORKERS = 5

# Timeout mỗi trang
PAGE_TIMEOUT = 15000

# Chờ JavaScript sau khi trang load
WAIT_AFTER_LOAD = 800

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
    print("Downloading home page...", flush=True)

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
# URL NORMALIZATION
# ============================================================

def normalize_match_url(url):
    """
    /truc-tiep/match/
    /truc-tiep/match/link/0
    /truc-tiep/match/link/1

    đều trở thành:

    /truc-tiep/match/
    """

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
# PLAYWRIGHT WORKER
# ============================================================

def scrape_one_match(browser, match_url, index, total):

    print(
        f"[{index}/{total}] START {match_url}",
        flush=True
    )

    # Mỗi worker có context riêng
    context = browser.new_context(
        user_agent=HEADERS["User-Agent"]
    )

    page = context.new_page()

    try:

        page.goto(
            match_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        # Cho JS có thời gian render player
        page.wait_for_timeout(
            WAIT_AFTER_LOAD
        )

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # PLAYERS
        # ----------------------------------------------------

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
            f"[{index}/{total}] ERROR "
            f"| {match_url} "
            f"| {error}",
            flush=True
        )

        return None

    finally:

        try:
            page.close()
        except Exception:
            pass

        try:
            context.close()
        except Exception:
            pass


# ============================================================
# SAVE
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

    print(
        f"Saved {len(results)} matches "
        f"to {OUTPUT_FILE}",
        flush=True
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # 1. HOME PAGE
    # --------------------------------------------------------

    html = get_home_page()

    # --------------------------------------------------------
    # 2. EVENTS
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
    # 3. PLAYWRIGHT
    # --------------------------------------------------------

    results = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        # ----------------------------------------------------
        # 4. PARALLEL SCRAPING
        # ----------------------------------------------------

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
                    browser,
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
                        results.append(result)

                except Exception as error:

                    print(
                        "Worker error:",
                        error,
                        flush=True
                    )

        browser.close()

    # --------------------------------------------------------
    # 5. SORT RESULTS
    # --------------------------------------------------------

    results.sort(
        key=lambda item: item["url"]
    )

    # --------------------------------------------------------
    # 6. SAVE
    # --------------------------------------------------------

    save_results(
        results
    )

    # --------------------------------------------------------
    # 7. SUMMARY
    # --------------------------------------------------------

    total_players = sum(
        len(match["players"])
        for match in results
    )

    events_with_players = sum(
        1
        for match in results
        if match["players"]
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


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
