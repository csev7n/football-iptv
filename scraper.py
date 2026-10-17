import json
import re
import time
import os
import tempfile
import uuid
import hashlib

from collections import deque

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# ============================================================
# CONFIG
# ============================================================

HOME_URL = "https://xoilacz.io/"
OUTPUT_FILE = Path("matches.json")

MAX_WORKERS = 3
PAGE_TIMEOUT = 20000

PLAYER_DISCOVERY_TIMEOUT_MS = 12000
AUTO_PLAY_WAIT_MS = 5000
CLICK_STREAM_TIMEOUT_MS = 6000
POLL_MS = 100

MAX_LOADMORE_CLICKS = 5

MAX_CLICK_RETRIES = 1
MAX_RELOAD_RETRIES = 1
RELOAD_AUTO_WAIT_MS = 2000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}

PLAYER_SELECTOR = "a.player-link"


# ============================================================
# BASIC
# ============================================================

def load_more_matches(page):

    print(
        "[LOADMORE] Start",
        flush=True,
    )

    for click_round in range(
        MAX_LOADMORE_CLICKS
    ):

        buttons = page.locator(
            "a.btn-loadmore"
        ).all()

        if not buttons:
            break

        clicked = 0

        for button in buttons:

            try:

                if not button.is_visible():
                    continue

                button.scroll_into_view_if_needed()

                button.click(
                    timeout=3000
                )

                page.wait_for_timeout(
                    1000
                )

                clicked += 1

            except Exception:
                continue

        print(
            f"[LOADMORE] Round "
            f"{click_round + 1} "
            f"clicked={clicked}",
            flush=True,
        )

        if clicked == 0:
            break

def get_home_page():

    print(
        "[HOME] Loading homepage...",
        flush=True,
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
        )

        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
        )

        page.goto(
            HOME_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=10000,
            )
        except Exception:
            pass

        page.wait_for_timeout(3000)

        last_height = 0

        for _ in range(5):

            height = page.evaluate(
                "document.body.scrollHeight"
            )

            if height == last_height:
                break

            page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )

            page.wait_for_timeout(1500)

            last_height = height

        load_more_matches(
            page
        )
        
        total_links = page.locator(
            "a[href*='/truc-tiep/']"
        ).count()
        print(
            f"[HOME] Match links after loadmore: "
            f"{total_links}",
            flush=True,
        )
        
        html = page.content()

        # Lấy URL cuối cùng sau khi chuyển hướng
        final_url = page.url
        print(
            f"[HOME] Final URL: {final_url}",
            flush=True,
        )

        browser.close()

        return (
            html,
            final_url,
        )


def normalize_match_url(url):
    url = re.sub(r"/link/\d+/?$", "/", url)
    return url.rstrip("/") + "/"


def find_match_urls(
    html,
    base_url,
):
    soup = BeautifulSoup(html, "html.parser")
    matches = set()

    

    for anchor in soup.select("a[href]"):
        href = anchor.get("href")

        if not href or "/truc-tiep/" not in href:
            continue

        full_url = urljoin(
            base_url,
            href,
        )
        parsed = urlparse(full_url)

        if parsed.scheme not in ("http", "https"):
            continue

        # Chỉ lấy trận thuộc hostname nguồn đã cấu hình.
        # if parsed.netloc.lower() != allowed_host:
        #     continue

        matches.add(normalize_match_url(full_url))

    for match in sorted(matches):
        print(
            f"[MATCH] {match}",
            flush=True,
        )
    return sorted(matches)


def is_flv_url(url):
    if not url:
        return False

    # Chỉ kiểm tra phần path, không kiểm tra query string.
    return urlparse(url).path.lower().endswith(".flv")


def unique_append(items, value):
    if value and value not in items:
        items.append(value)

def get_sport_name(page):
    try:
        html = page.content()

        patterns = [
            r'var\s+sportName\s*=\s*"([^"]+)"',
            r"var\s+sportName\s*=\s*'([^']+)'",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                html,
                re.IGNORECASE,
            )

            if match:
                sport = match.group(1).strip().lower()

                if sport in (
                    "lol",
                    "dota2",
                    "csgo",
                ):
                    sport = "esports"

                return sport

    except Exception:
        pass

    return "other"

# ============================================================
# PLAYER INFORMATION
# ============================================================

def get_player_name(element):
    try:
        html = element.inner_html()
        soup = BeautifulSoup(html, "html.parser")

        for svg in soup.find_all("svg"):
            svg.decompose()

        return re.sub(
            r"\s+",
            " ",
            soup.get_text(" ", strip=True),
        ).strip()

    except Exception:
        return ""


def get_player_info(element, index):
    def attr(name):
        try:
            return element.get_attribute(name) or ""
        except Exception:
            return ""

    href = attr("href")

    return {
        "index": index,
        "name": get_player_name(element),
        "server": attr("title"),
        "data_link": attr("data-link"),
        "href": urljoin(HOME_URL, href) if href else "",
        "classes": attr("class"),
    }


def make_fingerprint(info):
    """
    Fingerprint chỉ để tìm lại DOM element.

    Không dùng fingerprint làm định danh duy nhất:
    nhiều server có thể có thông tin giống hệt nhau.
    """
    return (
        info.get("name", "").strip(),
        info.get("server", "").strip(),
        info.get("data_link", "").strip(),
        info.get("href", "").strip(),
    )


# ============================================================
# PLAYER DOM
# ============================================================

