#!/usr/bin/env python3
"""Скачать первые эндуро-мотоциклы с motocikl.by и сохранить их в CSV.

Скрипт использует только стандартную библиотеку Python, поэтому для запуска не
нужно устанавливать дополнительные пакеты.
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
BASE_FIELDS = ["Название", "Цена", "Ссылка", "Изображение"]
IGNORED_LINK_TEXTS = {"купить", "подробнее", "в корзину", "сравнить", "избранное"}
IGNORED_LINK_WORDS = {"главная", "каталог", "кредит", "контакты"}
PRODUCT_MARKERS = (
    "enduro",
    "эндуро",
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
SPEC_LABELS = (
    "Тип ТС",
    "Бренд",
    "Наличие ЭПТС",
    "Двигатель",
    "Объем двигателя, см3",
    "Объём двигателя, см3",
    "Мощность двигателя, л.с.",
    "Мощность",
    "Коробка передач",
    "КПП",
    "Трансмиссия",
    "Охлаждение",
    "Стартер",
    "Привод",
    "Передняя подвеска",
    "Задняя подвеска",
    "Передний тормоз",
    "Задний тормоз",
    "Колеса",
    "Колёса",
    "Вес",
    "Высота по седлу",
    "Гарантия",
)


@dataclass
class Motorcycle:
    """Одна карточка мотоцикла и ее характеристики."""

    name: str = ""
    price: str = ""
    url: str = ""
    image: str = ""
    categories: dict[str, dict[str, str]] = field(default_factory=dict)


class LinkParser(HTMLParser):
    """Собирает ссылки и весь видимый текст внутри каждой ссылки."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._current: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attrs_dict = dict(attrs)
            self._current.append({"href": attrs_dict.get("href"), "text": []})

    def handle_data(self, data: str) -> None:
        for item in self._current:
            text = item.get("text")
            if isinstance(text, list):
                text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current:
            return

        item = self._current.pop()
        href = item.get("href")
        text_parts = item.get("text")
        if isinstance(href, str) and isinstance(text_parts, list):
            text = normalize_space(" ".join(str(part) for part in text_parts))
            self.links.append((href, text))


class CatalogCardParser(HTMLParser):
    """Собирает текст карточек каталога даже если внутри нет товарной ссылки."""

    CARD_CLASS_RE = re.compile(r"(?:catalog|product|item|card|tile)", re.I)
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self._open_cards: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        is_void_tag = tag.lower() in self.VOID_TAGS
        if not is_void_tag:
            for card in self._open_cards:
                card["depth"] = int(card["depth"]) + 1

        class_name = attrs_dict.get("class", "") or ""
        if tag.lower() in {"article", "div", "li"} and self.CARD_CLASS_RE.search(class_name):
            self._open_cards.append({"depth": 1, "text": [], "url": "", "image": ""})

        for card in self._open_cards:
            if tag.lower() == "a" and not card.get("url"):
                href = attrs_dict.get("href") or ""
                if "/katalog/" in href:
                    card["url"] = href
            if tag.lower() == "img" and not card.get("image"):
                image = attrs_dict.get("src") or attrs_dict.get("data-src") or ""
                card["image"] = image

    def handle_data(self, data: str) -> None:
        for card in self._open_cards:
            text = card.get("text")
            if isinstance(text, list):
                text.append(data)

    def handle_endtag(self, tag: str) -> None:
        closed: list[dict[str, object]] = []
        still_open: list[dict[str, object]] = []
        for card in self._open_cards:
            card["depth"] = int(card["depth"]) - 1
            if int(card["depth"]) <= 0:
                closed.append(card)
            else:
                still_open.append(card)
        self._open_cards = still_open

        for card in closed:
            text_parts = card.get("text")
            if not isinstance(text_parts, list):
                continue
            text = normalize_space(" ".join(str(part) for part in text_parts))
            if text:
                self.cards.append(
                    {
                        "text": text,
                        "url": str(card.get("url") or ""),
                        "image": str(card.get("image") or ""),
                    }
                )


def normalize_space(value: str) -> str:
    """Заменить повторяющиеся пробелы/переносы строк одним пробелом."""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def strip_tags(fragment: str) -> str:
    """Убрать HTML-теги из фрагмента."""
    fragment = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<.*?>", " ", fragment)
    return normalize_space(fragment)


