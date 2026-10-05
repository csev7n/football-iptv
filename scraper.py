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

# Không tăng quá cao vì mỗi worker tạo Chromium riêng.
MAX_WORKERS = 3

PAGE_TIMEOUT = 20000

# ------------------------------------------------------------
# PLAYER DISCOVERY
# ------------------------------------------------------------

PLAYER_DISCOVERY_TIMEOUT_MS = 5000

# ------------------------------------------------------------
# STREAM CAPTURE
# ------------------------------------------------------------

# Chờ tối đa sau khi click một server.
# Quan trọng:
# đây là MAX timeout, không phải thời gian luôn phải chờ.
#
# Nếu FLV xuất hiện sau 0.5s -> kết thúc ngay.
# Nếu FLV xuất hiện sau 2s -> kết thúc ngay.
# Nếu không có -> tối đa 3000ms.
# ------------------------------------------------------------

CLICK_STREAM_TIMEOUT_MS = 3000

# Khoảng polling network/player.
POLL_MS = 100

# ------------------------------------------------------------
# RETRY
# ------------------------------------------------------------

# Click lại tối đa 1 lần.
# Không retry nhiều lần để tránh làm scraper chậm.
MAX_CLICK_RETRIES = 1

# Reload chỉ dùng khi thật sự cần.
MAX_RELOAD_RETRIES = 1

# Sau reload chỉ chờ ngắn để bắt auto-load.
RELOAD_AUTO_WAIT_MS = 2000

# ------------------------------------------------------------
# NETWORK
# ------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}


# ============================================================
# BASIC
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


def normalize_match_url(url):

    url = re.sub(
        r"/link/\d+/?$",
        "/",
        url
    )

    return url.rstrip("/") + "/"


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

        matches.add(
            normalize_match_url(full_url)
        )

    return sorted(matches)


def is_flv_url(url):

    if not url:
        return False

    return ".flv" in url.lower()


# ============================================================
# PLAYER INFORMATION
# ============================================================

def get_player_name(element):

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

        return re.sub(
            r"\s+",
            " ",
            name
        ).strip()

    except Exception:

        return ""


def get_player_info(
    element,
    index
):

    try:
        name = get_player_name(
            element
        )
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

    try:
        classes = (
            element.get_attribute("class")
            or ""
        )
    except Exception:
        classes = ""

    return {
        "index": index,
        "name": name,
        "server": server,
        "data_link": data_link,
        "href": (
            urljoin(HOME_URL, href)
            if href
            else ""
        ),
        "classes": classes
    }


def make_player_fingerprint(info):

    """
    Fingerprint dùng để tìm lại element sau reload.

    KHÔNG dùng fingerprint làm identity.

    Hai server có thể hoàn toàn giống nhau,
    vì vậy instance_id + DOM occurrence vẫn là
    identity chính.
    """

    return (
        str(info.get("name", "")).strip(),
        str(info.get("server", "")).strip(),
        str(info.get("data_link", "")).strip(),
        str(info.get("href", "")).strip()
    )


# ============================================================
# PLAYER DOM
# ============================================================

def get_player_elements(page):

    try:

        return page.locator(
            "a.player-link"
        ).all()

    except Exception:

        return []


def get_current_players(page):

    players = []

    elements = get_player_elements(
        page
    )

    for index, element in enumerate(elements):

        try:

            players.append(
                get_player_info(
                    element,
                    index
                )
            )

        except Exception:
            continue

    return players


def wait_for_players(
    page,
    timeout_ms=PLAYER_DISCOVERY_TIMEOUT_MS
):

    started = time.monotonic()

    last_count = -1

    while True:

        players = get_current_players(
            page
        )

        if len(players) != last_count:

            print(
                f"[DOM] players={len(players)}",
                flush=True
            )

            last_count = len(players)

        if players:

            return players

        elapsed = (
            time.monotonic()
            - started
        ) * 1000

        if elapsed >= timeout_ms:

            return players

        try:

            page.wait_for_timeout(
                POLL_MS
            )

        except Exception:

            break

    return []


# ============================================================
# SERVER INSTANCE
# ============================================================

