import json
import re
from pathlib import Path
from urllib.parse import unquote


# ============================================================
# CONFIG
# ============================================================

INPUT_FILE = Path("matches.json")
OUTPUT_FILE = Path("playlist.m3u")
TEMP_FILE = Path("playlist.m3u.tmp")

GROUP_TITLE = "Football"

# Không cho phép tạo playlist nếu không có channel
MIN_CHANNELS = 1


# ============================================================
# NORMALIZE TEXT
# ============================================================

def normalize_text(value):

    if value is None:
        return ""

    value = str(value)

    # URL decode nếu có
    value = unquote(value)

    # Chuẩn hóa khoảng trắng
    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


# ============================================================
# CLEAN TEAM TITLE
#
# Ví dụ:
#
# PHÁT TRỰC TIẾP CROATIA VS ANH VÀO LÚC ,
#
# =>
#
# CROATIA VS ANH
#
# ============================================================

def clean_team_title(title):

    title = normalize_text(title)

    if not title:
        return ""

    # ========================================================
    # REMOVE PREFIXES
    # ========================================================

    prefix_patterns = [

        r"PHÁT\s+TRỰC\s+TIẾP",

        r"XEM\s+TRỰC\s+TIẾP",

        r"ĐANG\s+TRỰC\s+TIẾP",

        r"TRỰC\s+TIẾP",

        r"LIVE",

        r"STREAM",

    ]

    for pattern in prefix_patterns:

        title = re.sub(
            rf"^\s*{pattern}\s*[:\-–—|]*\s*",
            "",
            title,
            flags=re.IGNORECASE,
        )

    # ========================================================
    # REMOVE LEADING SYMBOLS / EMOJI
    # ========================================================

    title = re.sub(
        r"^[^A-Za-zÀ-ỹ0-9]+",
        "",
        title,
    )

    # ========================================================
    # REMOVE EVERYTHING AFTER:
    #
    # VÀO LÚC
    #
    # Ví dụ:
    #
    # CROATIA VS ANH VÀO LÚC 23:00
    #
    # =>
    #
    # CROATIA VS ANH
    # ========================================================

    title = re.split(
        r"\s+VÀO\s+LÚC\b",
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    # ========================================================
    # REMOVE "VÀO ..."
    #
    # Fallback cho một số title khác
    # ========================================================

    title = re.split(
        r"\s+VÀO\s+",
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    # ========================================================
    # REMOVE TIME
    #
    # 23:00
    # 23h00
    # 23h
    # ========================================================

    title = re.sub(
        r"\b\d{1,2}\s*[:hH]\s*\d{2}\b",
        "",
        title,
    )

    title = re.sub(
        r"\b\d{1,2}\s*[hH]\b",
        "",
        title,
    )

    # ========================================================
    # REMOVE DATE
    #
    # 03/10
    # 03-10
    # 03/10/2026
    # ========================================================

    title = re.sub(
        r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{4})?\b",
        "",
        title,
    )

    # ========================================================
    # REMOVE "NGÀY ..."
    # ========================================================

    title = re.sub(
        r"\s+NGÀY\s+"
        r"\d{1,2}[/-]\d{1,2}"
        r"(?:[/-]\d{4})?.*$",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # ========================================================
    # REMOVE COMMON TRAILING WORDS
    #
    # Một số title có thể có:
    #
    # - HÔM NAY
    # - TỐI NAY
    # - HD
    #
    # Không xóa HD vì có thể là một phần tên đội.
    # ========================================================

    title = re.sub(
        r"\s+(?:HÔM\s+NAY|TỐI\s+NAY)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # ========================================================
    # REMOVE TRAILING PUNCTUATION
    # ========================================================

    title = re.sub(
        r"[\s,.:;|–—\-]+$",
        "",
        title,
    )

    # ========================================================
    # NORMALIZE VS
    #
    # Các dạng:
    #
    # vs
    # VS
    # vs.
    # -vs-
    #
    # =>
    #
    # VS
    # ========================================================

    title = re.sub(
        r"\s*[-–—]?\s*VS\.?\s*[-–—]?\s*",
        " VS ",
        title,
        flags=re.IGNORECASE,
    )

    # ========================================================
    # NORMALIZE SPACES AGAIN
    # ========================================================

    title = normalize_text(
        title
    )

    # ========================================================
    # UPPERCASE
    # ========================================================

    title = title.upper()

    return title


# ============================================================
# PARSE TITLE
#
# TITLE LÀ NGUỒN CHÍNH
#
# Trả về:
#
# time_text
# date_text
# teams
#
# ============================================================

def parse_title(title):

    title = normalize_text(
        title
    )

    if not title:

        return (
            "",
            "",
            "",
        )

    # ========================================================
    # TIME
    # ========================================================

    time_text = ""

    time_match = re.search(
        r"\b(\d{1,2})\s*[:hH]\s*(\d{2})\b",
        title,
    )

    if time_match:

        hour = int(
            time_match.group(1)
        )

        minute = int(
            time_match.group(2)
        )

        if (
            0 <= hour <= 23
            and 0 <= minute <= 59
        ):

            time_text = (
                f"{hour:02d}:{minute:02d}"
            )

    # ========================================================
    # TIME WITHOUT MINUTE
    #
    # Ví dụ:
    #
    # 23h
    #
    # ========================================================

    if not time_text:

        time_match = re.search(
            r"\b(\d{1,2})\s*[hH]\b",
            title,
        )

        if time_match:

            hour = int(
                time_match.group(1)
            )

            if 0 <= hour <= 23:

                time_text = (
                    f"{hour:02d}:00"
                )

    # ========================================================
    # DATE
    #
    # 03/10
    # 03-10
    # 03/10/2026
    # ========================================================

    date_text = ""

    date_match = re.search(
        r"\b(?:NGÀY\s*)?"
        r"(\d{1,2})[/-]"
        r"(\d{1,2})"
        r"(?:[/-](\d{4}))?\b",
        title,
        flags=re.IGNORECASE,
    )

    if date_match:

        day = int(
            date_match.group(1)
        )

        month = int(
            date_match.group(2)
        )

        if (
            1 <= day <= 31
            and 1 <= month <= 12
        ):

            date_text = (
                f"{day:02d}/{month:02d}"
            )

    # ========================================================
    # TEAM NAME
    # ========================================================

    teams = clean_team_title(
        title
    )

    return (
        time_text,
        date_text,
        teams,
    )


# ============================================================
# PARSE MATCH URL
#
# FALLBACK KHI TITLE KHÔNG CÓ THÔNG TIN
#
# Ví dụ:
#
# croatia-vs-anh-luc-2300-ngay-03-10-2026
#
# =>
#
# 23:00
# 03/10
# CROATIA VS ANH
#
# ============================================================

def parse_match_url(url):

    url = unquote(
        str(url or "")
    )

    slug = (
        url
        .rstrip("/")
        .split("/")[-1]
    )

    # ========================================================
    # TIME + DATE
    # ========================================================

    time_text = ""
    date_text = ""

    match_time = re.search(
        r"-luc-(\d{4})-ngay-(\d{2})-(\d{2})-(\d{4})",
        slug,
        flags=re.IGNORECASE,
    )

    if match_time:

        hhmm = match_time.group(1)

        hour = hhmm[:2]
        minute = hhmm[2:]

        time_text = (
            f"{hour}:{minute}"
        )

        day = match_time.group(2)
        month = match_time.group(3)

        date_text = (
            f"{day}/{month}"
        )

    # ========================================================
    # TEAM NAME
    # ========================================================

    teams_part = re.split(
        r"-luc-\d{4}-ngay-",
        slug,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    # --------------------------------------------------------
    # "-vs-" => " VS "
    # --------------------------------------------------------

    teams_part = re.sub(
        r"-vs-",
        " VS ",
        teams_part,
        flags=re.IGNORECASE,
    )

    # --------------------------------------------------------
    # Các dấu "-" còn lại => space
    # --------------------------------------------------------

    teams_part = teams_part.replace(
        "-",
        " ",
    )

    teams_part = normalize_text(
        teams_part
    )

    # --------------------------------------------------------
    # Uppercase
    # --------------------------------------------------------

    teams_part = teams_part.upper()

    return (
        time_text,
        date_text,
        teams_part,
    )


# ============================================================
# GET MATCH INFORMATION
#
# QUAN TRỌNG:
#
# 1. TITLE TRƯỚC
# 2. URL SAU
#
# ============================================================

def get_match_info(match):

    title = normalize_text(
        match.get(
            "title",
            "",
        )
    )

    url = normalize_text(
        match.get(
            "url",
            "",
        )
    )

    # ========================================================
    # TITLE
    # ========================================================

    (
        title_time,
        title_date,
        title_teams,
    ) = parse_title(
        title
    )

    # ========================================================
    # URL FALLBACK
    # ========================================================

    (
        url_time,
        url_date,
        url_teams,
    ) = parse_match_url(
        url
    )

    # ========================================================
    # ƯU TIÊN TITLE
    # ========================================================

    time_text = (
        title_time
        or url_time
    )

    date_text = (
        title_date
        or url_date
    )

    teams = (
        title_teams
        or url_teams
    )

    return (
        time_text,
        date_text,
        teams,
    )


# ============================================================
# PLAYER NAME
# ============================================================

def get_player_name(
    player,
    player_index,
):

    # ========================================================
    # NAME
    # ========================================================

    name = normalize_text(
        player.get(
            "name",
            "",
        )
    )

    if name:
        return name

    # ========================================================
    # SERVER
    # ========================================================

    server = normalize_text(
        player.get(
            "server",
            "",
        )
    )

    if server:
        return server

    # ========================================================
    # FALLBACK
    # ========================================================

    return (
        f"Server {player_index + 1}"
    )


# ============================================================
# BUILD CHANNEL TITLE
#
# Ví dụ:
#
# 23:00 - 03/10 - CROATIA VS ANH - FABIO
#
# ============================================================

def build_title(
    match,
    player,
    player_index,
):

    (
        time_text,
        date_text,
        teams,
    ) = get_match_info(
        match
    )

    player_name = get_player_name(
        player,
        player_index,
    )

    parts = []

    if time_text:

        parts.append(
            time_text
        )

    if date_text:

        parts.append(
            date_text
        )

    if teams:

        parts.append(
            teams
        )

    if player_name:

        parts.append(
            player_name
        )

    if not parts:

        return "FOOTBALL"

    return " - ".join(
        parts
    )


# ============================================================
# LOAD JSON
# ============================================================

def load_data():

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"Không tìm thấy {INPUT_FILE}"
        )

    with INPUT_FILE.open(
        "r",
        encoding="utf-8",
    ) as f:

        data = json.load(f)

    if not isinstance(
        data,
        dict,
    ):

        raise ValueError(
            "matches.json không phải JSON object."
        )

    matches = data.get(
        "matches",
        [],
    )

    if not isinstance(
        matches,
        list,
    ):

        raise ValueError(
            "Trường 'matches' không phải list."
        )

    return data


# ============================================================
# BUILD PLAYLIST
# ============================================================

def build_playlist(data):

    lines = [
        "#EXTM3U"
    ]

    channel_count = 0
    duplicate_count = 0
    non_flv_count = 0

    # Dùng set để loại stream trùng
    seen_streams = set()

    matches = data.get(
        "matches",
        [],
    )

    # ========================================================
    # COLLECT CHANNELS
    #
    # Chưa xuất playlist ngay.
    # Thu thập toàn bộ channel trước để có thể SORT.
    # ========================================================

    channels = []

    for match_index, match in enumerate(matches):

        players = match.get(
            "players",
            [],
        )

        if not isinstance(
            players,
            list,
        ):
            continue

        # ====================================================
        # LẤY NGÀY + GIỜ CỦA TRẬN
        # ====================================================

        (
            time_text,
            date_text,
            teams,
        ) = get_match_info(
            match
        )

        # ====================================================
        # TẠO SORT KEY
        #
        # Ưu tiên:
        #
        # 1. Ngày/tháng
        # 2. Giờ/phút
        #
        # Ví dụ:
        #
        # 08/10 21:00
        # 08/10 21:45
        # 09/10 01:00
        #
        # ====================================================

        sort_date = (
            9999,
            99,
        )

        if date_text:

            date_match = re.match(
                r"^(\d{2})/(\d{2})$",
                date_text,
            )

            if date_match:

                day = int(
                    date_match.group(1)
                )

                month = int(
                    date_match.group(2)
                )

                sort_date = (
                    month,
                    day,
                )

        sort_time = (
            99,
            99,
        )

        if time_text:

            time_match = re.match(
                r"^(\d{2}):(\d{2})$",
                time_text,
            )

            if time_match:

                hour = int(
                    time_match.group(1)
                )

                minute = int(
                    time_match.group(2)
                )

                sort_time = (
                    hour,
                    minute,
                )

        # ====================================================
        # SORT KEY
        #
        # month → day → hour → minute
        #
        # Nếu cùng thời gian thì giữ thứ tự ban đầu.
        # ====================================================

        sort_key = (
            sort_date[0],
            sort_date[1],
            sort_time[0],
            sort_time[1],
            match_index,
        )

        # ====================================================
        # PLAYERS / STREAMS
        # ====================================================

        for player_index, player in enumerate(
            players
        ):

            streams = player.get(
                "stream_urls",
                [],
            )

            if not isinstance(
                streams,
                list,
            ):
                continue

            # =================================================
            # CHANNEL TITLE
            # =================================================

            title = build_title(
                match,
                player,
                player_index,
            )

            # =================================================
            # STREAMS
            # =================================================

            for stream_url in streams:

                stream_url = normalize_text(
                    stream_url
                )

                if not stream_url:
                    continue

                # =================================================
                # CHỈ NHẬN FLV
                # =================================================

                if ".flv" not in stream_url.lower():

                    non_flv_count += 1

                    continue

                # =================================================
                # LOẠI STREAM TRÙNG
                # =================================================

                if stream_url in seen_streams:

                    duplicate_count += 1

                    continue

                seen_streams.add(
                    stream_url
                )

                # =================================================
                # LƯU CHANNEL
                #
                # Chưa ghi vào playlist.
                # Đợi sort xong mới ghi.
                # =================================================

                channels.append(
                    (
                        sort_key,
                        title,
                        stream_url,
                    )
                )

                channel_count += 1

    # ========================================================
    # SORT CHANNELS
    #
    # NGÀY → GIỜ
    #
    # Ví dụ:
    #
    # 08/10 21:00
    # 08/10 21:45
    # 08/10 23:50
    # 09/10 01:00
    # 09/10 11:00
    #
    # ========================================================

    channels.sort(
        key=lambda item: item[0]
    )

    # ========================================================
    # BUILD FINAL PLAYLIST
    # ========================================================

    for (
        sort_key,
        title,
        stream_url,
    ) in channels:

        lines.append(
            f'#EXTINF:-1 '
            f'group-title="{GROUP_TITLE}",'
            f'{title}'
        )

        lines.append(
            stream_url
        )

    # ========================================================
    # QUALITY GATE
    # ========================================================

    if channel_count < MIN_CHANNELS:

        raise RuntimeError(
            "Playlist validation failed: "
            f"only {channel_count} channels found."
        )

    # ========================================================
    # FINAL PLAYLIST
    # ========================================================

    playlist = (
        "\n".join(lines)
        + "\n"
    )

    return (
        playlist,
        channel_count,
        duplicate_count,
        non_flv_count,
    )


# ============================================================
# ATOMIC WRITE
#
# Ghi file tạm trước.
# Chỉ thay playlist.m3u sau khi ghi hoàn tất.
# ============================================================

def write_playlist(
    playlist,
):

    TEMP_FILE.write_text(
        playlist,
        encoding="utf-8",
        newline="\n",
    )

    TEMP_FILE.replace(
        OUTPUT_FILE
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("BUILD IPTV PLAYLIST")
    print("=" * 70)

    # ========================================================
    # LOAD DATA
    # ========================================================

    data = load_data()

    matches = data.get(
        "matches",
        [],
    )

    if not matches:

        raise RuntimeError(
            "matches.json contains 0 matches."
        )

    # ========================================================
    # BUILD
    # ========================================================

    (
        playlist,
        channel_count,
        duplicate_count,
        non_flv_count,
    ) = build_playlist(
        data
    )

    # ========================================================
    # WRITE
    # ========================================================

    write_playlist(
        playlist
    )

    # ========================================================
    # STATISTICS
    # ========================================================

    print()
    print(
        f"Matches:             {len(matches)}"
    )

    print(
        f"Channels:            {channel_count}"
    )

    print(
        f"Duplicate streams:   {duplicate_count}"
    )

    print(
        f"Non-FLV skipped:      {non_flv_count}"
    )

    print(
        f"Output:               {OUTPUT_FILE}"
    )

    print(
        "Playlist validation:  PASS"
    )

    print("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
