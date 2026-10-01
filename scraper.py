from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests


HOME_URL = "https://xoilacz.vip"


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}


def get_home_page():
    response = requests.get(
        HOME_URL,
        headers=HEADERS,
        timeout=20
    )

    response.raise_for_status()

    return response.text


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

        matches.add(full_url)

    return sorted(matches)


def main():

    print("Downloading home page...")

    html = get_home_page()

    matches = find_match_urls(html)

    print(
        f"Found {len(matches)} matches"
    )

    for index, url in enumerate(matches, 1):

        print(
            f"{index}. {url}"
        )


if __name__ == "__main__":
    main()