def make_instance(
    info,
    instance_id
):

    return {
        # ----------------------------------------------------
        # CANONICAL IDENTITY
        # ----------------------------------------------------

        "instance_id": instance_id,

        "original_index": info["index"],

        # ----------------------------------------------------
        # PUBLIC DATA
        # ----------------------------------------------------

        "name": info["name"],
        "server": info["server"],
        "data_link": info["data_link"],
        "href": info["href"],

        # ----------------------------------------------------
        # STREAM
        # ----------------------------------------------------

        "stream_urls": [],

        # ----------------------------------------------------
        # STATE
        # ----------------------------------------------------

        "status": "discovered",

        # Internal
        "_fingerprint": make_player_fingerprint(
            info
        ),

        "_attempts": 0
    }


def build_server_registry(players):

    registry = []

    for index, player in enumerate(players):

        registry.append(
            make_instance(
                player,
                instance_id=f"instance-{index}"
            )
        )

    return registry


# ============================================================
# FIND ELEMENT FOR INSTANCE
# ============================================================

def find_element_for_instance(
    page,
    registry,
    instance
):

    elements = get_player_elements(
        page
    )

    if not elements:
        return None

    target_fingerprint = instance[
        "_fingerprint"
    ]

    # --------------------------------------------------------
    # Tính occurrence của fingerprint
    # --------------------------------------------------------

    target_occurrence = 0

    for other in registry:

        if other["instance_id"] == instance[
            "instance_id"
        ]:
            break

        if (
            other["_fingerprint"]
            == target_fingerprint
        ):

            target_occurrence += 1

    matching_elements = []

    for index, element in enumerate(elements):

        try:

            info = get_player_info(
                element,
                index
            )

            fingerprint = make_player_fingerprint(
                info
            )

            if fingerprint == target_fingerprint:

                matching_elements.append(
                    element
                )

        except Exception:

            continue

    # --------------------------------------------------------
    # Fingerprint + occurrence
    # --------------------------------------------------------

    if matching_elements:

        if target_occurrence < len(
            matching_elements
        ):

            return matching_elements[
                target_occurrence
            ]

        return matching_elements[-1]

    # --------------------------------------------------------
    # Original index fallback
    # --------------------------------------------------------

    original_index = instance[
        "original_index"
    ]

    if (
        0 <= original_index < len(elements)
    ):

        return elements[
            original_index
        ]

    return None


# ============================================================
# ACTIVE PLAYER
# ============================================================

def get_active_player_index(page):

    elements = get_player_elements(
        page
    )

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

    return None


# ============================================================
# ACTIVE PLAYER TRACKER
# ============================================================

class ActivePlayerTracker:

    def __init__(
        self,
        page,
        registry
    ):

        self.page = page

        self.registry = registry

        self.timeline = []

    def snapshot(self):

        index = get_active_player_index(
            self.page
        )

        if index is None:
            return

        now = time.monotonic()

        # Không ghi lặp liên tục cùng index.
        if (
            self.timeline
            and self.timeline[-1]["index"]
            == index
        ):

            return

        event = {
            "time": now,
            "index": index
        }

        self.timeline.append(
            event
        )

        if (
            0 <= index < len(self.registry)
        ):

            instance = self.registry[
                index
            ]

            print(
                "[ACTIVE] "
                f"{instance['instance_id']} | "
                f"{instance['name'] or instance['server']}",
                flush=True
            )

    def observe(
        self,
        duration_ms
    ):

        started = time.monotonic()

        while True:

            self.snapshot()

            elapsed = (
                time.monotonic()
                - started
            ) * 1000

            if elapsed >= duration_ms:
                break

            try:

                self.page.wait_for_timeout(
                    POLL_MS
                )

            except Exception:

                break

    def resolve_instance_at(
        self,
        event_time
    ):

        candidate = None

        for event in self.timeline:

            if event["time"] <= event_time:

                candidate = event

            else:

                break

        if candidate is None:
            return None

        index = candidate["index"]

        if (
            0 <= index < len(self.registry)
        ):

            return self.registry[
                index
            ]

        return None


# ============================================================
# FLV COLLECTOR
# ============================================================

