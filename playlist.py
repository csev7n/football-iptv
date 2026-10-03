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

MIN_CHANNELS = 1


# ============================================================
# NORMALIZE TEXT
# ============================================================

def normalize_text(value):

    if value is None:
        return ""

    value = str(value)

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


# ============================================================
# PARSE DATE/TIME FROM URL
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

    # --------------------------------------------------------
    # TIME + DATE
    #
    # luc-2300-ngay-01-10-2026
    # --------------------------------------------------------

    match_time = re.search(
        r"-luc-(\d{4})-ngay-(\d{2})-(\d{2})-(\d{4})",
        slug,
        re.IGNORECASE,
    )

    time_text = ""
    date_text = ""

    if match_time:

        hhmm = match_time.group(1)

        hour = hhmm[:2]
        minute = hhmm[2:]

        time_text = f"{hour}:{minute}"

        day = match_time.group(2)
        month = match_time.group(3)

        date_text = (
            f"{day}/{month}"
        )

    # --------------------------------------------------------
    # TEAMS
    # --------------------------------------------------------

    teams_part = re.split(
        r"-luc-\d{4}-ngay-",
        slug,
        flags=re.IGNORECASE,
    )[0]

    teams_part = re.sub(
        r"-vs-",
        " vs ",
        teams_part,
        flags=re.IGNORECASE,
    )

    teams_part = teams_part.replace(
        "-",
        " ",
    )

    teams_part = normalize_text(
        teams_part
    )

    if teams_part:

        teams_part = teams_part.title()

    return (
        time_text,
        date_text,
        teams_part,
    )


# ============================================================
# PARSE DATE/TIME FROM TITLE
#
# Hỗ trợ một số dạng thường gặp:
#
# 23:00
# 23h00
# 23h
# ngày 01/10
# 01/10/2026
# ============================================================

def parse_match_title(title):

    title = normalize_text(
        title
    )

    if not title:
        return (
            "",
            "",
            "",
        )

    # --------------------------------------------------------
    # TIME
    # --------------------------------------------------------

    time_text = ""

    time_match = re.search(
        r"\b(\d{1,2})\s*[:hH]\s*(\d{2})\b",
        title,
    )

    if time_match:

        hour = int(
            time_match.group(1)
        )

        minute = time_match.group(2)

        if 0 <= hour <= 23:

            time_text = (
                f"{hour:02d}:{minute}"
            )

    # --------------------------------------------------------
    # DATE
    # --------------------------------------------------------

    date_text = ""

    date_match = re.search(
        r"\b(?:ngày\s*)?"
        r"(\d{1,2})[/-]"
        r"(\d{1,2})"
        r"(?:[/-](\d{4}))?\b",
        title,
        re.IGNORECASE,
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

    # --------------------------------------------------------
    # TEAM NAME
    #
    # Cố gắng loại bỏ phần giờ/ngày khỏi title.
    # Nếu không xác định được thì trả lại title.
    # --------------------------------------------------------

    teams = title

    # Xóa các cụm thời gian
    teams = re.sub(
        r"\b\d{1,2}\s*[:hH]\s*\d{2}\b",
        "",
        teams,
    )

    teams = re.sub(
        r"\b\d{1,2}\s*[hH]\b",
        "",
        teams,
    )

    # Xóa ngày tháng
    teams = re.sub(
        r"\b(?:ngày\s*)?"
        r"\d{1,2}[/-]\d{1,2}"
        r"(?:[/-]\d{4})?\b",
        "",
        teams,
        flags=re.IGNORECASE,
    )

    # Xóa một số dấu phân cách thừa
    teams = re.sub(
        r"\s*[-|•]\s*$",
        "",
        teams,
    )

    teams = normalize_text(
        teams
    )

    return (
        time_text,
        date_text,
        teams,
    )


# ============================================================
# MATCH INFORMATION
#
# Ưu tiên title.
# URL làm fallback.
# ============================================================

def get_match_info(match):

    url = match.get(
        "url",
        "",
    )

    title = normalize_text(
        match.get(
            "title",
            "",
        )
    )

    # --------------------------------------------------------
    # Parse URL
    # --------------------------------------------------------

    (
        url_time,
        url_date,
        url_teams,
    ) = parse_match_url(
        url
    )

    # --------------------------------------------------------
    # Parse TITLE
    # --------------------------------------------------------

    (
        title_time,
        title_date,
        title_teams,
    ) = parse_match_title(
        title
    )

    # --------------------------------------------------------
    # Prefer title information
    # --------------------------------------------------------

    time_text = (
        title_time
        or url_time
    )

    date_text = (
        title_date
        or url_date
    )

    # Nếu parser title trả về title quá rộng
    # thì URL teams vẫn là fallback an toàn.
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

    name = normalize_text(
        player.get(
            "name",
            "",
        )
    )

    if name:
        return name

    server = normalize_text(
        player.get(
            "server",
            "",
        )
    )

    if server:
        return server

    return (
        f"Server {player_index + 1}"
    )


# ============================================================
# BUILD TITLE
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

    if parts:
        return " - ".join(parts)

    return "Football"


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

    matches = data.get(
        "matches",
        [],
    )

    # --------------------------------------------------------
    # Exact URL deduplication.
    #
    # IMPORTANT:
    # Hai URL khác nhau vẫn được giữ.
    # Chỉ loại khi URL hoàn toàn giống nhau.
    # --------------------------------------------------------

    seen_streams = set()

    for match in matches:

        players = match.get(
            "players",
            [],
        )

        if not isinstance(
            players,
            list,
        ):
            continue

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

            title = build_title(
                match,
                player,
                player_index,
            )

            for stream_url in streams:

                stream_url = normalize_text(
                    stream_url
                )

                if not stream_url:
                    continue

                # ------------------------------------------------
                # Exact duplicate
                # ------------------------------------------------

                if stream_url in seen_streams:

                    duplicate_count += 1

                    continue

                seen_streams.add(
                    stream_url
                )

                # ------------------------------------------------
                # EXTINF
                # ------------------------------------------------

                lines.append(
                    f'#EXTINF:-1 '
                    f'group-title="{GROUP_TITLE}",'
                    f'{title}'
                )

                lines.append(
                    stream_url
                )

                channel_count += 1

    # --------------------------------------------------------
    # QUALITY GATE
    # --------------------------------------------------------

    if channel_count < MIN_CHANNELS:

        raise RuntimeError(
            "Playlist validation failed: "
            f"only {channel_count} channels found. "
            "Refusing to create/publish an empty playlist."
        )

    playlist = (
        "\n".join(lines)
        + "\n"
    )

    return (
        playlist,
        channel_count,
        duplicate_count,
    )


# ============================================================
# ATOMIC WRITE
# ============================================================

def write_playlist(
    playlist,
):

    # Ghi ra file tạm trước.
    TEMP_FILE.write_text(
        playlist,
        encoding="utf-8",
        newline="\n",
    )

    # Chỉ replace khi ghi thành công.
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

    data = load_data()

    matches = data.get(
        "matches",
        [],
    )

    if not matches:

        raise RuntimeError(
            "matches.json contains 0 matches. "
            "Refusing to publish."
        )

    (
        playlist,
        channel_count,
        duplicate_count,
    ) = build_playlist(
        data
    )

    write_playlist(
        playlist
    )

    print(
        f"Matches:          {len(matches)}"
    )

    print(
        f"Channels:         {channel_count}"
    )

    print(
        f"Duplicates skip:  {duplicate_count}"
    )

    print(
        f"Output:           {OUTPUT_FILE}"
    )

    print(
        "Playlist validation: PASS"
    )

    print("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
