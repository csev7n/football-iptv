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

# Số trận chạy song song
MAX_WORKERS = 3

# Timeout mỗi trang
PAGE_TIMEOUT = 20000

# ------------------------------------------------------------
# SERVER DISCOVERY
#
# Website có thể render player chậm.
# Chúng ta không chốt server ngay khi thấy lần đầu.
# ------------------------------------------------------------

SERVER_DISCOVERY_TIMEOUT_MS = 12000

# Nếu số lượng player không đổi trong khoảng này
# thì có thể kết thúc discovery sớm.
SERVER_STABLE_MS = 2500

# Poll DOM
SERVER_DISCOVERY_POLL_MS = 250


# ------------------------------------------------------------
# PLAYER / STREAM
# ------------------------------------------------------------

# Sau khi mở trang, quan sát auto-load FLV
AUTO_STREAM_WAIT_MS = 8000

# Sau click player, chờ FLV
CLICK_STREAM_WAIT_MS = 10000

# Poll
PLAYER_STATE_POLL_MS = 100

# Retry click
MAX_CLICK_RETRIES = 2

# Reload recovery
MAX_RELOAD_RETRIES = 1

# Sau reload chờ DOM
RELOAD_SETTLE_MS = 1500


# ------------------------------------------------------------
# HTTP
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
# BASIC HTTP
# ============================================================

def get_home_page():
    """
    Download trang chủ để lấy danh sách trận.
    """

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
# URL NORMALIZATION
# ============================================================

def normalize_match_url(url):
    """
    Chuẩn hóa:

        /truc-tiep/foo/link/123
            ↓
        /truc-tiep/foo/

    """

    url = re.sub(
        r"/link/\d+/?$",
        "/",
        url
    )

    return url.rstrip("/") + "/"


# ============================================================
# FIND MATCH URLS
# ============================================================

def find_match_urls(html):
    """
    Tìm tất cả URL trận.
    """

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


# ============================================================
# FLV
# ============================================================

def is_flv_url(url):
    """
    Kiểm tra URL có phải FLV hay không.
    """

    if not url:
        return False

    return ".flv" in url.lower()


# ============================================================
# PLAYER NAME
# ============================================================

def get_player_name(element):
    """
    Lấy tên BLV từ player element.
    """

    try:

        html = element.inner_html()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # SVG icon không phải text tên
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


# ============================================================
# PLAYER INFO
# ============================================================

def get_player_info(
    element,
    index
):
    """
    Đọc toàn bộ metadata hiện có của player.
    """

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
            urljoin(
                HOME_URL,
                href
            )
            if href
            else ""
        ),
        "classes": classes
    }


# ============================================================
# PLAYER FINGERPRINT
# ============================================================

def make_player_fingerprint(info):
    """
    Fingerprint giúp tìm lại player sau reload.

    QUAN TRỌNG:

    Fingerprint KHÔNG phải unique ID.

    Có thể xảy ra:

        Minh Anh / Server A
        Minh Anh / Server A
        Minh Anh / Server A

    hoàn toàn giống nhau.

    Khi đó occurrence sẽ phân biệt chúng.
    """

    return (
        str(
            info.get(
                "name",
                ""
            )
        ).strip(),

        str(
            info.get(
                "server",
                ""
            )
        ).strip(),

        str(
            info.get(
                "data_link",
                ""
            )
        ).strip(),

        str(
            info.get(
                "href",
                ""
            )
        ).strip()
    )


# ============================================================
# PLAYER ELEMENTS
# ============================================================

def get_player_elements(page):
    """
    Lấy tất cả player-link hiện tại.

    Mỗi element = một server instance.
    """

    try:

        return page.locator(
            "a.player-link"
        ).all()

    except Exception:

        return []


# ============================================================
# CURRENT PLAYERS
# ============================================================

def get_current_players(page):
    """
    Đọc toàn bộ player hiện tại.
    """

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
# INSTANCE
# ============================================================

def create_instance(
    info,
    instance_id
):
    """
    Tạo một server instance.

    Một DOM player = một instance.

    Không bao giờ gộp theo name.
    """

    return {
        "instance_id": instance_id,

        # Index tại thời điểm discovery đầu tiên.
        "original_index": info["index"],

        "name": info["name"],

        "server": info["server"],

        "data_link": info["data_link"],

        "href": info["href"],

        # FLV của RIÊNG instance này.
        "stream_urls": [],

        # Internal
        "_fingerprint": make_player_fingerprint(
            info
        ),

        "_occurrence": 0,

        "_attempts": 0,

        "_mapped": False
    }