class FLVCollector:

    def __init__(
        self,
        page
    ):

        self.page = page

        self.events = []

        self.all_streams = set()

        self.current_session = None

        self.session_counter = 0

        # Listener phải được cài trước goto.
        page.on(
            "request",
            self.on_request
        )

        page.on(
            "response",
            self.on_response
        )

    # ========================================================
    # SESSION
    # ========================================================

    def start_session(
        self,
        instance
    ):

        self.session_counter += 1

        session = {
            "id": self.session_counter,

            "instance_id": instance[
                "instance_id"
            ],

            "started_at": time.monotonic(),

            "streams": []
        }

        self.current_session = session

        return session

    def end_session(self):

        session = self.current_session

        self.current_session = None

        return session

    # ========================================================
    # REQUEST
    # ========================================================

    def on_request(
        self,
        request
    ):

        url = request.url

        if not is_flv_url(url):
            return

        now = time.monotonic()

        session_id = None

        if self.current_session:

            session_id = (
                self.current_session["id"]
            )

        event = {
            "type": "request",
            "url": url,
            "time": now,
            "session_id": session_id
        }

        self.events.append(
            event
        )

        self.all_streams.add(
            url
        )

        print(
            f"[FLV] {url}",
            flush=True
        )

        # ----------------------------------------------------
        # Click session có owner rõ ràng.
        # ----------------------------------------------------

        if self.current_session:

            if url not in (
                self.current_session["streams"]
            ):

                self.current_session[
                    "streams"
                ].append(url)

    # ========================================================
    # RESPONSE
    # ========================================================

    def on_response(
        self,
        response
    ):

        url = response.url

        if not is_flv_url(url):
            return

        try:

            status = response.status

        except Exception:

            status = None

        try:

            content_type = (
                response.headers.get(
                    "content-type",
                    ""
                )
            )

        except Exception:

            content_type = ""

        self.events.append({
            "type": "response",
            "url": url,
            "time": time.monotonic(),
            "status": status,
            "content_type": content_type
        })

    # ========================================================
    # WAIT FOR CLICK SESSION
    # ========================================================

    def wait_for_stream(
        self,
        session,
        tracker,
        timeout_ms
    ):

        started = time.monotonic()

        while True:

            # Ghi active state.
            tracker.snapshot()

            # FLV xuất hiện -> KẾT THÚC NGAY.
            if session["streams"]:

                return True

            elapsed = (
                time.monotonic()
                - started
            ) * 1000

            if elapsed >= timeout_ms:

                break

            try:

                self.page.wait_for_timeout(
                    POLL_MS
                )

            except Exception:

                break

        return bool(
            session["streams"]
        )


# ============================================================
# AUTO STREAM MAPPING
# ============================================================

def resolve_auto_streams(
    collector,
    tracker,
    registry
):

    """
    FLV request không thuộc click session
    được map theo active-player timeline.
    """

    assignments = {}

    for event in collector.events:

        if event["type"] != "request":
            continue

        # Click stream đã có owner.
        if event.get("session_id") is not None:
            continue

        url = event["url"]

        instance = tracker.resolve_instance_at(
            event["time"]
        )

        if instance is None:

            continue

        instance_id = instance[
            "instance_id"
        ]

        assignments.setdefault(
            instance_id,
            []
        )

        if url not in assignments[
            instance_id
        ]:

            assignments[
                instance_id
            ].append(url)

    # --------------------------------------------------------
    # Apply
    # --------------------------------------------------------

    for instance in registry:

        instance_id = instance[
            "instance_id"
        ]

        streams = assignments.get(
            instance_id,
            []
        )

        for url in streams:

            if url not in instance[
                "stream_urls"
            ]:

                instance[
                    "stream_urls"
                ].append(url)

        if streams:

            instance["status"] = (
                "stream_found"
            )

            print(
                "[AUTO MAP] "
                f"{instance_id} "
                f"{instance['name'] or instance['server']} "
                f"→ {len(streams)} FLV",
                flush=True
            )


# ============================================================
# CLICK PLAYER
# ============================================================

def click_player(
    page,
    element,
    label
):

    try:

        element.scroll_into_view_if_needed(
            timeout=2000
        )

    except Exception:

        pass

    try:

        element.click(
            timeout=5000
        )

        print(
            f"[CLICK] {label}",
            flush=True
        )

        return True

    except Exception as error:

        print(
            f"[CLICK FAILED] "
            f"{label}: {error}",
            flush=True
        )

        return False


# ============================================================
# PROCESS ONE INSTANCE
# ============================================================