def get_player_elements(page):
    try:
        return page.locator(PLAYER_SELECTOR).all()
    except Exception:
        return []


def get_current_players(page):
    players = []

    for index, element in enumerate(get_player_elements(page)):
        try:
            players.append(get_player_info(element, index))
        except Exception:
            continue

    return players


def wait_for_players(
    page,
    timeout_ms=PLAYER_DISCOVERY_TIMEOUT_MS,
):
    started = time.monotonic()
    last_count = -1

    while (time.monotonic() - started) * 1000 < timeout_ms:
        players = get_current_players(page)

        if len(players) != last_count:
            print(
                f"[DOM] players={len(players)}",
                flush=True,
            )
            last_count = len(players)

        if players:
            return players

        try:
            page.wait_for_timeout(POLL_MS)
        except Exception:
            break

    return []


# ============================================================
# SERVER INSTANCE REGISTRY
# ============================================================

def make_instance(info, match_id, occurrence):
    """
    Mỗi server có instance_id riêng.

    instance_id được tạo một lần khi discovery và giữ nguyên
    trong toàn bộ lượt scrape của trận.

    occurrence giúp phân biệt các server trùng fingerprint.
    Không sử dụng player name làm khóa.
    """

    instance_id = (
        f"{match_id}:server:{info['index']}:{uuid.uuid4().hex[:8]}"
    )

    return {
        # Identity nội bộ, không phụ thuộc tên server.
        "instance_id": instance_id,

        # Thông tin DOM tại thời điểm discovery.
        "original_index": info["index"],
        "occurrence": occurrence,
        "_fingerprint": make_fingerprint(info),

        # Metadata đầu ra.
        "name": info["name"],
        "server": info["server"],
        "data_link": info["data_link"],
        "href": info["href"],

        # URL được ánh xạ riêng cho instance này.
        "stream_urls": [],

        # Trạng thái xử lý.
        "status": "discovered",
        "_attempts": 0,
        "_retry_used": False,
    }


def build_server_registry(players, match_id):
    registry = []
    occurrences = {}

    for player in players:
        fingerprint = make_fingerprint(player)

        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1

        registry.append(
            make_instance(
                player,
                match_id,
                occurrence,
            )
        )

    return registry


# ============================================================
# REGISTRY LOG
# ============================================================

def print_registry(registry):
    print("[REGISTRY] Discovered server instances", flush=True)

    for instance in registry:
        print(
            f"  id={instance['instance_id']} | "
            f"index={instance['original_index']} | "
            f"occurrence={instance['occurrence']} | "
            f"name={instance['name']!r} | "
            f"server={instance['server']!r}",
            flush=True,
        )


# ============================================================
# FIND ELEMENT FOR SERVER INSTANCE
# ============================================================

def find_element_for_instance(page, registry, instance):
    """
    Ưu tiên fingerprint + occurrence để xử lý tên trùng.

    Nếu DOM đã thay đổi và không thể xác định duy nhất,
    không nên gán stream theo tên.
    """

    elements = get_player_elements(page)

    if not elements:
        return None

    target_fingerprint = instance["_fingerprint"]
    target_occurrence = instance["occurrence"]

    matching_elements = []

    for index, element in enumerate(elements):
        try:
            info = get_player_info(element, index)

            if make_fingerprint(info) == target_fingerprint:
                matching_elements.append(element)

        except Exception:
            continue

    if target_occurrence < len(matching_elements):
        return matching_elements[target_occurrence]

    # Fingerprint không còn khớp: chỉ fallback index khi
    # phần tử tại index vẫn có fingerprint chính xác.
    original_index = instance["original_index"]

    if 0 <= original_index < len(elements):
        element = elements[original_index]

        try:
            info = get_player_info(element, original_index)

            if make_fingerprint(info) == target_fingerprint:
                return element
        except Exception:
            pass

    print(
        f"[IDENTITY WARNING] Cannot uniquely locate "
        f"{instance['instance_id']}",
        flush=True,
    )

    return None

# ============================================================
# ACTIVE SERVER — IDENTITY SAFE
# ============================================================

def get_active_instance(page, registry):
    elements = get_player_elements(page)

    if not elements:
        return None

    active_indexes = []

    for index, element in enumerate(elements):
        try:
            classes = (
                element.get_attribute("class") or ""
            ).split()

            if "playing" in classes:
                active_indexes.append(index)

        except Exception:
            continue

    if len(active_indexes) != 1:
        return None

    active_index = active_indexes[0]
    active_element = elements[active_index]

    try:
        active_info = get_player_info(
            active_element,
            active_index,
        )
    except Exception:
        return None

    fingerprint = make_fingerprint(active_info)

    occurrence = 0

    for index in range(active_index):
        try:
            info = get_player_info(
                elements[index],
                index,
            )

            if make_fingerprint(info) == fingerprint:
                occurrence += 1

        except Exception:
            continue

    matches = [
        instance
        for instance in registry
        if (
            instance.get("_fingerprint") == fingerprint
            and instance.get("occurrence") == occurrence
        )
    ]

    if len(matches) != 1:
        print(
            "[ACTIVE IDENTITY WARNING] "
            f"Khong xac dinh duy nhat server playing "
            f"tai index={active_index}.",
            flush=True,
        )
        return None

    return matches[0]

def is_instance_playing(
    page,
    registry,
    instance,
):
    active = get_active_instance(
        page,
        registry,
    )

    if active is None:
        return False

    return (
        active["instance_id"]
        == instance["instance_id"]
    )