# ============================================================
# BUILD REGISTRY
# ============================================================

def build_server_registry(players):
    """
    Tạo registry canonical.

    Ví dụ:

        Minh Anh
        Minh Anh
        Minh Anh
        Quang Huy

    sẽ thành:

        instance-0
        instance-1
        instance-2
        instance-3
    """

    registry = []

    occurrence_map = {}

    for index, info in enumerate(players):

        fingerprint = make_player_fingerprint(
            info
        )

        occurrence = occurrence_map.get(
            fingerprint,
            0
        )

        occurrence_map[
            fingerprint
        ] = occurrence + 1

        instance = create_instance(
            info,
            instance_id=f"instance-{index}"
        )

        instance[
            "_occurrence"
        ] = occurrence

        registry.append(
            instance
        )

    return registry


# ============================================================
# DISCOVERY SIGNATURE
# ============================================================

def make_players_signature(players):
    """
    Signature của DOM player list.

    Dùng để biết DOM có thay đổi hay không.
    """

    return tuple(
        make_player_fingerprint(player)
        for player in players
    )


# ============================================================
# SERVER DISCOVERY
# ============================================================

def discover_players(
    page,
    timeout_ms=SERVER_DISCOVERY_TIMEOUT_MS
):
    """
    Chờ website render player.

    Không chốt ngay lần đầu.

    Ví dụ:

        2
        3
        4
        5
        5
        5

    → lấy snapshot 5.

    Đây là phần quan trọng để tránh mất server
    do DOM render chậm.
    """

    started = time.monotonic()

    best_players = []

    best_count = 0

    last_signature = None

    stable_since = None

    while True:

        players = get_current_players(
            page
        )

        count = len(players)

        signature = make_players_signature(
            players
        )

        now = time.monotonic()

        # ----------------------------------------------------
        # Log khi DOM thay đổi
        # ----------------------------------------------------

        if signature != last_signature:

            print(
                f"[DISCOVERY] "
                f"players={count}",
                flush=True
            )

            last_signature = signature

            stable_since = now

        # ----------------------------------------------------
        # Giữ snapshot nhiều player nhất.
        #
        # Nếu lần sau DOM giảm tạm thời,
        # không làm mất server đã phát hiện.
        # ----------------------------------------------------

        if count > best_count:

            best_count = count

            best_players = players

            print(
                f"[DISCOVERY] "
                f"NEW MAX PLAYERS={count}",
                flush=True
            )

        # ----------------------------------------------------
        # Nếu DOM ổn định đủ lâu và đã có player
        # → có thể kết thúc sớm.
        # ----------------------------------------------------

        if (
            best_count > 0
            and stable_since is not None
        ):

            stable_ms = (
                now - stable_since
            ) * 1000

            if stable_ms >= SERVER_STABLE_MS:

                break

        # ----------------------------------------------------
        # Timeout
        # ----------------------------------------------------

        elapsed_ms = (
            now - started
        ) * 1000

        if elapsed_ms >= timeout_ms:

            break

        page.wait_for_timeout(
            SERVER_DISCOVERY_POLL_MS
        )

    print(
        f"[DISCOVERY] FINAL players={len(best_players)}",
        flush=True
    )

    return best_players


# ============================================================
# FIND ELEMENT FOR INSTANCE
# ============================================================

def find_element_for_instance(
    page,
    registry,
    instance
):
    """
    Tìm lại đúng DOM element của instance.

    Thứ tự:

    1. fingerprint
    2. occurrence
    3. original index

    Quan trọng:

    Không bao giờ tìm đơn giản bằng name.
    """

    elements = get_player_elements(
        page
    )

    if not elements:

        return None

    target_fingerprint = instance[
        "_fingerprint"
    ]

    target_occurrence = instance[
        "_occurrence"
    ]

    matching = []

    for index, element in enumerate(elements):

        try:

            info = get_player_info(
                element,
                index
            )

            fingerprint = (
                make_player_fingerprint(
                    info
                )
            )

            if fingerprint == target_fingerprint:

                matching.append(
                    element
                )

        except Exception:

            continue

    # --------------------------------------------------------
    # Fingerprint + occurrence
    # --------------------------------------------------------

    if matching:

        if target_occurrence < len(
            matching
        ):

            return matching[
                target_occurrence
            ]

        # Nếu occurrence vượt quá,
        # lấy cuối cùng làm fallback.
        return matching[-1]

    # --------------------------------------------------------
    # Fallback original index
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
# FIND ACTIVE PLAYER
# ============================================================

