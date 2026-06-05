#!/usr/bin/env python3
"""Parse the first 5 enduro motorcycles from motocikl.by and save them to CSV.

The script is intentionally dependency-free: it uses only the Python standard
library, so it can be copied to a server and run without installing packages.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

CATALOG_URL = "https://motocikl.by/katalog/vse_tovary/mototsikly/enduro/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)


@dataclass
class Motorcycle:
    """One parsed motorcycle with grouped specifications."""

    name: str = ""
    price: str = ""
    url: str = ""
    image: str = ""
    categories: dict[str, dict[str, str]] = field(default_factory=dict)


class LinkParser(HTMLParser):
    """Collect links with their visible text from an HTML document.

    Some Bitrix catalog templates put badges, images and titles into one large
    clickable block.  We therefore collect text for every open ``<a>`` tag
    instead of keeping only the last link encountered by the parser.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._stack: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attr_map = dict(attrs)
            self._stack.append({"href": attr_map.get("href"), "text": []})

    def handle_data(self, data: str) -> None:
        for link in self._stack:
            text = link["text"]
            if isinstance(text, list):
                text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._stack:
            return

        link = self._stack.pop()
        href = link.get("href")
        text_parts = link.get("text")
        if isinstance(href, str) and isinstance(text_parts, list):
            text = normalize_space(" ".join(str(part) for part in text_parts))
            self.links.append((href, text))


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<.*?>", " ", fragment)
    return normalize_space(fragment)


def fetch(url: str, timeout: int = 30, retries: int = 2, pause: float = 1.0) -> str:
    """Download a page and return decoded HTML."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(pause)

    raise RuntimeError(f"Cannot download {url}: {last_error}")


def jsonld_products(page_html: str, base_url: str) -> list[Motorcycle]:
    """Extract products from JSON-LD blocks when the site provides them."""
    products: list[Motorcycle] = []
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        flags=re.I | re.S,
    )

    def walk(obj: object) -> Iterable[dict]:
        if isinstance(obj, dict):
            if str(obj.get("@type", "")).lower() == "product":
                yield obj
            for value in obj.values():
                yield from walk(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from walk(item)

    for raw in blocks:
        try:
            data = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        for product in walk(data):
            offers = product.get("offers") if isinstance(product.get("offers"), dict) else {}
            image = product.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            products.append(
                Motorcycle(
                    name=normalize_space(str(product.get("name", ""))),
                    price=normalize_space(str(offers.get("price", ""))),
                    url=urljoin(base_url, str(product.get("url", ""))),
                    image=urljoin(base_url, str(image)) if image else "",
                )
            )
    return products


def html_links(page_html: str) -> list[tuple[str, str]]:
    """Return links using both HTMLParser and a regex fallback.

    The regex fallback helps with malformed templates where HTMLParser may miss
    an anchor because of invalid nesting or unclosed tags.
    """
    parser = LinkParser()
    parser.feed(page_html)
    links = parser.links[:]

    anchor_re = re.compile(
        r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        flags=re.I | re.S,
    )
    for href, body in anchor_re.findall(page_html):
        links.append((href, strip_tags(body)))

    return links


def looks_like_product_link(href: str, text: str, base_url: str) -> bool:
    """Detect product links without relying on one exact URL layout."""
    normalized_text = normalize_space(text)
    lower_text = normalized_text.lower()
    parsed_base = urlparse(base_url)
    parsed = urlparse(urljoin(base_url, href))
    base_path = parsed_base.path.rstrip("/")
    path = parsed.path.rstrip("/")

    if parsed.netloc != parsed_base.netloc:
        return False
    if not path or path == base_path:
        return False
    if not path.startswith("/katalog/"):
        return False
    if lower_text in {"купить", "подробнее", "в корзину", "сравнить", "избранное"}:
        return False
    if any(word in lower_text for word in ("главная", "каталог", "кредит", "контакты")):
        return False
    if re.fullmatch(r"[0-9\s.,]+(?:руб\.?|byn)?", lower_text):
        return False

    product_markers = (
        "enduro",
        "ataki",
        "apollo",
        "avantis",
        "gr",
        "kayo",
        "kews",
        "lifan",
        "minsk",
        "motoland",
        "nfx",
        "nine fox",
        "racer",
        "ram",
        "regulmoto",
        "rockot",
        "sprmotors",
        "storm",
        "zontes",
    )
    has_model_number = bool(re.search(r"\d", normalized_text))
    has_product_marker = any(marker in lower_text for marker in product_markers)

    return (has_model_number or has_product_marker) and len(normalized_text) >= 5


def product_name_from_link_text(text: str) -> str:
    """Remove catalog badges/spec snippets from a product anchor's visible text."""
    cleaned = normalize_space(text)
    cleaned = re.sub(r"^(?:0%|хит|new|новинка|спец\. предложение)\s+", "", cleaned, flags=re.I)
    cleaned = re.split(
        r"\s+(?:Тип ТС|Бренд|Наличие ЭПТС|Двигатель|Объем двигателя|Цена|Купить|Сравнить)\b",
        cleaned,
        maxsplit=1,
    )[0]
    cleaned = re.split(r"\s+\d[\d\s.,]*\s*(?:руб\.?|BYN)\b", cleaned, maxsplit=1, flags=re.I)[0]
    return cleaned


