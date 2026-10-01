import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

HOME_URL = "https://xoilacz.vip/"

OUTPUT_FILE = Path("matches.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}

PAGE_TIMEOUT = 30000

WAIT_AFTER_PAGE_LOAD = 2000


# ============================================================
# GET HOME PAGE
# ============================================================

def get_home_page():
    print("Downloading home page...")

    response = requests.get(
        HOME_URL,
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    print(
        f"Home page status: {response.status_code}"
    )

    return response.text


# ============================================================
# FIND MATCH URLS
# ============================================================

def normalize_match_url(url):
    """
    Chuyển:

        /truc-tiep/match/
        /truc-tiep/match/link/0
        /truc-tiep/match/link/1

    thành cùng một URL trận.
    """

    # Loại /link/0, /link/1, /link/2...
    url = re.sub(
        r"/link/\d+/?$",
        "/",
        url
    )

    # Chuẩn hóa //
    url = re.sub(
        r"/+$",
        "/",
        url
    )

    return url


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

        # Chỉ lấy trang trận
        if "/truc-tiep/" not in href:
            continue

        # Chuyển relative URL thành absolute URL
        full_url = urljoin(
            HOME_URL,
            href
        )

        # Loại /link/N
        full_url = normalize_match_url(
            full_url
        )

        matches.add(full_url)

    return sorted(matches)


# ============================================================
# EXTRACT MATCH TITLE
# ============================================================

def get_match_title(page):
    """
    Cố gắng lấy tiêu đề trận.
    Có nhiều fallback để tránh phụ thuộc một selector duy nhất.
    """

    title = page.title()

    if title:
        title = title.strip()

        if title:
            return title

    selectors = [
        "h1",
        ".match-title",
        ".title",
        ".event-title"
    ]

    for selector in selectors:

        element = page.locator(
            selector
        ).first

        try:

            if element.count() > 0:

                text = element.inner_text(
                    strip=True
                )

                if text:
                    return text

        except Exception:
            pass

    return "Unknown Match"


# ============================================================
# GET PLAYERS FROM MATCH PAGE
# ============================================================

def get_players(page, match_url):

    print()
    print("=" * 70)
    print("MATCH:")
    print(match_url)
    print("=" * 70)

    try:

        page.goto(
            match_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

    except PlaywrightTimeoutError:

        print("WARNING: Page load timeout")

        # Có thể trang vẫn đã load đủ DOM.
        # Tiếp tục xử lý thay vì bỏ luôn.

    except Exception as error:

        print(
            "ERROR loading page:",
            error
        )

        return None

    # Chờ JavaScript của trang hoàn thành
    page.wait_for_timeout(
        WAIT_AFTER_PAGE_LOAD
    )

    match_title = get_match_title(
        page
    )

    print(
        "Title:",
        match_title
    )

    elements = page.locator(
        "a.player-link"
    ).all()

    print(
        f"Players found: {len(elements)}"
    )

    players = []

    for index, element in enumerate(
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

        player = {
            "index": index,
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
        }

        players.append(
            player
        )

        print(
            f"  [{index}] "
            f"{name} | "
            f"{server} | "
            f"data-link={data_link}"
        )

    return {
        "title": match_title,
        "url": match_url,
        "players": players
    }


# ============================================================
# SAVE JSON
# ============================================================

def save_results(matches):

    data = {
        "generated_at": int(
            time.time()
        ),
        "match_count": len(matches),
        "matches": matches
    }

    OUTPUT_FILE.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print()
    print(
        f"Saved {len(matches)} matches to "
        f"{OUTPUT_FILE}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # 1. Download homepage
    # --------------------------------------------------------

    html = get_home_page()

    # --------------------------------------------------------
    # 2. Find unique match URLs
    # --------------------------------------------------------

    match_urls = find_match_urls(
        html
    )

    print()
    print(
        f"Found {len(match_urls)} unique matches"
    )

    for index, url in enumerate(
        match_urls,
        1
    ):

        print(
            f"{index}. {url}"
        )

    if not match_urls:

        print()
        print(
            "No matches found."
        )

        return

    # --------------------------------------------------------
    # 3. Open Chromium
    # --------------------------------------------------------

    results = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            user_agent=HEADERS["User-Agent"]
        )

        # ----------------------------------------------------
        # 4. Process every match
        # ----------------------------------------------------

        for index, match_url in enumerate(
            match_urls,
            1
        ):

            print()
            print(
                f"[{index}/{len(match_urls)}]"
            )

            result = get_players(
                page,
                match_url
            )

            if result is None:
                continue

            results.append(
                result
            )

            # Nghỉ một chút giữa các trang
            time.sleep(0.5)

        browser.close()

    # --------------------------------------------------------
    # 5. Save result
    # --------------------------------------------------------

    save_results(
        results
    )

    # --------------------------------------------------------
    # 6. Summary
    # --------------------------------------------------------

    total_players = sum(
        len(match["players"])
        for match in results
    )

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print(
        "Matches:",
        len(results)
    )

    print(
        "Players:",
        total_players
    )

    print(
        "Output:",
        OUTPUT_FILE
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
