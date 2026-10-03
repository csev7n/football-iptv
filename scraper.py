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

PAGE_TIMEOUT = 20_000

# Chờ player xuất hiện
PLAYER_READY_TIMEOUT = 5_000

# Sau khi FLV đầu tiên xuất hiện, tiếp tục gom thêm request
# để không bỏ mất trường hợp một server phát nhiều FLV.
STREAM_SETTLE_MS = 1_500

# Mỗi server tối đa thử:
# 1. click bình thường
# 2. reload + click lại
# 3. reload + click lần cuối
MAX_PLAYER_ATTEMPTS = 3

# Khoảng thời gian tối đa chờ stream ở mỗi attempt
STREAM_WAIT_MS = 7_000

# Chờ sau reload để JS/player ổn định
RELOAD_WAIT_MS = 1_000

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

    """
    Lấy đúng tên hiển thị trên site.

    Ví dụ:

        <a class="player-link" title="Server 4">
            <svg>...</svg>
            FULL HD BRADY
        </a>

    Kết quả:

        FULL HD BRADY

    Không tự đổi thành:
        FILIP 1
        FILIP 2
    """

    try:

        html = element.inner_html()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

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
# GET PLAYERS
# ============================================================

def get_player_elements(page):

    """
    Lấy danh sách player hiện tại từ DOM.

    Hàm này PHẢI được gọi lại sau reload.
    Không tái sử dụng locator/element cũ.
    """

    try:

        page.locator(
            "a.player-link"
        ).first.wait_for(
            state="attached",
            timeout=PLAYER_READY_TIMEOUT
        )

    except Exception:
        pass

    return page.locator(
        "a.player-link"
    ).all()


# ============================================================
# PLAYER INFO
# ============================================================

def read_player_info(element):

    try:
        server = (
            element.get_attribute(
                "title"
            )
            or ""
        )
    except Exception:
        server = ""

    try:
        data_link = (
            element.get_attribute(
                "data-link"
            )
            or ""
        )
    except Exception:
        data_link = ""

    try:
        href = (
            element.get_attribute(
                "href"
            )
            or ""
        )
    except Exception:
        href = ""

    name = get_player_name(
        element
    )

    return {
        "name": name,
        "server": server,
        "data_link": data_link,
        "href": href
    }


# ============================================================
# FIND PLAYER AFTER RELOAD
# ============================================================

def find_player_again(
    page,
    data_link,
    player_index
):

    """
    Ưu tiên data-link vì name có thể trùng.

    Ví dụ:
        Server 2 = FILIP
        Server 3 = FILIP

    Không được tìm bằng name.
    """

    # --------------------------------------------------------
    # 1. Ưu tiên data-link
    # --------------------------------------------------------

    if data_link:

        locator = page.locator(
            f'a.player-link[data-link="{data_link}"]'
        )

        try:

            if locator.count() > 0:

                return locator.first

        except Exception:
            pass

    # --------------------------------------------------------
    # 2. Fallback theo index
    # --------------------------------------------------------

    try:

        elements = get_player_elements(
            page
        )

        if player_index < len(elements):

            return elements[player_index]

    except Exception:
        pass

    return None


# ============================================================
# STREAM COLLECTOR
# ============================================================

class StreamCollector:

    def __init__(self):

        self.urls = []

    def clear(self):

        self.urls.clear()

    def add(self, url):

        if not is_flv_url(url):
            return

        url = str(
            url
        ).strip()

        if not url:
            return

        if url not in self.urls:

            self.urls.append(
                url
            )

            print(
                f"FLV detected: {url}",
                flush=True
            )

    def get(self):

        return list(
            self.urls
        )


# ============================================================
# WAIT FOR STREAMS
# ============================================================

def wait_for_streams(
    page,
    collector,
    max_wait_ms=STREAM_WAIT_MS
):

    """
    Adaptive wait:

    - Không có FLV -> chờ tối đa max_wait_ms.
    - FLV đầu tiên xuất hiện -> tiếp tục chờ
      STREAM_SETTLE_MS để gom thêm nguồn.
    """

    started = time.monotonic()

    first_stream_time = None

    while True:

        now = time.monotonic()

        elapsed_ms = (
            now - started
        ) * 1000

        streams = collector.get()

        # ----------------------------------------------------
        # FLV đầu tiên xuất hiện
        # ----------------------------------------------------

        if streams:

            if first_stream_time is None:

                first_stream_time = now

            settle_ms = (
                now - first_stream_time
            ) * 1000

            if settle_ms >= STREAM_SETTLE_MS:

                break

        # ----------------------------------------------------
        # Timeout
        # ----------------------------------------------------

        if elapsed_ms >= max_wait_ms:

            break

        page.wait_for_timeout(
            100
        )

    return collector.get()


