from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

import aiohttp

from services.net import ipv4_connector

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

LEGAL_NAME_SHORTENERS = (
    ("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ", "ООО"),
    ("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ПАО"),
    ("НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "НАО"),
    ("АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО"),
    ("ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ", "ИП"),
)


def shorten_company_name(name: str | None) -> str | None:
    if not name:
        return None
    result = name.strip()
    upper = result.upper()
    for full, short in LEGAL_NAME_SHORTENERS:
        if upper.startswith(full):
            rest = result[len(full) :].strip()
            return f"{short} {rest}".strip()
    return result


def extract_region(address: str | None, girbo_region: str | None = None) -> str | None:
    if girbo_region:
        return girbo_region.strip()
    if not address:
        return None

    patterns = [
        r"(\d{6},\s*)?([^,]+область)",
        r"(\d{6},\s*)?([^,]+край)",
        r"(\d{6},\s*)?([^,]+Республика[^,]*)",
        r"(\d{6},\s*)?(г\.?\s*[А-ЯЁ][а-яё\-]+)",
        r"(\d{6},\s*)?(г\s+[А-ЯЁ][а-яё\-]+)",
        r"(САНКТ-ПЕТЕРБУРГ|МОСКВА|СЕВАСТОПОЛЬ)",
    ]
    for pattern in patterns:
        match = re.search(pattern, address, re.IGNORECASE)
        if match:
            return match.group(match.lastindex).strip()
    return None


def extract_city(
    address: str | None,
    *,
    girbo_city: str | None = None,
    girbo_region: str | None = None,
) -> str | None:
    if girbo_city:
        city = girbo_city.strip()
        if city.lower().startswith("г."):
            city = city[2:].strip()
        elif city.lower().startswith("г "):
            city = city[1:].strip()
        return city

    if address:
        patterns = [
            r"г\.?\s*([А-ЯЁ][а-яё\-]+)",
            r"город\s+([А-ЯЁ][а-яё\-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, address, re.IGNORECASE)
            if match:
                return match.group(1).strip()

    if girbo_region:
        region = girbo_region.strip()
        if region in {"МОСКВА", "САНКТ-ПЕТЕРБУРГ", "СЕВАСТОПОЛЬ"}:
            return region.title()
        match = re.match(r"([А-ЯЁ][а-яё\-]+)(?:ская|ский|ская)?\s+область", region, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


def _strip_quotes(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r'["«»„""\'`]', "", text)
    return re.sub(r"\s+", " ", cleaned).strip() or None


def build_alice_query(short_name: str | None, city: str | None) -> str | None:
    name = _strip_quotes(short_name)
    place = _strip_quotes(city)
    parts = [name, place, "о компании"]
    query = " ".join(part for part in parts if part)
    return query or None


def alice_search_url(query: str) -> str:
    return f"https://ya.ru/search/?text={quote_plus(query)}"


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = text.replace("\\n", " ").replace("\\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_from_html(html: str) -> str | None:
    candidates: list[str] = []

    for match in re.finditer(
        r'"(?:plainText|text|answer|content)"\s*:\s*"((?:\\.|[^"\\])*)"',
        html,
    ):
        raw = match.group(1)
        try:
            text = json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            text = raw.replace("\\n", " ").replace('\\"', '"')
        text = _clean_html_text(text)
        if len(text) > 80:
            candidates.append(text)

    html_patterns = [
        r'class="[^"]*(?:ExtendedText|FactFold|fact-fold|Neuro|Futuris)[^"]*"[^>]*>(.*?)</div>',
        r'class="[^"]*OrganicTextContentSpan[^"]*"[^>]*>(.*?)</span>',
    ]
    for pattern in html_patterns:
        for match in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            text = _clean_html_text(match.group(1))
            if len(text) > 80:
                candidates.append(text)

    if not candidates:
        return None

    best = max(candidates, key=len)
    return best[:1200]


async def fetch_alice_about_company(query: str) -> str | None:
    encoded = quote_plus(query)
    urls = [
        f"https://ya.ru/search/?text={encoded}",
        f"https://yandex.ru/search/?text={encoded}",
    ]

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(
        timeout=timeout, headers=HEADERS, connector=ipv4_connector()
    ) as session:
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
                    text = _extract_from_html(html)
                    if text:
                        return text
            except Exception:
                continue
    return None


_FEDERAL_CITIES = frozenset({"МОСКВА", "САНКТ-ПЕТЕРБУРГ", "СЕВАСТОПОЛЬ"})


def _normalize_region_label(region: str | None) -> str | None:
    if not region:
        return None
    region = region.strip()
    if not region:
        return None
    upper = region.upper()
    if upper in _FEDERAL_CITIES:
        return region.title()
    if re.search(r"(область|край|республика)", region, re.IGNORECASE):
        return region[0].upper() + region[1:]
    return f"{region.title()} область"


def _normalize_city_label(city: str | None) -> str | None:
    if not city:
        return None
    city = city.strip()
    if city.lower().startswith("г."):
        city = city[2:].strip()
    elif city.lower().startswith("г "):
        city = city[1:].strip()
    if not city:
        return None
    if city.upper() in _FEDERAL_CITIES:
        return city.title()
    return f"г. {city[0].upper() + city[1:]}"


def _region_in_text(text: str, region_label: str) -> bool:
    left = re.sub(r"\s+", " ", text.lower())
    right = re.sub(r"\s+", " ", region_label.lower())
    return right in left


def _city_in_text(text: str, city_label: str) -> bool:
    city_name = re.sub(r"^г\.\s*", "", city_label, flags=re.IGNORECASE).strip().lower()
    return bool(city_name and city_name in text.lower())


def format_company_location(
    address: str | None,
    region: str | None = None,
    city: str | None = None,
) -> str | None:
    """Регион и населённый пункт для блока «О компании»."""
    region_label = _normalize_region_label(region)
    city_label = _normalize_city_label(city)

    if address:
        addr = re.sub(r"\s+", " ", address.strip(" .;,"))
        if addr:
            has_region = bool(region_label and _region_in_text(addr, region_label))
            has_city = bool(city_label and _city_in_text(addr, city_label))
            if has_region or has_city or not (region_label or city_label):
                return addr
            extra: list[str] = []
            if region_label and not has_region:
                extra.append(region_label)
            if city_label and not has_city:
                extra.append(city_label)
            if extra:
                return f"{', '.join(extra)}, {addr}"
            return addr

    if region_label and region_label.upper() in _FEDERAL_CITIES:
        return region_label
    if city_label and city_label.upper() in {item.title() for item in _FEDERAL_CITIES}:
        return city_label

    parts: list[str] = []
    if region_label:
        parts.append(region_label)
    if city_label:
        city_name = re.sub(r"^г\.\s*", "", city_label, flags=re.IGNORECASE).strip()
        region_core = re.sub(
            r"\s+(область|край|республика.*)$",
            "",
            region_label or "",
            flags=re.IGNORECASE,
        ).strip()
        if not region_core or city_name.lower() not in region_core.lower():
            parts.append(city_label)
    return ", ".join(parts) if parts else None


def build_company_about(
    *,
    name: str | None,
    region: str | None,
    address: str | None,
    city: str | None = None,
) -> dict[str, str | None]:
    """Полное наименование и адрес для шапки карточки."""
    full_name = name.strip() if name else None
    location = format_company_location(address, region, city)
    return {"full_name": full_name, "location": location}


def build_company_about_line(
    *,
    name: str | None,
    region: str | None,
    address: str | None,
    city: str | None = None,
) -> str | None:
    about = build_company_about(name=name, region=region, address=address, city=city)
    parts = [part for part in (about["full_name"], about["location"]) if part]
    return ". ".join(parts) if parts else None


def build_fallback_description(
    *,
    name: str | None,
    region: str | None,
    status: str | None,
    address: str | None,
    city: str | None = None,
) -> str:
    parts: list[str] = []
    if name:
        parts.append(name)
    location = format_company_location(address, region, city)
    if location:
        parts.append(location)
    if status:
        parts.append(f"Статус: {status}")
    return ". ".join(parts) if parts else "Описание недоступно."


async def build_company_description(
    *,
    short_name: str | None,
    city: str | None,
    name: str | None,
    inn: str,
    region: str | None,
    status: str | None,
    address: str | None,
) -> dict:
    display_name = short_name or shorten_company_name(name) or name
    alice_query = build_alice_query(display_name, city)
    alice_url = alice_search_url(alice_query) if alice_query else None
    alice_text = await fetch_alice_about_company(alice_query) if alice_query else None
    about_line = build_company_about_line(
        name=name, region=region, address=address, city=city
    )
    about = build_company_about(name=name, region=region, address=address, city=city)

    fallback = build_fallback_description(
        name=name,
        region=region,
        status=status,
        address=address,
        city=city,
    )

    if alice_text:
        return {
            "text": alice_text,
            "about": about,
            "about_line": about_line,
            "source": "alice",
            "alice_url": alice_url,
            "alice_query": alice_query,
        }

    return {
        "text": fallback,
        "about": about,
        "about_line": about_line,
        "source": "registry",
        "alice_url": alice_url,
    }
