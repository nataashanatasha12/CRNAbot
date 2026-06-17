"""ГИР БО — строгий клиент.

Правило одно: скачиваем только отчёт из карточки организации, где
organization_info.inn совпадает с запрошенным ИНН. Ссылка и detailId —
только из этого же объекта reports[] (поле url / detail_id / audit_report).
Никакого /bfo, correction.id и URL с id из поиска.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from pypdf import PdfReader

from services.girbo_forms import extract_form_rows
from services.girbo_excel import form_rows_to_xlsx
from services.inn_utils import inn_equal, normalize_inn
from services.net import ipv4_connector
from services.okved import GIRBO_OKVED_PERIOD

logger = logging.getLogger("crnabot.girbo")

BO_SITE = "https://bo.nalog.gov.ru"
BO_API = "https://bo.nalog.ru"
BFO_CLOSED_SITE = "https://bfo.nalog.gov.ru"
API_BASES = (BO_API, BO_SITE)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

API_HEADERS_RU = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
API_HEADERS_GOV = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{BO_SITE}/",
    "Origin": BO_SITE,
}
DOWNLOAD_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Referer": f"{BO_SITE}/",
    "Origin": BO_SITE,
}

TIMEOUT = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
API_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)

CACHE_VERSION = "strict-v28"
CACHE_TTL_SEC = 300
_PDF_BYTES_CACHE: dict[str, tuple[float, bytes, str, str]] = {}
_PDF_BYTES_CACHE_TTL_SEC = 6 * 3600
_org_cache: dict[str, tuple[float, "BoOrganization"]] = {}


class GirBoError(Exception):
    pass


@dataclass
class BoDocument:
    title: str
    period: str
    kind: str
    format: str
    org_id: str | None = None
    url: str | None = None
    detail_id: str | None = None
    fallback_urls: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    expected_inn: str | None = None
    expected_org_name: str | None = None


@dataclass
class BoOrganization:
    org_id: str
    inn: str
    name: str
    short_name: str | None
    ogrn: str
    card_url: str
    okved: str | None = None
    region: str | None = None
    city: str | None = None
    documents: list[BoDocument] = field(default_factory=list)
    latest_balance_rows: list[dict[str, Any]] = field(default_factory=list)
    latest_financial_rows: list[dict[str, Any]] = field(default_factory=list)
    latest_period: str | None = None
    no_public_reporting: bool = False
    stale_open_reporting: bool = False


def minimum_acceptable_report_period(today: date | None = None) -> int:
    """Минимально допустимый год отчётности в открытом ГИР БО.

    С апреля (month >= 4): нужна отчётность минимум за прошлый календарный год
    (year - 1). Январь–март: льготный период — отчётность за (year - 2) ещё
    допустима, пока отчётность за прошлый год обычно ещё не опубликована.
    """
    ref = today or date.today()
    if ref.month < 4:
        return ref.year - 2
    return ref.year - 1


def expected_report_year() -> str:
    """Номинально ожидаемый год отчётности (прошлый календарный год)."""
    return str(date.today().year - 1)


def is_stale_open_reporting(latest_period: str | None) -> bool:
    """В открытом ГИР БО осталась только устаревшая отчётность — актуальная в закрытом контуре."""
    period = str(latest_period or "").strip()
    if not period.isdigit():
        return False
    return int(period) < minimum_acceptable_report_period()


def girbo_stale_open_notice(latest_period: str) -> str:
    period = str(latest_period or "").strip()
    return f"В открытом ГИР БО доступна только устаревшая отчётность за {period} год."


def bfo_closed_card_url(org_id: str) -> str:
    return f"{BFO_CLOSED_SITE}/organizations-card/{org_id.strip()}"


def bfo_closed_search_url(inn: str) -> str:
    return f"{BFO_CLOSED_SITE}/search?query={normalize_inn(inn.strip())}"


def bfo_closed_link_url(*, inn: str, org_id: str | None = None) -> str:
    if org_id:
        return bfo_closed_card_url(org_id)
    return bfo_closed_search_url(inn)


GIRBO_INVALID_INN_MARKER = "нужен корректный инн"


def girbo_offer_closed_contour(exc: Exception, message: str = "") -> bool:
    """Кнопка bfo.nalog.gov.ru — для всех сбоев ГИР БО, кроме явно неверного ИНН."""
    text = (message or str(exc) or "").strip().lower()
    return GIRBO_INVALID_INN_MARKER not in text


def girbo_open_notice(message: str) -> str:
    """Текст для карточки: что не удалось получить из открытого ГИР БО."""
    text = (message or "").strip().lower()
    if "не найдена в гир бо" in text:
        return "В открытом ГИР БО организация не найдена"
    if "не удалось найти отчётность" in text:
        return "В открытом ГИР БО отчётность не найдена"
    if any(marker in text for marker in ("timeout", "connection", "недоступен")):
        return "В открытом ГИР БО данные не получены (нет связи с сервисом ФНС)"
    return "В открытом ГИР БО данные не найдены"


GIRBO_OPEN_NO_REPORTING_NOTICE = "В открытом ГИР БО отчётность не размещена"


@dataclass
class _VerifiedReport:
    period: str
    detail_id: str
    pdf_urls: list[str]
    audit_url: str | None
    report_inn: str
    org_name: str | None = None
    pdf_org_id: str | None = None
    pdf_org_ids: list[str] = field(default_factory=list)
    balance_rows: list[dict[str, Any]] = field(default_factory=list)
    financial_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pdf_url(self) -> str:
        return self.pdf_urls[0] if self.pdf_urls else ""


def clear_org_cache(inn: str | None = None) -> None:
    if inn:
        _org_cache.pop(f"{CACHE_VERSION}:{normalize_inn(inn)}", None)
    else:
        _org_cache.clear()


def latest_year_documents(docs: list[BoDocument]) -> list[BoDocument]:
    periods = [d.period.strip() for d in docs if d.period.strip().isdigit()]
    if not periods:
        return docs
    latest = max(periods)
    picked = [d for d in docs if d.period.strip() == latest]
    if not any(d.kind == "audit" for d in picked):
        for doc in docs:
            if doc.kind != "audit" or doc in picked:
                continue
            if doc.period.strip().isdigit() and doc.period.strip() == latest:
                picked.append(doc)
            elif any(doc.period.strip() == d.period.strip() for d in picked if d.kind == "balance"):
                picked.append(doc)
            elif not doc.period.strip():
                picked.append(doc)
    return picked


def _api_headers(base: str) -> dict[str, str]:
    if base == BO_API:
        return API_HEADERS_RU
    return {
        **API_HEADERS_GOV,
        "Referer": f"{base}/",
        "Origin": base,
    }


async def _api_get_on_base(
    session: aiohttp.ClientSession,
    base: str,
    path: str,
    params: dict | None = None,
) -> Any:
    paths = [path, f"{path}/"] if "/bfo" in path and not path.endswith("/") else [path]
    last: Exception | None = None
    for p in paths:
        try:
            async with session.get(
                f"{base}{p}",
                params=params,
                headers=_api_headers(base),
                allow_redirects=True,
                timeout=API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    last = GirBoError(f"HTTP {resp.status}")
                    continue
                return await _parse_json(await resp.read(), resp.status)
        except (GirBoError, aiohttp.ClientError, json.JSONDecodeError) as exc:
            last = exc
    raise last or GirBoError("ГИР БО недоступен.")


async def _api_get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> Any:
    last: Exception | None = None
    for base in API_BASES:
        try:
            return await _api_get_on_base(session, base, path, params)
        except (GirBoError, aiohttp.ClientError, json.JSONDecodeError) as exc:
            last = exc
    raise last or GirBoError("ГИР БО недоступен.")


async def _parse_json(raw: bytes, status: int) -> Any:
    text = raw.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
    if not text or text[0] not in "{[":
        raise GirBoError(f"не JSON (HTTP {status})")
    return json.loads(text)


async def _warm(session: aiohttp.ClientSession) -> None:
    await session.get(f"{BO_SITE}/", headers=API_HEADERS_GOV)
    await session.get(f"{BO_API}/", headers=API_HEADERS_RU)


def _pick_name(org: dict[str, Any]) -> str:
    for k in ("shortName", "short_name", "fullName", "full_name", "name"):
        if org.get(k):
            return str(org[k]).strip()
    return "—"


def _pick_short_name(org: dict[str, Any]) -> str | None:
    v = org.get("shortName") or org.get("short_name")
    return str(v).strip() if v else None


_OKVED_CODE_RE = re.compile(r"^\d{2}\.\d{2}(?:\.\d{1,2})?$")
_OKVED_INLINE_RE = re.compile(
    r"(\d{2}\.\d{2}(?:\.\d{1,2})?)\s*[-–—]?\s*(.+)",
    re.UNICODE,
)


def _okved_codes_from_value(value: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        code_keys = (
            "subId",
            "subCode",
            "sub_id",
            "sub_code",
            "fullId",
            "fullCode",
            "leafId",
            "leafCode",
            "code",
            "id",
            "okved",
            "value",
        )
        name_keys = ("subName", "sub_name", "fullName", "name", "title")
        codes: list[str] = []
        for key in code_keys:
            raw = str(value.get(key) or "").strip()
            if _OKVED_CODE_RE.fullmatch(raw):
                codes.append(raw)
        name = ""
        for key in name_keys:
            raw = str(value.get(key) or "").strip()
            if raw:
                name = raw
                break
        for code in codes:
            pairs.append((code, name))
        for subkey in ("subOkved", "subOkved2", "subokved2", "sub", "child", "children"):
            sub = value.get(subkey)
            if sub is not None:
                pairs.extend(_okved_codes_from_value(sub))
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                pairs.extend(_okved_codes_from_value(nested))
    elif isinstance(value, list):
        for item in value:
            pairs.extend(_okved_codes_from_value(item))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return pairs
        match = _OKVED_INLINE_RE.search(text)
        if match:
            pairs.append((match.group(1), match.group(2).strip()))
        elif _OKVED_CODE_RE.fullmatch(text):
            pairs.append((text, ""))
    return pairs


def _pick_most_specific_okved(pairs: list[tuple[str, str]]) -> tuple[str, str] | None:
    best_by_code: dict[str, str] = {}
    for code, name in pairs:
        code = code.strip()
        if not _OKVED_CODE_RE.fullmatch(code):
            continue
        prev_name = best_by_code.get(code, "")
        if len(name) > len(prev_name):
            best_by_code[code] = name.strip()
    if not best_by_code:
        return None
    code = max(best_by_code, key=lambda item: (item.count("."), len(item)))
    return code, best_by_code[code]


def _format_okved(okved: Any) -> str | None:
    if not okved:
        return None
    if isinstance(okved, str):
        text = okved.strip()
        if not text:
            return None
        match = _OKVED_INLINE_RE.search(text)
        if match:
            return f"{match.group(1)} — {match.group(2).strip()}"
        if _OKVED_CODE_RE.fullmatch(text):
            return text
        return text
    picked = _pick_most_specific_okved(_okved_codes_from_value(okved))
    if not picked:
        return None
    code, name = picked
    return f"{code} — {name}" if code and name else code


def _okved_payload_from_obj(obj: dict[str, Any]) -> Any | None:
    for key in ("okved2", "okved", "mainOkved", "main_okved", "primaryOkved", "okvedMain"):
        if obj.get(key):
            return obj[key]
    return None


def _okved_from_reports(card: dict[str, Any], period: str) -> Any | None:
    for report in _collect_reports(card):
        if _item_period(report) != period:
            continue
        for node in _iter_nodes(report):
            payload = _okved_payload_from_obj(node)
            if payload:
                return payload
            info = node.get("organization_info") or node.get("organizationInfo")
            if isinstance(info, dict):
                payload = _okved_payload_from_obj(info)
                if payload:
                    return payload
    return None


def _okved_specificity(text: str | None) -> tuple[int, int]:
    if not text:
        return (0, 0)
    match = re.match(r"(\d{2}\.\d{2}(?:\.\d{1,2})?)", text.strip())
    if not match:
        return (0, len(text))
    code = match.group(1)
    return (code.count("."), len(code))


def _resolve_org_okved(
    card: dict[str, Any],
    search_row: dict[str, Any],
    period_card: dict[str, Any] | None,
    period: str,
) -> str | None:
    raw_candidates: list[Any] = []
    for source in (period_card, card):
        if not isinstance(source, dict):
            continue
        payload = _okved_payload_from_obj(source)
        if payload:
            raw_candidates.append(payload)
        report_payload = _okved_from_reports(source, period)
        if report_payload:
            raw_candidates.append(report_payload)
    for source in (card, search_row):
        payload = _okved_payload_from_obj(source)
        if payload:
            raw_candidates.append(payload)

    formatted: list[str] = []
    for raw in raw_candidates:
        text = _format_okved(raw)
        if text:
            formatted.append(text)
    if not formatted:
        return None
    best = max(formatted, key=_okved_specificity)
    if len(formatted) > 1:
        logger.info(
            "ГИР БО okved pick period=%s candidates=%s chosen=%s",
            period,
            formatted[:4],
            best,
        )
    return best


def parse_okved_from_card_text(text: str) -> str | None:
    """ОКВЭД с текстовой карточки ГИР БО (как на organizations-card?period=2024)."""
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text)
    for pattern in (
        r"ОКВЭД(?:\s*\([^)]*\))?\s*[:\s]*(\d{2}\.\d{2}(?:\.\d{1,2})?)\s*[-–—]?\s*"
        r"([^|\n]{10,240}?)(?:\s{2,}|ОКВЭД|ИНН|ОГРН|Адрес|\||$)",
        r"основной вид деятельности\s*[:\s]*(\d{2}\.\d{2}(?:\.\d{1,2})?)\s*[-–—]?\s*"
        r"([^|\n]{10,240}?)(?:\s{2,}|ИНН|ОГРН|$)",
    ):
        match = re.search(pattern, compact, re.I)
        if match:
            code = match.group(1)
            name = match.group(2).strip(" -–—|")
            return f"{code} — {name}" if name else code
    codes = re.findall(r"(?<![\d.])(\d{2}\.\d{2}(?:\.\d{1,2})?)(?![\d.])", compact)
    if not codes:
        return None
    code = max(set(codes), key=lambda item: (item.count("."), len(item)))
    return code


def _extract_okved_from_html(html: str) -> str | None:
    if not html:
        return None
    for chunk in re.findall(r"okved2?.{0,1200}", html, re.I):
        pairs = _okved_codes_from_value(chunk)
        picked = _pick_most_specific_okved(pairs)
        if picked:
            code, name = picked
            return f"{code} — {name}" if name else code
    plain = re.sub(r"<[^>]+>", " ", html)
    plain = re.sub(r"\s+", " ", plain)
    return parse_okved_from_card_text(plain)


_okved_page_cache: dict[str, tuple[float, str]] = {}
_OKVED_PAGE_CACHE_TTL_SEC = 8 * 3600


def _okved_page_cache_key(org_id: str, period: str) -> str:
    return f"{org_id}:{period}"


def cache_girbo_okved_from_page(org_id: str, period: str, okved: str | None) -> None:
    if not okved:
        return
    _okved_page_cache[_okved_page_cache_key(org_id, period)] = (time.monotonic(), okved)


def get_cached_girbo_okved_from_page(org_id: str, period: str) -> str | None:
    cached = _okved_page_cache.get(_okved_page_cache_key(org_id, period))
    if cached and time.monotonic() - cached[0] < _OKVED_PAGE_CACHE_TTL_SEC:
        return cached[1]
    return None


async def _fetch_okved_from_card_html(
    session: aiohttp.ClientSession,
    org_id: str,
    period: str,
) -> str | None:
    url = f"{BO_SITE}/organizations-card/{org_id}?period={period}"
    headers = {
        **API_HEADERS_GOV,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            html = await resp.text(errors="ignore")
    except Exception as exc:
        logger.info("GIR BO okved html fetch failed org_id=%s period=%s: %s", org_id, period, exc)
        return None
    return _extract_okved_from_html(html)


async def _enrich_org_okved_from_card_page(
    session: aiohttp.ClientSession,
    org_id: str,
    period: str,
    api_okved: str | None,
) -> str | None:
    cached = get_cached_girbo_okved_from_page(org_id, period)
    if cached and _okved_specificity(cached) >= _okved_specificity(api_okved):
        return cached

    best = api_okved
    if _okved_specificity(best)[0] < 2:
        html_okved = await _fetch_okved_from_card_html(session, org_id, period)
        if html_okved and _okved_specificity(html_okved) > _okved_specificity(best):
            best = html_okved

    if _okved_specificity(best)[0] < 2:
        try:
            from services.girbo_screenshot import extract_girbo_okved_from_card

            live_okved = await extract_girbo_okved_from_card(org_id, period)
        except Exception as exc:
            logger.info("GIR BO okved playwright fallback failed org_id=%s: %s", org_id, exc)
            live_okved = None
        if live_okved and _okved_specificity(live_okved) > _okved_specificity(best):
            best = live_okved

    if best and best != api_okved:
        logger.info(
            "ГИР БО okved enriched org_id=%s period=%s api=%s page=%s",
            org_id,
            period,
            api_okved,
            best,
        )
    if best:
        cache_girbo_okved_from_page(org_id, period, best)
    return best or api_okved


def _region_from_org(org: dict[str, Any]) -> str | None:
    if org.get("region"):
        return str(org["region"])
    parts = [str(p) for p in (org.get("city"), org.get("settlement")) if p]
    return ", ".join(parts) if parts else None


def _city_from_org(org: dict[str, Any]) -> str | None:
    for k in ("city", "settlement"):
        if org.get(k):
            return str(org[k]).strip()
    return None


def _parse_pdf_url(url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    m = re.search(r"/download/bfo/pdf/(\d+)", url, re.I)
    path_id = m.group(1) if m else ""
    if path_id:
        out["pdf_org_id"] = path_id
    qs = parse_qs(urlparse(url).query)
    if qs.get("detailId"):
        out["detail_id"] = str(qs["detailId"][0])
    elif qs.get("detail_id"):
        out["detail_id"] = str(qs["detail_id"][0])
    elif path_id and not qs:
        # Как на сайте ФНС: /download/bfo/pdf/57516677 — id корректировки в пути
        out["detail_id"] = path_id
        out["pdf_path_id"] = path_id
    if qs.get("period"):
        out["period"] = str(qs["period"][0])
    if qs.get("knd"):
        out["knd"] = str(qs["knd"][0])
    return out


def _detail_path_pdf_urls(detail_id: str, knd: str | None = None) -> list[str]:
    """Официальный формат с сайта bo.nalog.gov.ru: id корректировки в пути URL."""
    urls: list[str] = []
    base = f"{BO_SITE}/download/bfo/pdf/{detail_id}"
    if knd:
        with_knd = f"{base}?knd={knd}"
        urls.append(with_knd)
    if base not in urls:
        urls.append(base)
    return urls


def _report_inn(report: dict[str, Any]) -> str:
    info = report.get("organization_info") or report.get("organizationInfo") or {}
    return normalize_inn(info.get("inn"))


def _report_name(report: dict[str, Any]) -> str | None:
    info = report.get("organization_info") or report.get("organizationInfo") or {}
    for k in ("full_name", "fullName", "short_name", "shortName"):
        if info.get(k):
            return str(info[k]).strip()
    return None


def _pdf_text(data: bytes) -> str:
    try:
        from io import BytesIO

        reader = PdfReader(BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return data.decode("latin-1", errors="ignore")


def _org_name_markers(org_name: str | None) -> list[str]:
    if not org_name:
        return []
    markers: list[str] = []
    cleaned = org_name.strip().strip('"«»')
    if cleaned:
        markers.append(cleaned)
    upper = cleaned.upper()
    for token in re.split(r"[\s\"«»]+", upper):
        if len(token) >= 4 and token not in {"ОБЩЕСТВО", "ОГРАНИЧЕННОЙ", "ОТВЕТСТВЕННОСТЬЮ", "ПУБЛИЧНОЕ", "АКЦИОНЕРНОЕ"}:
            markers.append(token)
    return list(dict.fromkeys(markers))


def _number_variants(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    try:
        number = int(float(str(value).replace(",", ".").replace(" ", "")))
    except (TypeError, ValueError):
        return []
    variants = {str(number), str(abs(number))}
    if number < 0:
        variants.add(str(number).lstrip("-"))
    spaced = f"{number:,}".replace(",", " ")
    variants.add(spaced)
    variants.add(spaced.replace(" ", "\u00a0"))
    return [v for v in variants if v]


def _pdf_matches_figures(text: str, rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    key_codes = {"1600", "1300", "2110", "2400", "1200", "1500"}
    compact = re.sub(r"\D", "", text)
    spaced = text.replace("\u00a0", " ")
    hits = 0
    for row in rows:
        code = str(row.get("code") or "")
        if code not in key_codes:
            continue
        for field in ("current", "previous", "before_previous"):
            for variant in _number_variants(row.get(field)):
                if variant in compact or variant in spaced:
                    hits += 1
                    break
            else:
                continue
            break
    if any(str(row.get("code")) == "1600" for row in rows):
        return hits >= 1
    return hits >= 2


def _pdf_matches_report(
    data: bytes,
    inn: str,
    org_name: str | None = None,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> bool:
    text = _pdf_text(data)
    compact = re.sub(r"\D", "", text)
    normalized = normalize_inn(inn)
    if normalized:
        for variant in {normalized, normalized.zfill(10), normalized.lstrip("0")}:
            if variant and variant in compact:
                return True
            if variant and variant in text.replace(" ", ""):
                return True

    upper_text = text.upper()
    for marker in _org_name_markers(org_name):
        if marker.upper() in upper_text:
            return True

    if rows and _pdf_matches_figures(text, rows):
        return True

    return False


def _pdf_contains_inn(data: bytes, inn: str) -> bool:
    return _pdf_matches_report(data, inn)


def _matching_correction(
    pool: list[dict[str, Any]],
    detail_id: str,
    inn: str,
    period: str,
) -> dict[str, Any] | None:
    for item in pool:
        if _item_period(item, pool) != period:
            continue
        for bucket in (item.get("typeCorrections"), item.get("corrections")):
            if not isinstance(bucket, list):
                continue
            for entry in bucket:
                if not isinstance(entry, dict):
                    continue
                corr = entry.get("correction") or entry
                if not isinstance(corr, dict):
                    continue
                corr_inn = _report_inn(corr)
                if corr_inn and not inn_equal(corr_inn, inn):
                    continue
                if str(corr.get("id") or "") == detail_id:
                    return corr
                audit = corr.get("audit_report") or corr.get("auditReport") or {}
                if isinstance(audit, dict):
                    furl = str(audit.get("file_url") or audit.get("fileUrl") or "")
                    if detail_id and detail_id in furl:
                        return corr
    return None


def _resolve_pdf_org_id(
    pool: list[dict[str, Any]],
    period: str,
    detail_id: str,
    org_id: str,
    *,
    inn: str = "",
) -> str:
    ids = _pdf_org_ids_from_pool(pool, period, check_inn=inn)
    if ids:
        ids.sort(key=lambda pid: (0 if pid != str(org_id) else 1, pid))
        return ids[0]
    return str(org_id)


def _official_ms_excel_urls(
    pdf_org_ids: list[str],
    period: str,
    detail_id: str,
    knd: str | None = None,
) -> list[str]:
    urls: list[str] = []
    for pdf_org_id in pdf_org_ids:
        base = f"{BO_SITE}/download/bfo/ms-excel/{pdf_org_id}?period={period}&detailId={detail_id}"
        if base not in urls:
            urls.append(base)
        if knd:
            with_knd = f"{base}&knd={knd}"
            if with_knd not in urls:
                urls.append(with_knd)
    return urls


def _official_full_pdf_urls(
    pdf_org_ids: list[str],
    period: str,
    detail_id: str,
) -> list[str]:
    urls: list[str] = []
    for pdf_org_id in pdf_org_ids:
        url = f"{BO_SITE}/download/bfo/pdf/{pdf_org_id}?period={period}&detailId={detail_id}"
        if url not in urls:
            urls.append(url)
    return urls


def _knd_urls_from_pool(
    pool: list[dict[str, Any]],
    period: str,
    detail_id: str,
    knd: str,
    *,
    inn: str = "",
) -> list[str]:
    urls: list[str] = []
    needle = f"detailId={detail_id}"
    for candidate in pool:
        if inn and _item_inn(candidate) and not inn_equal(_item_inn(candidate), inn):
            continue
        if _item_period(candidate, pool) != period:
            continue
        for node in _iter_nodes(candidate):
            url = node.get("url")
            if not isinstance(url, str):
                continue
            if "download/bfo/" not in url or f"knd={knd}" not in url.lower():
                continue
            if needle not in url:
                continue
            if url not in urls:
                urls.append(url)
    return urls


def _collect_pdf_org_ids(
    pool: list[dict[str, Any]],
    period: str,
    org_id: str,
    *,
    inn: str = "",
    detail_id: str = "",
) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    bfo_ids: list[str] = []
    url_ids: list[str] = []

    def add(value: Any, *, bucket: list[str] | None = None) -> None:
        sid = str(value or "").strip()
        if sid.isdigit() and sid not in seen:
            seen.add(sid)
            (bucket or ids).append(sid)

    for candidate in pool:
        if not _is_root_bfo_row(candidate):
            continue
        if _item_period(candidate, pool) != period:
            continue
        for key in ("id", "bfoId", "fileId"):
            sid = str(candidate.get(key) or "").strip()
            if sid.isdigit() and sid != str(org_id):
                add(sid, bucket=bfo_ids)
    for candidate in pool:
        if check_inn := inn:
            item_inn = _item_inn(candidate)
            if item_inn and not inn_equal(item_inn, check_inn):
                continue
        if _item_period(candidate, pool) != period:
            continue
        for node in _iter_nodes(candidate):
            url = str(node.get("url") or "")
            if not url or "download/bfo/pdf" not in url:
                continue
            if detail_id and f"detailId={detail_id}" not in url and f"detail_id={detail_id}" not in url:
                continue
            parsed = _parse_pdf_url(url)
            if parsed.get("period") and parsed["period"] != period:
                continue
            add(parsed.get("pdf_org_id"), bucket=url_ids)

    for sid in bfo_ids + url_ids + [str(org_id)]:
        add(sid)
    return ids


def _official_form_pdf_urls(
    pdf_org_ids: list[str],
    period: str,
    detail_id: str,
    knd: str,
) -> list[str]:
    urls: list[str] = []
    for pdf_org_id in pdf_org_ids:
        url = f"{BO_SITE}/download/bfo/pdf/{pdf_org_id}?period={period}&detailId={detail_id}&knd={knd}"
        if url not in urls:
            urls.append(url)
    return urls


def _form_download_urls(pdf_org_id: str, period: str, detail_id: str, knd: str) -> list[str]:
    return _official_form_pdf_urls([pdf_org_id], period, detail_id, knd)


def _collect_reports(card: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("reports", "reportList", "bfoReports"):
        bucket = card.get(key)
        if isinstance(bucket, list):
            items.extend(x for x in bucket if isinstance(x, dict))
    return items


def _iter_nodes(item: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = [item]
    for bucket in (item.get("typeCorrections"), item.get("corrections")):
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            if isinstance(entry, dict):
                nodes.append(entry)
                corr = entry.get("correction")
                if isinstance(corr, dict):
                    nodes.append(corr)
    return nodes


def _official_pdf_from_node(node: dict[str, Any]) -> tuple[str, str, str] | None:
    """Точная ссылка PDF из узла: (url, detail_id, period)."""
    url = node.get("url")
    if not isinstance(url, str) or "download/bfo/pdf" not in url:
        return None
    if "knd=" in url.lower():
        return None
    parsed = _parse_pdf_url(url)
    detail_id = parsed.get("detail_id")
    period = parsed.get("period") or str(node.get("period") or node.get("year") or "").strip()
    if detail_id and (period or parsed.get("pdf_path_id")):
        return url, detail_id, period
    return None


def _find_official_pdf(item: dict[str, Any]) -> tuple[str, str, str] | None:
    for node in _iter_nodes(item):
        found = _official_pdf_from_node(node)
        if found:
            return found
    return None


def _item_period(item: dict[str, Any], pool: list[dict[str, Any]] | None = None) -> str:
    period = str(item.get("period") or item.get("year") or "").strip()
    if not period and isinstance(item.get("url"), str):
        period = _parse_pdf_url(item["url"]).get("period", "")
    if not period and pool:
        for candidate in pool:
            if candidate is item:
                continue
            c_period = str(candidate.get("period") or candidate.get("year") or "").strip()
            if not c_period:
                continue
            for node in _iter_nodes(candidate):
                if node is item or node.get("id") == item.get("id"):
                    return c_period
    return period


def _correction_id_from_nodes(report: dict[str, Any], org_id: str | None = None) -> str | None:
    """correction.id как detailId — только в typeCorrections, не id строки /bfo."""
    for bucket in (report.get("typeCorrections"), report.get("corrections")):
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            sources: list[dict[str, Any]] = []
            if isinstance(entry, dict):
                sources.append(entry)
                corr = entry.get("correction")
                if isinstance(corr, dict):
                    sources.append(corr)
            for node in sources:
                val = node.get("id")
                if val is None:
                    continue
                sid = str(val).strip()
                if not sid.isdigit():
                    continue
                if org_id and sid == str(org_id):
                    continue
                return sid
    return None


def _detail_id_from_report(
    report: dict[str, Any],
    org_id: str | None = None,
    *,
    pool: list[dict[str, Any]] | None = None,
) -> str | None:
    """detailId из официального PDF-url, явных полей, audit; correction.id — последний fallback."""
    for node in _iter_nodes(report):
        found = _official_pdf_from_node(node)
        if found:
            return found[1]

    period = _item_period(report, pool)
    if pool and period:
        report_inn = _item_inn(report)
        for item in pool:
            if report_inn and _item_inn(item) and not inn_equal(_item_inn(item), report_inn):
                continue
            for node in _iter_nodes(item):
                found = _official_pdf_from_node(node)
                if found and found[2] == period:
                    return found[1]

    detail_keys = ("detailId", "detail_id", "publishedDetailId", "actualDetailId")
    for node in _iter_nodes(report):
        for key in detail_keys:
            val = node.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()

    audit_id = _audit_detail_id(report)
    if audit_id:
        return audit_id

    return _correction_id_from_nodes(report, org_id)


def _is_root_bfo_row(item: dict[str, Any]) -> bool:
    return isinstance(item.get("typeCorrections"), list) or isinstance(item.get("corrections"), list)


def _nbo_bfo_download_url(item: dict[str, Any], period: str, org_id: str) -> str | None:
    if not _is_root_bfo_row(item):
        return None
    for key in ("id", "bfoId", "fileId"):
        val = item.get(key)
        if val is None:
            continue
        sid = str(val).strip()
        if sid and sid != str(org_id):
            return f"{BO_SITE}/nbo/bfo/{sid}/download?period={period}"
    return None


def _find_pdf_by_detail_in_pool(
    items: list[dict[str, Any]],
    inn: str,
    detail_id: str,
) -> tuple[str, str, str] | None:
    for item in items:
        item_inn = _item_inn(item)
        if inn and item_inn and not inn_equal(item_inn, inn):
            continue
        for node in _iter_nodes(item):
            url = node.get("url")
            if not isinstance(url, str):
                continue
            if "download/bfo/pdf" not in url or "knd=" in url.lower():
                continue
            if (
                f"detailId={detail_id}" not in url
                and f"detail_id={detail_id}" not in url
                and f"/download/bfo/pdf/{detail_id}" not in url
            ):
                continue
            parsed = _parse_pdf_url(url)
            period = parsed.get("period") or str(node.get("period") or node.get("year") or "")
            if parsed.get("detail_id") and (period or parsed.get("pdf_path_id")):
                return url, parsed["detail_id"], period
    return None


def _normalize_audit_url(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"/download/audit/(\d+)", text, re.I)
    if match:
        return f"{BO_SITE}/download/audit/{match.group(1)}"
    if text.isdigit():
        return f"{BO_SITE}/download/audit/{text}"
    return None


def _find_audit_url_in_item(item: dict[str, Any]) -> str | None:
    def walk(node: Any) -> str | None:
        if isinstance(node, str):
            if "/download/audit/" not in node.lower():
                return None
            return _normalize_audit_url(node)
        if isinstance(node, dict):
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return None

    return walk(item)


def _item_has_audit_markers(item: dict[str, Any]) -> bool:
    for node in _iter_nodes(item):
        if node.get("hasAuditReport") or node.get("has_audit_report"):
            return True
        audit = node.get("audit_report") or node.get("auditReport")
        if isinstance(audit, dict):
            return True
    return bool(item.get("hasAuditReport") or item.get("has_audit_report"))


def _audit_detail_id(item: dict[str, Any]) -> str | None:
    for node in _iter_nodes(item):
        audit = node.get("audit_report") or node.get("auditReport")
        if not isinstance(audit, dict):
            continue
        for key in ("file_url", "fileUrl", "url", "downloadUrl", "download_url"):
            furl = audit.get(key)
            if furl:
                normalized = _normalize_audit_url(str(furl))
                if normalized:
                    match = re.search(r"/download/audit/(\d+)", normalized, re.I)
                    if match:
                        return match.group(1)
        for key in ("id", "detailId", "detail_id", "fileId"):
            value = audit.get(key)
            if value:
                sid = str(value).strip()
                if sid.isdigit():
                    return sid
    return None


def _raw_official_pdf_url(node: dict[str, Any]) -> str | None:
    url = node.get("url")
    if isinstance(url, str) and "download/bfo/pdf" in url and "knd=" not in url.lower():
        return url
    return None


def _find_raw_official_pdf(item: dict[str, Any]) -> str | None:
    for node in _iter_nodes(item):
        url = _raw_official_pdf_url(node)
        if url:
            return url
    return None


def _pdf_org_ids_from_pool(
    pool: list[dict[str, Any]],
    period: str,
    *,
    check_inn: str = "",
) -> list[str]:
    found: list[str] = []
    for candidate in pool:
        if check_inn and _item_inn(candidate) and not inn_equal(_item_inn(candidate), check_inn):
            continue
        if _item_period(candidate, pool) != period:
            continue
        for node in _iter_nodes(candidate):
            parsed = _parse_pdf_url(str(node.get("url") or ""))
            pdf_org_id = parsed.get("pdf_org_id")
            if pdf_org_id and pdf_org_id not in found:
                found.append(pdf_org_id)
    return found


def _constructed_official_pdf_url(pdf_org_id: str, period: str, detail_id: str) -> str:
    return f"{BO_SITE}/download/bfo/pdf/{pdf_org_id}?period={period}&detailId={detail_id}"


def _pdf_urls_for_item(
    item: dict[str, Any],
    detail_id: str,
    period: str,
    org_id: str,
    pool: list[dict[str, Any]] | None = None,
    *,
    inn: str = "",
) -> list[str]:
    """Официальный PDF-url из API первым; nbo/bfo — только с корневой строки /bfo."""
    urls: list[str] = []
    check_inn = inn or _item_inn(item)
    search_pool = pool or [item]

    by_detail = _find_pdf_by_detail_in_pool(search_pool, check_inn, detail_id)
    if by_detail:
        urls.append(by_detail[0])

    for candidate in search_pool:
        if check_inn and _item_inn(candidate) and not inn_equal(_item_inn(candidate), check_inn):
            continue
        for node in _iter_nodes(candidate):
            raw = _raw_official_pdf_url(node)
            if not raw or raw in urls:
                continue
            parsed = _parse_pdf_url(raw)
            node_period = parsed.get("period") or _item_period(candidate, search_pool)
            if node_period != period:
                continue
            urls.append(raw)

    official = [u for u in urls if "/download/bfo/pdf/" in u]
    if not official and detail_id and period:
        pdf_org_ids = _pdf_org_ids_from_pool(search_pool, period, check_inn=check_inn)
        if not pdf_org_ids:
            pdf_org_ids = [str(org_id)]
        pdf_org_ids.sort(key=lambda pid: (0 if pid != str(org_id) else 1, pid))
        for pdf_org_id in pdf_org_ids:
            built = _constructed_official_pdf_url(pdf_org_id, period, detail_id)
            if built not in urls:
                urls.insert(0, built)
        official = [u for u in urls if "/download/bfo/pdf/" in u]

    if official:
        official.sort(
            key=lambda u: (
                0 if (_parse_pdf_url(u).get("pdf_org_id") or "") != str(org_id) else 1,
                u,
            )
        )
        rest = [u for u in urls if u not in official]
        urls = official + rest

    for candidate in search_pool:
        if check_inn and _item_inn(candidate) and not inn_equal(_item_inn(candidate), check_inn):
            continue
        if _item_period(candidate, search_pool) != period:
            continue
        nbo = _nbo_bfo_download_url(candidate, period, org_id)
        if nbo and nbo not in urls:
            urls.append(nbo)

    return urls


def _audit_report_has_file(audit: dict[str, Any]) -> bool:
    for key in (
        "file_url",
        "fileUrl",
        "url",
        "downloadUrl",
        "download_url",
        "fileName",
        "file_name",
        "name",
    ):
        if audit.get(key):
            return True
    return False


def _audit_url_from_node(node: dict[str, Any], *, detail_id: str = "") -> str | None:
    audit = node.get("audit_report") or node.get("auditReport")
    if isinstance(audit, dict):
        for key in ("file_url", "fileUrl", "url", "downloadUrl", "download_url"):
            value = audit.get(key)
            if value:
                normalized = _normalize_audit_url(str(value).strip())
                return normalized or str(value).strip()
        for key in ("id", "detailId", "detail_id", "fileId"):
            value = audit.get(key)
            if value:
                sid = str(value).strip()
                if sid.isdigit():
                    return f"{BO_SITE}/download/audit/{sid}"
    for key in ("auditFileUrl", "audit_file_url"):
        value = node.get(key)
        if value:
            normalized = _normalize_audit_url(str(value).strip())
            return normalized or str(value).strip()
    return None


def _audit_url_from_report(report: dict[str, Any], detail_id: str) -> str | None:
    for node in _iter_nodes(report):
        url = _audit_url_from_node(node, detail_id=detail_id)
        if url:
            return url
    audit_id = _audit_detail_id(report)
    if audit_id:
        return f"{BO_SITE}/download/audit/{audit_id}"
    return _find_audit_url_in_item(report)


def _audit_url_from_pool(
    pool: list[dict[str, Any]],
    period: str,
    inn: str,
) -> str | None:
    for item in pool:
        if _item_period(item, pool) != period:
            continue
        if inn and _item_inn(item) and not inn_equal(_item_inn(item), inn):
            continue
        for node in _iter_nodes(item):
            node_detail = str(
                node.get("id") or node.get("detailId") or node.get("detail_id") or ""
            ).strip()
            url = _audit_url_from_node(node, detail_id=node_detail)
            if url:
                return url
    return None


def _card_inn(card: dict[str, Any]) -> str:
    for source in (card, card.get("organization") or {}):
        if isinstance(source, dict) and source.get("inn") not in (None, ""):
            return normalize_inn(source.get("inn"))
    return ""


def _item_inn(item: dict[str, Any]) -> str:
    info = item.get("organization_info") or item.get("organizationInfo") or {}
    return normalize_inn(info.get("inn"))


def _inn_allowed(item: dict[str, Any], inn: str, card_inn: str, *, org_scoped: bool) -> bool:
    item_inn = _item_inn(item)
    if item_inn and inn_equal(item_inn, inn):
        return True
    if org_scoped and inn_equal(card_inn, inn):
        if item_inn and not inn_equal(item_inn, inn):
            logger.warning(
                "ИНН в отчёте (%s) ≠ запрос (%s), доверяем org-scoped /bfo",
                item_inn,
                inn,
            )
        return True
    if item_inn:
        return False
    return org_scoped


def _expand_bfo_items(bfo_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in bfo_items:
        if not isinstance(item, dict):
            continue
        expanded.append(item)
        info = item.get("organizationInfo") or item.get("organization_info")
        for entry in item.get("typeCorrections") or item.get("corrections") or []:
            if not isinstance(entry, dict):
                continue
            corr = entry.get("correction")
            merged = dict(entry)
            if isinstance(corr, dict):
                merged = {**merged, **corr}
            if info and not merged.get("organizationInfo"):
                merged["organizationInfo"] = info
            expanded.append(merged)
    return expanded


def _verify_score(item: dict[str, Any], row: _VerifiedReport, org_id: str) -> int:
    score = 0
    if _find_raw_official_pdf(item) or _find_official_pdf(item):
        score += 30
    if any("/download/bfo/pdf/" in u for u in row.pdf_urls):
        score += 30
    if _nbo_bfo_download_url(item, row.period, org_id):
        score += 5
    return score


def _try_verify_item(
    item: dict[str, Any],
    inn: str,
    card_inn: str,
    org_id: str,
    *,
    org_scoped: bool,
    pool: list[dict[str, Any]] | None = None,
) -> _VerifiedReport | None:
    if not _inn_allowed(item, inn, card_inn, org_scoped=org_scoped):
        return None

    period = _item_period(item, pool)
    detail_id = _detail_id_from_report(item, org_id, pool=pool)
    if not period or not detail_id:
        return None

    correction = _matching_correction(pool or [], detail_id, inn, period) if pool else None
    has_rows = False
    if correction:
        has_rows = bool(
            extract_form_rows(correction, "balance")
            or extract_form_rows(correction, "financial_result", "financialResult")
        )

    pdf_urls = _pdf_urls_for_item(item, detail_id, period, org_id, pool=pool, inn=inn)
    if not pdf_urls and not has_rows:
        return None

    use_inn = _item_inn(item) or card_inn or inn
    return _VerifiedReport(
        period=period,
        detail_id=detail_id,
        pdf_urls=pdf_urls,
        audit_url=_audit_url_from_report(item, detail_id),
        report_inn=use_inn,
        org_name=_report_name(item),
    )


def _verify_reports(card: dict[str, Any], inn: str, org_id: str) -> list[_VerifiedReport]:
    verified: list[_VerifiedReport] = []
    seen: set[str] = set()
    card_inn = _card_inn(card) or inn

    for report in _collect_reports(card):
        row = _try_verify_item(report, inn, card_inn, org_id, org_scoped=True)
        if not row or row.period in seen:
            continue
        seen.add(row.period)
        verified.append(row)

    return sorted(verified, key=lambda r: r.period, reverse=True)


def _verify_bfo(bfo_items: list[dict[str, Any]], inn: str, org_id: str, card_inn: str) -> list[_VerifiedReport]:
    expanded = _expand_bfo_items(bfo_items)
    pool = expanded + [x for x in bfo_items if x not in expanded]
    by_period: dict[str, _VerifiedReport] = {}
    scores: dict[str, int] = {}

    for item in bfo_items + expanded:
        row = _try_verify_item(item, inn, card_inn, org_id, org_scoped=True, pool=pool)
        if not row:
            continue
        score = _verify_score(item, row, org_id)
        if row.period not in by_period or score > scores.get(row.period, -1):
            by_period[row.period] = row
            scores[row.period] = score

    rows = sorted(by_period.values(), key=lambda r: r.period, reverse=True)
    return _finalize_verified_rows(rows, pool, inn, org_id)


def _finalize_verified_rows(
    rows: list[_VerifiedReport],
    pool: list[dict[str, Any]],
    inn: str,
    org_id: str,
) -> list[_VerifiedReport]:
    finalized: list[_VerifiedReport] = []
    for row in rows:
        root = next(
            (
                item
                for item in pool
                if _is_root_bfo_row(item) and _item_period(item, pool) == row.period
            ),
            None,
        )
        base = root or (pool[0] if pool else None)
        if not base:
            finalized.append(row)
            continue
        pdf_urls = _pdf_urls_for_item(
            base,
            row.detail_id,
            row.period,
            org_id,
            pool=pool,
            inn=inn,
        )
        correction = _matching_correction(pool, row.detail_id, inn, row.period)
        pdf_org_id = _resolve_pdf_org_id(pool, row.period, row.detail_id, org_id, inn=inn)
        pdf_org_ids = _collect_pdf_org_ids(pool, row.period, org_id, inn=inn, detail_id=row.detail_id)
        balance_rows: list[dict[str, Any]] = []
        financial_rows: list[dict[str, Any]] = []
        if correction:
            balance_rows = extract_form_rows(correction, "balance")
            financial_rows = extract_form_rows(
                correction,
                "financial_result",
                "financialResult",
            )
        audit_url = row.audit_url
        if not audit_url:
            audit_url = _audit_url_from_pool(pool, row.period, inn)
        if not audit_url and correction:
            audit_url = _audit_url_from_node(correction, detail_id=row.detail_id)
        if not audit_url:
            audit_id = _audit_detail_id(base) if base else None
            if audit_id:
                audit_url = f"{BO_SITE}/download/audit/{audit_id}"
        if not audit_url and row.detail_id:
            marker_item = correction or base
            if marker_item and _item_has_audit_markers(marker_item):
                audit_url = f"{BO_SITE}/download/audit/{row.detail_id}"
        finalized.append(
            _VerifiedReport(
                period=row.period,
                detail_id=row.detail_id,
                pdf_urls=pdf_urls,
                audit_url=audit_url,
                report_inn=row.report_inn,
                org_name=row.org_name,
                pdf_org_id=pdf_org_id,
                pdf_org_ids=pdf_org_ids,
                balance_rows=balance_rows,
                financial_rows=financial_rows,
            )
        )
    return finalized


def _merge_verified(card_rows: list[_VerifiedReport], bfo_rows: list[_VerifiedReport]) -> list[_VerifiedReport]:
    by_period: dict[str, _VerifiedReport] = {}
    for row in bfo_rows:
        by_period[row.period] = row
    for row in card_rows:
        prev = by_period.get(row.period)
        if prev is None or any("/download/bfo/pdf/" in u for u in row.pdf_urls):
            audit_url = row.audit_url or (prev.audit_url if prev else None)
            by_period[row.period] = replace(row, audit_url=audit_url)
        elif prev and not prev.audit_url and row.audit_url:
            by_period[row.period] = replace(prev, audit_url=row.audit_url)
    return sorted(by_period.values(), key=lambda r: r.period, reverse=True)


def _build_documents(report: _VerifiedReport, card_org_id: str, pool: list[dict[str, Any]] | None = None) -> list[BoDocument]:
    pdf_org_ids = report.pdf_org_ids or ([report.pdf_org_id] if report.pdf_org_id else [card_org_id])
    docs: list[BoDocument] = []

    kind = "balance"
    knd = "0710001"
    title = f"Отчётность за {report.period}"
    rows = report.balance_rows
    urls: list[str] = []
    for url in _detail_path_pdf_urls(report.detail_id, knd):
        if url not in urls:
            urls.append(url)
    if pool:
        path_needle = f"/download/bfo/pdf/{report.detail_id}"
        for candidate in pool:
            if report.report_inn and _item_inn(candidate) and not inn_equal(
                _item_inn(candidate), report.report_inn
            ):
                continue
            if _item_period(candidate, pool) != report.period:
                continue
            for node in _iter_nodes(candidate):
                node_url = node.get("url")
                if isinstance(node_url, str) and path_needle in node_url and node_url not in urls:
                    urls.append(node_url)
        for direct in _knd_urls_from_pool(
            pool, report.period, report.detail_id, knd, inn=report.report_inn
        ):
            if direct not in urls:
                urls.append(direct)
    for url in _official_form_pdf_urls(pdf_org_ids, report.period, report.detail_id, knd):
        if url not in urls:
            urls.append(url)
    for extra in report.pdf_urls:
        if "knd=" not in extra and extra not in urls:
            urls.append(extra)
    for url in _official_full_pdf_urls(pdf_org_ids, report.period, report.detail_id):
        if url not in urls:
            urls.append(url)
    for extra in report.pdf_urls:
        if "knd=" in extra and extra not in urls:
            urls.append(extra)
    for url in _official_ms_excel_urls(pdf_org_ids, report.period, report.detail_id):
        if url not in urls:
            urls.append(url)
    for url in _official_ms_excel_urls(pdf_org_ids, report.period, report.detail_id, knd):
        if url not in urls:
            urls.append(url)
    for pdf_org_id in pdf_org_ids:
        if pdf_org_id != card_org_id:
            nbo = f"{BO_SITE}/nbo/bfo/{pdf_org_id}/download?period={report.period}"
            if nbo not in urls:
                urls.append(nbo)
    if urls:
        docs.append(
            BoDocument(
                title=title,
                period=report.period,
                kind=kind,
                format="pdf",
                org_id=card_org_id,
                detail_id=report.detail_id,
                url=urls[0],
                fallback_urls=urls[1:],
                rows=rows,
                expected_inn=report.report_inn,
                expected_org_name=report.org_name,
            )
        )

    audit_url = report.audit_url
    if not audit_url and pool:
        audit_url = _audit_url_from_pool(pool, report.period, report.report_inn or "")
    if audit_url:
        audit_urls: list[str] = []
        for candidate in [audit_url, _normalize_audit_url(audit_url or "")]:
            if candidate and candidate not in audit_urls:
                audit_urls.append(candidate)
        match = re.search(r"/download/audit/(\d+)", audit_url or "", re.I)
        if match:
            audit_id = match.group(1)
            for base in (BO_SITE, BO_API):
                built = f"{base}/download/audit/{audit_id}"
                if built not in audit_urls:
                    audit_urls.append(built)
        if report.detail_id:
            detail_audit = f"{BO_SITE}/download/audit/{report.detail_id}"
            if detail_audit not in audit_urls:
                audit_urls.append(detail_audit)
        docs.append(
            BoDocument(
                title=f"Аудиторское заключение за {report.period}",
                period=report.period,
                kind="audit",
                format="pdf",
                org_id=card_org_id,
                detail_id=report.detail_id,
                url=audit_urls[0],
                fallback_urls=audit_urls[1:],
                expected_inn=report.report_inn,
                expected_org_name=report.org_name,
            )
        )
    return docs


async def _search_org(session: aiohttp.ClientSession, inn: str) -> tuple[dict[str, Any], str]:
    for base in API_BASES:
        try:
            data = await _api_get_on_base(
                session,
                base,
                "/advanced-search/organizations/search",
                {"query": inn, "page": "0"},
            )
        except (GirBoError, aiohttp.ClientError):
            continue
        for row in data.get("content") or data.get("rows") or []:
            if inn_equal(row.get("inn"), inn):
                return row, base
    raise GirBoError(f"Организация с ИНН {inn} не найдена в ГИР БО.")


async def lookup_girbo_org_id(inn: str) -> str | None:
    """ID организации в ГИР БО только по поиску (без отчётности)."""
    inn = normalize_inn(inn.strip())
    if not inn.isdigit() or len(inn) not in (10, 12):
        return None
    async with aiohttp.ClientSession(
        timeout=TIMEOUT, cookie_jar=aiohttp.CookieJar(unsafe=True), connector=ipv4_connector()
    ) as session:
        await _warm(session)
        try:
            search_row, _ = await _search_org(session, inn)
        except GirBoError:
            return None
        org_id = str(search_row.get("id") or "").strip()
        return org_id or None


async def get_organization(inn: str) -> BoOrganization:
    inn = normalize_inn(inn.strip())
    if not inn.isdigit() or len(inn) not in (10, 12):
        raise GirBoError("Нужен корректный ИНН (10 или 12 цифр).")

    key = f"{CACHE_VERSION}:{inn}"
    cached = _org_cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SEC:
        return cached[1]

    async with aiohttp.ClientSession(
        timeout=TIMEOUT, cookie_jar=aiohttp.CookieJar(unsafe=True), connector=ipv4_connector()
    ) as session:
        await _warm(session)
        search_row, search_base = await _search_org(session, inn)
        org_id = str(search_row.get("id") or "")
        if not org_id:
            raise GirBoError("Не удалось определить ID организации.")

        card = await _api_get_on_base(session, search_base, f"/nbo/organizations/{org_id}")
        if not isinstance(card, dict):
            raise GirBoError("Пустая карточка организации.")

        period_card: dict[str, Any] | None = None
        try:
            raw_period = await _api_get_on_base(
                session,
                search_base,
                f"/nbo/organizations/{org_id}?period={GIRBO_OKVED_PERIOD}",
            )
            if isinstance(raw_period, dict):
                period_card = raw_period
        except GirBoError:
            period_card = None

        org_okved = _resolve_org_okved(card, search_row, period_card, GIRBO_OKVED_PERIOD)
        org_okved = await _enrich_org_okved_from_card_page(
            session, org_id, GIRBO_OKVED_PERIOD, org_okved
        )

        card_inn = _card_inn(card) or inn
        card_rows = _verify_reports(card, inn, org_id)

        bfo_items: list[dict[str, Any]] = []
        try:
            raw_bfo = await _api_get_on_base(session, search_base, f"/nbo/organizations/{org_id}/bfo")
            if isinstance(raw_bfo, list):
                bfo_items = raw_bfo
            elif isinstance(raw_bfo, dict):
                bfo_items = raw_bfo.get("reports") or raw_bfo.get("content") or []
        except GirBoError:
            bfo_items = []

        bfo_rows = _verify_bfo(bfo_items, inn, org_id, card_inn) if bfo_items else []
        verified = _merge_verified(card_rows, bfo_rows)

        logger.info(
            "ГИР БО resolve inn=%s org=%s card_reports=%s bfo_items=%s card_ok=%s bfo_ok=%s",
            inn,
            org_id,
            len(_collect_reports(card)),
            len(bfo_items),
            len(card_rows),
            len(bfo_rows),
        )

        if not verified:
            display = card if card.get("fullName") or card.get("full_name") else search_row
            org = BoOrganization(
                org_id=org_id,
                inn=inn,
                name=_pick_name(display),
                short_name=_pick_short_name(display) or _pick_short_name(search_row),
                ogrn=str(search_row.get("ogrn") or card.get("ogrn") or ""),
                card_url=f"{BO_SITE}/organizations-card/{org_id}",
                okved=org_okved,
                region=_region_from_org(card) or _region_from_org(search_row),
                city=_city_from_org(card) or _city_from_org(search_row),
                documents=[],
                no_public_reporting=True,
            )
            logger.info(
                "ГИР БО inn=%s org=%s — отчётность в открытом доступе не найдена, карточка на %s",
                inn,
                org_id,
                BFO_CLOSED_SITE,
            )
            _org_cache[key] = (time.monotonic(), org)
            return org

        latest = verified[0]
        expanded = _expand_bfo_items(bfo_items) if bfo_items else []
        pool = expanded + [x for x in bfo_items if x not in expanded] if bfo_items else []
        documents = _build_documents(latest, org_id, pool=pool or None)
        audit_doc_url = next((d.url for d in documents if d.kind == "audit"), None)
        logger.info(
            "ГИР БО inn=%s org=%s period=%s detail_id=%s pdf_org_ids=%s balance_url=%s audit_url=%s",
            inn,
            org_id,
            latest.period,
            latest.detail_id,
            latest.pdf_org_ids,
            next((d.url for d in documents if d.kind == "balance"), None),
            audit_doc_url or "none",
        )

        display = card if card.get("fullName") or card.get("full_name") else search_row
        stale_open = is_stale_open_reporting(latest.period)

        org = BoOrganization(
            org_id=org_id,
            inn=inn,
            name=latest.org_name or _pick_name(display),
            short_name=_pick_short_name(display) or _pick_short_name(search_row),
            ogrn=str(search_row.get("ogrn") or card.get("ogrn") or ""),
            card_url=f"{BO_SITE}/organizations-card/{org_id}",
            okved=org_okved,
            region=_region_from_org(card) or _region_from_org(search_row),
            city=_city_from_org(card) or _city_from_org(search_row),
            documents=[] if stale_open else documents,
            latest_balance_rows=[] if stale_open else latest.balance_rows,
            latest_financial_rows=[] if stale_open else latest.financial_rows,
            latest_period=latest.period,
            stale_open_reporting=stale_open,
        )
        if stale_open:
            logger.info(
                "ГИР БО inn=%s org=%s — устаревшая отчётность в открытом доступе period=%s, нужен %s",
                inn,
                org_id,
                latest.period,
                BFO_CLOSED_SITE,
            )
        _org_cache[key] = (time.monotonic(), org)
        return org


async def _download_pdf(
    session: aiohttp.ClientSession,
    urls: list[str],
    *,
    referer: str,
    expected_inn: str | None = None,
    expected_org_name: str | None = None,
    expected_rows: list[dict[str, Any]] | None = None,
    allow_xlsx: bool = False,
) -> tuple[bytes, str]:
    last_status: int | str = "?"
    last_url = ""
    for url in urls:
        headers = {**DOWNLOAD_HEADERS, "Referer": referer}
        attempts = [(url, headers)]
        if BO_SITE in url:
            attempts.append((url.replace(BO_SITE, BO_API, 1), {**headers, "Host": "bo.nalog.gov.ru"}))
        for try_url, h in attempts:
            last_url = try_url
            async with session.get(try_url, headers=h, allow_redirects=True, timeout=TIMEOUT) as resp:
                last_status = resp.status
                if resp.status != 200:
                    logger.info("download %s -> %s", try_url[:100], resp.status)
                    continue
                data = await resp.read()
                content_type = resp.headers.get("Content-Type", "").lower()
                if data[:5] == b"%PDF-" or "pdf" in content_type:
                    if expected_inn and not _pdf_matches_report(
                        data,
                        expected_inn,
                        expected_org_name,
                        rows=expected_rows,
                    ):
                        logger.info("download wrong org, skip %s", try_url[:100])
                        continue
                    logger.info("download OK %s (%s bytes)", try_url[:100], len(data))
                    return data, "pdf"
                if allow_xlsx and (
                    data[:2] == b"PK"
                    or "excel" in content_type
                    or "spreadsheet" in content_type
                    or "/ms-excel/" in try_url
                ):
                    logger.info("download OK xlsx %s (%s bytes)", try_url[:100], len(data))
                    return data, "xlsx"
    raise GirBoError(
        f"Не удалось скачать официальную форму PDF с ИНН {expected_inn or '—'} "
        f"(HTTP {last_status}): {last_url[:150]}"
    )


async def download_url(
    url: str,
    *,
    fallback_urls: list[str] | None = None,
    referer: str | None = None,
    org_id: str | None = None,
    expected_inn: str | None = None,
    expected_org_name: str | None = None,
    expected_rows: list[dict[str, Any]] | None = None,
    allow_xlsx: bool = False,
) -> tuple[bytes, str]:
    urls: list[str] = []
    for u in [url] + [x for x in (fallback_urls or []) if x != url]:
        if u not in urls:
            urls.append(u)

    async with aiohttp.ClientSession(
        timeout=TIMEOUT, cookie_jar=aiohttp.CookieJar(unsafe=True), connector=ipv4_connector()
    ) as session:
        await _warm(session)
        if org_id:
            await session.get(f"{BO_SITE}/organizations-card/{org_id}", headers=DOWNLOAD_HEADERS)
        return await _download_pdf(
            session,
            urls,
            referer=referer or f"{BO_SITE}/",
            expected_inn=expected_inn,
            expected_org_name=expected_org_name,
            expected_rows=expected_rows,
            allow_xlsx=allow_xlsx,
        )


async def download_document(doc: BoDocument) -> tuple[bytes, str, str]:
    if not doc.url:
        raise GirBoError(f"Нет ссылки: {doc.title}")

    cache_key = f"{CACHE_VERSION}:{doc.url}"
    cached = _PDF_BYTES_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _PDF_BYTES_CACHE_TTL_SEC:
        return cached[1], cached[2], cached[3]

    referer = f"{BO_SITE}/organizations-card/{doc.org_id}" if doc.org_id else f"{BO_SITE}/"
    try:
        data, fmt = await download_url(
            doc.url,
            fallback_urls=doc.fallback_urls,
            referer=referer,
            org_id=doc.org_id,
            expected_inn=doc.expected_inn if doc.kind != "audit" else None,
            expected_org_name=doc.expected_org_name if doc.kind != "audit" else None,
            expected_rows=doc.rows if doc.kind != "audit" else None,
            allow_xlsx=doc.kind in {"balance", "financial"},
        )
    except GirBoError as exc:
        if doc.kind in {"balance", "financial"} and doc.rows:
            logger.warning(
                "ГИР БО: официальный PDF недоступен для inn=%s kind=%s, отдаём Excel из JSON API: %s",
                doc.expected_inn,
                doc.kind,
                exc,
            )
            sheet = "Баланс" if doc.kind == "balance" else "Финрезультат"
            data = form_rows_to_xlsx(
                doc.rows,
                form_title=doc.title,
                period=doc.period,
                sheet_name=sheet,
            )
            fmt = "xlsx"
        else:
            raise
    kind = {"balance": "balance", "financial": "financial", "audit": "audit"}.get(doc.kind, doc.kind)
    ext = "xlsx" if fmt == "xlsx" else "pdf"
    media = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if fmt == "xlsx"
        else "application/pdf"
    )
    filename = f"girbo_{kind}_{doc.period}.{ext}"
    _PDF_BYTES_CACHE[cache_key] = (time.monotonic(), data, filename, media)
    return data, filename, media


@dataclass
class BoReport:
    inn: str
    period: str
    title: str
    download_url: str


async def list_reports(inn: str) -> list[BoReport]:
    org = await get_organization(inn)
    return [
        BoReport(inn=inn, period=d.period, title=d.title, download_url=d.url or "")
        for d in org.documents
        if d.kind in ("balance", "financial")
    ]


async def download_report(report: BoReport) -> bytes:
    data, _ = await download_url(report.download_url)
    return data
