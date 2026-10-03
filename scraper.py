import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

HOME_URL = "https://xoilacz.vip/"

OUTPUT_FILE = Path("matches.json")

# Số Chromium worker chạy song song.
# GitHub Actions ubuntu-latest thường 3 là mức an toàn.
MAX_WORKERS = 3

PAGE_TIMEOUT = 20_000

# Tổng thời gian tối đa chờ FLV sau khi click player.
PLAYER_WAIT_MS = 2_000

# Khoảng thời gian kiểm tra FLV.
STREAM_POLL_MS = 100

# Số lần thử mở lại một trận nếu navigation lỗi.
MATCH_RETRIES = 2

# Không cho phép scraper thành công quá ít trận.
#
# Ví dụ:
# 60 trận tìm thấy
# 42 trận xử lý được = 70% -> OK
# 41 trận = 68.3% -> FAIL
MIN_MATCH_SUCCESS_RATIO = 0.70

# Playlist phải có ít nhất một stream.
MIN_TOTAL_STREAMS = 1

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
        timeout=20,
    )

    response.raise_for_status()

    print(
        f"Home page status: {response.status_code}",
        flush=True,
    )

    return response.text


# ============================================================
# NORMALIZE URL
# ============================================================

def normalize_match_url(url):

    url = re.sub(
        r"/link/\d+/?$",
        "/",
        url,
    )

    return url.rstrip("/") + "/"


# ============================================================
# FIND MATCHES
# ============================================================

