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

# Thời gian chờ sau khi mở trang / click server
PLAYER_WAIT_MS = 2500

# Số lần retry bằng reload khi server không sinh FLV
MAX_PLAYER_RETRIES = 2

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
# PLAYER NAME
# ============================================================

def get_player_name(element):

    try:

        html = element.inner_html()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # Không lấy text nằm trong SVG
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
# GET PLAYER INFO
# ============================================================

def get_player_info(element, index):

    try:
        name = get_player_name(element)
    except Exception:
        name = ""

    try:
        server = (
            element.get_attribute("title")
            or ""
        )
    except Exception:
        server = ""

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

    return {
        "index": index,
        "name": name,
        "server": server,
        "data_link": data_link,
        "href": (
            urljoin(HOME_URL, href)
            if href
            else ""
        )
    }


# ============================================================
# PLAYER KEY
#
# Dùng data-link trước.
# Nếu không có thì dùng index.
#
# Không dùng tên player làm key vì:
#
# Server 2 = FILIP
# Server 3 = FILIP
#
# vẫn là 2 server khác nhau.
# ============================================================

def player_key(info):

    data_link = str(
        info.get("data_link", "")
    ).strip()

    if data_link:
        return f"data-link:{data_link}"

    return f"index:{info.get('index', -1)}"


# ============================================================
# FIND ACTIVE PLAYER
#
# Khi mở trang, website thường tự động active
# một player.
#
# Dựa vào class "playing" để nhận diện.
# ============================================================

def find_active_player_index(page):

    try:

        elements = page.locator(
            "a.player-link"
        ).all()

        for index, element in enumerate(elements):

            try:

                classes = (
                    element.get_attribute(
                        "class"
                    )
                    or ""
                )

                if "playing" in classes.split():

                    return index

            except Exception:
                continue

    except Exception:
        pass

    return None


# ============================================================
# GET FLV REQUESTS
# ============================================================

def create_flv_listener(page, captured_streams):

    def handle_request(request):

        url = request.url

        if not is_flv_url(url):
            return

        if url in captured_streams:
            return

        captured_streams.append(url)

        print(
            f"FLV detected: {url}",
            flush=True
        )

    page.on(
        "request",
        handle_request
    )


# ============================================================
# WAIT FOR STREAM
# ============================================================

def wait_for_stream(
    page,
    captured_streams,
    wait_ms=PLAYER_WAIT_MS
):

    # Chia nhỏ thời gian chờ để có thể thoát sớm
    # ngay khi đã bắt được FLV.

    interval = 250

    elapsed = 0

    while elapsed < wait_ms:

        if captured_streams:
            return True

        page.wait_for_timeout(
            interval
        )

        elapsed += interval

    return bool(captured_streams)


# ============================================================
# GET CURRENT PLAYERS
# ============================================================

def get_current_players(page):

    elements = page.locator(
        "a.player-link"
    ).all()

    players = []

    for index, element in enumerate(elements):

        info = get_player_info(
            element,
            index
        )

        players.append(info)

    return players


# ============================================================
# FIND SAME PLAYER AFTER RELOAD
# ============================================================

def find_player_element(
    page,
    target_info
):

    elements = page.locator(
        "a.player-link"
    ).all()

    target_data_link = str(
        target_info.get(
            "data_link",
            ""
        )
    ).strip()

    target_index = target_info.get(
        "index",
        -1
    )

    # --------------------------------------------------------
    # Ưu tiên data-link
    # --------------------------------------------------------

    if target_data_link:

        for element in elements:

            try:

                value = (
                    element.get_attribute(
                        "data-link"
                    )
                    or ""
                )

                if value == target_data_link:
                    return element

            except Exception:
                continue

    # --------------------------------------------------------
    # Fallback index
    # --------------------------------------------------------

    if (
        target_index >= 0
        and target_index < len(elements)
    ):

        return elements[target_index]

    return None


# ============================================================
# CLICK PLAYER
# ============================================================

def click_player(
    page,
    player_element,
    player_name
):

    try:

        # Scroll vào vùng nhìn thấy
        player_element.scroll_into_view_if_needed(
            timeout=3000
        )

    except Exception:
        pass

    try:

        player_element.click(
            timeout=7000
        )

        return True

    except Exception as error:

        print(
            f"Click failed: "
            f"{player_name} - {error}",
            flush=True
        )

        return False


# ============================================================
# LOAD MATCH PAGE
# ============================================================