# ============================================================
# FLV COLLECTOR — SESSION OWNERSHIP
# ============================================================

class FLVCollector:
    """
    Mỗi request được ghi nhận với session/owner tại thời điểm
    request xuất hiện.

    Chỉ request trong click session mới được gán cho server
    của session đó.

    Request ngoài session chỉ được thu thập trong khoảng
    auto-play được bật rõ ràng.

    Request không xác định được owner không được tự động
    gán cho server đang playing ở thời điểm sau đó.
    """

    def __init__(self, page):
        self.page = page

        self.events = deque(maxlen=10000)
        self.request_events = {}
        self.responses = {}

        self.current_session = None
        self.session_counter = 0

        # Chế độ thu thập request auto-play ban đầu.
        self.auto_capture = True

        page.on("request", self.on_request)
        page.on("response", self.on_response)
        page.on("requestfailed", self.on_request_failed)

    def begin_auto_capture(self):
        self.auto_capture = True

    def end_auto_capture(self):
        self.auto_capture = False

    def start_session(self, instance):
        if self.current_session is not None:
            raise RuntimeError(
                "Cannot start a new session while another "
                "session is active."
            )

        self.session_counter += 1

        session = {
            "id": self.session_counter,
            "instance_id": instance["instance_id"],
            "started_at": time.monotonic(),
            "ended_at": None,
            "streams": [],
        }

        self.current_session = session

        print(
            f"[SESSION START] id={session['id']} "
            f"owner={session['instance_id']}",
            flush=True,
        )

        return session

    def end_session(self, session):
        session["ended_at"] = time.monotonic()

        if self.current_session is session:
            self.current_session = None

        print(
            f"[SESSION END] id={session['id']} "
            f"streams={len(session['streams'])}",
            flush=True,
        )

    def on_request(self, request):
        url = request.url

        if not is_flv_url(url):
            return

        now = time.monotonic()
        session = self.current_session

        if session is not None:
            session_id = session["id"]
            instance_id = session["instance_id"]
            source = "click-session"

        elif self.auto_capture:
            session_id = None
            instance_id = None
            source = "auto-play"

        else:
            # Không gán request ngoài khoảng thu thập.
            session_id = None
            instance_id = None
            source = "unassigned"

        event = {
            "type": "request",
            "request_id": id(request),
            "url": url,
            "time": now,
            "session_id": session_id,
            "instance_id": instance_id,
            "source": source,
        }

        self.events.append(event)
        self.request_events[event["request_id"]] = event

        if session is not None:
            unique_append(session["streams"], url)

            print(
                f"[FLV REQUEST] session={session_id} "
                f"owner={instance_id} {url}",
                flush=True,
            )

        elif source == "auto-play":
            print(
                f"[FLV AUTO REQUEST] {url}",
                flush=True,
            )

        else:
            print(
                f"[FLV UNASSIGNED] {url}",
                flush=True,
            )

    def on_response(self, response):
        url = response.url

        if not is_flv_url(url):
            return

        try:
            status = response.status
        except Exception:
            status = None
			
        if status not in (200, 206):
            return

        try:
            content_type = response.headers.get(
                "content-type",
                "",
            )
        except Exception:
            content_type = ""

        record = {
            "status": status,
            "content_type": content_type,
            "time": time.monotonic(),
        }

        self.responses.setdefault(url, []).append(record)

        request = response.request
        request_id = id(request)
        request_event = self.request_events.pop(
            request_id,
            None,
        )

        self.events.append({
            "type": "response",
            "request_id": request_id,
            "url": url,
            "time": record["time"],
            "status": status,
            "content_type": content_type,
            "session_id": (
                request_event.get("session_id")
                if request_event else None
            ),
            "instance_id": (
                request_event.get("instance_id")
                if request_event else None
            ),
            "source": (
                request_event.get("source")
                if request_event else "unassigned"
            ),
        })

        print(
            f"[FLV RESPONSE] HTTP={status} {url}",
            flush=True,
        )

    def on_request_failed(self, request):
        url = request.url

        if not is_flv_url(url):
            return

        try:
            failure = request.failure
        except Exception:
            failure = "unknown"

        request_id = id(request)

        # Tìm sự kiện request gốc để đối chiếu chủ sở hữu.
        request_event = self.request_events.pop(
            request_id,
            None,
        )

        self.events.append({
            "type": "requestfailed",
            "request_id": request_id,
            "url": url,
            "time": time.monotonic(),
            "failure": failure,
            "session_id": (
                request_event.get("session_id")
                if request_event else None
            ),
            "instance_id": (
                request_event.get("instance_id")
                if request_event else None
            ),
            "source": (
                request_event.get("source")
                if request_event else "unassigned"
            ),
        })

        print(
            f"[FLV FAILED] {failure} {url}",
            flush=True,
        )

    def wait_for_stream(self, session, timeout_ms):
        started = time.monotonic()

        while (
            (time.monotonic() - started) * 1000
            < timeout_ms
        ):
            if session["streams"]:
                return True

            try:
                self.page.wait_for_timeout(POLL_MS)
            except Exception:
                break

        return bool(session["streams"])

    def get_auto_requests(self, after_time=None):
        urls = []

        for event in self.events:
            if event.get("type") != "request":
                continue

            if event.get("source") != "auto-play":
                continue

            if (
                after_time is not None
                and event["time"] < after_time
            ):
                continue

            unique_append(urls, event["url"])

        return urls

    def get_auto_request_events(self, after_time=None):
        """
        Trả về các sự kiện request FLV auto-play,
        giữ nguyên timestamp và metadata của từng request.
        """
        events = []

        for event in self.events:
            if event.get("type") != "request":
                continue

            if event.get("source") != "auto-play":
                continue

            if (
                after_time is not None
                and event.get("time", 0) < after_time
            ):
                continue

            events.append(event.copy())

        return events