def find_match_urls(html):

    soup = BeautifulSoup(
        html,
        "html.parser",
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
            href,
        )

        full_url = normalize_match_url(
            full_url,
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
# GET PLAYER NAME
# ============================================================

def get_player_name(element):

    try:

        html = element.inner_html()

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        for svg in soup.find_all("svg"):
            svg.decompose()

        name = soup.get_text(
            " ",
            strip=True,
        )

        name = re.sub(
            r"\s+",
            " ",
            name,
        ).strip()

        return name

    except Exception as error:

        print(
            f"Could not read player name: {error}",
            flush=True,
        )

        return ""


# ============================================================
# WAIT FOR STREAM
# ============================================================

def wait_for_stream(
    captured_streams,
    max_wait_ms,
):

    deadline = time.monotonic() + (
        max_wait_ms / 1000
    )

    while time.monotonic() < deadline:

        if captured_streams:
            return

        time.sleep(
            STREAM_POLL_MS / 1000
        )


# ============================================================
# SCRAPE ONE MATCH
# ============================================================

def scrape_one_match(
    page,
    match_url,
    index,
    total,
):

    print(
        f"[{index}/{total}] START {match_url}",
        flush=True,
    )

    last_error = None

    for attempt in range(
        1,
        MATCH_RETRIES + 1,
    ):

        try:

            # ------------------------------------------------
            # STREAM CAPTURE
            # ------------------------------------------------

            captured_streams = []

            def handle_request(request):

                url = request.url

                if not is_flv_url(url):
                    return

                if url not in captured_streams:

                    captured_streams.append(url)

                    print(
                        f"[{index}/{total}] "
                        f"FLV detected",
                        flush=True,
                    )

            page.on(
                "request",
                handle_request,
            )

            # ------------------------------------------------
            # OPEN MATCH
            # ------------------------------------------------

            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT,
            )

            page.wait_for_timeout(500)

            # ------------------------------------------------
            # TITLE
            # ------------------------------------------------

            title = "Unknown Event"

            try:

                h1 = page.locator(
                    "h1"
                ).first

                if h1.count() > 0:

                    value = h1.inner_text(
                        timeout=2000,
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
            # PLAYERS
            # ------------------------------------------------

            elements = page.locator(
                "a.player-link"
            ).all()

            players = []

            print(
                f"[{index}/{total}] "
                f"Players found: {len(elements)}",
                flush=True,
            )

            # ------------------------------------------------
            # PROCESS PLAYERS
            # ------------------------------------------------

            for player_index, element in enumerate(
                elements
            ):

                # Clear previous player's streams.
                captured_streams.clear()

                # --------------------------------------------
                # NAME
                # --------------------------------------------

                name = get_player_name(
                    element
                )

                # --------------------------------------------
                # SERVER
                # --------------------------------------------

                try:

                    server = (
                        element.get_attribute(
                            "title"
                        )
                        or ""
                    )

                except Exception:

                    server = ""

                # --------------------------------------------
                # DATA LINK
                # --------------------------------------------

                try:

                    data_link = (
                        element.get_attribute(
                            "data-link"
                        )
                        or ""
                    )

                except Exception:

                    data_link = ""

                # --------------------------------------------
                # HREF
                # --------------------------------------------

                try:

                    href = (
                        element.get_attribute(
                            "href"
                        )
                        or ""
                    )

                except Exception:

                    href = ""

                print(
                    f"[{index}/{total}] "
                    f"PLAYER {player_index + 1}: "
                    f"name='{name}' "
                    f"| server='{server}'",
                    flush=True,
                )

                # --------------------------------------------
                # CLICK
                # --------------------------------------------

                try:

                    element.click(
                        timeout=5000,
                    )

                    wait_for_stream(
                        captured_streams,
                        PLAYER_WAIT_MS,
                    )

                except Exception as error:

                    print(
                        f"[{index}/{total}] "
                        f"Player click failed: "
                        f"{name or server} "
                        f"- {error}",
                        flush=True,
                    )

                # --------------------------------------------
                # UNIQUE STREAMS
                # --------------------------------------------

                streams = list(
                    dict.fromkeys(
                        captured_streams
                    )
                )

                players.append({

                    "index": player_index,

                    "name": name,

                    "server": server,

                    "data_link": data_link,

                    "href": (
                        urljoin(
                            HOME_URL,
                            href,
                        )
                        if href
                        else ""
                    ),

                    "stream_urls": streams,
                })

                print(
                    f"[{index}/{total}] "
                    f"PLAYER: {name or server} "
                    f"| streams={len(streams)}",
                    flush=True,
                )

            # ------------------------------------------------
            # REMOVE EVENT LISTENER
            # ------------------------------------------------

            try:

                page.remove_listener(
                    "request",
                    handle_request,
                )

            except Exception:
                pass

            # ------------------------------------------------
            # RESULT
            # ------------------------------------------------

            result = {

                "title": title,

                "url": match_url,

                "players": players,
            }

            print(
                f"[{index}/{total}] OK "
                f"| players={len(players)} "
                f"| {title}",
                flush=True,
            )

            return result

        except PlaywrightTimeoutError as error:

            last_error = error

            print(
                f"[{index}/{total}] "
                f"TIMEOUT "
                f"(attempt {attempt}/{MATCH_RETRIES})",
                flush=True,
            )

        except Exception as error:

            last_error = error

            print(
                f"[{index}/{total}] "
                f"ERROR "
                f"(attempt {attempt}/{MATCH_RETRIES})",
                flush=True,
            )

            print(
                f"{type(error).__name__}: {error}",
                flush=True,
            )

        finally:

            try:

                page.remove_listener(
                    "request",
                    handle_request,
                )

            except Exception:
                pass

        # ----------------------------------------------------
        # RESET PAGE BEFORE RETRY
        # ----------------------------------------------------

        try:
            page.goto(
                "about:blank",
                wait_until="domcontentloaded",
                timeout=5000,
            )
        except Exception:
            pass

    print(
        f"[{index}/{total}] FAILED "
        f"after {MATCH_RETRIES} attempts",
        flush=True,
    )

    if last_error:

        print(
            f"Last error: "
            f"{type(last_error).__name__}: "
            f"{last_error}",
            flush=True,
        )

    return None


# ============================================================
# WORKER
#
# Mỗi worker tạo Chromium MỘT LẦN,
# sau đó xử lý nhiều trận.
# ============================================================

def scrape_batch(
    batch,
    worker_id,
    total,
):

    results = []

    print(
        f"[WORKER {worker_id}] "
        f"Starting with {len(batch)} matches",
        flush=True,
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
        )

        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
        )

        page = context.new_page()

        try:

            for index, match_url in batch:

                result = scrape_one_match(
                    page,
                    match_url,
                    index,
                    total,
                )

                if result is not None:

                    results.append(result)

        finally:

            context.close()
            browser.close()

    print(
        f"[WORKER {worker_id}] "
        f"Finished "
        f"| success={len(results)}",
        flush=True,
    )

    return results


# ============================================================
# SPLIT WORK
# ============================================================

def split_batches(
    match_urls,
    workers,
):

    indexed = list(
        enumerate(
            match_urls,
            1,
        )
    )

    workers = min(
        workers,
        len(indexed),
    )

    batches = [
        []
        for _ in range(workers)
    ]

    for position, item in enumerate(indexed):

        batches[
            position % workers
        ].append(item)

    return batches


