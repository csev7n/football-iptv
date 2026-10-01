import json
from pathlib import Path


INPUT_FILE = Path("matches.json")
OUTPUT_FILE = Path("playlist.m3u")


def load_matches():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Không tìm thấy {INPUT_FILE}"
        )

    with INPUT_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)


def clean_text(value):
    if value is None:
        return ""

    return (
        str(value)
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def get_stream_url(player):
    """
    Chỉ sử dụng stream_url nếu nguồn dữ liệu
    của bạn đã cung cấp URL stream được phép sử dụng.
    """

    url = player.get("stream_url", "")

    if not url:
        return ""

    return clean_text(url)


def build_playlist(data):

    lines = [
        "#EXTM3U"
    ]

    for match in data.get("matches", []):

        title = clean_text(
            match.get(
                "title",
                "Unknown Event"
            )
        )

        players = match.get(
            "players",
            []
        )

        for player in players:

            stream_url = get_stream_url(
                player
            )

            # Chưa có stream URL thì bỏ qua
            if not stream_url:
                continue

            commentator = clean_text(
                player.get(
                    "name",
                    ""
                )
            )

            server = clean_text(
                player.get(
                    "server",
                    ""
                )
            )

            # Tên hiển thị
            parts = [title]

            if commentator:
                parts.append(
                    commentator
                )

            if server:
                parts.append(
                    server
                )

            display_name = " - ".join(
                parts
            )

            lines.append(
                '#EXTINF:-1 group-title="Live",'
                + display_name
            )

            lines.append(
                stream_url
            )

    return "\n".join(lines) + "\n"


def main():

    data = load_matches()

    playlist = build_playlist(
        data
    )

    OUTPUT_FILE.write_text(
        playlist,
        encoding="utf-8"
    )

    print(
        f"Created {OUTPUT_FILE}"
    )

    print(
        f"Playlist size: "
        f"{len(playlist)} bytes"
    )


if __name__ == "__main__":
    main()