# ============================================================
# STREAM ASSIGNMENT
# ============================================================

def assign_streams(instance, urls, source):
    """
    Gán URL cho đúng instance, không sử dụng tên player
    hoặc server làm khóa.
    """
    added = []

    for url in urls:
        if not url:
            continue

        if url not in instance["stream_urls"]:
            instance["stream_urls"].append(url)
            added.append(url)

    if instance["stream_urls"]:
        instance["status"] = "stream_found"

    if added:

        print(
            f"[MAP] owner={instance['instance_id']} "
            f"source={source} "
            f"added={len(added)}",
            flush=True,
        )

        for url in added:
            print(
                f"       -> {url}",
                flush=True,
            )

    return added


def map_auto_play(page, collector, registry):
    """
    Chỉ gán request auto-play khi xác định được một
    server đang playing duy nhất và không có dấu hiệu
    chuyển trạng thái trong khoảng thu thập.

    Nếu không đủ bằng chứng, không gán URL.
    """

    events = collector.get_auto_request_events()

    if not events:
        print(
            "[AUTO MAP] Không phát hiện request FLV auto-play.",
            flush=True,
        )
        return False

    # Ghi nhận server đang playing tại thời điểm kiểm tra.
    active_before = get_active_instance(page, registry)

    if active_before is None:
        print(
            "[AUTO MAP] Không xác định được server playing; "
            "không gán URL.",
            flush=True,
        )
        return False

    # Kiểm tra lại trạng thái DOM.
    active_after = get_active_instance(page, registry)

    if (
        active_after is None
        or active_after["instance_id"]
        != active_before["instance_id"]
    ):
        print(
            "[AUTO MAP] Trạng thái playing không ổn định; "
            "không gán URL.",
            flush=True,
        )
        return False

    urls = list(dict.fromkeys(
        event["url"]
        for event in events
        if event.get("url")
    ))

    if not urls:
        return False

    assign_streams(
        active_before,
        urls,
        source="auto-play",
    )

    print(
        f"[AUTO MAP SUCCESS] "
        f"{active_before['instance_id']} "
        f"urls={len(urls)}",
        flush=True,
    )

    return True


# ============================================================
# CLICK AND CAPTURE
# ============================================================

def click_player(page, element, label):
    try:
        element.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    try:
        element.click(timeout=5000)

        print(f"[CLICK] {label}", flush=True)
        return True

    except Exception as error:
        print(
            f"[CLICK FAILED] {label}: {error}",
            flush=True,
        )
        return False


def capture_click_session(
    page,
    collector,
    registry,
    instance,
):
    """
    Bắt stream của đúng server instance vừa click.

    Nếu target đang playing thì bỏ qua, tránh click lặp
    vào server không tạo request FLV mới.
    """
    if instance["stream_urls"]:
        instance["status"] = "stream_found"
        return True

    if is_instance_playing(page, registry, instance):
        print(
            f"[SKIP PLAYING] {instance['instance_id']}",
            flush=True,
        )
        return False

    element = find_element_for_instance(
        page,
        registry,
        instance,
    )

    if element is None:
        instance["status"] = "not_found"
        return False

    session = collector.start_session(instance)

    try:
        clicked = click_player(
            page,
            element,
            (
                instance["name"]
                or instance["server"]
                or instance["instance_id"]
            ),
        )

        if clicked:
            collector.wait_for_stream(
                session,
                CLICK_STREAM_TIMEOUT_MS,
            )

    finally:
        collector.end_session(session)

    # Chỉ dùng stream trong session của instance này.
    assign_streams(
        instance,
        session["streams"],
        source=f"click-session-{session['id']}",
    )

    if instance["stream_urls"]:
        return True

    instance["status"] = "pending"
    return False
	
# ============================================================
# SERVER PROCESSING — PART 3
# ============================================================


# ------------------------------------------------------------
# LABEL / STATE
# ------------------------------------------------------------

def get_instance_label(instance):
    return (
        instance.get("name")
        or instance.get("server")
        or f"Server {instance['original_index'] + 1}"
    )


def get_missing_instances(registry):
    return [
        instance
        for instance in registry
        if not instance["stream_urls"]
    ]


def get_playing_instance_id(page, registry):
    active = get_active_instance(page, registry)

    if active is None:
        return None

    return active["instance_id"]


# ------------------------------------------------------------
# CLICK ONE SERVER
# ------------------------------------------------------------

