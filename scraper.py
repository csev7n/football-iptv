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

# Chờ player xuất hiện sau khi mở trang
PLAYER_DISCOVERY_TIMEOUT_MS = 6000

# Poll trạng thái active player
PLAYER_STATE_POLL_MS = 100

# Chờ FLV sau click
PLAYER_WAIT_MS = 5000

# Click lại cùng player
MAX_CLICK_RETRIES = 2

# Reload để recovery player bị thiếu
MAX_RELOAD_RETRIES = 1

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
# PLAYER DOM INFORMATION
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


# ============================================================
# SERVER INSTANCE
#
# QUAN TRỌNG:
#
# Không dùng name.
# Không dùng data-link.
# Không dùng href.
#
# Vì nhiều server có thể giống nhau hoàn toàn.
#
# instance_id được tạo theo DOM order.
#
# Ví dụ:
#
#   instance-0
#   instance-1
#   instance-2
#   instance-3
#
# ============================================================

def make_instance(
    info,
    instance_id
):

    return {
        "instance_id": instance_id,

        # Vị trí ban đầu trong DOM
        "original_index": info["index"],

        "name": info["name"],
        "server": info["server"],
        "data_link": info["data_link"],
        "href": info["href"],

        # Stream cuối cùng
        "stream_urls": [],

        # Internal metadata
        "_fingerprint": make_player_fingerprint(
            info
        ),

        "_mapped": False,
        "_attempts": 0,
        "_last_session": None
    }


def make_player_fingerprint(info):

    """
    Fingerprint KHÔNG phải unique ID.

    Nó chỉ giúp tìm lại đúng loại DOM element
    sau reload.

    Nếu nhiều server hoàn toàn giống nhau,
    occurrence/order sẽ được dùng thêm.
    """

    return (
        str(info.get("name", "")).strip(),
        str(info.get("server", "")).strip(),
        str(info.get("data_link", "")).strip(),
        str(info.get("href", "")).strip()
    )


# ============================================================
# GET CURRENT PLAYER ELEMENTS
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


# ============================================================
# INITIAL SERVER REGISTRY
# ============================================================

def build_server_registry(players):

    registry = []

    for index, player in enumerate(players):

        instance = make_instance(
            player,
            instance_id=f"instance-{index}"
        )

        registry.append(
            instance
        )

    return registry


# ============================================================
# PLAYER INSTANCE MATCHING
#
# Sau reload:
#
# 1. fingerprint
# 2. occurrence
# 3. original position fallback
#
# Không bao giờ chỉ tìm bằng name.
# ============================================================

def find_element_for_instance(
    page,
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

    target_occurrence = 0

    # Đếm occurrence của fingerprint
    # trong registry trước instance hiện tại.
    for other in SERVER_REGISTRY_CONTEXT:

        if other is instance:
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

    if matching_elements:

        if target_occurrence < len(
            matching_elements
        ):

            return matching_elements[
                target_occurrence
            ]

        return matching_elements[-1]

    # Fallback original DOM index
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


# Global context chỉ dùng trong một match.
# Vì worker chạy song song nên mỗi worker có process-local
# Python execution, nhưng để tránh phụ thuộc global,
# hàm dưới sẽ được thiết lập ngay trước thao tác.
SERVER_REGISTRY_CONTEXT = []


# ============================================================
# ACTIVE PLAYER DETECTION
# ============================================================

def get_active_instance_index(page):

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

            class_list = classes.split()

            if "playing" in class_list:

                return index

        except Exception:
            continue

    return None


# ============================================================
# ACTIVE PLAYER TRACKER
#
# Ghi lịch sử:
#
# time → DOM player index
#
# Điều này cho phép xử lý auto-load:
#
#   Server 3 active
#       ↓
#   FLV request
#       ↓
#   Server 3 → FLV
#
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

        self.running = False

    def snapshot(self):

        active_index = (
            get_active_instance_index(
                self.page
            )
        )

        now = time.monotonic()

        if active_index is None:

            return

        if (
            self.timeline
            and self.timeline[-1]["index"]
            == active_index
        ):

            return

        event = {
            "time": now,
            "index": active_index
        }

        self.timeline.append(
            event
        )

        if (
            0 <= active_index
            < len(self.registry)
        ):

            instance = self.registry[
                active_index
            ]

            print(
                "[ACTIVE] "
                f"{instance['instance_id']} "
                f"| "
                f"{instance['name'] or instance['server']}",
                flush=True
            )

    def run_for(
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
                    PLAYER_STATE_POLL_MS
                )

            except Exception:
                break

    def start_background_observation(
        self,
        duration_ms
    ):

        # Playwright sync API không cần thread riêng.
        # Collector sẽ gọi observe trong các khoảng chờ.
        self.run_for(
            duration_ms
        )

    def resolve_instance_at(
        self,
        event_time
    ):

        """
        Tìm active player gần nhất trước FLV event.
        """

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
        page,
        registry,
        active_tracker
    ):

        self.page = page

        self.registry = registry

        self.active_tracker = active_tracker

        self.events = []

        self.all_streams = set()

        self.current_session = None

        self.session_counter = 0

        # Listener được bật ngay khi object tạo.
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

        instance["_last_session"] = (
            session["id"]
        )

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
        # Nếu đang click một server:
        # map trực tiếp vào session.
        # ----------------------------------------------------

        if self.current_session:

            if url not in (
                self.current_session["streams"]
            ):

                self.current_session[
                    "streams"
                ].append(url)

            return

        # ----------------------------------------------------
        # Auto-load:
        #
        # KHÔNG đoán Server 1.
        #
        # Được resolve sau bằng active timeline.
        # ----------------------------------------------------

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

        now = time.monotonic()

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
            "time": now,
            "status": status,
            "content_type": content_type
        })

    # ========================================================
    # WAIT SESSION
    # ========================================================

    def wait_for_session(
        self,
        session,
        tracker,
        timeout_ms
    ):

        started = time.monotonic()

        while True:

            tracker.snapshot()

            if session["streams"]:

                return True

            elapsed = (
                time.monotonic()
                - started
            ) * 1000

            if elapsed >= timeout_ms:

                break

            self.page.wait_for_timeout(
                PLAYER_STATE_POLL_MS
            )

        return bool(
            session["streams"]
        )