# ============================================================
# CLICK PLAYER
# ============================================================

def click_player(
    page,
    player,
    collector,
    index,
    total,
    attempt
):

    try:

        player.scroll_into_view_if_needed(
            timeout=3_000
        )

    except Exception:
        pass

    try:

        player.click(
            timeout=5_000
        )

    except Exception as error:

        print(
            f"[{index}/{total}] "
            f"Click attempt {attempt} warning: "
            f"{error}",
            flush=True
        )

        # Một số trường hợp click timeout nhưng
        # JS/request vẫn tiếp tục chạy.
        # Vì vậy KHÔNG return ngay.

    streams = wait_for_streams(
        page,
        collector
    )

    return streams


# ============================================================
# RELOAD PAGE
# ============================================================

def reload_match_page(
    page,
    match_url,
    index,
    total
):

    print(
        f"[{index}/{total}] "
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

        return True

    except Exception as error:

        print(
            f"[{index}/{total}] "
            f"Reload failed: {error}",
            flush=True
        )

        # Nếu reload thất bại thì thử goto lại.
        try:

            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

            page.wait_for_timeout(
                RELOAD_WAIT_MS
            )

            return True

        except Exception as second_error:

            print(
                f"[{index}/{total}] "
                f"Re-open failed: {second_error}",
                flush=True
            )

            return False


# ============================================================
# CAPTURE ONE PLAYER
# ============================================================

def capture_player(
    page,
    match_url,
    player_info,
    player_index,
    total_players,
    match_index,
    total_matches,
    collector
):

    name = player_info["name"]

    server = player_info["server"]

    data_link = player_info["data_link"]

    # --------------------------------------------------------
    # Retry
    # --------------------------------------------------------

    for attempt in range(
        1,
        MAX_PLAYER_ATTEMPTS + 1
    ):

        # ----------------------------------------------------
        # Retry từ lần 2 trở đi -> reload
        # ----------------------------------------------------

        if attempt > 1:

            if not reload_match_page(
                page,
                match_url,
                match_index,
                total_matches
            ):

                continue

        # ----------------------------------------------------
        # IMPORTANT:
        # Sau reload phải tìm player mới.
        # Không dùng element cũ.
        # ----------------------------------------------------

        player = find_player_again(
            page,
            data_link,
            player_index
        )

        if player is None:

            print(
                f"[{match_index}/{total_matches}] "
                f"PLAYER NOT FOUND "
                f"| attempt={attempt} "
                f"| {server} "
                f"| {name}",
                flush=True
            )

            continue

        # ----------------------------------------------------
        # Clear capture cho attempt hiện tại
        # ----------------------------------------------------

        collector.clear()

        print(
            f"[{match_index}/{total_matches}] "
            f"CLICK "
            f"| attempt={attempt}/{MAX_PLAYER_ATTEMPTS} "
            f"| {server} "
            f"| {name} "
            f"| data-link={data_link}",
            flush=True
        )

        streams = click_player(
            page,
            player,
            collector,
            match_index,
            total_matches,
            attempt
        )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        if streams:

            print(
                f"[{match_index}/{total_matches}] "
                f"PLAYER SUCCESS "
                f"| {server} "
                f"| {name} "
                f"| streams={len(streams)}",
                flush=True
            )

            return streams

        # ----------------------------------------------------
        # FAILED ATTEMPT
        # ----------------------------------------------------

        print(
            f"[{match_index}/{total_matches}] "
            f"NO FLV "
            f"| attempt={attempt}/{MAX_PLAYER_ATTEMPTS} "
            f"| {server} "
            f"| {name}",
            flush=True
        )

    # --------------------------------------------------------
    # All attempts failed
    # --------------------------------------------------------

    print(
        f"[{match_index}/{total_matches}] "
        f"PLAYER FAILED "
        f"| {server} "
        f"| {name} "
        f"| attempts={MAX_PLAYER_ATTEMPTS}",
        flush=True
    )

    return []


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

    started = time.monotonic()

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
            # Capture tất cả FLV request của page
            # ------------------------------------------------

            collector = StreamCollector()

            def handle_request(request):

                try:

                    url = request.url

                    if is_flv_url(url):

                        collector.add(
                            url
                        )

                except Exception:
                    pass

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
                1_000
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
                        timeout=2_000
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

            elements = get_player_elements(
                page
            )

            player_infos = []

            for player_index, element in enumerate(
                elements
            ):

                info = read_player_info(
                    element
                )

                info["index"] = player_index

                player_infos.append(
                    info
                )

            print(
                f"[{index}/{total}] "
                f"Players found: "
                f"{len(player_infos)}",
                flush=True
            )

            # ------------------------------------------------
            # Process players
            # ------------------------------------------------

            players = []

            for player_info in player_infos:

                player_index = player_info[
                    "index"
                ]

                name = player_info[
                    "name"
                ]

                server = player_info[
                    "server"
                ]

                data_link = player_info[
                    "data_link"
                ]

                href = player_info[
                    "href"
                ]

                print(
                    f"[{index}/{total}] "
                    f"PLAYER "
                    f"{player_index + 1}: "
                    f"name='{name}' "
                    f"| server='{server}' "
                    f"| data-link='{data_link}'",
                    flush=True
                )

                streams = capture_player(
                    page,
                    match_url,
                    player_info,
                    player_index,
                    len(player_infos),
                    index,
                    total,
                    collector
                )

                players.append({

                    "index": player_index,

                    # Giữ nguyên tên site
                    "name": name,

                    "server": server,

                    # ID kỹ thuật để tìm lại sau reload
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
                    f"PLAYER: "
                    f"{name or server} "
                    f"| streams={len(streams)}",
                    flush=True
                )

            # ------------------------------------------------
            # Close
            # ------------------------------------------------

            context.close()

            browser.close()

            elapsed = (
                time.monotonic()
                - started
            )

            print(
                f"[{index}/{total}] OK "
                f"| players={len(players)} "
                f"| {title} "
                f"| {elapsed:.1f}s",
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

    started = time.monotonic()

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

    # --------------------------------------------------------
    # Chia match cho worker
    # --------------------------------------------------------

    chunks = [
        match_urls[i::MAX_WORKERS]
        for i in range(MAX_WORKERS)
    ]

    def worker(
        worker_index,
        urls
    ):

        worker_results = []

        print(
            f"[WORKER {worker_index}] "
            f"Starting with {len(urls)} matches",
            flush=True
        )

        for local_index, match_url in enumerate(
            urls,
            1
        ):

            # Tìm index thật trong toàn bộ danh sách
            global_index = (
                match_urls.index(
                    match_url
                )
                + 1
            )

            result = scrape_one_match(
                match_url,
                global_index,
                len(match_urls)
            )

            if result is not None:

                worker_results.append(
                    result
                )

        print(
            f"[WORKER {worker_index}] "
            f"Finished | success="
            f"{len(worker_results)}",
            flush=True
        )

        return worker_results

    # --------------------------------------------------------
    # Execute workers
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = []

        for worker_index, urls in enumerate(
            chunks,
            1
        ):

            if not urls:
                continue

            futures.append(
                executor.submit(
                    worker,
                    worker_index,
                    urls
                )
            )

        for future in as_completed(
            futures
        ):

            try:

                worker_results = future.result()

                results.extend(
                    worker_results
                )

            except Exception as error:

                print(
                    f"Worker exception: {error}",
                    flush=True
                )

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    results.sort(
        key=lambda x: x["url"]
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    save_results(
        results
    )

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    total_players = sum(
        len(
            match["players"]
        )
        for match in results
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

    failed_players = (
        total_players
        - players_with_stream
    )

    elapsed = (
        time.monotonic()
        - started
    )

    success_ratio = (
        (
            len(results)
            / len(match_urls)
        )
        * 100
        if match_urls
        else 0
    )

    # --------------------------------------------------------
    # Validation
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
        f"{len(match_urls)}"
    )

    print(
        f"Events processed:      "
        f"{len(results)}"
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
        f"Players without FLV:   "
        f"{failed_players}"
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

    print()

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
        "Quality gate: "
        + (
            "PASS"
            if results
            else "FAIL"
        )
    )

    print(
        "=" * 70
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
