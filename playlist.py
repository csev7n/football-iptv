import json
from pathlib import Path


INPUT_FILE = Path("matches.json")
OUTPUT_FILE = Path("playlist.m3u")


def clean_text(value):

    if value is None:
        return ""

    return (
        str(value)
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def load_data():

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"Không tìm thấy {INPUT_FILE}"
        )

    with INPUT_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:

        return json.load(file)


def build_playlist(data):

    lines = [
        "#EXTM3U"
    ]

    channel_count = 0

    for match in data.get(
        "matches",
        []
    ):

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

            name = clean_text(
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

            streams = player.get(
                "stream_urls",
                []
            )

            if not streams:
                continue

            for stream_url in streams:

                stream_url = clean_text(
                    stream_url
                )

                if not stream_url:
                    continue

                # --------------------------------------------
                # Display name
                # --------------------------------------------

                parts = [title]

                if name:
                    parts.append(name)

                if server:
                    parts.append(server)

                display_name = " - ".join(
                    parts
                )

                # --------------------------------------------
                # M3U
                # --------------------------------------------

                lines.append(
                    '#EXTINF:-1 '
                    'group-title="Live",'
                    + display_name
                )

                lines.append(
                    stream_url
                )

                channel_count += 1

    return (
        "\n".join(lines)
        + "\n",
        channel_count
    )


def main():

    data = load_data()

    playlist, count = build_playlist(
        data
    )

    OUTPUT_FILE.write_text(
        playlist,
        encoding="utf-8"
    )

    print(
        "=" * 70
    )

    print(
        "PLAYLIST COMPLETE"
    )

    print(
        "=" * 70
    )

    print(
        f"Channels: {count}"
    )

    print(
        f"Output:   {OUTPUT_FILE}"
    )

    print(
        f"Size:     {len(playlist)} bytes"
    )


if __name__ == "__main__":
    main()