def get_active_player_index(page):
    """
    Tìm player đang có class playing.
    """

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

        self.last_index = None

    def snapshot(self):
        """
        Chụp active player hiện tại.
        """

        index = get_active_player_index(
            self.page
        )

        now = time.monotonic()

        if index is None:

            return

        # Không ghi duplicate liên tục.
        if index == self.last_index:

            return

        self.last_index = index

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

    def run_for(
        self,
        duration_ms
    ):
        """
        Theo dõi active player trong khoảng thời gian.
        """

        started = time.monotonic()

        while True:

            self.snapshot()

            elapsed_ms = (
                time.monotonic()
                - started
            ) * 1000

            if elapsed_ms >= duration_ms:

                break

            try:

                self.page.wait_for_timeout(
                    PLAYER_STATE_POLL_MS
                )

            except Exception:

                break

    def resolve_instance_at(
        self,
        event_time
    ):
        """
        Tìm active instance gần nhất trước
        thời điểm FLV request.
        """

        candidate = None

        for event in self.timeline:

            if event["time"] <= event_time:

                candidate = event

            else:

                break

        if candidate is None:

            return None

        index = candidate[
            "index"
        ]

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

        # Tất cả FLV event
        self.events = []

        # Tất cả URL đã nhìn thấy
        self.all_streams = set()

        # Session hiện tại
        self.current_session = None

        self.session_counter = 0

        # ----------------------------------------------------
        # Listener được gắn TRƯỚC page.goto()
        #
        # Điều này rất quan trọng vì stream có thể
        # xuất hiện cực sớm.
        # ----------------------------------------------------

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
        instance_id
    ):
        """
        Tạo session TRƯỚC khi click.
        """

        self.session_counter += 1

        session = {
            "id": self.session_counter,

            "instance_id": instance_id,

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

        instance_id = None

        if self.current_session:

            session_id = (
                self.current_session["id"]
            )

            instance_id = (
                self.current_session[
                    "instance_id"
                ]
            )

        event = {
            "type": "request",

            "url": url,

            "time": now,

            "session_id": session_id,

            "instance_id": instance_id
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
        # Nếu đang click một instance,
        # map trực tiếp vào session đó.
        # ----------------------------------------------------

        if self.current_session:

            streams = (
                self.current_session[
                    "streams"
                ]
            )

            if url not in streams:

                streams.append(url)

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

            "content_type": content_type,

            "session_id": (
                self.current_session["id"]
                if self.current_session
                else None
            ),

            "instance_id": (
                self.current_session[
                    "instance_id"
                ]
                if self.current_session
                else None
            )
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
        """
        Chờ FLV sau click.

        Dừng ngay khi thấy FLV.
        """

        started = time.monotonic()

        while True:

            tracker.snapshot()

            if session["streams"]:

                return True

            elapsed_ms = (
                time.monotonic()
                - started
            ) * 1000

            if elapsed_ms >= timeout_ms:

                break

            self.page.wait_for_timeout(
                PLAYER_STATE_POLL_MS
            )

        return bool(
            session["streams"]
        )


# ============================================================
# APPLY SESSION STREAMS
# ============================================================

def apply_session_streams(
    instance,
    streams
):
    """
    Ghi stream vào đúng instance.
    """

    added = 0

    for url in streams:

        if url not in instance[
            "stream_urls"
        ]:

            instance[
                "stream_urls"
            ].append(url)

            added += 1

    if added > 0:

        instance["_mapped"] = True

    return added


# ============================================================
# AUTO STREAM RESOLUTION
# ============================================================

def resolve_auto_streams(
    collector,
    tracker,
    registry
):
    """
    Xử lý FLV tự động load khi không có click session.

    Ví dụ:

        active instance-2
              ↓
          request X.flv
              ↓
        instance-2

    Chỉ xử lý event không thuộc click session.
    """

    assignments = {}

    for event in collector.events:

        if event["type"] != "request":

            continue

        # Click session đã map chính xác.
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
                "[AUTO] Cannot determine "
                f"owner for FLV: {url}",
                flush=True
            )

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

        if not streams:

            continue

        added = apply_session_streams(
            instance,
            streams
        )

        if added:

            print(
                "[AUTO MAP] "
                f"{instance_id} "
                f"{instance['name'] or instance['server']} "
                f"→ {added} FLV",
                flush=True
            )


# ============================================================
# MERGE DISCOVERED SERVERS
# ============================================================

def merge_discovered_servers(
    registry,
    players
):
    """
    Merge server mới sau reload.

    Registry cũ là canonical.

    Không xóa stream cũ.

    Nếu website xuất hiện server mới,
    server đó được thêm vào.
    """

    # --------------------------------------------------------
    # Đếm occurrence trong snapshot mới.
    # --------------------------------------------------------

    occurrence_map = {}

    added = []

    for info in players:

        fingerprint = make_player_fingerprint(
            info
        )

        occurrence = occurrence_map.get(
            fingerprint,
            0
        )

        occurrence_map[
            fingerprint
        ] = occurrence + 1

        # ----------------------------------------------------
        # Tìm instance hiện có bằng:
        #
        # fingerprint + occurrence
        # ----------------------------------------------------

        existing = None

        for instance in registry:

            if (
                instance["_fingerprint"]
                == fingerprint
                and
                instance["_occurrence"]
                == occurrence
            ):

                existing = instance

                break

        if existing is not None:

            # ------------------------------------------------
            # Cập nhật metadata nhưng giữ stream.
            # ------------------------------------------------

            if info.get("name"):

                existing["name"] = info[
                    "name"
                ]

            if info.get("server"):

                existing["server"] = info[
                    "server"
                ]

            if info.get("data_link"):

                existing[
                    "data_link"
                ] = info[
                    "data_link"
                ]

            if info.get("href"):

                existing["href"] = info[
                    "href"
                ]

            continue

        # ----------------------------------------------------
        # Server mới
        # ----------------------------------------------------

        new_index = len(registry)

        new_instance = create_instance(
            info,
            instance_id=f"instance-{new_index}"
        )

        new_instance[
            "_occurrence"
        ] = occurrence

        registry.append(
            new_instance
        )

        added.append(
            new_instance
        )

    if added:

        print(
            f"[DISCOVERY MERGE] "
            f"Added {len(added)} new server(s)",
            flush=True
        )

        for instance in added:

            print(
                "[NEW SERVER] "
                f"{instance['instance_id']} | "
                f"{instance['name'] or '-'} | "
                f"{instance['server'] or '-'}",
                flush=True
            )

    return added


# ============================================================
# CLICK PLAYER
# ============================================================

def click_player(
    page,
    element,
    label
):
    """
    Click server.
    """

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
# PROCESS INSTANCE
# ============================================================

def process_instance(
    page,
    collector,
    tracker,
    registry,
    instance,
    match_index,
    total_matches
):
    """
    Xử lý MỘT server.

    Session được tạo trước click.

    Nếu FLV xuất hiện:
        map ngay.

    Nếu không:
        retry.
    """

    label = (
        instance["name"]
        or instance["server"]
        or (
            f"Server "
            f"{instance['original_index'] + 1}"
        )
    )

    # --------------------------------------------------------
    # Đã có stream
    # --------------------------------------------------------

    if instance["stream_urls"]:

        print(
            f"[{match_index}/{total_matches}] "
            f"SKIP "
            f"{instance['instance_id']} "
            f"{label} "
            f"(already mapped)",
            flush=True
        )

        return True

    # --------------------------------------------------------
    # Retry
    # --------------------------------------------------------

    for attempt in range(
        MAX_CLICK_RETRIES + 1
    ):

        instance[
            "_attempts"
        ] += 1

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

        element = find_element_for_instance(
            page,
            registry,
            instance
        )

        if element is None:

            print(
                "[PROCESS] Cannot find "
                f"{instance['instance_id']}",
                flush=True
            )

            return False

        # ----------------------------------------------------
        # Reset tracker state để tránh resolve
        # nhầm active player cũ.
        # ----------------------------------------------------

        tracker.last_index = None

        # ----------------------------------------------------
        # Session PHẢI tạo trước click.
        # ----------------------------------------------------

        session = collector.start_session(
            instance["instance_id"]
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
                CLICK_STREAM_WAIT_MS
            )

        collector.end_session()

        # ----------------------------------------------------
        # Unique streams
        # ----------------------------------------------------

        streams = list(
            dict.fromkeys(
                session["streams"]
            )
        )

        if streams:

            added = apply_session_streams(
                instance,
                streams
            )

            print(
                "[MAP] "
                f"{instance['instance_id']} "
                f"{label} "
                f"→ {added} new FLV",
                flush=True
            )

            return True

        print(
            "[NO FLV] "
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
    """
    Reload trang để recovery.
    """

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
        RELOAD_SETTLE_MS
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
    """
    In registry để debug.
    """

    print(
        f"{prefix}SERVER REGISTRY",
        flush=True
    )

    for instance in registry:

        label = (
            instance["name"]
            or instance["server"]
            or "-"
        )

        print(
            f"{prefix}"
            f"{instance['instance_id']} | "
            f"occurrence={instance['_occurrence']} | "
            f"index={instance['original_index']} | "
            f"name={instance['name']!r} | "
            f"server={instance['server']!r} | "
            f"streams={len(instance['stream_urls'])}",
            flush=True
        )


# ============================================================
# OUTPUT PLAYER
# ============================================================

def output_player(instance):
    """
    Chuyển internal instance thành format cũ.

    playlist.py không cần sửa.
    """

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
# MATCH STATUS
# ============================================================

def get_match_status(registry):
    """
    Phân loại:

        READY
        PARTIAL
        NO_STREAM
    """

    total = len(registry)

    mapped = sum(
        1
        for instance in registry
        if instance["stream_urls"]
    )

    if total == 0:

        return "NO_SERVERS"

    if mapped == 0:

        return "NO_STREAM"

    if mapped < total:

        return "PARTIAL"

    return "READY"


# ============================================================
# SCRAPE ONE MATCH
# ============================================================

def scrape_one_match(
    match_url,
    index,
    total
):
    """
    Scrape một trận.

    Không sử dụng global registry.
    Mỗi worker có registry riêng.
    """

    print()
    print(
        "=" * 70
    )

    print(
        f"[{index}/{total}] START",
        flush=True
    )

    print(
        match_url,
        flush=True
    )

    print(
        "=" * 70
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

            # =================================================
            # IMPORTANT
            #
            # Collector được tạo TRƯỚC goto.
            #
            # Vì FLV auto-load có thể xuất hiện rất sớm.
            # =================================================

            collector = FLVCollector(
                page
            )

            # =================================================
            # GOTO
            # =================================================

            page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

            # =================================================
            # SERVER DISCOVERY
            # =================================================

            players = discover_players(
                page
            )

            if not players:

                print(
                    f"[{index}/{total}] "
                    "NO SERVERS FOUND",
                    flush=True
                )

                title = get_match_title(
                    page
                )

                result = {
                    "title": title,

                    "url": match_url,

                    "players": []
                }

                context.close()
                browser.close()

                return result

            # =================================================
            # BUILD CANONICAL REGISTRY
            # =================================================

            registry = build_server_registry(
                players
            )

            print_registry(
                registry,
                prefix=f"[{index}/{total}] "
            )

            # =================================================
            # ACTIVE TRACKER
            #
            # Tracker được tạo sau registry.
            # =================================================

            tracker = ActivePlayerTracker(
                page,
                registry
            )

            # =================================================
            # AUTO LOAD OBSERVATION
            # =================================================

            print(
                f"[{index}/{total}] "
                "Watching auto-load streams...",
                flush=True
            )

            tracker.run_for(
                AUTO_STREAM_WAIT_MS
            )

            # =================================================
            # MAP AUTO FLV
            # =================================================

            resolve_auto_streams(
                collector,
                tracker,
                registry
            )

            # =================================================
            # CLICK MISSING SERVERS
            # =================================================

            for instance in list(
                registry
            ):

                if instance[
                    "stream_urls"
                ]:

                    continue

                process_instance(
                    page,
                    collector,
                    tracker,
                    registry,
                    instance,
                    index,
                    total
                )

            # =================================================
            # RECOVERY
            # =================================================

            for recovery_round in range(
                1,
                MAX_RELOAD_RETRIES + 1
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

                print()
                print(
                    f"[{index}/{total}] "
                    f"RECOVERY ROUND "
                    f"{recovery_round}"
                )

                print(
                    f"Missing streams: "
                    f"{len(missing)}",
                    flush=True
                )

                try:

                    reload_match(
                        page,
                        match_url
                    )

                except Exception as error:

                    print(
                        "[RECOVERY ERROR] "
                        f"{type(error).__name__}: "
                        f"{error}",
                        flush=True
                    )

                    break

                # ------------------------------------------------
                # Discovery lại DOM.
                # ------------------------------------------------

                fresh_players = discover_players(
                    page
                )

                if not fresh_players:

                    print(
                        "[RECOVERY] "
                        "No players after reload",
                        flush=True
                    )

                    continue

                # ------------------------------------------------
                # Merge server mới.
                # ------------------------------------------------

                merge_discovered_servers(
                    registry,
                    fresh_players
                )

                # ------------------------------------------------
                # Tracker mới cho timeline mới.
                # ------------------------------------------------

                tracker = ActivePlayerTracker(
                    page,
                    registry
                )

                # ------------------------------------------------
                # Auto-load sau reload.
                # ------------------------------------------------

                tracker.run_for(
                    AUTO_STREAM_WAIT_MS
                )

                resolve_auto_streams(
                    collector,
                    tracker,
                    registry
                )

                # ------------------------------------------------
                # Retry missing.
                # ------------------------------------------------

                for instance in list(
                    registry
                ):

                    if instance[
                        "stream_urls"
                    ]:

                        continue

                    process_instance(
                        page,
                        collector,
                        tracker,
                        registry,
                        instance,
                        index,
                        total
                    )

            # =================================================
            # FINAL VALIDATION
            # =================================================

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

            status = get_match_status(
                registry
            )

            # =================================================
            # PRINT RESULT
            # =================================================

            print()
            print(
                "=" * 70
            )

            print(
                f"[{index}/{total}] "
                "MATCH VALIDATION"
            )

            print(
                f"Status:             {status}"
            )

            print(
                f"Servers discovered: {len(registry)}"
            )

            print(
                f"Servers mapped:     {len(mapped)}"
            )

            print(
                f"Servers missing:    {len(missing)}"
            )

            print(
                f"Total FLV streams:  {total_streams}"
            )

            print(
                "=" * 70
            )

            # =================================================
            # DETAIL
            # =================================================

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
                        "[MAPPED] "
                        f"{instance['instance_id']} | "
                        f"{label} | "
                        f"{len(instance['stream_urls'])} FLV",
                        flush=True
                    )

                else:

                    print(
                        "[MISSING STREAM] "
                        f"{instance['instance_id']} | "
                        f"{label}",
                        flush=True
                    )

            # =================================================
            # OUTPUT
            # =================================================

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

        print()
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
    """
    Ghi matches.json.
    """

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

    print()
    print(
        f"Found {len(match_urls)} unique events",
        flush=True
    )

    if not match_urls:

        print(
            "No events found.",
            flush=True
        )

        raise RuntimeError(
            "No match URLs found."
        )

    # ========================================================
    # PARALLEL SCRAPE
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

            futures[
                future
            ] = match_url

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
                    "[WORKER ERROR] "
                    f"{match_url}",
                    flush=True
                )

                print(
                    f"{type(error).__name__}: "
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

    ready_matches = sum(
        1
        for match in results
        if (
            match["players"]
            and all(
                player.get(
                    "stream_urls"
                )
                for player in match["players"]
            )
        )
    )

    no_stream_matches = sum(
        1
        for match in results
        if (
            match["players"]
            and not any(
                player.get(
                    "stream_urls"
                )
                for player in match["players"]
            )
        )
    )

    elapsed = (
        time.time()
        - start_time
    )

    # ========================================================
    # GLOBAL VALIDATION
    # ========================================================

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
        f"Total server instances:"
        f" {total_players}"
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
        f"{incomplete_matches - no_stream_matches}"
    )

    print(
        f"No-stream matches:     "
        f"{no_stream_matches}"
    )

    print(
        f"Elapsed:               "
        f"{elapsed:.1f}s"
    )

    print(
        "=" * 70
    )

    # ========================================================
    # QUALITY GATE
    # ========================================================
    #
    # Chỉ FAIL nếu scraper không xử lý đủ số trận.
    #
    # PARTIAL / NO_STREAM không làm workflow fail,
    # vì có thể trận chưa bắt đầu.
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
        f"Output file: "
        f"{OUTPUT_FILE}"
    )

    print(
        "Quality gate: PASS"
    )

    print(
        "=" * 70
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