def process_instance(
    page,
    collector,
    registry,
    instance,
):
    """
    Click server một lần và bắt FLV.

    Không click nếu:
    - Server đã có FLV.
    - Server đang có class playing.

    Nếu không có FLV, đánh dấu pending.
    Retry sẽ do retry_missing_servers() quyết định.
    """

    if instance["stream_urls"]:
        instance["status"] = "stream_found"
        return True

    playing_id = get_playing_instance_id(
        page,
        registry,
    )

    if playing_id is None:
        print(
            f"[SKIP UNKNOWN] Không xác định được server "
            f"playing; không click {instance['instance_id']}.",
            flush=True,
        )
        instance["status"] = "pending"
        return False

    if playing_id == instance["instance_id"]:
        print(
            f"[SKIP PLAYING] {instance['instance_id']} "
            "đang phát; không click lại.",
            flush=True,
        )
        instance["status"] = "pending"
        return False

    element = find_element_for_instance(
        page,
        registry,
        instance,
    )

    if element is None:
        instance["status"] = "not_found"

        print(
            f"[NOT FOUND] {instance['instance_id']}",
            flush=True,
        )
        return False

    instance["_attempts"] += 1

    print(
        f"[PROCESS] {instance['instance_id']} "
        f"{get_instance_label(instance)} "
        f"attempt={instance['_attempts']}",
        flush=True,
    )

    # Mỗi lần click có session và owner riêng.
    session = collector.start_session(instance)

    try:
        clicked = click_player(
            page,
            element,
            get_instance_label(instance),
        )

        if clicked:
            collector.wait_for_stream(
                session,
                CLICK_STREAM_TIMEOUT_MS,
            )

    finally:
        collector.end_session(session)

    # Chỉ gán URL trong session cho instance sở hữu session.
    assign_streams(
        instance,
        session["streams"],
        source=f"click-session-{session['id']}",
    )

    if instance["stream_urls"]:
        instance["status"] = "stream_found"
        return True

    instance["status"] = "pending"

    print(
        f"[PENDING] {instance['instance_id']} "
        "chưa bắt được FLV.",
        flush=True,
    )

    return False


# ------------------------------------------------------------
# FIRST PASS
# ------------------------------------------------------------

def process_all_servers(
    page,
    collector,
    registry,
):
    """
    Lượt đầu tiên:
    - Bỏ qua server đã có FLV.
    - Không click server đang playing.
    - Click lần lượt các server còn thiếu FLV.
    - Không retry tại đây.
    """

    print("[PASS 1] Bắt đầu.", flush=True)

    for instance in registry:
        if instance["stream_urls"]:
            continue

        if (
            get_playing_instance_id(page, registry)
            == instance["instance_id"]
        ):
            print(
                f"[PASS 1] Skip playing "
                f"{instance['instance_id']}",
                flush=True,
            )
            continue

        process_instance(
            page,
            collector,
            registry,
            instance,
        )

    missing = get_missing_instances(registry)

    print(
        f"[PASS 1] Kết quả: "
        f"{len(registry) - len(missing)}/{len(registry)} "
        "server có FLV.",
        flush=True,
    )

    return missing


# ------------------------------------------------------------
# RETRY HELPERS
# ------------------------------------------------------------

def switch_to_another_server(
    page,
    collector,
    registry,
    target,
):
    """
    Chuyển sang một instance khác target và xác minh
    class playing đã chuyển khỏi target.

    Không click lại candidate đang playing.
    Không gán URL của session switch cho target.
    """

    target_id = target["instance_id"]

    candidates = [
        instance
        for instance in registry
        if instance["instance_id"] != target_id
        and instance["status"] != "not_found"
    ]

    candidates.sort(
        key=lambda instance: (
            not bool(instance["stream_urls"]),
            instance["original_index"],
        )
    )

    for candidate in candidates:
        playing_id = get_playing_instance_id(
            page,
            registry,
        )

        # Chỉ xác nhận trạng thái hiện tại.
        # Không kết luận thao tác chuyển server vừa click
        # thành công chỉ dựa trên trạng thái này.
        if playing_id is None:
            continue

        if playing_id != target_id:
            return True

        # Không click lại server đang playing.
        if playing_id == candidate["instance_id"]:
            return True

        element = find_element_for_instance(
            page,
            registry,
            candidate,
        )

        if element is None:
            continue

        # Ghi nhận server đang phát trước khi click.
        previous_playing_id = get_playing_instance_id(
            page,
            registry,
        )

        session = collector.start_session(candidate)

        try:
            clicked = click_player(
                page,
                element,
                get_instance_label(candidate),
            )

            if clicked:
                collector.wait_for_stream(
                    session,
                    CLICK_STREAM_TIMEOUT_MS,
                )
        finally:
            collector.end_session(session)

        # URL trong session chỉ thuộc candidate.
        assign_streams(
            candidate,
            session["streams"],
            source=f"switch-session-{session['id']}",
        )

        if not clicked:
            continue

        # Chờ website cập nhật trạng thái playing.
        started = time.monotonic()
        switched = False

        while (
            (time.monotonic() - started) * 1000
            < CLICK_STREAM_TIMEOUT_MS
        ):
            playing_id = get_playing_instance_id(
                page,
                registry,
            )

            if (
                playing_id is not None
                and playing_id != target_id
                and playing_id != previous_playing_id
            ):
                switched = True
                break

            try:
                page.wait_for_timeout(POLL_MS)
            except Exception:
                break

        if switched:
            print(
                f"[SWITCH OK] target={target_id} "
                f"active={playing_id}",
                flush=True,
            )
            return True

    print(
        f"[SWITCH FAILED] Không xác minh được đã rời "
        f"{target_id}.",
        flush=True,
    )

    return False


# ------------------------------------------------------------
# RETRY MISSING SERVERS
# ------------------------------------------------------------