def fallback_catalog_products(page_html: str, base_url: str, limit: int) -> list[Motorcycle]:
    """Parse products directly from catalog cards when detail links are unusual."""
    products: list[Motorcycle] = []
    for href, text in html_links(page_html):
        if not looks_like_product_link(href, text, base_url):
            continue
        products.append(
            Motorcycle(
                name=product_name_from_link_text(text),
                url=urljoin(base_url, href).split("#", 1)[0],
            )
        )
        if len(dedupe_products(products)) >= limit:
            break
    return dedupe_products(products)[:limit]


def catalog_products(page_html: str, base_url: str, limit: int) -> list[Motorcycle]:
    """Find product URLs on the category page and keep the first unique ones."""
    products = [item for item in jsonld_products(page_html, base_url) if item.url]
    if len(products) >= limit:
        return dedupe_products(products)[:limit]

    # The old implementation required product URLs to start with the exact
    # category URL.  On motocikl.by product pages can live in a sibling catalog
    # path, so we accept any product-looking /katalog/ link.
    for href, text in html_links(page_html):
        if not looks_like_product_link(href, text, base_url):
            continue
        products.append(
            Motorcycle(
                name=product_name_from_link_text(text),
                url=urljoin(base_url, href).split("#", 1)[0],
            )
        )

    return dedupe_products(products)[:limit]


def dedupe_products(products: Iterable[Motorcycle]) -> list[Motorcycle]:
    seen: set[str] = set()
    unique: list[Motorcycle] = []
    for product in products:
        key = product.url or product.name
        if key and key not in seen:
            seen.add(key)
            unique.append(product)
    return unique


def first_match(patterns: Iterable[str], page_html: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.I | re.S)
        if match:
            return normalize_space(match.group(1))
    return ""


def meta_content(page_html: str, property_name: str) -> str:
    pattern = (
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(property_name)}["\'][^>]+content=["\'](.*?)["\']'
    )
    return first_match([pattern], page_html)


def parse_specs(page_html: str) -> dict[str, dict[str, str]]:
    """Parse specification tables/lists and group rows by the nearest heading."""
    specs: dict[str, dict[str, str]] = {}
    current_category = "Характеристики"
    cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", page_html)

    token_re = re.compile(
        r"(?is)<h[2-4][^>]*>(.*?)</h[2-4]>"
        r"|<tr[^>]*>\s*<t[dh][^>]*>(.*?)</t[dh]>\s*<t[dh][^>]*>(.*?)</t[dh]>.*?</tr>"
        r"|<li[^>]*>\s*([^:<]{2,80})\s*[:—-]\s*(.{1,200}?)</li>"
    )

    for match in token_re.finditer(cleaned):
        heading, table_key, table_value, list_key, list_value = match.groups()
        if heading:
            text = strip_tags(heading)
            if text:
                current_category = text
            continue

        key = strip_tags(table_key or list_key or "")
        value = strip_tags(table_value or list_value or "")
        if not key or not value:
            continue
        if len(key) > 90 or len(value) > 300:
            continue
        specs.setdefault(current_category, {})[key] = value

    return specs


def parse_detail(product: Motorcycle, page_html: str) -> Motorcycle:
    product.name = first_match(
        [r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"], page_html
    ) or product.name
    product.price = first_match(
        [
            r'itemprop=["\']price["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\'](.*?)["\']',
            r"([0-9][0-9\s.,]{1,20}\s*(?:BYN|руб\.?|р\.))",
        ],
        page_html,
    ) or product.price
    product.image = meta_content(page_html, "og:image") or product.image
    if product.image:
        product.image = urljoin(product.url, product.image)
    product.categories = parse_specs(page_html)
    return product


def flatten_for_csv(products: list[Motorcycle]) -> tuple[list[str], list[dict[str, str]]]:
    base_fields = ["Название", "Цена", "Ссылка", "Изображение"]
    spec_fields: list[str] = []

    for product in products:
        for category, values in product.categories.items():
            for key in values:
                field = f"{category}: {key}"
                if field not in spec_fields:
                    spec_fields.append(field)

    rows: list[dict[str, str]] = []
    for product in products:
        row = {
            "Название": product.name,
            "Цена": product.price,
            "Ссылка": product.url,
            "Изображение": product.image,
        }
        for category, values in product.categories.items():
            for key, value in values.items():
                row[f"{category}: {key}"] = value
        rows.append(row)

    return base_fields + spec_fields, rows


def save_csv(products: list[Motorcycle], output_path: str) -> None:
    fields, rows = flatten_for_csv(products)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse first enduro motorcycles from motocikl.by and export CSV."
    )
    parser.add_argument("--url", default=CATALOG_URL, help="Catalog page URL")
    parser.add_argument("--limit", type=int, default=5, help="How many motorcycles to parse")
    parser.add_argument("--output", default="enduro_motocikl.csv", help="CSV file path")
    parser.add_argument("--pause", type=float, default=1.0, help="Pause between product pages")
    parser.add_argument(
        "--debug-html",
        help="Save the downloaded catalog HTML to this file for troubleshooting",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog_html = fetch(args.url)
    if args.debug_html:
        with open(args.debug_html, "w", encoding="utf-8") as debug_file:
            debug_file.write(catalog_html)

    products = catalog_products(catalog_html, args.url, args.limit)

    if not products:
        print(
            "No product links were found on the catalog page. "
            "Run again with --debug-html catalog.html and send that file for inspection.",
            file=sys.stderr,
        )
        return 1

    parsed: list[Motorcycle] = []
    for number, product in enumerate(products, start=1):
        print(f"[{number}/{len(products)}] {product.url}")
        detail_html = fetch(product.url)
        parsed.append(parse_detail(product, detail_html))
        time.sleep(args.pause)

    save_csv(parsed, args.output)
    print(f"CSV saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
