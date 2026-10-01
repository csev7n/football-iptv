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

HOME_URL = "https://YOUR-AUTHORIZED-DOMAIN.example/"

OUTPUT_FILE = Path("matches.json")

MAX_WORKERS = 3

PAGE_TIMEOUT = 20000

PLAYER_WAIT_MS = 2000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}


# ============================================================
# GET HOME PAGE
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
# NORMALIZE URL
# ============================================================

def normalize_match_url(url):

    url = re.sub(
        r"/link/\d+/?$",
        "/",
        url
    )

    return url.rstrip("/") + "/"


# ============================================================
# FIND MATCHES
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
# FLV DETECTOR
# ============================================================

def is_flv_url(url):

    if not url:
        return False

    return ".flv" in url.lower()


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

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            context = browser.new_context(
                user_agent=HEADERS["User-Agent"]
            )

            page = context.new_page()

            # ------------------------------------------------
            # Capture authorized media requests
            # ------------------------------------------------

            captured_streams = []

            def handle_request(request):

                url = request.url

                if is_flv_url(url):

                    if url not in captured_streams:

                        captured_streams.append(url)

                        print(
                            f"[{index}/{total}] "
                            f"FLV: {url}",
                            flush=True
                        )

            page.on(
                "request",
                handle_request
            )

            # ------------------------------------------------
            # Open match
            # ------------------------------------------------

            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

            page.wait_for_timeout(
                1000
            )

            # ------------------------------------------------
            # Get title
            # ------------------------------------------------

            title = "Unknown Event"

            try:

                h1 = page.locator(
                    "h1"
                ).first

                if h1.count() > 0:

                    value = h1.inner_text(
                        timeout=2000
                    ).strip()

                    if value:
                        title = value

            except Exception:
                pass

            if title == "Unknown Event":

                try:

                    value = page.title()

                    if value:
                        title = value.strip()

                except Exception:
                    pass

            # ------------------------------------------------
            # Get players
            # ------------------------------------------------

            elements = page.locator(
                "a.player-link"
            ).all()

            players = []

            for player_index, element in enumerate(
                elements
            ):

                # Clear streams for this player
                captured_streams.clear()

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

                # ------------------------------------------------
                # Click player
                # ------------------------------------------------

                try:

                    element.click(
                        timeout=5000
                    )

                    page.wait_for_timeout(
                        PLAYER_WAIT_MS
                    )

                except Exception as error:

                    print(
                        f"[{index}/{total}] "
                        f"Player click failed: "
                        f"{name} - {error}",
                        flush=True
                    )

                # ------------------------------------------------
                # Save captured streams
                # ------------------------------------------------

                streams = list(
                    captured_streams
                )

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
                    ),
                    "stream_urls": streams
                })

                print(
                    f"[{index}/{total}] "
                    f"PLAYER: {name} "
                    f"| streams={len(streams)}",
                    flush=True
                )

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
            f"URL: {match_url}",
            flush=True
        )

        print(
            f"{type(error).__name__}: {error}",
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

    html = get_home_page()

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
                    results.append(result)

            except Exception as error:

                print(
                    f"Worker exception: {error}",
                    flush=True
                )

    results.sort(
        key=lambda x: x["url"]
    )

    save_results(
        results
    )

    total_players = sum(
        len(x["players"])
        for x in results
    )

    players_with_stream = sum(
        1
        for match in results
        for player in match["players"]
        if player.get("stream_urls")
    )

    total_streams = sum(
        len(
            player.get(
                "stream_urls",
                []
            )
        )
        for match in results
        for player in match["players"]
    )

    print()
    print("=" * 70)
    print("SCRAPER COMPLETE")
    print("=" * 70)

    print(
        f"Events found:          {len(match_urls)}"
    )

    print(
        f"Events processed:      {len(results)}"
    )

    print(
        f"Total players:         {total_players}"
    )

    print(
        f"Players with stream:   {players_with_stream}"
    )

    print(
        f"Total FLV streams:     {total_streams}"
    )

    print(
        f"Output file:           {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
