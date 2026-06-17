from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlencode

import aiohttp

from services.inn_utils import inn_equal, normalize_inn
from services.net import ipv4_connector

logger = logging.getLogger("crnabot.fedresurs")

FEDRESURS_BASE = "https://fedresurs.ru"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

TIMEOUT = aiohttp.ClientTimeout(total=90)

AUDIT_PUBLICATION_TYPES = {"AmReport", "AuditReport", "SroAmMessage", "SfactMessage"}

BACKEND_PATH_BY_TYPE = {
    "AmReport": "am-reports",
    "AuditReport": "am-reports",
    "SroAmMessage": "sro-am-messages",
    "SfactMessage": "sfact-messages",
    "BankruptcyMessage": "bankruptcy-messages",
    "FirmBankruptMessage": "firm-bankrupt-messages",
    "TradeOrgMessage": "trade-org-messages",
}

FRONTEND_PATH_BY_TYPE = {
    "AmReport": "amreports",
    "AuditReport": "amreports",
    "SroAmMessage": "sroammessages",
    "SfactMessage": "sfactmessages",
    "BankruptcyMessage": "bankruptmessages",
    "FirmBankruptMessage": "firmbankruptmessages",
    "TradeOrgMessage": "tradeorgmessages",
}


class FedresursError(Exception):
    pass


@dataclass
class FedresursCompany:
    guid: str
    inn: str
    name: str
    ogrn: str | None
    card_url: str


@dataclass
class FedresursAuditPublication:
    guid: str
    publication_type: str
    title: str
    type_label: str
    date_publish: str
    period: str
    publication_url: str
    number: str | None = None
    delivery: str = "file"


@dataclass
class FedresursAuditDelivery:
    publication: FedresursAuditPublication
    mode: str
    file_data: bytes | None = None
    file_media_type: str | None = None
    source_text: str | None = None


def _json_headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": referer,
    }


def _publication_filters(*, audit_only: bool) -> dict[str, str]:
    if audit_only:
        return {
            "searchAmReport": "true",
            "searchSfactsMessage": "false",
            "searchCompanyEfrsb": "false",
            "searchFirmBankruptMessage": "false",
            "searchFirmBankruptMessageWithoutLegalCase": "false",
            "searchSroAmMessage": "false",
            "searchTradeOrgMessage": "false",
        }
    return {
        "searchAmReport": "true",
        "searchSfactsMessage": "true",
        "searchCompanyEfrsb": "true",
        "searchFirmBankruptMessage": "true",
        "searchFirmBankruptMessageWithoutLegalCase": "false",
        "searchSroAmMessage": "true",
        "searchTradeOrgMessage": "true",
    }


def _year_bounds(year: str) -> tuple[str, str]:
    start = f"{year}-01-01T00:00:00.000Z"
    end = f"{int(year) + 1}-01-01T00:00:00.000Z"
    return start, end


def _publication_url(pub_type: str, guid: str) -> str:
    segment = FRONTEND_PATH_BY_TYPE.get(pub_type, "publications")
    return f"{FEDRESURS_BASE}/{segment}/{guid}"


def _audit_text(pub: dict[str, Any]) -> str:
    return " ".join(
        str(pub.get(key) or "")
        for key in ("type", "title", "publicationType", "number")
    ).strip()


def _publication_full_text(pub: dict[str, Any]) -> str:
    return _audit_text(pub)


def _detail_text(detail: dict[str, Any]) -> str:
    try:
        import json

        return json.dumps(detail, ensure_ascii=False)
    except Exception:
        return str(detail)


def _in_calendar_year(pub: dict[str, Any], calendar_year: str) -> bool:
    date_publish = str(pub.get("datePublish") or "")
    return date_publish.startswith(str(calendar_year).strip())


def _mentions_report_period(pub: dict[str, Any], report_year: str) -> bool:
    report_year = str(report_year).strip()
    if not report_year.isdigit():
        return True
    text = _publication_full_text(pub)
    if re.search(rf"\b{re.escape(report_year)}\b", text):
        return True
    date_publish = str(pub.get("datePublish") or "")
    return report_year in date_publish


