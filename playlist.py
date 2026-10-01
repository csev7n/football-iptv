import json
import re
from pathlib import Path
from urllib.parse import unquote


INPUT_FILE = Path("matches.json")
OUTPUT_FILE = Path("playlist.m3u")


# ============================================================
# ĐỌC TRẬN TỪ URL
#
# Ví dụ:
# .../azerbaijan-vs-liechtenstein-luc-2300-ngay-01-10-2026/
#
# Kết quả:
# time  = 23:00
# date  = 01/10
# teams = Azerbaijan vs Liechtenstein
# ============================================================

def parse_match_url(url):

    url = unquote(url)

    # lấy phần cuối URL
    slug = url.rstrip("/").split("/")[-1]

    # --------------------------------------------------------
    # Lấy giờ + ngày tháng
    # --------------------------------------------------------

    match_time = re.search(
        r"-luc-(\d{4})-ngay-(\d{2})-(\d{2})-(\d{4})",
        slug,
        re.IGNORECASE
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

        date_text = f"{day}/{month}"

    # --------------------------------------------------------
    # Lấy tên hai đội
    # --------------------------------------------------------

    teams_part = re.split(
        r"-luc-\d{4}-ngay-",
        slug,
        flags=re.IGNORECASE
    )[0]

    # URL dùng "-vs-" để phân cách hai đội
    teams_part = re.sub(
        r"-vs-",
        " vs ",
        teams_part,
        flags=re.IGNORECASE
    )

    # dấu "-" còn lại -> khoảng trắng
    teams_part = teams_part.replace(
        "-",
        " "
    )

    teams_part = re.sub(
        r"\s+",
        " ",
        teams_part
    ).strip()

    # --------------------------------------------------------
    # Viết hoa đẹp hơn
    # --------------------------------------------------------

    teams_part = teams_part.title()

    return (
        time_text,
        date_text,
        teams_part
    )


# ============================================================
# TÊN PLAYER
# ============================================================

def get_player_name(
    player,
    player_index
):

    name = str(
        player.get(
            "name",
            ""
        )
    ).strip()

    # --------------------------------------------------------
    # Nếu có tên bình luận viên -> dùng luôn
    # --------------------------------------------------------

    if name:

        # bỏ khoảng trắng thừa
        name = re.sub(
            r"\s+",
            " ",
            name
        )

        return name

    # --------------------------------------------------------
    # Không có tên -> Server 1, Server 2...
    # --------------------------------------------------------

    return f"Server {player_index + 1}"


# ============================================================
# TẠO TITLE
# ============================================================

def build_title(
    match,
    player,
    player_index
):

    url = match.get(
        "url",
        ""
    )

    time_text, date_text, teams = parse_match_url(
        url
    )

    player_name = get_player_name(
        player,
        player_index
    )

    parts = []

    if time_text:
        parts.append(time_text)

    if date_text:
        parts.append(date_text)

    if teams:
        parts.append(teams)

    if player_name:
        parts.append(player_name)

    return " - ".join(parts)


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
        encoding="utf-8"
    ) as f:

        return json.load(f)


# ============================================================
# BUILD PLAYLIST
# ============================================================

def build_playlist(data):

    lines = [
        "#EXTM3U"
    ]

    channel_count = 0

    matches = data.get(
        "matches",
        []
    )

    for match in matches:

        players = match.get(
            "players",
            []
        )

        for player_index, player in enumerate(
            players
        ):

            streams = player.get(
                "stream_urls",
                []
            )

            if not streams:
                continue

            title = build_title(
                match,
                player,
                player_index
            )

            # ------------------------------------------------
            # Mỗi stream tạo một channel
            # ------------------------------------------------

            for stream_url in streams:

                stream_url = str(
                    stream_url
                ).strip()

                if not stream_url:
                    continue

                lines.append(
                    '#EXTINF:-1 group-title="Football",'
                    + title
                )

                lines.append(
                    stream_url
                )

                channel_count += 1

    playlist = (
        "\n".join(lines)
        + "\n"
    )

    return (
        playlist,
        channel_count
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("BUILD IPTV PLAYLIST")
    print("=" * 70)

    data = load_data()

    playlist, channel_count = build_playlist(
        data
    )

    OUTPUT_FILE.write_text(
        playlist,
        encoding="utf-8"
    )

    print(
        f"Matches:  "
        f"{len(data.get('matches', []))}"
    )

    print(
        f"Channels: "
        f"{channel_count}"
    )

    print(
        f"Output:   "
        f"{OUTPUT_FILE}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