# ============================================================
# SAVE JSON
# ============================================================

def save_results(results):

    data = {

        "generated_at": int(
            time.time()
        ),

        "match_count": len(
            results
        ),

        "matches": results,
    }

    OUTPUT_FILE.write_text(

        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),

        encoding="utf-8",
    )


# ============================================================
# STATISTICS
# ============================================================

def get_statistics(results):

    total_players = sum(

        len(
            match.get(
                "players",
                [],
            )
        )

        for match in results
    )

    players_with_stream = sum(

        1

        for match in results

        for player in match.get(
            "players",
            [],
        )

        if player.get(
            "stream_urls"
        )
    )

    total_streams = sum(

        len(
            player.get(
                "stream_urls",
                [],
            )
        )

        for match in results

        for player in match.get(
            "players",
            [],
        )
    )

    return (
        total_players,
        players_with_stream,
        total_streams,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    started_at = time.monotonic()

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    html = get_home_page()

    # --------------------------------------------------------
    # FIND EVENTS
    # --------------------------------------------------------

    match_urls = find_match_urls(
        html
    )

    total_matches = len(
        match_urls
    )

    print(
        f"Found {total_matches} unique events",
        flush=True,
    )

    if not match_urls:

        raise RuntimeError(
            "No events found. "
            "Refusing to overwrite/publish playlist."
        )

    # --------------------------------------------------------
    # WORKERS
    # --------------------------------------------------------

    worker_count = min(
        MAX_WORKERS,
        total_matches,
    )

    print(
        f"Using {worker_count} workers...",
        flush=True,
    )

    batches = split_batches(
        match_urls,
        worker_count,
    )

    results = []

    # --------------------------------------------------------
    # PARALLEL SCRAPE
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=worker_count
    ) as executor:

        futures = {}

        for worker_id, batch in enumerate(
            batches,
            1,
        ):

            future = executor.submit(
                scrape_batch,
                batch,
                worker_id,
                total_matches,
            )

            futures[future] = worker_id

        for future in as_completed(
            futures
        ):

            worker_id = futures[future]

            try:

                worker_results = (
                    future.result()
                )

                results.extend(
                    worker_results
                )

            except Exception as error:

                print(
                    f"[WORKER {worker_id}] "
                    f"FATAL ERROR: "
                    f"{type(error).__name__}: "
                    f"{error}",
                    flush=True,
                )

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    results.sort(
        key=lambda item: item["url"]
    )

    successful_matches = len(
        results
    )

    success_ratio = (
        successful_matches / total_matches
    )

    # --------------------------------------------------------
    # STATISTICS
    # --------------------------------------------------------

    (
        total_players,
        players_with_stream,
        total_streams,
    ) = get_statistics(
        results
    )

    elapsed = (
        time.monotonic()
        - started_at
    )

    # --------------------------------------------------------
    # QUALITY GATE
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("SCRAPER VALIDATION")
    print("=" * 70)

    print(
        f"Events found:          {total_matches}"
    )

    print(
        f"Events processed:      {successful_matches}"
    )

    print(
        f"Success ratio:         "
        f"{success_ratio:.1%}"
    )

    print(
        f"Total players:         {total_players}"
    )

    print(
        f"Players with stream:   "
        f"{players_with_stream}"
    )

    print(
        f"Total FLV streams:     "
        f"{total_streams}"
    )

    print(
        f"Elapsed:               "
        f"{elapsed:.1f}s"
    )

    print("=" * 70)

    if success_ratio < MIN_MATCH_SUCCESS_RATIO:

        raise RuntimeError(
            "SCRAPER QUALITY CHECK FAILED: "
            f"only {successful_matches}/{total_matches} "
            f"events processed "
            f"({success_ratio:.1%}), "
            f"minimum required is "
            f"{MIN_MATCH_SUCCESS_RATIO:.1%}. "
            "Refusing to publish."
        )

    if total_streams < MIN_TOTAL_STREAMS:

        raise RuntimeError(
            "SCRAPER QUALITY CHECK FAILED: "
            f"only {total_streams} FLV streams found. "
            "Refusing to publish."
        )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    save_results(
        results
    )

    print()
    print("=" * 70)
    print("SCRAPER COMPLETE")
    print("=" * 70)

    print(
        f"Output file: {OUTPUT_FILE}"
    )

    print(
        "Quality gate: PASS"
    )

    print("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