_ACCOUNTING_REPORT_TITLE_RE = re.compile(
    r"сведения\s+о\s+бухгалтерской\s*[\(（]?\s*финансовой\s*[\)）]?\s*отч[её]тности",
    re.IGNORECASE,
)


def _is_accounting_report_publication(pub: dict[str, Any]) -> bool:
    return bool(_ACCOUNTING_REPORT_TITLE_RE.search(_publication_full_text(pub)))


def _mandatory_audit_score(pub: dict[str, Any]) -> int:
    text = _publication_full_text(pub).lower()
    score = 0
    if "результат" in text and "обязательн" in text and "аудит" in text:
        score += 120
    pub_type = str(pub.get("publicationType") or "")
    if pub_type in AUDIT_PUBLICATION_TYPES:
        score += 50
    if re.search(r"аудитор", text, re.IGNORECASE):
        score += 15
    return score


def _is_net_assets_publication(pub: dict[str, Any]) -> bool:
    text = _publication_full_text(pub).lower()
    return "стоимость" in text and "чист" in text and "актив" in text


def _is_mandatory_audit_candidate(pub: dict[str, Any]) -> bool:
    if _is_accounting_report_publication(pub):
        return False
    if _is_net_assets_publication(pub):
        return False
    return _mandatory_audit_score(pub) >= 50


def _in_report_year(pub: dict[str, Any], year: str) -> bool:
    date_publish = str(pub.get("datePublish") or "")
    if date_publish.startswith(year):
        return True
    text = _audit_text(pub)
    return bool(re.search(rf"\b{re.escape(year)}\b", text))


def _is_audit_publication(pub: dict[str, Any], year: str) -> bool:
    return _is_mandatory_audit_candidate(pub) and _in_calendar_year(pub, year)


def _pick_company(rows: list[dict[str, Any]], inn: str) -> dict[str, Any] | None:
    for row in rows:
        row_inn = normalize_inn(row.get("inn"))
        if row_inn and inn_equal(row_inn, inn):
            return row
    return None