# ============================================================
# RESOLVE AUTO STREAMS
# ============================================================

def resolve_auto_streams(
    collector,
    tracker,
    registry
):

    """
    Tất cả FLV request không thuộc click session
    được ghép với active-player timeline.
    """

    assignments = {}

    for event in collector.events:

        if event["type"] != "request":
            continue

        # Stream do click đã có owner rõ ràng.
        if event.get("session_id") is not None:
            continue

        url = event["url"]

        instance = (
            tracker.resolve_instance_at(
                event["time"]
            )
        )

        if instance is None:

            print(
                "[AUTO] FLV chưa xác định server: "
                f"{url}",
                flush=True
            )

            continue

        instance_id = instance[
            "instance_id"
        ]

        if instance_id not in assignments:

            assignments[
                instance_id
            ] = []

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

            instance["_mapped"] = True

            print(
                "[AUTO MAP] "
                f"{instance_id} "
                f"{instance['name'] or instance['server']} "
                f"→ {len(streams)} FLV",
                flush=True
            )


# ============================================================
# WAIT FOR PLAYER DOM
# ============================================================

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

        page.wait_for_timeout(
            PLAYER_STATE_POLL_MS
        )

    return []


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
            timeout=3000
        )

    except Exception:
        pass

    try:

        element.click(
            timeout=7000
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
# PROCESS ONE SERVER INSTANCE
# ============================================================

def process_instance(
    page,
    collector,
    tracker,
    instance,
    match_index,
    total_matches
):

    label = (
        instance["name"]
        or instance["server"]
        or f"Server {instance['original_index'] + 1}"
    )

    # --------------------------------------------------------
    # Already mapped
    # --------------------------------------------------------

    if instance["stream_urls"]:

        print(
            f"[{match_index}/{total_matches}] "
            f"SKIP {instance['instance_id']} "
            f"{label} "
            f"(already mapped)",
            flush=True
        )

        return True

    # --------------------------------------------------------
    # Attempts
    # --------------------------------------------------------

    for attempt in range(
        MAX_CLICK_RETRIES + 1
    ):

        instance["_attempts"] += 1

        print(
            f"[{match_index}/{total_matches}] "
            f"PROCESS "
            f"{instance['instance_id']} "
            f"{label} "
            f"attempt={attempt + 1}",
            flush=True
        )

        # ----------------------------------------------------
        # Tìm element hiện tại.
        # ----------------------------------------------------

        global SERVER_REGISTRY_CONTEXT

        SERVER_REGISTRY_CONTEXT = (
            CURRENT_SERVER_REGISTRY
        )

        element = find_element_for_instance(
            page,
            instance
        )

        if element is None:

            print(
                f"[PROCESS] Cannot find "
                f"{instance['instance_id']}",
                flush=True
            )

            return False

        # ----------------------------------------------------
        # Session phải tạo TRƯỚC click.
        # ----------------------------------------------------

        session = collector.start_session(
            instance
        )

        click_ok = click_player(
            page,
            element,
            label
        )

        if click_ok:

            collector.wait_for_session(
                session,
                tracker,
                PLAYER_WAIT_MS
            )

        collector.end_session()

        # ----------------------------------------------------
        # Save streams
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

            instance["_mapped"] = True

            print(
                f"[MAP] "
                f"{instance['instance_id']} "
                f"{label} "
                f"→ {len(streams)} FLV",
                flush=True
            )

            return True

        print(
            f"[NO FLV] "
            f"{instance['instance_id']} "
            f"{label}",
            flush=True
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
        "[RECOVERY] Reload page...",
        flush=True
    )

    page.goto(
        match_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    page.wait_for_timeout(
        1000
    )


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
# BUILD OUTPUT PLAYER
#
# Bỏ internal fields.
# Giữ format cũ để file khác không phải sửa.
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
# SCRAPE ONE MATCH
# ============================================================

def scrape_one_match(
    match_url,
    index,
    total
):

    global CURRENT_SERVER_REGISTRY

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
                user_agent=HEADERS[
                    "User-Agent"
                ]
            )

            page = context.new_page()

            # ------------------------------------------------
            # IMPORTANT:
            #
            # Registry chưa có nhưng collector phải được tạo
            # trước goto để không mất FLV auto-load sớm.
            # ------------------------------------------------

            registry = []

            CURRENT_SERVER_REGISTRY = (
                registry
            )

            tracker = ActivePlayerTracker(
                page,
                registry
            )

            collector = FLVCollector(
                page,
                registry,
                tracker
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
            # DOM
            # ------------------------------------------------

            players = wait_for_players(
                page
            )

            if not players:

                print(
                    f"[{index}/{total}] "
                    "NO PLAYERS",
                    flush=True
                )

                context.close()
                browser.close()

                return None

            # ------------------------------------------------
            # Build registry NGAY khi thấy player.
            # ------------------------------------------------

            registry = build_server_registry(
                players
            )

            CURRENT_SERVER_REGISTRY = (
                registry
            )

            tracker.registry = registry

            collector.registry = registry

            print_registry(
                registry,
                prefix=f"[{index}/{total}] "
            )

            # ------------------------------------------------
            # Theo dõi active state sau DOM discovery.
            #
            # Khoảng này bắt auto-load.
            # ------------------------------------------------

            tracker.run_for(
                PLAYER_WAIT_MS
            )

            # ------------------------------------------------
            # Auto stream mapping
            # ------------------------------------------------

            resolve_auto_streams(
                collector,
                tracker,
                registry
            )

            # ------------------------------------------------
            # Process từng server instance chưa có stream.
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
                    instance,
                    index,
                    total
                )

            # ------------------------------------------------
            # RECOVERY
            #
            # Chỉ reload nếu còn server thiếu.
            # ------------------------------------------------

            reload_attempt = 0

            while (
                reload_attempt
                < MAX_RELOAD_RETRIES
            ):

                missing = [
                    instance
                    for instance in registry
                    if not instance[
                        "stream_urls"
                    ]
                ]

                if not missing:

                    break

                reload_attempt += 1

                print(
                    f"[{index}/{total}] "
                    f"RECOVERY "
                    f"{len(missing)} "
                    f"missing server(s)",
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
                # Sau reload:
                #
                # Không tạo registry mới.
                # Registry cũ là canonical identity.
                # ------------------------------------------------

                fresh_players = (
                    wait_for_players(page)
                )

                if not fresh_players:

                    continue

                # ------------------------------------------------
                # Tracker cần timeline mới.
                # ------------------------------------------------

                tracker.timeline = []

                # ------------------------------------------------
                # Quan sát auto-load sau reload.
                # ------------------------------------------------

                tracker.run_for(
                    PLAYER_WAIT_MS
                )

                resolve_auto_streams(
                    collector,
                    tracker,
                    registry
                )

                # ------------------------------------------------
                # Thử lại chỉ missing.
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
                        instance,
                        index,
                        total
                    )

            # ------------------------------------------------
            # FINAL VALIDATION
            # ------------------------------------------------

            mapped = [
                instance
                for instance in registry
                if instance[
                    "stream_urls"
                ]
            ]

            missing = [
                instance
                for instance in registry
                if not instance[
                    "stream_urls"
                ]
            ]

            total_streams = sum(
                len(
                    instance[
                        "stream_urls"
                    ]
                )
                for instance in registry
            )

            print()
            print(
                "=" * 70
            )

            print(
                f"[{index}/{total}] "
                f"SERVER VALIDATION"
            )

            print(
                f"Expected servers: "
                f"{len(registry)}"
            )

            print(
                f"Mapped servers:   "
                f"{len(mapped)}"
            )

            print(
                f"Missing servers:  "
                f"{len(missing)}"
            )

            print(
                f"Total FLV:        "
                f"{total_streams}"
            )

            print(
                "=" * 70
            )

            # ------------------------------------------------
            # Detailed mapping
            # ------------------------------------------------

            for instance in registry:

                label = (
                    instance["name"]
                    or instance["server"]
                    or f"Server {instance['original_index'] + 1}"
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
                        f"[MISSING] "
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

            context.close()
            browser.close()

            return result

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
                    f"Worker exception: "
                    f"{error}",
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

    incomplete_matches = sum(
        1
        for match in results
        if any(
            not player.get(
                "stream_urls"
            )
            for player in match["players"]
        )
    )

    elapsed = (
        time.time()
        - start_time
    )

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
        f"Incomplete matches:    "
        f"{incomplete_matches}"
    )

    print(
        f"Elapsed:               "
        f"{elapsed:.1f}s"
    )

    print("=" * 70)

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
    print("SCRAPER COMPLETE")
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

    # Context dùng để tìm lại instance sau reload.
    CURRENT_SERVER_REGISTRY = []
    SERVER_REGISTRY_CONTEXT = []

    main()