def retry_missing_servers(
    page,
    collector,
    registry,
):
    """
    Trận 0 server:
        Không làm gì.

    Trận 1 server:
        Không click retry. Phần 4 xử lý reload nếu thiếu FLV.

    Trận >= 2 server:
        Mỗi server thiếu FLV được retry tối đa một lần.
        Bắt buộc chuyển sang server khác trước.
        Không retry khi target vẫn playing.
    """

    server_count = len(registry)

    if server_count == 0:
        print("[RETRY] Không có server.", flush=True)
        return

    # Trận một server chỉ có một cách recovery:
    # reload trang, được triển khai trong Phần 4.
    if server_count == 1:
        instance = registry[0]

        if not instance["stream_urls"]:
            instance["status"] = "pending"

            print(
                "[RETRY] Trận chỉ có một server. "
                "Không click lại; chuyển quyền xử lý "
                "cho recovery/reload ở Phần 4.",
                flush=True,
            )

        return

    print(
        "[RETRY] Trận nhiều server; bắt đầu retry.",
        flush=True,
    )

    retry_targets = [
        instance
        for instance in registry
        if not instance["stream_urls"]
        and not instance["_retry_used"]
        and instance["status"] != "not_found"
    ]

    for target in retry_targets:
        if target["stream_urls"]:
            continue

        # Đánh dấu trước để bảo đảm không retry quá một lần.
        target["_retry_used"] = True

        print(
            f"[RETRY TARGET] {target['instance_id']} "
            f"{get_instance_label(target)}",
            flush=True,
        )

        # Bước 1: phải chuyển sang server khác.
        switched = switch_to_another_server(
            page,
            collector,
            registry,
            target,
        )

        if not switched:
            print(
                f"[RETRY SKIP] Không thể xác minh đã "
                f"chuyển khỏi {target['instance_id']}.",
                flush=True,
            )
            continue

        # Bước 2: xác minh target không còn playing.
        # Nếu chưa xác định được server đang phát, không click liều.
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass

        playing_id = get_playing_instance_id(
            page,
            registry,
        )

        if playing_id is None:
            print(
                f"[RETRY ABORT] Không xác định được "
                f"server playing; bỏ qua {target['instance_id']}.",
                flush=True,
            )
            continue

        if playing_id == target["instance_id"]:
            print(
                f"[RETRY ABORT] {target['instance_id']} "
                "vẫn playing; không click lại.",
                flush=True,
            )
            continue

        # Bước 3: chỉ retry một lần nếu target vẫn thiếu FLV.
        # process_instance() cũng kiểm tra trạng thái playing
        # trước khi click.
        if not target["stream_urls"]:
            process_instance(
                page,
                collector,
                registry,
                target,
            )

    missing = get_missing_instances(registry)

    print(
        f"[RETRY] Kết quả: "
        f"{server_count - len(missing)}/{server_count} "
        "server có FLV.",
        flush=True,
    )


# ------------------------------------------------------------
# VALIDATE SERVER REGISTRY
# ------------------------------------------------------------

def validate_registry(registry):
    """
    Kiểm tra định danh và loại bỏ URL trùng bên trong
    từng server. Không gộp các server trùng tên.
    """

    ids = [
        instance["instance_id"]
        for instance in registry
    ]

    if len(ids) != len(set(ids)):
        raise RuntimeError(
            "Duplicate instance_id detected."
        )

    for instance in registry:
        streams = instance.get("stream_urls", [])

        if not isinstance(streams, list):
            raise RuntimeError(
                f"Invalid stream_urls for "
                f"{instance['instance_id']}"
            )

        instance["stream_urls"] = list(
            dict.fromkeys(streams)
        )

        if instance["stream_urls"]:
            instance["status"] = "stream_found"

    mapped = sum(
        bool(instance["stream_urls"])
        for instance in registry
    )

    print(
        f"[VALIDATION] {mapped}/{len(registry)} "
        "server có FLV.",
        flush=True,
    )

    return mapped
	
# ============================================================
# PART 4 — MATCH SCRAPER / RECOVERY / OUTPUT
# ============================================================


# ------------------------------------------------------------
# MATCH TITLE
# ------------------------------------------------------------

def get_match_title(page):
    try:
        h1 = page.locator("h1").first

        if h1.count() > 0:
            title = h1.inner_text(timeout=2000).strip()

            if title:
                return title

    except Exception:
        pass

    try:
        title = page.title().strip()

        if title:
            return title

    except Exception:
        pass

    return "Unknown Event"


# ------------------------------------------------------------
# WAIT FOR AUTO-PLAY
# ------------------------------------------------------------

def wait_for_auto_play(page, registry, timeout_ms):
    """
    Quan sát trạng thái player và chờ request auto-play.
    Không click server trong giai đoạn này.
    """

    started = time.monotonic()

    while (time.monotonic() - started) * 1000 < timeout_ms:
        try:
            page.wait_for_timeout(POLL_MS)
        except Exception:
            break

    # Sau khi quan sát xong mới ánh xạ request auto-play.
    return get_active_instance(page, registry)


# ------------------------------------------------------------
# MAP ONLY NEW AUTO REQUESTS
# ------------------------------------------------------------

def map_new_auto_requests(
    page,
    collector,
    registry,
    since_time,
):
    """
    Chỉ thu thập request auto-play mới.
    Không gán URL nếu chưa xác minh được chủ sở hữu
    tại thời điểm request xuất hiện.
    """

    events = collector.get_auto_request_events(
        after_time=since_time,
    )

    if not events:
        print(
            "[AUTO MAP] Không có request FLV auto-play mới.",
            flush=True,
        )
        return False

    # Các event hiện chưa lưu bằng chứng đủ tin cậy để
    # liên kết auto-play request với server cụ thể.
    # Không suy luận chủ sở hữu từ trạng thái hiện tại.
    active = get_active_instance(
        page,
        registry,
    )

    if active is None:
        print(
            "[RECOVERY AUTO MAP] "
            "Không xác định được server playing.",
            flush=True,
        )
        return False

    urls = list(dict.fromkeys(
        event["url"]
        for event in events
        if event.get("url")
    ))

    if not urls:
        return False

    assign_streams(
        active,
        urls,
        source="reload-auto-play",
    )

    print(
        f"[RECOVERY AUTO MAP] "
        f"{active['instance_id']} "
        f"urls={len(urls)}",
        flush=True,
    )

    return True