def _walk_file_candidates(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_lower = str(key).lower()
            if isinstance(value, bool) and value and key_lower in {
                "hasfile",
                "hasattachedfile",
                "isfileattached",
                "fileattached",
                "hasfiles",
                "isfilesattached",
            }:
                out.append(f"bool:{key}")
            if isinstance(value, str):
                if key_lower in {
                    "url",
                    "fileurl",
                    "downloadurl",
                    "href",
                    "link",
                    "path",
                    "filename",
                    "name",
                } and (
                    value.startswith("http")
                    or value.startswith("/backend/")
                    or value.lower().endswith(".pdf")
                    or "/file" in value.lower()
                ):
                    out.append(value)
            elif key_lower in {"files", "attachments", "documents", "filelist"} and isinstance(value, list):
                for item in value:
                    _walk_file_candidates(item, out)
            else:
                _walk_file_candidates(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_file_candidates(item, out)


_OPINION_KEY_RE = re.compile(
    r"opinion|conclus|мнен|extract|auditor.*text|audit.*result|auditorreport",
    re.IGNORECASE,
)


def _collect_opinion_texts(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_text = str(key)
            if _OPINION_KEY_RE.search(key_text):
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
                elif isinstance(value, (int, float)) and str(value).strip():
                    out.append(str(value).strip())
            _collect_opinion_texts(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_opinion_texts(item, out)


def publication_has_documents(detail: dict[str, Any]) -> bool:
    docs = detail.get("docs")
    if isinstance(docs, list) and any(isinstance(item, dict) for item in docs):
        return True
    content = detail.get("content")
    if isinstance(content, dict):
        message_docs = content.get("messageDocList")
        if isinstance(message_docs, list) and message_docs:
            return True
    return False


def _extract_document_download_urls(detail: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        normalized = _normalize_download_url(path)
        if normalized not in seen:
            seen.add(normalized)
            urls.append(path)

    docs = detail.get("docs")
    if isinstance(docs, list):
        for item in docs:
            if not isinstance(item, dict):
                continue
            doc_guid = str(item.get("guid") or "").strip()
            if doc_guid:
                add(f"/backend/sfact-message-docs/{doc_guid}")
                add(f"/backend/files/{doc_guid}")

    content = detail.get("content")
    if isinstance(content, dict):
        for item in content.get("messageDocList") or []:
            if not isinstance(item, dict):
                continue
            doc_guid = str(item.get("guid") or "").strip()
            if doc_guid:
                add(f"/backend/sfact-message-docs/{doc_guid}")

    return urls


def _normalize_download_url(url: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return f"{FEDRESURS_BASE}{url}"
    return f"{FEDRESURS_BASE}/{url.lstrip('/')}"


def _publication_to_audit(pub: dict[str, Any], year: str) -> FedresursAuditPublication:
    pub_type = str(pub.get("publicationType") or "AmReport")
    guid = str(pub.get("guid") or "")
    title = str(pub.get("title") or pub.get("type") or "Аудиторское заключение").strip()
    type_label = str(pub.get("type") or title).strip()
    return FedresursAuditPublication(
        guid=guid,
        publication_type=pub_type,
        title=title,
        type_label=type_label,
        date_publish=str(pub.get("datePublish") or ""),
        period=year,
        publication_url=_publication_url(pub_type, guid),
        number=str(pub.get("number") or "") or None,
    )


async def _fetch_json(
    session: aiohttp.ClientSession,
    path: str,
    *,
    referer: str,
) -> Any:
    url = path if path.startswith("http") else f"{FEDRESURS_BASE}{path}"
    async with session.get(url, headers=_json_headers(referer), allow_redirects=True) as resp:
        text = await resp.text()
        if resp.status == 451:
            raise FedresursError("Федресурс временно ограничил доступ (451). Повторите позже.")
        if resp.status >= 400:
            raise FedresursError(f"Федресурс: HTTP {resp.status} для {path}")
        try:
            return await resp.json(content_type=None)
        except Exception as exc:
            raise FedresursError(f"Федресурс: не удалось разобрать ответ ({exc})") from exc


async def lookup_company(session: aiohttp.ClientSession, inn: str) -> FedresursCompany | None:
    inn = normalize_inn(inn)
    referer = f"{FEDRESURS_BASE}/entities?searchString={inn}"
    queries = [
        f"/backend/companies?limit=15&offset=0&searchString={inn}",
        f"/backend/companies?limit=15&offset=0&code={inn}",
    ]
    for query in queries:
        try:
            payload = await _fetch_json(session, query, referer=referer)
        except FedresursError as exc:
            logger.info("Fedresurs company search failed inn=%s query=%s: %s", inn, query, exc)
            continue
        rows = payload.get("pageData") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            continue
        row = _pick_company([x for x in rows if isinstance(x, dict)], inn)
        if not row:
            continue
        guid = str(row.get("guid") or "").strip()
        if not guid:
            continue
        return FedresursCompany(
            guid=guid,
            inn=normalize_inn(row.get("inn")) or inn,
            name=str(row.get("name") or row.get("fullName") or "").strip(),
            ogrn=str(row.get("ogrn") or "").strip() or None,
            card_url=f"{FEDRESURS_BASE}/companies/{guid}",
        )
    return None


async def _fetch_publications(
    session: aiohttp.ClientSession,
    company_guid: str,
    *,
    year: str,
    audit_only: bool,
) -> list[dict[str, Any]]:
    referer = f"{FEDRESURS_BASE}/companies/{company_guid}"
    start_date, end_date = _year_bounds(year)
    filters = _publication_filters(audit_only=audit_only)
    publications: list[dict[str, Any]] = []

    for offset in range(0, 500, 50):
        params = {
            "limit": "50",
            "offset": str(offset),
            "startDate": start_date,
            "endDate": end_date,
            **filters,
        }
        query = f"/backend/companies/{company_guid}/publications?{urlencode(params)}"
        payload = await _fetch_json(session, query, referer=referer)
        page = payload.get("pageData") if isinstance(payload, dict) else None
        if not isinstance(page, list) or not page:
            break
        publications.extend(x for x in page if isinstance(x, dict))
        if len(page) < 50:
            break
    return publications


async def find_audit_publication(
    session: aiohttp.ClientSession,
    company_guid: str,
    report_year: str,
    *,
    publication_year: str | None = None,
) -> FedresursAuditPublication | None:
    report_year = str(report_year).strip()
    if not report_year.isdigit():
        raise FedresursError("Некорректный отчётный год.")
    calendar_year = str(publication_year or current_calendar_year()).strip()

    candidates: list[dict[str, Any]] = []
    for audit_only in (True, False):
        try:
            publications = await _fetch_publications(
                session,
                company_guid,
                year=calendar_year,
                audit_only=audit_only,
            )
        except FedresursError as exc:
            logger.info(
                "Fedresurs publications failed guid=%s calendar_year=%s audit_only=%s: %s",
                company_guid,
                calendar_year,
                audit_only,
                exc,
            )
            continue
        for pub in publications:
            if not _is_mandatory_audit_candidate(pub):
                continue
            if not _in_calendar_year(pub, calendar_year):
                continue
            candidates.append(pub)

    if not candidates:
        return None

    period_matches = [pub for pub in candidates if _mentions_report_period(pub, report_year)]
    if not period_matches:
        logger.info(
            "Fedresurs audit not found for report_year=%s guid=%s (candidates=%s)",
            report_year,
            company_guid,
            len(candidates),
        )
        return None
    pool = period_matches
    pool.sort(
        key=lambda pub: (
            1 if _mentions_report_period(pub, report_year) else 0,
            _mandatory_audit_score(pub),
            str(pub.get("datePublish") or ""),
        ),
        reverse=True,
    )
    chosen = pool[0]
    logger.info(
        "Fedresurs audit pick guid=%s calendar_year=%s report_year=%s title=%s date=%s",
        company_guid,
        calendar_year,
        report_year,
        str(chosen.get("title") or chosen.get("type") or "")[:120],
        chosen.get("datePublish"),
    )
    return _publication_to_audit(chosen, report_year)


async def _fetch_publication_detail(
    session: aiohttp.ClientSession,
    publication: FedresursAuditPublication,
) -> dict[str, Any]:
    pub_type = publication.publication_type
    guid = publication.guid
    referer = publication.publication_url
    candidates = []
    mapped = BACKEND_PATH_BY_TYPE.get(pub_type)
    if mapped:
        candidates.append(f"/backend/{mapped}/{guid}")
    candidates.extend(
        [
            f"/backend/am-reports/{guid}",
            f"/backend/amreports/{guid}",
            f"/backend/sfact-messages/{guid}",
            f"/backend/sro-am-messages/{guid}",
        ]
    )

    last_error: FedresursError | None = None
    for path in candidates:
        try:
            payload = await _fetch_json(session, path, referer=referer)
        except FedresursError as exc:
            last_error = exc
            continue
        if isinstance(payload, dict):
            return payload
    if last_error:
        raise last_error
    raise FedresursError("Не удалось получить сведения о публикации на Федресурсе.")


async def _download_binary(
    session: aiohttp.ClientSession,
    url: str,
    *,
    referer: str,
) -> tuple[bytes, str]:
    normalized = _normalize_download_url(url)
    headers = _json_headers(referer)
    headers["Accept"] = "*/*"
    async with session.get(normalized, headers=headers, allow_redirects=True) as resp:
        data = await resp.read()
        if resp.status >= 400:
            raise FedresursError(f"Федресурс: HTTP {resp.status} при скачивании файла")
        media_type = resp.headers.get("Content-Type", "application/octet-stream")
        return data, media_type


def _collect_json_strings(node: Any, out: list[str], *, min_len: int = 12) -> None:
    if isinstance(node, str):
        text = re.sub(r"\s+", " ", node).strip()
        if len(text) >= min_len:
            out.append(text)
        return
    if isinstance(node, dict):
        for value in node.values():
            _collect_json_strings(value, out, min_len=min_len)
        return
    if isinstance(node, list):
        for item in node:
            _collect_json_strings(item, out, min_len=min_len)


def extract_audit_text_from_detail(detail: dict[str, Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = re.sub(r"\s+", " ", value).strip()
        if len(text) < 12:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        parts.append(text)

    json_strings: list[str] = []
    _collect_json_strings(detail, json_strings)
    for text in json_strings:
        add(text)

    return "\n".join(parts)


async def fetch_audit_text_from_delivery(delivery: FedresursAuditDelivery) -> str | None:
    chunks: list[str] = []
    seen: set[str] = set()

    def add(text: str | None) -> None:
        if not text:
            return
        cleaned = re.sub(r"\s+", " ", text).strip()
        if len(cleaned) < 20:
            return
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        chunks.append(cleaned)

    add(delivery.source_text)
    if delivery.file_data and delivery.file_data[:4] == b"%PDF":
        from services.audit_analysis import audit_text_from_pdf

        add(audit_text_from_pdf(delivery.file_data, max_pages=None))

    connector = ipv4_connector(ssl=False)
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=jar,
        timeout=TIMEOUT,
        trust_env=False,
    ) as session:
        detail = await _fetch_publication_detail(session, delivery.publication)
        add(extract_audit_text_from_detail(detail))

    if not chunks:
        return None
    return "\n".join(chunks)


async def download_audit_file(
    session: aiohttp.ClientSession,
    publication: FedresursAuditPublication,
) -> tuple[bytes, str]:
    detail = await _fetch_publication_detail(session, publication)
    candidates: list[str] = []
    candidates.extend(_extract_document_download_urls(detail))
    _walk_file_candidates(detail, candidates)

    pub_type = publication.publication_type
    guid = publication.guid
    mapped = BACKEND_PATH_BY_TYPE.get(pub_type, "am-reports")
    candidates.extend(
        [
            f"/backend/{mapped}/{guid}/file",
            f"/backend/{mapped}/{guid}/files/archive",
            f"/backend/{mapped}/{guid}/download",
            f"/backend/files/{guid}",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_download_url(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            data, media_type = await _download_binary(
                session,
                normalized,
                referer=publication.publication_url,
            )
        except FedresursError:
            continue
        if not data:
            continue
        if data[:4] == b"%PDF" or "pdf" in media_type.lower():
            return data, "application/pdf"
        if data[:2] == b"PK":
            return data, media_type

    raise FedresursError(
        "Аудиторское заключение найдено на Федресурсе, но файл для скачивания недоступен."
    )


def publication_has_attachment(detail: dict[str, Any]) -> bool:
    if publication_has_documents(detail):
        return True
    candidates: list[str] = []
    _walk_file_candidates(detail, candidates)
    for candidate in candidates:
        lower = candidate.lower()
        if lower.startswith("bool:"):
            return True
        if (
            lower.endswith(".pdf")
            or "/file" in lower
            or "/download" in lower
            or "/files/" in lower
            or "/archive" in lower
        ):
            return True
    return False


def publication_has_auditor_opinion(detail: dict[str, Any]) -> bool:
    content = detail.get("content")
    if isinstance(content, dict):
        statement = str(content.get("auditorsStatement") or "").strip()
        if statement:
            return True
    opinion_texts: list[str] = []
    _collect_opinion_texts(detail, opinion_texts)
    if opinion_texts:
        return True
    text = _detail_text(detail).lower()
    return any(
        marker in text
        for marker in (
            "мнение аудитора",
            "auditorsstatement",
            "выдержка словами из аудиторского заключения",
            "выдержка словами из аудиторского",
            "auditoropinion",
            "auditorconclusion",
            "opinionofauditor",
        )
    )


async def try_download_audit_file(
    session: aiohttp.ClientSession,
    publication: FedresursAuditPublication,
) -> tuple[bytes, str] | None:
    try:
        return await download_audit_file(session, publication)
    except FedresursError as exc:
        logger.info("Fedresurs audit file unavailable guid=%s: %s", publication.guid, exc)
        return None


async def resolve_fedresurs_audit_delivery(
    session: aiohttp.ClientSession,
    company: FedresursCompany,
    report_year: str,
) -> FedresursAuditDelivery | None:
    publication = await find_audit_publication(
        session,
        company.guid,
        report_year,
        publication_year=current_calendar_year(),
    )
    if not publication:
        return None

    detail = await _fetch_publication_detail(session, publication)
    has_docs = publication_has_documents(detail)
    has_opinion = publication_has_auditor_opinion(detail)

    downloaded = await try_download_audit_file(session, publication)
    if downloaded:
        publication.delivery = "file"
        data, media_type = downloaded
        logger.info(
            "Fedresurs audit file OK inn=%s guid=%s period=%s size=%s",
            company.inn,
            publication.guid,
            publication.period,
            len(data),
        )
        return FedresursAuditDelivery(
            publication=publication,
            mode="file",
            file_data=data,
            file_media_type=media_type,
        )

    if has_docs:
        logger.info(
            "Fedresurs audit docs present but download failed inn=%s guid=%s",
            company.inn,
            publication.guid,
        )
        return None

    if has_opinion:
        publication.delivery = "screenshot"
        source_text = extract_audit_text_from_detail(detail)
        logger.info(
            "Fedresurs audit screenshot mode inn=%s guid=%s period=%s has_opinion=%s text_len=%s",
            company.inn,
            publication.guid,
            publication.period,
            has_opinion,
            len(source_text or ""),
        )
        return FedresursAuditDelivery(
            publication=publication,
            mode="screenshot",
            source_text=source_text or None,
        )

    logger.info(
        "Fedresurs audit publication found but no file/opinion inn=%s guid=%s keys=%s",
        company.inn,
        publication.guid,
        list(detail.keys())[:20],
    )
    return None


def default_report_year() -> str:
    return str(date.today().year - 1)


def current_calendar_year() -> str:
    return str(date.today().year)


def publications_card_url(company_guid: str) -> str:
    guid = (company_guid or "").strip()
    return f"{FEDRESURS_BASE}/companies/{guid}/publications"


def filter_publications_for_report_year(
    publications: list[dict[str, Any]],
    year: str,
) -> list[dict[str, Any]]:
    """Оставляет публикации отчётного года; если ничего не совпало — исходный список."""
    year = str(year).strip()
    if not year.isdigit() or not publications:
        return publications
    filtered = [pub for pub in publications if _in_report_year(pub, year)]
    return filtered if filtered else publications


async def lookup_company_publications(
    inn: str,
    report_year: str | None = None,
    *,
    audit_only: bool = False,
) -> tuple[FedresursCompany | None, list[dict[str, Any]]]:
    year = str(report_year or default_report_year())
    connector = ipv4_connector(ssl=False)
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=jar,
        timeout=TIMEOUT,
        trust_env=False,
    ) as session:
        company = await lookup_company(session, inn)
        if not company:
            return None, []
        try:
            publications = await _fetch_publications(
                session,
                company.guid,
                year=year,
                audit_only=audit_only,
            )
        except FedresursError as exc:
            logger.info(
                "Fedresurs publications list failed inn=%s year=%s: %s",
                inn,
                year,
                exc,
            )
            return company, []
        return company, publications


async def lookup_fedresurs_audit(
    inn: str,
    report_year: str | None = None,
) -> tuple[FedresursCompany | None, FedresursAuditPublication | None]:
    year = str(report_year or default_report_year())
    connector = ipv4_connector(ssl=False)
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=jar,
        timeout=TIMEOUT,
        trust_env=False,
    ) as session:
        company = await lookup_company(session, inn)
        if not company:
            return None, None
        delivery = await resolve_fedresurs_audit_delivery(session, company, year)
        if delivery:
            return company, delivery.publication
        return company, None


async def lookup_fedresurs_audit_delivery(
    inn: str,
    report_year: str | None = None,
) -> tuple[FedresursCompany | None, FedresursAuditDelivery | None]:
    year = str(report_year or default_report_year())
    connector = ipv4_connector(ssl=False)
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=jar,
        timeout=TIMEOUT,
        trust_env=False,
    ) as session:
        company = await lookup_company(session, inn)
        if not company:
            return None, None
        delivery = await resolve_fedresurs_audit_delivery(session, company, year)
        return company, delivery