def fetch(url: str, timeout: int = 30, retries: int = 2, pause: float = 1.0) -> str:
    """Скачать страницу и вернуть HTML."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
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


def iter_jsonld_objects(value: object) -> Iterable[dict]:
    """Рекурсивно пройти JSON-LD и вернуть все словари."""
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_jsonld_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_jsonld_objects(nested)


def jsonld_products(page_html: str, base_url: str) -> list[Motorcycle]:
    """Достать товары из JSON-LD, если сайт их отдает."""
    products: list[Motorcycle] = []
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        flags=re.I | re.S,
    )

    for raw in blocks:
        try:
            data = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue

        for product in iter_jsonld_objects(data):
            if str(product.get("@type", "")).lower() != "product":
                continue

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
    """Вернуть ссылки из HTML через HTMLParser и запасной regex-поиск."""
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
    """Проверить, похожа ли ссылка на товарную карточку."""
    clean_text = normalize_space(text)
    lower_text = clean_text.lower()
    parsed_base = urlparse(base_url)
    parsed_url = urlparse(urljoin(base_url, href))
    base_path = parsed_base.path.rstrip("/")
    path = parsed_url.path.rstrip("/")

    if parsed_url.netloc != parsed_base.netloc:
        return False
    if not path or path == base_path:
        return False
    if not path.startswith("/katalog/"):
        return False
    if lower_text in IGNORED_LINK_TEXTS:
        return False
    if any(word in lower_text for word in IGNORED_LINK_WORDS):
        return False
    if re.fullmatch(r"[0-9\s.,]+(?:руб\.?|byn)?", lower_text):
        return False

    has_model_number = bool(re.search(r"\d", clean_text))
    has_product_marker = any(marker in lower_text for marker in PRODUCT_MARKERS)
    return len(clean_text) >= 5 and (has_model_number or has_product_marker)


def product_name_from_link_text(text: str) -> str:
    """Очистить название товара от бейджей, цены и характеристик карточки."""
    cleaned = normalize_space(text)
    cleaned = re.sub(r"^(?:0%|хит|new|новинка|спец\. предложение)\s+", "", cleaned, flags=re.I)
    cleaned = re.split(
        r"\s+(?:Тип ТС|Бренд|Наличие ЭПТС|Двигатель|Объем двигателя|Цена|Купить|Сравнить)\b",
        cleaned,
        maxsplit=1,
    )[0]
    cleaned = re.split(r"\s+\d[\d\s.,]*\s*(?:руб\.?|BYN)\b", cleaned, maxsplit=1, flags=re.I)[0]
    return cleaned


def first_spec_position(text: str) -> int:
    """Найти позицию первой характеристики в тексте карточки."""
    positions = [text.find(label) for label in SPEC_LABELS if text.find(label) >= 0]
    return min(positions) if positions else -1


def parse_specs_from_text(text: str) -> dict[str, dict[str, str]]:
    """Разобрать характеристики из плоского текста карточки каталога."""
    specs: dict[str, dict[str, str]] = {}
    matches = list(
        re.finditer(
            "|".join(
                rf"\b{re.escape(label)}\b"
                for label in sorted(SPEC_LABELS, key=len, reverse=True)
            ),
            text,
            flags=re.I,
        )
    )
    for index, match in enumerate(matches):
        key = normalize_space(match.group(0))
        value_start = match.end()
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = normalize_space(text[value_start:value_end])
        value = re.split(r"\s+(?:Купить|В корзину|Сравнить|Избранное)\b", value, maxsplit=1)[0]
        value = re.split(r"\s+\d[\d\s.,]*\s*(?:руб\.?|BYN)\b", value, maxsplit=1, flags=re.I)[0]
        if value and len(value) <= 160:
            specs.setdefault("Характеристики", {})[key] = value
    return specs


def looks_like_catalog_card_text(text: str) -> bool:
    """Проверить, похож ли плоский текст на карточку мотоцикла."""
    lower_text = text.lower()
    has_product_marker = any(marker in lower_text for marker in PRODUCT_MARKERS)
    has_specs = any(label.lower() in lower_text for label in SPEC_LABELS)
    return has_product_marker and has_specs


def product_from_catalog_card(card: dict[str, str], base_url: str) -> Motorcycle | None:
    """Создать товар из HTML-блока каталога без перехода в карточку товара."""
    text = normalize_space(card.get("text", ""))
    if not looks_like_catalog_card_text(text):
        return None

    spec_position = first_spec_position(text)
    name_source = text[:spec_position] if spec_position > 0 else text
    name = product_name_from_link_text(name_source)
    if not name or len(name) < 3:
        return None

    price = first_match([r"([0-9][0-9\s.,]{1,20}\s*(?:BYN|руб\.?|р\.))"], text)
    product_url = urljoin(base_url, card.get("url", "")) if card.get("url") else ""
    image = urljoin(base_url, card.get("image", "")) if card.get("image") else ""
    return Motorcycle(
        name=name,
        price=price,
        url=product_url,
        image=image,
        categories=parse_specs_from_text(text),
    )


def catalog_block_products(page_html: str, base_url: str, limit: int) -> list[Motorcycle]:
    """Fallback: достать товары прямо из HTML-блоков каталога без ссылок."""
    parser = CatalogCardParser()
    parser.feed(page_html)

    # Сначала берем самые маленькие подходящие блоки: большие контейнеры Bitrix
    # часто содержат сразу все товары, а маленькие блоки обычно являются карточками.
    indexed_cards = sorted(
        enumerate(parser.cards),
        key=lambda item: len(item[1].get("text", "")),
    )
    indexed_products: list[tuple[int, Motorcycle]] = []
    for card_index, card in indexed_cards:
        product = product_from_catalog_card(card, base_url)
        if product is None:
            continue
        indexed_products.append((card_index, product))
        products_for_limit = dedupe_products(product for _, product in indexed_products)
        if len(products_for_limit) >= limit:
            break

    indexed_products.sort(key=lambda item: item[0])
    products = dedupe_products(product for _, product in indexed_products)
    if products:
        return products[:limit]

    whole_page_product = product_from_catalog_card({"text": strip_tags(page_html)}, base_url)
    return [whole_page_product] if whole_page_product is not None else []


def dedupe_products(products: Iterable[Motorcycle]) -> list[Motorcycle]:
    """Убрать дубли, сохранив порядок."""
    seen: set[str] = set()
    unique: list[Motorcycle] = []
    for product in products:
        key = product.url or product.name
        if key and key not in seen:
            seen.add(key)
            unique.append(product)
    return unique


def catalog_products(page_html: str, base_url: str, limit: int) -> list[Motorcycle]:
    """Найти первые товары на странице каталога."""
    products = [product for product in jsonld_products(page_html, base_url) if product.url]
    if len(products) >= limit:
        return dedupe_products(products)[:limit]

    for href, text in html_links(page_html):
        if not looks_like_product_link(href, text, base_url):
            continue
        products.append(
            Motorcycle(
                name=product_name_from_link_text(text),
                url=urljoin(base_url, href).split("#", 1)[0],
            )
        )

    products = dedupe_products(products)
    if products:
        return products[:limit]

    # На motocikl.by карточки каталога могут содержать все данные прямо в HTML,
    # но не иметь нормальных ссылок на отдельные страницы.  Поэтому, если ссылки
    # не нашлись, парсим сами HTML-блоки каталога.
    return catalog_block_products(page_html, base_url, limit)


def first_match(patterns: Iterable[str], page_html: str) -> str:
    """Вернуть первое совпадение регулярных выражений."""
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.I | re.S)
        if match:
            return normalize_space(match.group(1))
    return ""


def meta_content(page_html: str, property_name: str) -> str:
    """Достать content из meta property/name."""
    pattern = (
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(property_name)}["\'][^>]+content=["\'](.*?)["\']'
    )
    return first_match([pattern], page_html)


def parse_specs(page_html: str) -> dict[str, dict[str, str]]:
    """Разобрать таблицы/списки характеристик и сгруппировать их по заголовкам."""
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
    """Дополнить товар данными со страницы карточки."""
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
    """Преобразовать список товаров в колонки и строки CSV."""
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

    return BASE_FIELDS + spec_fields, rows


def save_csv(products: list[Motorcycle], output_path: str) -> None:
    """Сохранить товары в CSV с кодировкой для Excel."""
    fields, rows = flatten_for_csv(products)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    """Прочитать параметры командной строки."""
    parser = argparse.ArgumentParser(
        description="Parse first enduro motorcycles from motocikl.by and export CSV."
    )
    parser.add_argument("--url", default=CATALOG_URL, help="Catalog page URL")
    parser.add_argument("--limit", type=int, default=5, help="How many motorcycles to parse")
    parser.add_argument("--output", default="enduro_motocikl.csv", help="CSV file path")
    parser.add_argument("--pause", type=float, default=1.0, help="Pause between product pages")
    parser.add_argument(
        "--debug-html",
        nargs="?",
        const="catalog.html",
        help="Save the downloaded catalog HTML to this file for troubleshooting",
    )
    return parser.parse_args()


def main() -> int:
    """Основной сценарий запуска."""
    args = parse_args()
    catalog_html = fetch(args.url)
    if args.debug_html:
        with open(args.debug_html, "w", encoding="utf-8") as debug_file:
            debug_file.write(catalog_html)

    products = catalog_products(catalog_html, args.url, args.limit)
    if not products:
        print(
            "No products were found on the catalog page. "
            "Run again with --debug-html catalog.html and send that file for inspection.",
            file=sys.stderr,
        )
        return 1

    parsed: list[Motorcycle] = []
    for number, product in enumerate(products, start=1):
        if product.categories:
            print(f"[{number}/{len(products)}] {product.name} (parsed from catalog HTML)")
            parsed.append(product)
            continue
        if not product.url:
            print(f"[{number}/{len(products)}] {product.name} (no detail URL)")
            parsed.append(product)
            continue

        print(f"[{number}/{len(products)}] {product.url}")
        detail_html = fetch(product.url)
        parsed.append(parse_detail(product, detail_html))
        time.sleep(args.pause)

    save_csv(parsed, args.output)
    print(f"CSV saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