# ------------------------------------------------------------
# RELOAD — SINGLE SERVER ONLY
# ------------------------------------------------------------

def recover_single_server(
    page,
    collector,
    match_url,
    registry,
):
    """
    Chỉ dùng cho trận có đúng một server.

    Điều kiện:
    - Auto-play ban đầu không có FLV.
    - Chỉ reload tối đa một lần.
    - Không click server sau reload.
    - Chỉ chấp nhận request auto-play mới phát sinh
      sau khi bắt đầu reload.
    """

    if len(registry) != 1:
        return False

    instance = registry[0]

    if instance["stream_urls"]:
        return True

    print(
        f"[RECOVERY] Single server: "
        f"{instance['instance_id']}; reload once.",
        flush=True,
    )

    reload_started = time.monotonic()
    found = False
    fresh_players = []

    # Bật capture trước khi reload để ghi nhận request mới.
    collector.begin_auto_capture()

    try:
        try:
            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT,
            )
        except Exception as error:
            print(
                f"[RECOVERY ERROR] {type(error).__name__}: "
                f"{error}",
                flush=True,
            )

        # Chờ DOM xuất hiện sau reload.
        fresh_players = wait_for_players(page)

        if not fresh_players:
            print(
                "[RECOVERY] Không tìm thấy player sau reload.",
                flush=True,
            )
            instance["status"] = "pending"
            return False

        # Xác minh server ban đầu vẫn tìm thấy được.
        refreshed_element = find_element_for_instance(
            page,
            registry,
            instance,
        )

        if refreshed_element is None:
            print(
                "[RECOVERY] Không xác minh được server "
                "sau reload; không gán URL.",
                flush=True,
            )
            instance["status"] = "pending"
            return False
        else:
            # Chờ website tự phát, không click server.
            wait_for_auto_play(
                page,
                registry,
                RELOAD_AUTO_WAIT_MS,
            )

            found = map_new_auto_requests(
                page,
                collector,
                registry,
                reload_started,
            )

    finally:
        collector.end_auto_capture()

    if not fresh_players:
        return False

    if found:
        instance["status"] = "stream_found"

        print(
            f"[RECOVERY SUCCESS] "
            f"{instance['instance_id']}",
            flush=True,
        )

        return True

    instance["status"] = "pending"

    print(
        f"[RECOVERY FAILED] "
        f"{instance['instance_id']} vẫn chưa có FLV.",
        flush=True,
    )

    return False


# ------------------------------------------------------------
# PLAYER OUTPUT
# ------------------------------------------------------------

def output_player(instance):
    return {
        "instance_id": instance["instance_id"],
        "index": instance["original_index"],
        "name": instance["name"],
        "server": instance["server"],
        "data_link": instance["data_link"],
        "href": instance["href"],
        "stream_urls": list(
            dict.fromkeys(instance["stream_urls"])
        ),
        "status": (
            "stream_found"
            if instance["stream_urls"]
            else instance["status"]
        ),
    }


# ------------------------------------------------------------
# MATCH QUALITY
# ------------------------------------------------------------

def classify_match(registry):
    total = len(registry)

    if total == 0:
        return "no_server"

    mapped = sum(
        bool(instance["stream_urls"])
        for instance in registry
    )

    if mapped == total:
        return "ready"

    if mapped == 0:
        return "no_stream"

    return "partial"


# ------------------------------------------------------------
# SCRAPE ONE MATCH
# ------------------------------------------------------------