def process_instance(
    page,
    collector,
    tracker,
    registry,
    instance
):

    label = (
        instance["name"]
        or instance["server"]
        or (
            f"Server "
            f"{instance['original_index'] + 1}"
        )
    )

    # --------------------------------------------------------
    # Already have stream.
    # --------------------------------------------------------

    if instance["stream_urls"]:

        instance["status"] = (
            "stream_found"
        )

        return True

    # --------------------------------------------------------
    # Try click.
    # --------------------------------------------------------

    for attempt in range(
        MAX_CLICK_RETRIES + 1
    ):

        instance["_attempts"] += 1

        print(
            f"[PROCESS] "
            f"{instance['instance_id']} "
            f"{label} "
            f"attempt={attempt + 1}",
            flush=True
        )

        element = find_element_for_instance(
            page,
            registry,
            instance
        )

        if element is None:

            print(
                f"[NOT FOUND] "
                f"{instance['instance_id']}",
                flush=True
            )

            instance["status"] = (
                "not_found"
            )

            return False

        # ----------------------------------------------------
        # Session BEFORE click.
        # ----------------------------------------------------

        session = collector.start_session(
            instance
        )

        clicked = click_player(
            page,
            element,
            label
        )

        if clicked:

            got_stream = (
                collector.wait_for_stream(
                    session,
                    tracker,
                    CLICK_STREAM_TIMEOUT_MS
                )
            )

        else:

            got_stream = False

        collector.end_session()

        # ----------------------------------------------------
        # Save stream.
        # ----------------------------------------------------

        streams = list(
            dict.fromkeys(
                session["streams"]
            )
        )

        if streams:

            for url in streams:

                if url not in instance[
                    "stream_urls"
                ]:

                    instance[
                        "stream_urls"
                    ].append(url)

            instance["status"] = (
                "stream_found"
            )

            print(
                "[MAP] "
                f"{instance['instance_id']} "
                f"{label} "
                f"→ {len(streams)} FLV",
                flush=True
            )

            return True

        # ----------------------------------------------------
        # No stream.
        #
        # Retry tối đa 1 lần.
        # ----------------------------------------------------

        print(
            "[PENDING] "
            f"{instance['instance_id']} "
            f"{label} "
            f"has no FLV yet",
            flush=True
        )

    instance["status"] = (
        "pending"
    )

    return False


# ============================================================
# RELOAD
# ============================================================

