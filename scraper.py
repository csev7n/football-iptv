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

MAX_WORKERS = 3

PAGE_TIMEOUT = 20000

# Thời gian chờ FLV sau khi click
STREAM_WAIT_MS = 5000

# Chờ trang ổn định sau reload
RELOAD_WAIT_MS = 1500

# Số lần thử tối đa cho mỗi server
MAX_PLAYER_ATTEMPTS = 2

# Retry tải trang chủ
HOME_RETRIES = 3


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

    last_error = None

    for attempt in range(1, HOME_RETRIES + 1):

        try:

            print(
                f"Downloading home page "
                f"(attempt {attempt}/{HOME_RETRIES})...",
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

        except Exception as error:

            last_error = error

            print(
                f"Home page download failed: {error}",
                flush=True
            )

            if attempt < HOME_RETRIES:

                time.sleep(2)

    raise last_error


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
# GET PLAYER NAME
# ============================================================

def get_player_name(element):

    try:

        html = element.inner_html()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # Xóa SVG
        for svg in soup.find_all("svg"):
            svg.decompose()

        name = soup.get_text(
            " ",
            strip=True
        )

        name = re.sub(
            r"\s+",
            " ",
            name
        ).strip()

        return name

    except Exception as error:

        print(
            f"Could not read player name: {error}",
            flush=True
        )

        return ""


# ============================================================
# GET PLAYER METADATA
# ============================================================

def get_player_metadata(
    element
):

    try:
        title = (
            element.get_attribute("title")
            or ""
        )
    except Exception:
        title = ""

    try:
        data_link = (
            element.get_attribute("data-link")
            or ""
        )
    except Exception:
        data_link = ""

    try:
        href = (
            element.get_attribute("href")
            or ""
        )
    except Exception:
        href = ""

    name = get_player_name(
        element
    )

    return {
        "name": name,
        "server": title,
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


# ============================================================
# FIND PLAYER ELEMENT
#
# Sau reload phải tìm lại element.
# Không sử dụng locator/element cũ.
# ============================================================

def find_player_element(
    page,
    player_key
):

    elements = page.locator(
        "a.player-link"
    ).all()

    for element in elements:

        try:

            data_link = (
                element.get_attribute(
                    "data-link"
                )
                or ""
            )

            if (
                player_key.get("data_link")
                and
                data_link == player_key["data_link"]
            ):
                return element

        except Exception:
            continue

    # Fallback bằng server/title
    if player_key.get("server"):

        for element in elements:

            try:

                server = (
                    element.get_attribute(
                        "title"
                    )
                    or ""
                )

                if (
                    server
                    == player_key["server"]
                ):
                    return element

            except Exception:
                continue

    # Fallback bằng index
    index = player_key.get(
        "index"
    )

    if (
        isinstance(index, int)
        and
        0 <= index < len(elements)
    ):

        return elements[index]

    return None


# ============================================================
# WAIT FOR FLV
# ============================================================

def wait_for_flv(
    page,
    captured_streams,
    wait_ms
):

    elapsed = 0

    interval = 250

    while elapsed < wait_ms:

        if captured_streams:

            return list(
                captured_streams
            )

        page.wait_for_timeout(
            interval
        )

        elapsed += interval

    return list(
        captured_streams
    )


# ============================================================
# CLICK PLAYER AND CAPTURE FLV
# ============================================================

def capture_player_stream(
    page,
    player_key,
    index,
    total,
    attempt
):

    captured_streams = []

    def handle_request(request):

        url = request.url

        if not is_flv_url(url):
            return

        if url not in captured_streams:

            captured_streams.append(
                url
            )

            print(
                f"[{index}/{total}] "
                f"FLV detected "
                f"(server={player_key.get('server', '')}, "
                f"attempt={attempt})",
                flush=True
            )

    page.on(
        "request",
        handle_request
    )

    try:

        element = find_player_element(
            page,
            player_key
        )

        if element is None:

            print(
                f"[{index}/{total}] "
                f"Player not found: "
                f"{player_key.get('server', '')}",
                flush=True
            )

            return []

        try:

            element.scroll_into_view_if_needed(
                timeout=3000
            )

        except Exception:
            pass

        try:

            element.click(
                timeout=5000
            )

        except Exception as error:

            print(
                f"[{index}/{total}] "
                f"Player click failed: "
                f"{player_key.get('name') or player_key.get('server')} "
                f"- {error}",
                flush=True
            )

            # Không return ngay.
            # Một số trang vẫn xử lý click dù Playwright
            # báo timeout.

        streams = wait_for_flv(
            page,
            captured_streams,
            STREAM_WAIT_MS
        )

        return streams

    finally:

        try:
            page.remove_listener(
                "request",
                handle_request
            )
        except Exception:
            pass


# ============================================================
# SCRAPE ONE PLAYER
#
# Logic:
#
# Attempt 1:
#     click
#     wait FLV
#
# Nếu không có:
#
# Attempt 2:
#     reload page
#     tìm lại player
#     click
#     wait FLV
#
# Nếu vẫn không có:
#     []
#
# ============================================================

def scrape_one_player(
    page,
    player_key,
    index,
    total
):

    player_label = (
        player_key.get("name")
        or player_key.get("server")
        or f"Server {player_key.get('index', 0) + 1}"
    )

    print(
        f"[{index}/{total}] "
        f"PLAYER: {player_label}",
        flush=True
    )

    # --------------------------------------------------------
    # ATTEMPT 1
    # --------------------------------------------------------

    print(
        f"[{index}/{total}] "
        f"Attempt 1: "
        f"{player_label}",
        flush=True
    )

    streams = capture_player_stream(
        page,
        player_key,
        index,
        total,
        attempt=1
    )

    if streams:

        print(
            f"[{index}/{total}] "
            f"PLAYER OK: "
            f"{player_label} "
            f"| streams={len(streams)}",
            flush=True
        )

        return streams

    # --------------------------------------------------------
    # ATTEMPT 2
    #
    # Reload vì player có thể đang ở trạng thái active/playing
    # và click lại không tạo request FLV mới.
    # --------------------------------------------------------

    print(
        f"[{index}/{total}] "
        f"No FLV after attempt 1. "
        f"Reloading page...",
        flush=True
    )

    try:

        page.reload(
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        page.wait_for_timeout(
            RELOAD_WAIT_MS
        )

    except Exception as error:

        print(
            f"[{index}/{total}] "
            f"Reload failed: {error}",
            flush=True
        )

        return []

    print(
        f"[{index}/{total}] "
        f"Attempt 2 after reload: "
        f"{player_label}",
        flush=True
    )

    streams = capture_player_stream(
        page,
        player_key,
        index,
        total,
        attempt=2
    )

    if streams:

        print(
            f"[{index}/{total}] "
            f"PLAYER RECOVERED: "
            f"{player_label} "
            f"| streams={len(streams)}",
            flush=True
        )

    else:

        print(
            f"[{index}/{total}] "
            f"No FLV available: "
            f"{player_label}",
            flush=True
        )

    return streams


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
            # OPEN MATCH
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
            # TITLE
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
            # GET PLAYERS
            # ------------------------------------------------

            elements = page.locator(
                "a.player-link"
            ).all()

            player_keys = []

            print(
                f"[{index}/{total}] "
                f"Players found: "
                f"{len(elements)}",
                flush=True
            )

            # ------------------------------------------------
            # Snapshot metadata
            #
            # Không giữ Playwright element.
            # Chỉ giữ metadata để sau reload tìm lại.
            # ------------------------------------------------

            for player_index, element in enumerate(
                elements
            ):

                metadata = get_player_metadata(
                    element
                )

                metadata["index"] = player_index

                player_keys.append(
                    metadata
                )

                print(
                    f"[{index}/{total}] "
                    f"PLAYER {player_index + 1}: "
                    f"name='{metadata['name']}' "
                    f"| server='{metadata['server']}' "
                    f"| data-link='{metadata['data_link']}'",
                    flush=True
                )

            # ------------------------------------------------
            # PROCESS PLAYERS
            # ------------------------------------------------

            players = []

            for player_key in player_keys:

                player_index = player_key[
                    "index"
                ]

                streams = scrape_one_player(
                    page,
                    player_key,
                    index,
                    total
                )

                players.append({

                    "index": player_index,

                    "name": player_key[
                        "name"
                    ],

                    "server": player_key[
                        "server"
                    ],

                    "data_link": player_key[
                        "data_link"
                    ],

                    "href": player_key[
                        "href"
                    ],

                    "stream_urls": streams

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

        "match_count": len(
            results
        ),

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

    started_at = time.time()

    print(
        "=" * 70
    )

    print(
        "START SCRAPER"
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    html = get_home_page()

    # --------------------------------------------------------
    # MATCHES
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
            "ERROR: No events found.",
            flush=True
        )

        raise RuntimeError(
            "No events found"
        )

    # --------------------------------------------------------
    # WORKERS
    # --------------------------------------------------------

    print(
        f"Using {MAX_WORKERS} workers...",
        flush=True
    )

    results = []

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

            match_url = futures[
                future
            ]

            try:

                result = future.result()

                if result is not None:

                    results.append(
                        result
                    )

            except Exception as error:

                print(
                    f"Worker exception "
                    f"for {match_url}: "
                    f"{error}",
                    flush=True
                )

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    results.sort(
        key=lambda x: x["url"]
    )

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    events_found = len(
        match_urls
    )

    events_processed = len(
        results
    )

    success_ratio = (
        events_processed / events_found * 100
        if events_found
        else 0
    )

    total_players = sum(

        len(
            match.get(
                "players",
                []
            )
        )

        for match in results

    )

    players_with_stream = sum(

        1

        for match in results

        for player in match.get(
            "players",
            []
        )

        if player.get(
            "stream_urls"
        )

    )

    total_streams = sum(

        len(
            player.get(
                "stream_urls",
                []
            )
        )

        for match in results

        for player in match.get(
            "players",
            []
        )

    )

    # --------------------------------------------------------
    # QUALITY GATE
    #
    # Không yêu cầu mọi player phải có FLV.
    #
    # Vì scraper có thể chạy trước giờ trận.
    # --------------------------------------------------------

    quality_gate = (
        events_found > 0
        and
        events_processed > 0
        and
        success_ratio >= 95
    )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    save_results(
        results
    )

    elapsed = time.time() - started_at

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    print()

    print(
        "=" * 70
    )

    print(
        "SCRAPER VALIDATION"
    )

    print(
        "=" * 70
    )

    print(
        f"Events found:          "
        f"{events_found}"
    )

    print(
        f"Events processed:      "
        f"{events_processed}"
    )

    print(
        f"Success ratio:         "
        f"{success_ratio:.1f}%"
    )

    print(
        f"Total players:         "
        f"{total_players}"
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

    print(
        "=" * 70
    )

    if not quality_gate:

        print(
            "QUALITY GATE: FAIL"
        )

        print(
            "=" * 70
        )

        raise RuntimeError(
            "Scraper quality gate failed"
        )

    print(
        "QUALITY GATE: PASS"
    )

    print(
        "=" * 70
    )

    print(
        "SCRAPER COMPLETE"
    )

    print(
        "=" * 70
    )

    print(
        f"Output file: {OUTPUT_FILE}"
    )

    print(
        f"Quality gate: PASS"
    )

    print(
        "=" * 70
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