def scrape_one_match(
    match_url,
    index,
    total_matches,
):
    print(
        f"[{index}/{total_matches}] START {match_url}",
        flush=True,
    )

    browser = None
    context = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
            )

            page = context.new_page()

            # Phải đăng ký listener trước page.goto().
            collector = FLVCollector(page)

            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT,
            )

            # --------------------------------------------
            # DISCOVER SERVERS
            # --------------------------------------------

            players = wait_for_players(page)

            if not players:
                print(
                    f"[{index}/{total_matches}] NO PLAYERS",
                    flush=True,
                )
                return None
                
            sport_name = get_sport_name(page)
            
            # Dùng ID theo URL trận để dễ đọc log.
            match_id = hashlib.md5(
                match_url.encode("utf-8")
            ).hexdigest()[:12]

            registry = build_server_registry(
                players,
                match_id,
            )

            print_registry(registry)

            # --------------------------------------------
            # INITIAL AUTO-PLAY
            # --------------------------------------------

            collector.begin_auto_capture()

            try:
                wait_for_auto_play(
                    page,
                    registry,
                    AUTO_PLAY_WAIT_MS,
                )

                map_auto_play(
                    page,
                    collector,
                    registry,
                )
            finally:
                collector.end_auto_capture()

            # --------------------------------------------
            # SINGLE SERVER
            # --------------------------------------------

            if len(registry) == 1:
                if not registry[0]["stream_urls"]:
                    recover_single_server(
                        page,
                        collector,
                        match_url,
                        registry,
                    )

            # --------------------------------------------
            # MULTIPLE SERVERS
            # --------------------------------------------

            elif len(registry) > 1:
                # Click các server chưa có stream.
                process_all_servers(
                    page,
                    collector,
                    registry,
                )

                # Retry chỉ sau lượt click đầu tiên.
                retry_missing_servers(
                    page,
                    collector,
                    registry,
                )

            # --------------------------------------------
            # VALIDATE
            # --------------------------------------------

            validate_registry(registry)

            status = classify_match(registry)

            mapped = sum(
                bool(instance["stream_urls"])
                for instance in registry
            )

            stream_count = sum(
                len(instance["stream_urls"])
                for instance in registry
            )

            print(
                f"[{index}/{total_matches}] RESULT "
                f"{status.upper()} "
                f"{mapped}/{len(registry)} servers "
                f"| {stream_count} FLV",
                flush=True,
            )

            for instance in registry:
                print(
                    f"[SERVER] "
                    f"id={instance['instance_id']} | "
                    f"name={instance['name']!r} | "
                    f"server={instance['server']!r} | "
                    f"streams={len(instance['stream_urls'])} | "
                    f"status={instance['status']}",
                    flush=True,
                )

            return {
                "title": get_match_title(page),
                "url": match_url,
                "sport_name": sport_name,
                "status": status,
                "players": [
                    output_player(instance)
                    for instance in registry
                ],
            }

    except Exception as error:
        print(
            f"[{index}/{total_matches}] ERROR "
            f"{type(error).__name__}: {error}",
            flush=True,
        )

        print(f"URL: {match_url}", flush=True)

        return None

    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass

        if browser:
            try:
                browser.close()
            except Exception:
                pass


# ------------------------------------------------------------
# ATOMIC SAVE
# ------------------------------------------------------------

def save_results(results):
    data = {
        "generated_at": int(time.time()),
        "match_count": len(results),
        "matches": results,
    }

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Ghi file tạm trước, sau đó thay file chính để tránh
    # JSON bị ghi dở nếu tiến trình gặp lỗi giữa chừng.
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(OUTPUT_FILE.parent),
            prefix=OUTPUT_FILE.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            json.dump(
                data,
                temp_file,
                ensure_ascii=False,
                indent=2,
            )

            temp_file.write("\n")
            temp_path = Path(temp_file.name)

        os.replace(
            str(temp_path),
            str(OUTPUT_FILE),
        )

    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    print(
        f"[SAVE] {OUTPUT_FILE} "
        f"| matches={len(results)}",
        flush=True,
    )


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    started = time.time()

    html, final_home_url = get_home_page()

    match_urls = find_match_urls(
        html,
        final_home_url,
    )

    print(
    f"[HOME] Found {len(match_urls)} links",
    flush=True,
    )

    for url in match_urls:
        if (
            "persib" in url.lower()
            or "viettel" in url.lower()
        ):
            print(
              f"[FOUND TARGET] {url}",
              flush=True,
            )
    
    print(
        f"[MAIN] Found {len(match_urls)} unique matches.",
        flush=True,
    )

    if not match_urls:
        print("[MAIN] No matches found.", flush=True)
        return

    results = []

    print(
        f"[MAIN] Workers={MAX_WORKERS}",
        flush=True,
    )

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
    ) as executor:

        futures = {
            executor.submit(
                scrape_one_match,
                match_url,
                index,
                len(match_urls),
            ): match_url
            for index, match_url in enumerate(
                match_urls,
                start=1,
            )
        }

        for future in as_completed(futures):
            match_url = futures[future]

            try:
                result = future.result()

                if result is not None:
                    results.append(result)

            except Exception as error:
                print(
                    f"[WORKER ERROR] {match_url}: {error}",
                    flush=True,
                )

    results.sort(key=lambda item: item["url"])

    # Lưu cả khi một số trận lỗi, để không mất dữ liệu
    # từ các trận đã xử lý thành công.
    save_results(results)

    total_servers = sum(
        len(match["players"])
        for match in results
    )

    mapped_servers = sum(
        bool(player["stream_urls"])
        for match in results
        for player in match["players"]
    )

    total_streams = sum(
        len(player["stream_urls"])
        for match in results
        for player in match["players"]
    )

    ready_matches = sum(
        match["status"] == "ready"
        for match in results
    )

    partial_matches = sum(
        match["status"] == "partial"
        for match in results
    )

    no_stream_matches = sum(
        match["status"] in ("no_stream", "no_server")
        for match in results
    )

    elapsed = time.time() - started

    print()
    print("=" * 60)
    print("SCRAPER SUMMARY")
    print("=" * 60)
    print(f"Events found:       {len(match_urls)}")
    print(f"Events processed:   {len(results)}")
    print(f"Servers discovered: {total_servers}")
    print(f"Servers with FLV:   {mapped_servers}")
    print(f"Total FLV URLs:     {total_streams}")
    print(f"Ready matches:      {ready_matches}")
    print(f"Partial matches:    {partial_matches}")
    print(f"No-stream matches:  {no_stream_matches}")
    print(f"Elapsed:            {elapsed:.1f}s")
    print(f"Output:             {OUTPUT_FILE}")
    print("=" * 60)

    if len(results) != len(match_urls):
        print(
            "[QUALITY GATE] FAIL: "
            "Một số trận chưa scrape được.",
            flush=True,
        )
    else:
        print("[QUALITY GATE] PASS", flush=True)


# ------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------

if __name__ == "__main__":
    main()