def reload_match(
    page,
    match_url
):

    print(
        "[RECOVERY] Reload...",
        flush=True
    )

    page.goto(
        match_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    try:

        page.wait_for_timeout(
            500
        )

    except Exception:

        pass


# ============================================================
# MATCH TITLE
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
# PRINT REGISTRY
# ============================================================

def print_registry(
    registry,
    prefix=""
):

    print(
        f"{prefix}SERVER REGISTRY",
        flush=True
    )

    for instance in registry:

        print(
            f"{prefix}"
            f"{instance['instance_id']} | "
            f"index={instance['original_index']} | "
            f"name={instance['name']!r} | "
            f"server={instance['server']!r} | "
            f"data-link={instance['data_link']!r}",
            flush=True
        )


# ============================================================
# OUTPUT
# ============================================================

def output_player(instance):

    return {
        "index": instance[
            "original_index"
        ],

        "name": instance[
            "name"
        ],

        "server": instance[
            "server"
        ],

        "data_link": instance[
            "data_link"
        ],

        "href": instance[
            "href"
        ],

        "stream_urls": list(
            dict.fromkeys(
                instance[
                    "stream_urls"
                ]
            )
        )
    }


# ============================================================
# MATCH QUALITY
# ============================================================

def classify_match(registry):

    total = len(registry)

    with_stream = sum(
        1
        for instance in registry
        if instance["stream_urls"]
    )

    if total == 0:

        return "no_server"

    if with_stream == total:

        return "ready"

    if with_stream == 0:

        return "no_stream"

    return "partial"


# ============================================================
# SCRAPE ONE MATCH
# ============================================================

def scrape_one_match(
    match_url,
    index,
    total_matches
):

    print(
        f"[{index}/{total_matches}] START "
        f"{match_url}",
        flush=True
    )

    browser = None
    context = None

    try:

        with sync_playwright() as p:

            # ------------------------------------------------
            # Browser
            # ------------------------------------------------

            browser = p.chromium.launch(
                headless=True
            )

            context = browser.new_context(
                user_agent=HEADERS[
                    "User-Agent"
                ]
            )

            page = context.new_page()

            # ------------------------------------------------
            # IMPORTANT:
            #
            # Collector phải được tạo TRƯỚC goto.
            # ------------------------------------------------

            collector = FLVCollector(
                page
            )

            registry = []

            tracker = ActivePlayerTracker(
                page,
                registry
            )

            # ------------------------------------------------
            # GOTO
            # ------------------------------------------------

            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

            # ------------------------------------------------
            # DISCOVERY
            # ------------------------------------------------

            players = wait_for_players(
                page
            )

            if not players:

                print(
                    f"[{index}/{total_matches}] "
                    "NO PLAYERS",
                    flush=True
                )

                return None

            # ------------------------------------------------
            # Canonical registry
            # ------------------------------------------------

            registry = build_server_registry(
                players
            )

            tracker.registry = registry

            print_registry(
                registry,
                prefix=(
                    f"[{index}/{total_matches}] "
                )
            )

            # ------------------------------------------------
            # AUTO-LOAD OBSERVATION
            #
            # Không chờ quá lâu.
            # Chỉ quan sát 2 giây.
            # ------------------------------------------------

            tracker.observe(
                RELOAD_AUTO_WAIT_MS
            )

            # ------------------------------------------------
            # Map auto FLV.
            # ------------------------------------------------

            resolve_auto_streams(
                collector,
                tracker,
                registry
            )

            # ------------------------------------------------
            # CLICK ONLY MISSING
            # ------------------------------------------------

            for instance in registry:

                if instance[
                    "stream_urls"
                ]:

                    continue

                process_instance(
                    page,
                    collector,
                    tracker,
                    registry,
                    instance
                )

            # ------------------------------------------------
            # AUTO MAP AGAIN
            #
            # Một số click có thể kích hoạt
            # player khác sau đó.
            # ------------------------------------------------

            tracker.observe(
                500
            )

            resolve_auto_streams(
                collector,
                tracker,
                registry
            )

            # ------------------------------------------------
            # RECOVERY
            #
            # Chỉ reload nếu:
            #
            # 1. Có server missing
            # 2. Đã thử click
            # 3. Có khả năng player thực sự bị lỗi
            #
            # Không reload chỉ vì trận chưa bắt đầu.
            # ------------------------------------------------

            missing = [
                instance
                for instance in registry
                if not instance[
                    "stream_urls"
                ]
            ]

            reload_count = 0

            if missing:

                # Chỉ recovery một lần.
                while (
                    reload_count
                    < MAX_RELOAD_RETRIES
                ):

                    reload_count += 1

                    print(
                        f"[{index}/{total_matches}] "
                        f"RECOVERY: "
                        f"{len(missing)} missing",
                        flush=True
                    )

                    try:

                        reload_match(
                            page,
                            match_url
                        )

                    except Exception as error:

                        print(
                            f"[RECOVERY ERROR] "
                            f"{error}",
                            flush=True
                        )

                        break

                    # ------------------------------------------------
                    # DOM sau reload
                    # ------------------------------------------------

                    fresh_players = (
                        wait_for_players(page)
                    )

                    if not fresh_players:

                        break

                    # ------------------------------------------------
                    # Timeline mới.
                    # ------------------------------------------------

                    tracker.timeline = []

                    # ------------------------------------------------
                    # Quan sát auto-load ngắn.
                    # ------------------------------------------------

                    tracker.observe(
                        RELOAD_AUTO_WAIT_MS
                    )

                    resolve_auto_streams(
                        collector,
                        tracker,
                        registry
                    )

                    # ------------------------------------------------
                    # Click lại ONLY missing.
                    # ------------------------------------------------

                    for instance in registry:

                        if instance[
                            "stream_urls"
                        ]:

                            continue

                        process_instance(
                            page,
                            collector,
                            tracker,
                            registry,
                            instance
                        )

                    missing = [
                        instance
                        for instance in registry
                        if not instance[
                            "stream_urls"
                        ]
                    ]

                    if not missing:

                        break

            # ------------------------------------------------
            # FINAL AUTO MAP
            # ------------------------------------------------

            resolve_auto_streams(
                collector,
                tracker,
                registry
            )

            # ------------------------------------------------
            # CLASSIFY
            # ------------------------------------------------

            status = classify_match(
                registry
            )

            mapped = sum(
                1
                for instance in registry
                if instance[
                    "stream_urls"
                ]
            )

            total_streams = sum(
                len(
                    instance[
                        "stream_urls"
                    ]
                )
                for instance in registry
            )

            print(
                f"[{index}/{total_matches}] "
                f"RESULT: "
                f"{status.upper()} "
                f"{mapped}/{len(registry)} servers "
                f"| {total_streams} FLV",
                flush=True
            )

            # ------------------------------------------------
            # Detailed mapping
            # ------------------------------------------------

            for instance in registry:

                label = (
                    instance["name"]
                    or instance["server"]
                    or (
                        f"Server "
                        f"{instance['original_index'] + 1}"
                    )
                )

                if instance[
                    "stream_urls"
                ]:

                    print(
                        f"[MAPPED] "
                        f"{instance['instance_id']} "
                        f"{label} "
                        f"→ "
                        f"{len(instance['stream_urls'])} FLV",
                        flush=True
                    )

                else:

                    print(
                        f"[PENDING] "
                        f"{instance['instance_id']} "
                        f"{label}",
                        flush=True
                    )

            # ------------------------------------------------
            # Output
            # ------------------------------------------------

            result_players = [
                output_player(instance)
                for instance in registry
            ]

            title = get_match_title(
                page
            )

            result = {
                "title": title,

                "url": match_url,

                "players": result_players
            }

            return result

    except Exception as error:

        print(
            f"[{index}/{total_matches}] ERROR",
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

    finally:

        try:

            if context:

                context.close()

        except Exception:

            pass

        try:

            if browser:

                browser.close()

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

    # ========================================================
    # HOME
    # ========================================================

    html = get_home_page()

    # ========================================================
    # FIND MATCHES
    # ========================================================

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

    # ========================================================
    # WORKERS
    # ========================================================

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
                    f"Worker exception: "
                    f"{error}",
                    flush=True
                )

                print(
                    f"URL: {match_url}",
                    flush=True
                )

    # ========================================================
    # SORT
    # ========================================================

    results.sort(
        key=lambda x: x["url"]
    )

    # ========================================================
    # SAVE
    # ========================================================

    save_results(
        results
    )

    # ========================================================
    # STATISTICS
    # ========================================================

    total_players = sum(
        len(match["players"])
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

    ready_matches = sum(
        1
        for match in results
        if match["players"]
        and all(
            player.get(
                "stream_urls"
            )
            for player in match["players"]
        )
    )

    partial_matches = sum(
        1
        for match in results
        if any(
            player.get(
                "stream_urls"
            )
            for player in match["players"]
        )
        and any(
            not player.get(
                "stream_urls"
            )
            for player in match["players"]
        )
    )

    no_stream_matches = sum(
        1
        for match in results
        if match["players"]
        and not any(
            player.get(
                "stream_urls"
            )
            for player in match["players"]
        )
    )

    incomplete_matches = (
        partial_matches
        + no_stream_matches
    )

    elapsed = (
        time.time()
        - start_time
    )

    # ========================================================
    # VALIDATION
    # ========================================================

    print()
    print("=" * 70)
    print("SCRAPER VALIDATION")
    print("=" * 70)

    print(
        f"Events found:          "
        f"{len(match_urls)}"
    )

    print(
        f"Events processed:      "
        f"{len(results)}"
    )

    print(
        f"Total server instances: "
        f"{total_players}"
    )

    print(
        f"Servers with stream:   "
        f"{players_with_stream}"
    )

    print(
        f"Total FLV streams:     "
        f"{total_streams}"
    )

    print(
        f"Ready matches:         "
        f"{ready_matches}"
    )

    print(
        f"Partial matches:       "
        f"{partial_matches}"
    )

    print(
        f"No-stream matches:     "
        f"{no_stream_matches}"
    )

    print(
        f"Elapsed:               "
        f"{elapsed:.1f}s"
    )

    print("=" * 70)

    # ========================================================
    # QUALITY GATE
    #
    # Chỉ fail nếu không scrape được toàn bộ events.
    #
    # Partial không phải fatal:
    # server có thể chưa có FLV vì trận chưa bắt đầu.
    # ========================================================

    if len(results) != len(
        match_urls
    ):

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

    print("=" * 70)

    print(
        "SCRAPER COMPLETE"
    )

    print("=" * 70)

    print(
        f"Output file: "
        f"{OUTPUT_FILE}"
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