def load_match_page(
    page,
    match_url
):

    page.goto(
        match_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    page.wait_for_timeout(
        1000
    )


# ============================================================
# GET MATCH TITLE
# ============================================================

def get_match_title(page):

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

    return title


# ============================================================
# CAPTURE AUTO-LOADED PLAYER
#
# Khi page vừa mở:
#
#   site tự load một server
#       ↓
#   request FLV
#
# Chúng ta bắt request đó.
#
# Không click lại active player.
# ============================================================

def capture_auto_loaded_player(
    page,
    players
):

    active_index = find_active_player_index(
        page
    )

    # --------------------------------------------------------
    # Nếu xác định được player active
    # --------------------------------------------------------

    if active_index is not None:

        print(
            f"Auto-loaded player: "
            f"#{active_index + 1}",
            flush=True
        )

        # Chờ request tự động xuất hiện
        streams = []

        listener = None

        def handle_request(request):

            url = request.url

            if not is_flv_url(url):
                return

            if url not in streams:

                streams.append(url)

                print(
                    f"Auto FLV detected: {url}",
                    flush=True
                )

        listener = handle_request

        page.on(
            "request",
            listener
        )

        wait_for_stream(
            page,
            streams,
            PLAYER_WAIT_MS
        )

        page.remove_listener(
            "request",
            listener
        )

        return (
            active_index,
            streams
        )

    # --------------------------------------------------------
    # Không xác định được active player
    #
    # Vẫn cho website thêm thời gian load.
    # --------------------------------------------------------

    streams = []

    listener = None

    def handle_fallback_request(request):

        url = request.url

        if not is_flv_url(url):
            return

        if url not in streams:

            streams.append(url)

            print(
                f"Auto FLV detected: {url}",
                flush=True
            )

    listener = handle_fallback_request

    page.on(
        "request",
        listener
    )

    wait_for_stream(
        page,
        streams,
        PLAYER_WAIT_MS
    )

    page.remove_listener(
        "request",
        listener
    )

    if streams and players:

        # Trong trường hợp không biết active,
        # mặc định stream tự load thuộc player đầu tiên.
        return (
            0,
            streams
        )

    return (
        None,
        streams
    )


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

            load_match_page(
                page,
                match_url
            )

            # ------------------------------------------------
            # GET TITLE
            # ------------------------------------------------

            title = get_match_title(
                page
            )

            # ------------------------------------------------
            # GET PLAYERS
            # ------------------------------------------------

            players = get_current_players(
                page
            )

            print(
                f"[{index}/{total}] "
                f"Players found: {len(players)}",
                flush=True
            )

            # ------------------------------------------------
            # RESULT STRUCTURE
            # ------------------------------------------------

            result_players = []

            for player in players:

                result_players.append({

                    "index": player["index"],

                    "name": player["name"],

                    "server": player["server"],

                    "data_link": player["data_link"],

                    "href": player["href"],

                    "stream_urls": []
                })

            # ------------------------------------------------
            # DONE SET
            #
            # Server đã bắt được stream thì không scrape lại.
            # ------------------------------------------------

            completed = set()

            # ------------------------------------------------
            # AUTO PLAYER
            #
            # Trang vừa mở đã tự load một server.
            # Bắt server đó trước.
            # ------------------------------------------------

            auto_index, auto_streams = (
                capture_auto_loaded_player(
                    page,
                    players
                )
            )

            if auto_index is not None:

                if (
                    0 <= auto_index
                    < len(result_players)
                ):

                    result_players[
                        auto_index
                    ]["stream_urls"] = list(
                        dict.fromkeys(
                            auto_streams
                        )
                    )

                    if auto_streams:

                        completed.add(
                            player_key(
                                players[auto_index]
                            )
                        )

                        print(
                            f"[{index}/{total}] "
                            f"AUTO PLAYER "
                            f"{auto_index + 1}: "
                            f"{players[auto_index]['name']} "
                            f"| streams="
                            f"{len(auto_streams)}",
                            flush=True
                        )

            # ------------------------------------------------
            # PROCESS REMAINING PLAYERS
            # ------------------------------------------------

            for player_index, target_player in enumerate(
                players
            ):

                target_key = player_key(
                    target_player
                )

                # --------------------------------------------
                # PLAYER ĐÃ XONG
                # --------------------------------------------

                if target_key in completed:

                    print(
                        f"[{index}/{total}] "
                        f"SKIP DONE PLAYER "
                        f"{player_index + 1}: "
                        f"{target_player['name']}",
                        flush=True
                    )

                    continue

                player_name = (
                    target_player["name"]
                    or target_player["server"]
                    or f"Server {player_index + 1}"
                )

                print(
                    f"[{index}/{total}] "
                    f"PROCESS PLAYER "
                    f"{player_index + 1}: "
                    f"name='{target_player['name']}' "
                    f"| server='{target_player['server']}'",
                    flush=True
                )

                success = False

                # ============================================
                # TRY + RELOAD RETRIES
                # ============================================

                for attempt in range(
                    MAX_PLAYER_RETRIES + 1
                ):

                    # ----------------------------------------
                    # RETRY > 0
                    #
                    # Reload page.
                    # Website sẽ lại auto-load player mặc định.
                    #
                    # Nhưng player mặc định đã DONE thì bỏ qua.
                    # ----------------------------------------

                    if attempt > 0:

                        print(
                            f"[{index}/{total}] "
                            f"RELOAD RETRY "
                            f"{attempt}/"
                            f"{MAX_PLAYER_RETRIES} "
                            f"FOR {player_name}",
                            flush=True
                        )

                        try:

                            load_match_page(
                                page,
                                match_url
                            )

                        except Exception as error:

                            print(
                                f"[{index}/{total}] "
                                f"Reload failed: "
                                f"{error}",
                                flush=True
                            )

                            continue

                    # ----------------------------------------
                    # Tìm lại target player sau reload.
                    #
                    # Dùng data-link trước index.
                    # ----------------------------------------

                    element = find_player_element(
                        page,
                        target_player
                    )

                    if element is None:

                        print(
                            f"[{index}/{total}] "
                            f"Player not found: "
                            f"{player_name}",
                            flush=True
                        )

                        continue

                    # ----------------------------------------
                    # Request list RIÊNG cho lần thử này.
                    # ----------------------------------------

                    captured_streams = []

                    def handle_request(
                        request
                    ):

                        url = request.url

                        if not is_flv_url(url):
                            return

                        if url not in captured_streams:

                            captured_streams.append(
                                url
                            )

                            print(
                                f"[{index}/{total}] "
                                f"FLV detected: "
                                f"{url}",
                                flush=True
                            )

                    page.on(
                        "request",
                        handle_request
                    )

                    # ----------------------------------------
                    # CLICK TARGET
                    # ----------------------------------------

                    click_ok = click_player(
                        page,
                        element,
                        player_name
                    )

                    if click_ok:

                        wait_for_stream(
                            page,
                            captured_streams,
                            PLAYER_WAIT_MS
                        )

                    # ----------------------------------------
                    # REMOVE LISTENER
                    # ----------------------------------------

                    page.remove_listener(
                        "request",
                        handle_request
                    )

                    # ----------------------------------------
                    # SAVE
                    # ----------------------------------------

                    if captured_streams:

                        result_players[
                            player_index
                        ]["stream_urls"] = list(
                            dict.fromkeys(
                                captured_streams
                            )
                        )

                        completed.add(
                            target_key
                        )

                        success = True

                        print(
                            f"[{index}/{total}] "
                            f"PLAYER: "
                            f"{player_name} "
                            f"| streams="
                            f"{len(captured_streams)}",
                            flush=True
                        )

                        break

                    # ----------------------------------------
                    # NO STREAM
                    # ----------------------------------------

                    print(
                        f"[{index}/{total}] "
                        f"No FLV for "
                        f"{player_name} "
                        f"on attempt "
                        f"{attempt + 1}",
                        flush=True
                    )

                # ============================================
                # FINAL STATUS
                # ============================================

                if not success:

                    print(
                        f"[{index}/{total}] "
                        f"PLAYER FAILED: "
                        f"{player_name}",
                        flush=True
                    )

            # ------------------------------------------------
            # CLOSE
            # ------------------------------------------------

            context.close()

            browser.close()

            total_streams = sum(
                len(
                    player.get(
                        "stream_urls",
                        []
                    )
                )
                for player in result_players
            )

            print(
                f"[{index}/{total}] OK "
                f"| players="
                f"{len(result_players)} "
                f"| streams="
                f"{total_streams} "
                f"| {title}",
                flush=True
            )

            return {

                "title": title,

                "url": match_url,

                "players": result_players
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

    start_time = time.time()

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

                    results.append(
                        result
                    )

            except Exception as error:

                print(
                    f"Worker exception: {error}",
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
    # STATISTICS
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

        for player in match["players"]
    )

    elapsed = (
        time.time()
        - start_time
    )

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

    # --------------------------------------------------------
    # QUALITY GATE
    # --------------------------------------------------------

    if len(results) != len(match_urls):

        print(
            "QUALITY GATE: FAIL",
            flush=True
        )

        raise RuntimeError(
            "Not all events were processed."
        )

    print(
        "QUALITY GATE: PASS",
        flush=True
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
