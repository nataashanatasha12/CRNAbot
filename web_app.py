from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import zipfile
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from services.description import (
    build_company_about,
    build_company_description,
    extract_city,
    extract_region,
    shorten_company_name,
)
from services.egrul import EgrulError, download_egrul_pdf, search_egrul
from services.egrul_parse import parse_egrul_pdf
from services.bankruptcy import BANKRUPTCY_URL, STATUS_UNKNOWN, lookup_bankruptcy
from services.audit_analysis import (
    AUDIT_ANALYSIS_NOT_DONE,
    analyze_audit_text,
    audit_opinion_text_from_pdf,
    detect_opinion_in_text,
    opinion_marker_hits,
)
from services.fedresurs import (
    FedresursError,
    default_report_year,
    current_calendar_year,
    download_audit_file,
    fetch_audit_text_from_delivery,
    lookup_company,
    lookup_fedresurs_audit,
    lookup_fedresurs_audit_delivery,
)
from services.girbo import (
    BoDocument,
    GirBoError,
    bfo_closed_link_url,
    download_document,
    get_organization,
    girbo_stale_open_notice,
    GIRBO_OPEN_NO_REPORTING_NOTICE,
    girbo_open_notice,
    girbo_offer_closed_contour,
    lookup_girbo_org_id,
    latest_year_documents,
)
from services.links import FSGS_INFO_URL, build_links
from services.net import ipv4_connector
from services.casebook import (
    CasebookError,
    capture_casebook_both_screenshots,
    capture_casebook_cases_screenshot,
    capture_casebook_enforcement_screenshot,
    casebook_configured,
    fetch_casebook_claim_metrics,
)
from services.casebook_metrics import (
    compute_claim_load,
    load_cached_casebook_metrics,
    revenue_from_financial_rows,
)
from services.fedresurs_screenshot import (
    capture_fedresurs_publication_png,
    capture_fedresurs_publications_png,
)
from services.filenames import (
    audit_filename,
    casebook_screenshot_filename,
    casebook_enforcement_screenshot_filename,
    content_disposition_attachment,
    documents_zip_filename,
    egrul_filename,
    egrul_predecessor_filename,
    fedresurs_audit_screenshot_filename,
    fedresurs_publications_screenshot_filename,
    fsgs_filename,
    girbo_okved_screenshot_filename,
    organization_folder_name,
    reporting_filename,
    rmsp_filename,
    zip_entry_path,
)
from services.fsgs import (
    FsgsError,
    FsgsMatchHints,
    detect_branch_entity,
    download_fsgs_pdf,
    get_cached_fsgs_pdf,
)
from services.girbo_screenshot import capture_girbo_card_png
from services.okved import GIRBO_OKVED_PERIOD, resolve_okved
from services.report_validation import validate_reporting
from services.rmsp import RmspError, download_rmsp_report, lookup_rmsp

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("crnabot.web")

APP_API_TOKEN = os.getenv("APP_API_TOKEN", "").strip()

app = FastAPI(title="CRNAbot Desk", version="1.3")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_DOWNLOAD_META_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}
_DOWNLOAD_META_TTL_SEC = 600
_CASEBOOK_IDENTITY_CACHE: dict[str, tuple[float, dict[str, str | None]]] = {}
_FSGS_HINTS_CACHE: dict[str, tuple[float, FsgsMatchHints]] = {}
_FSGS_HINTS_TTL_SEC = 3600


def _fsgs_filename_meta(inn: str) -> tuple[str, str] | None:
    """OGRN/short name from hints or download-meta cache (no API calls)."""
    hints_cached = _FSGS_HINTS_CACHE.get(inn)
    if hints_cached and time.monotonic() - hints_cached[0] < _FSGS_HINTS_TTL_SEC:
        hints = hints_cached[1]
        return (hints.ogrn or "", hints.short_name or inn)
    meta_cached = _DOWNLOAD_META_CACHE.get(inn)
    if meta_cached and time.monotonic() - meta_cached[0] < _DOWNLOAD_META_TTL_SEC:
        return meta_cached[1]
    return None


@app.middleware("http")
async def api_token_middleware(request: Request, call_next):
    if APP_API_TOKEN and request.url.path.startswith("/api/"):
        token = request.query_params.get("token") or request.headers.get("X-CRNAbot-Token", "")
        if token != APP_API_TOKEN:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        media_type="application/json; charset=utf-8",
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


def _latest_year_docs(docs: list[BoDocument]) -> list[BoDocument]:
    return latest_year_documents(docs)


def _report_year_from_docs(docs: list[BoDocument]) -> str:
    """Последний отчётный год — по балансу (как на карточке), без старых АЗ."""
    balance_periods = [
        d.period.strip()
        for d in docs
        if d.kind == "balance" and d.period.strip().isdigit()
    ]
    if balance_periods:
        return max(balance_periods)
    reporting_periods = [
        d.period.strip()
        for d in docs
        if d.kind in ("balance", "financial") and d.period.strip().isdigit()
    ]
    if reporting_periods:
        return max(reporting_periods)
    return default_report_year()


def _pick_girbo_audit_doc(documents: list[BoDocument], period: str) -> BoDocument | None:
    """АЗ только за тот же отчётный год, что и баланс."""
    audit_docs = [doc for doc in documents if doc.kind == "audit"]
    if not audit_docs:
        return None
    balance_period = ""
    balance_docs = [doc for doc in documents if doc.kind == "balance"]
    if balance_docs:
        balance = next(
            (d for d in balance_docs if period and _period_match(d.period, period)),
            balance_docs[0],
        )
        balance_period = balance.period.strip() or period
    match_period = balance_period or period
    if not match_period:
        return None
    return next(
        (d for d in audit_docs if _period_match(d.period, match_period)),
        None,
    )


def _has_girbo_audit_for_year(docs: list[BoDocument], period: str) -> bool:
    return _pick_girbo_audit_doc(docs, period) is not None


async def _fedresurs_zip_entries(
    inn: str,
    ogrn: str,
    short_name: str,
    *,
    has_audit: bool,
    report_year: str | None = None,
) -> list[tuple[str, bytes]]:
    """АЗ с Федресурса (файл или скрин публикации) или скриншот списка публикаций."""
    if has_audit:
        return []
    entries: list[tuple[str, bytes]] = []
    year = str(report_year or default_report_year())
    try:
        fed_company, delivery = await lookup_fedresurs_audit_delivery(inn, year)
        if delivery:
            pub = delivery.publication
            if delivery.mode == "file" and delivery.file_data:
                entries.append((audit_filename(ogrn, short_name, pub.period), delivery.file_data))
            elif delivery.mode == "screenshot":
                screenshot = await capture_fedresurs_publication_png(
                    pub.publication_url,
                    inn=inn,
                    company_guid=fed_company.guid if fed_company else None,
                )
                if screenshot:
                    entries.append(
                        (
                            fedresurs_audit_screenshot_filename(ogrn, short_name),
                            screenshot,
                        )
                    )
                else:
                    logger.info(
                        "Fedresurs audit publication screenshot empty inn=%s guid=%s",
                        inn,
                        pub.guid,
                    )
        elif fed_company:
            screenshot = await capture_fedresurs_publications_png(
                fed_company.guid,
                inn=inn,
                year=current_calendar_year(),
                company_name=fed_company.name,
                company_ogrn=fed_company.ogrn,
            )
            if screenshot:
                entries.append(
                    (
                        fedresurs_publications_screenshot_filename(ogrn, short_name),
                        screenshot,
                    )
                )
            else:
                logger.info(
                    "Fedresurs publications screenshot empty inn=%s guid=%s",
                    inn,
                    fed_company.guid,
                )
    except FedresursError as exc:
        logger.info("Fedresurs zip entries skipped inn=%s: %s", inn, exc)
    except Exception as exc:
        logger.info("Fedresurs zip entries error inn=%s: %s", inn, exc)
    return entries


def _period_match(left: str, right: str) -> bool:
    return left.strip() == right.strip()


def _company_short_name(
    *,
    girbo_short: str | None = None,
    egrul_short: str | None = None,
    egrul_name: str | None = None,
    girbo_name: str | None = None,
    rmsp_name: str | None = None,
    inn: str,
) -> str:
    """Краткое наименование для карточки, ZIP и имён файлов."""
    for candidate in (
        girbo_short,
        egrul_short,
        shorten_company_name(egrul_name) if egrul_name else None,
        shorten_company_name(girbo_name) if girbo_name else None,
        egrul_name,
        girbo_name,
        shorten_company_name(rmsp_name) if rmsp_name else None,
        rmsp_name,
        inn,
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return inn


def _girbo_doc_payload(doc: BoDocument, doc_short: str) -> dict:
    return {
        "title": doc.title,
        "period": doc.period,
        "kind": doc.kind,
        "format": doc.format,
        "label": (
            f"Отчетность {doc_short} {doc.period}"
            if doc.kind == "balance"
            else f"АЗ {doc_short} {doc.period}"
            if doc.kind == "audit"
            else doc.title
        ),
    }


def _lookup_girbo_documents(org, doc_short: str, report_year: str | None = None) -> list[dict]:
    """Ссылки на карточке: баланс и АЗ за актуальный период (как в ZIP)."""
    period = (
        (report_year or "").strip()
        or _report_year_from_docs(org.documents)
        or (org.latest_period or "")
    ).strip()
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(doc: BoDocument) -> None:
        key = (doc.kind, doc.period)
        if key in seen:
            return
        seen.add(key)
        out.append(_girbo_doc_payload(doc, doc_short))

    balance_docs = [d for d in org.documents if d.kind == "balance"]
    if balance_docs:
        balance = next(
            (d for d in balance_docs if period and _period_match(d.period, period)),
            balance_docs[0],
        )
        add(balance)
        period = balance.period.strip() or period

    audit_docs = [d for d in org.documents if d.kind == "audit"]
    if audit_docs:
        audit = _pick_girbo_audit_doc(org.documents, period)
        if audit:
            add(audit)

    return out


def _has_girbo_audit(docs: list[BoDocument]) -> bool:
    return any(d.kind == "audit" for d in docs)


def _latest_audit_period(docs: list[BoDocument]) -> str | None:
    periods = [
        d.period.strip()
        for d in docs
        if d.kind == "audit" and d.period.strip().isdigit()
    ]
    return max(periods) if periods else None


def _fedresurs_audit_period(fedresurs: dict | None) -> str | None:
    if not fedresurs:
        return None
    audit = fedresurs.get("audit")
    if not audit:
        return None
    period = str(audit.get("period") or "").strip()
    return period or None


def _audit_matches_report_year(*, report_year: str, girbo_documents: list[BoDocument], fedresurs: dict | None) -> bool:
    if _has_girbo_audit_for_year(girbo_documents, report_year):
        return True
    fed_period = _fedresurs_audit_period(fedresurs)
    return bool(fed_period and _period_match(fed_period, report_year))


def _set_audit_status(
    result: dict,
    *,
    report_year: str,
    girbo_documents: list[BoDocument] | None = None,
) -> None:
    if not result.get("girbo"):
        return
    documents = result["girbo"].get("documents") or []
    has_card_audit = any(d.get("kind") == "audit" for d in documents)
    fedresurs = result.get("fedresurs")
    if _audit_matches_report_year(
        report_year=report_year,
        girbo_documents=girbo_documents or [],
        fedresurs=fedresurs,
    ):
        result["audit"] = {"found": True, "report_year": report_year}
        return

    stale_period = None
    if girbo_documents:
        latest = _latest_audit_period(girbo_documents)
        if latest and not _period_match(latest, report_year):
            stale_period = latest
    result["audit"] = {
        "found": False,
        "status": "Аудиторское заключение не найдено",
        "report_year": report_year,
        "stale_period": stale_period,
    }


async def _audit_text_from_girbo_documents(
    documents: list[BoDocument], period: str
) -> tuple[str | None, str | None, bytes | None]:
    preferred = _pick_girbo_audit_doc(documents, period)
    if preferred is None:
        return None, None, None

    try:
        data, _, _ = await download_document(preferred)
    except Exception as exc:
        logger.info(
            "АЗ analysis girbo download failed period=%s url=%s: %s",
            preferred.period,
            (preferred.url or "")[:120],
            exc,
        )
        return None, None, None
    if data[:4] != b"%PDF":
        logger.info(
            "АЗ analysis girbo non-pdf period=%s bytes=%s head=%r",
            preferred.period,
            len(data),
            data[:32],
        )
        return None, None, None
    cache_key = preferred.url or f"{preferred.period}:{preferred.detail_id or ''}"
    opinion_text = audit_opinion_text_from_pdf(data, cache_key=cache_key)
    text = opinion_text
    logger.info(
        "АЗ analysis girbo extract period=%s bytes=%s chars=%s opinion_chars=%s url=%s",
        preferred.period,
        len(data),
        len(text.strip()),
        len(opinion_text.strip()),
        (preferred.url or "")[:120],
    )
    if text.strip():
        return text, opinion_text, None
    return None, None, None


async def _resolve_audit_text(
    *,
    inn: str,
    report_year: str,
    girbo_documents: list[BoDocument],
    fed_delivery,
    skip_fedresurs_lookup: bool = False,
) -> tuple[str | None, str | None, bytes | None]:
    text: str | None = None
    opinion_text: str | None = None
    pdf_bytes: bytes | None = None

    if _has_girbo_audit_for_year(girbo_documents, report_year):
        text, opinion_text, pdf_bytes = await _audit_text_from_girbo_documents(
            girbo_documents, report_year
        )

    girbo_text_ok = bool(text and len(text.strip()) >= 40)

    if not girbo_text_ok and fed_delivery is not None:
        fed_period = str(getattr(fed_delivery.publication, "period", "") or "").strip()
        if fed_period and not _period_match(fed_period, report_year):
            fed_delivery = None
        else:
            try:
                fed_text = await fetch_audit_text_from_delivery(fed_delivery)
            except Exception as exc:
                logger.info("АЗ analysis fedresurs text failed inn=%s: %s", inn, exc)
                fed_text = None
            if fed_text and len(fed_text.strip()) >= 40:
                text = fed_text
                opinion_text = fed_text
            elif fed_text and not text:
                text = fed_text
                opinion_text = fed_text

    if not girbo_text_ok and fed_delivery is None and not skip_fedresurs_lookup:
        try:
            _, delivery = await lookup_fedresurs_audit_delivery(inn, report_year)
        except Exception as exc:
            logger.info("АЗ analysis fedresurs lookup failed inn=%s: %s", inn, exc)
            delivery = None
        if delivery is not None:
            fed_period = str(getattr(delivery.publication, "period", "") or "").strip()
            if fed_period and not _period_match(fed_period, report_year):
                delivery = None
        if delivery is not None:
            try:
                fed_text = await fetch_audit_text_from_delivery(delivery)
            except Exception as exc:
                logger.info("АЗ analysis fedresurs text failed inn=%s: %s", inn, exc)
                fed_text = None
            else:
                if fed_text and len(fed_text.strip()) >= 40:
                    text = fed_text
                    opinion_text = fed_text
                elif fed_text and not text:
                    text = fed_text
                    opinion_text = fed_text

    if text:
        logger.info(
            "АЗ analysis text inn=%s chars=%s opinion_chars=%s",
            inn,
            len(text.strip()),
            len((opinion_text or "").strip()),
        )
    else:
        logger.info("АЗ analysis text unavailable inn=%s", inn)
    return text, opinion_text, pdf_bytes


async def _apply_audit_analysis(
    result: dict,
    *,
    inn: str,
    report_year: str,
    girbo_documents: list[BoDocument],
    fed_delivery,
    org_name: str | None,
    short_name: str | None,
    ogrn: str | None,
    skip_fedresurs_lookup: bool = False,
) -> None:
    _set_audit_status(
        result,
        report_year=report_year,
        girbo_documents=girbo_documents,
    )
    audit_state = result.get("audit") or {}
    if not audit_state.get("found"):
        return

    text, opinion_text, _pdf_bytes = await _resolve_audit_text(
        inn=inn,
        report_year=report_year,
        girbo_documents=girbo_documents,
        fed_delivery=fed_delivery,
        skip_fedresurs_lookup=skip_fedresurs_lookup,
    )

    if not text or len(text.strip()) < 40:
        audit_state["analysis"] = {
            "is_audit_report": False,
            "year_ok": False,
            "org_ok": False,
            "opinion": "unknown",
            "status": AUDIT_ANALYSIS_NOT_DONE,
            "details": [],
            "ok": False,
        }
        result["audit"] = audit_state
        return

    fed_name = ((result.get("fedresurs") or {}).get("name") or "").strip() or None
    girbo_name = ((result.get("girbo") or {}).get("name") or "").strip() or None
    egrul_name = ((result.get("egrul") or {}).get("name") or "").strip() or None
    has_girbo_audit = _has_girbo_audit_for_year(girbo_documents, report_year)
    girbo_opinion_known = bool(
        has_girbo_audit
        and detect_opinion_in_text(opinion_text or text or "") in {"qualified", "unqualified"}
    )
    girbo_audit_doc = _pick_girbo_audit_doc(girbo_documents, report_year) if has_girbo_audit else None
    trusted_audit = bool(has_girbo_audit or fed_delivery is not None)
    girbo_audit_period = (girbo_audit_doc.period if girbo_audit_doc else "") or report_year
    confirm_year = bool(
        has_girbo_audit
        and girbo_audit_doc
        and _period_match(girbo_audit_doc.period, report_year)
    ) or bool(
        fed_delivery is not None
        and _period_match(
            str(getattr(fed_delivery.publication, "period", "") or ""),
            report_year,
        )
    )
    analysis = analyze_audit_text(
        text,
        expected_year=report_year,
        expected_names=[
            org_name,
            short_name,
            girbo_name,
            egrul_name,
            fed_name,
            result.get("display_name"),
        ],
        inn=inn,
        ogrn=ogrn,
        trusted_audit=trusted_audit,
        confirm_year=confirm_year,
        opinion_text=opinion_text,
        pdf_bytes=None,
    )

    if analysis.opinion == "unknown" and not girbo_opinion_known:
        fed_opinion: str | None = None
        delivery = fed_delivery
        if delivery is None:
            try:
                _, delivery = await lookup_fedresurs_audit_delivery(inn, report_year)
            except Exception as exc:
                logger.info("АЗ analysis fedresurs opinion fallback failed inn=%s: %s", inn, exc)
                delivery = None
        if delivery is not None:
            try:
                fed_opinion = await fetch_audit_text_from_delivery(delivery)
            except Exception as exc:
                logger.info("АЗ analysis fedresurs opinion text failed inn=%s: %s", inn, exc)
                fed_opinion = None
        if fed_opinion and len(fed_opinion.strip()) >= 40:
            logger.info(
                "АЗ analysis fedresurs opinion fallback inn=%s chars=%s",
                inn,
                len(fed_opinion.strip()),
            )
            analysis = analyze_audit_text(
                text,
                expected_year=report_year,
                expected_names=[
                    org_name,
                    short_name,
                    girbo_name,
                    egrul_name,
                    fed_name,
                    result.get("display_name"),
                ],
                inn=inn,
                ogrn=ogrn,
                trusted_audit=trusted_audit,
                confirm_year=confirm_year,
                opinion_text=fed_opinion,
                pdf_bytes=None,
            )

    audit_state["analysis"] = {
        "is_audit_report": analysis.is_audit_report,
        "year_ok": analysis.year_ok,
        "org_ok": analysis.org_ok,
        "opinion": analysis.opinion,
        "status": analysis.status,
        "details": [],
        "ok": analysis.ok,
    }
    result["audit"] = audit_state
    logger.info(
        "АЗ analysis inn=%s opinion=%s year_ok=%s org_ok=%s is_audit=%s trusted=%s chars=%s opinion_chars=%s",
        inn,
        analysis.opinion,
        analysis.year_ok,
        analysis.org_ok,
        analysis.is_audit_report,
        trusted_audit,
        len(text.strip()),
        len((opinion_text or "").strip()),
    )
    if analysis.opinion == "unknown":
        hits = opinion_marker_hits(f"{opinion_text or ''}\n{text}")
        logger.info("АЗ opinion debug inn=%s hits=%s", inn, hits)


async def _enrich_egrul_record_from_search(inn: str, egrul: dict) -> str | None:
    """Дополняем выписку из PDF через поиск egrul.nalog.ru (имя, адрес, ОГРН)."""
    need_name = not (egrul.get("name") or "").strip()
    need_address = not (egrul.get("address") or "").strip()
    need_ogrn = not (egrul.get("ogrn") or "").strip()
    if not need_name and not need_address and not need_ogrn:
        return egrul.get("name")
    try:
        rows = await search_egrul(inn)
    except Exception as exc:
        logger.info("ЕГРЮЛ search enrich fallback inn=%s: %s", inn, exc)
        return egrul.get("name")
    if not rows:
        return egrul.get("name")
    row = rows[0]
    if need_name and row.name:
        egrul["name"] = row.name
    if need_address and row.address:
        egrul["address"] = row.address
    if not egrul.get("status") and row.status:
        egrul["status"] = row.status
    if need_ogrn and row.ogrn:
        egrul["ogrn"] = row.ogrn
    return row.name or egrul.get("name")


async def _resolve_download_names(
    inn: str,
    *,
    egrul_parsed: dict | None = None,
    org=None,
) -> tuple[str, str]:
    """ОГРН и краткое наименование для имён файлов и ZIP."""
    if not egrul_parsed and org is None:
        cached = _DOWNLOAD_META_CACHE.get(inn)
        if cached and time.monotonic() - cached[0] < _DOWNLOAD_META_TTL_SEC:
            return cached[1]

    ogrn = ""
    egrul_name: str | None = None
    egrul_short: str | None = None
    girbo_name: str | None = None
    girbo_short: str | None = None

    if egrul_parsed:
        egrul_name = (egrul_parsed.get("name") or "").strip() or None
        egrul_short = (egrul_parsed.get("short_name") or "").strip() or None
        ogrn = (egrul_parsed.get("ogrn") or "").strip() or ""

    if org is not None:
        ogrn = ogrn or (org.ogrn or "")
        girbo_name = org.name
        girbo_short = org.short_name

    if not egrul_name or not ogrn:
        try:
            rows = await search_egrul(inn)
            if rows:
                ogrn = ogrn or rows[0].ogrn or ""
                egrul_name = egrul_name or rows[0].name
        except EgrulError:
            pass

    if org is None and not girbo_short:
        try:
            fetched = await get_organization(inn)
            ogrn = ogrn or fetched.ogrn or ""
            girbo_name = girbo_name or fetched.name
            girbo_short = girbo_short or fetched.short_name
        except GirBoError:
            pass

    short_name = _company_short_name(
        girbo_short=girbo_short,
        egrul_short=egrul_short,
        egrul_name=egrul_name,
        girbo_name=girbo_name,
        inn=inn,
    )
    result = (ogrn, short_name)
    _DOWNLOAD_META_CACHE[inn] = (time.monotonic(), result)
    return result


async def _download_meta(inn: str) -> tuple[str, str]:
    return await _resolve_download_names(inn)


async def _fsgs_match_hints(inn: str) -> FsgsMatchHints:
    """Branch hints from lookup cache and EGRUL search only (no GirBo get_organization)."""
    cached_hints = _FSGS_HINTS_CACHE.get(inn)
    if cached_hints and time.monotonic() - cached_hints[0] < _FSGS_HINTS_TTL_SEC:
        return cached_hints[1]

    ogrn = ""
    short_name = ""
    meta_cached = _DOWNLOAD_META_CACHE.get(inn)
    if meta_cached and time.monotonic() - meta_cached[0] < _DOWNLOAD_META_TTL_SEC:
        ogrn, short_name = meta_cached[1]

    name: str | None = None
    address: str | None = None
    city: str | None = None
    try:
        rows = await search_egrul(inn)
        if rows:
            row = rows[0]
            name = row.name or None
            address = row.address or None
            ogrn = ogrn or row.ogrn or ""
            short_name = short_name or shorten_company_name(row.name) or row.name or ""
            city = extract_city(address)
    except EgrulError:
        pass

    hints = FsgsMatchHints(
        ogrn=ogrn or None,
        name=name,
        short_name=short_name or None,
        address=address,
        city=city,
        is_branch_entity=detect_branch_entity(name),
    )
    _FSGS_HINTS_CACHE[inn] = (time.monotonic(), hints)
    return hints


async def _casebook_identity(inn: str) -> dict[str, str | None]:
    cached = _CASEBOOK_IDENTITY_CACHE.get(inn)
    if cached and time.monotonic() - cached[0] < _DOWNLOAD_META_TTL_SEC:
        return cached[1]

    name: str | None = None
    address: str | None = None
    ogrn: str | None = None
    short_name: str | None = None
    parsed: dict = {}
    try:
        pdf = await download_egrul_pdf(inn)
        parsed = parse_egrul_pdf(pdf)
        await _enrich_egrul_record_from_search(inn, parsed)
        name = (parsed.get("name") or "").strip() or None
        address = (parsed.get("address") or "").strip() or None
        ogrn = (parsed.get("ogrn") or "").strip() or None
        short_name = (parsed.get("short_name") or "").strip() or None
    except Exception as exc:
        logger.info("Casebook identity EGRUL pdf inn=%s: %s", inn, exc)

    if not name or not address or not ogrn:
        try:
            rows = await search_egrul(inn)
            if rows:
                row = rows[0]
                name = name or (row.name or "").strip() or None
                address = address or (row.address or "").strip() or None
                ogrn = ogrn or (row.ogrn or "").strip() or None
        except EgrulError as exc:
            logger.info("Casebook identity EGRUL search inn=%s: %s", inn, exc)

    if not short_name and name:
        short_name = shorten_company_name(name)
    result = {"name": name, "address": address, "ogrn": ogrn, "short_name": short_name}
    _CASEBOOK_IDENTITY_CACHE[inn] = (time.monotonic(), result)
    return result


def _girbo_download_filename(kind: str, ogrn: str, short_name: str, period: str, ext: str) -> str:
    if kind == "audit":
        return audit_filename(ogrn, short_name, period)
    return reporting_filename(ogrn, short_name, period, ext)


def _parse_bool_flag(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in ("0", "false", "no"):
        return False
    if v in ("1", "true", "yes"):
        return True
    return default


def _needs_okved_screenshot_for_selection(
    *,
    egrul_selected: bool,
    activities_type: str | None,
) -> bool:
    return egrul_selected and activities_type == "applicant"


def _needs_fedresurs_publications_screenshot(
    *,
    reporting_selected: bool,
    has_girbo_audit: bool,
    has_fedresurs_audit: bool,
) -> bool:
    return reporting_selected and not has_girbo_audit and not has_fedresurs_audit


async def _lookup_preview(inn: str) -> dict:
    """Быстрый предпросмотр для таблицы выбора документов (без тяжёлых загрузок)."""
    result: dict = {
        "inn": inn,
        "preview": True,
        "egrul": None,
        "okved": None,
        "display_name": inn,
        "errors": [],
        "selection_hints": {},
    }

    egrul_from_pdf: dict = {}
    activities_type: str | None = None
    egrul_main: str | None = None
    egrul_name: str | None = None
    girbo_okved: str | None = None
    girbo_documents: list[BoDocument] = []
    girbo_stale_open = False
    girbo_no_public = False
    has_girbo_audit = False
    has_fedresurs_audit = False
    report_year = default_report_year()

    async def _load_egrul_pdf_data() -> dict:
        try:
            pdf = await download_egrul_pdf(inn)
            return parse_egrul_pdf(pdf)
        except Exception as exc:
            logger.info("Preview EGRUL pdf inn=%s: %s", inn, exc)
            return {}

    egrul_from_pdf, girbo_result = await asyncio.gather(
        _load_egrul_pdf_data(),
        get_organization(inn),
        return_exceptions=True,
    )

    if isinstance(egrul_from_pdf, Exception):
        egrul_from_pdf = {}
    elif isinstance(egrul_from_pdf, dict):
        activities_type = egrul_from_pdf.get("activities_type")
        egrul_main = egrul_from_pdf.get("main_okved")
        name = (egrul_from_pdf.get("name") or "").strip()
        if name:
            egrul_name = name
            result["egrul"] = {
                "name": name,
                "inn": egrul_from_pdf.get("inn") or inn,
                "ogrn": egrul_from_pdf.get("ogrn"),
                "status": egrul_from_pdf.get("status"),
            }
            result["display_name"] = shorten_company_name(name) or name

    if not result.get("egrul"):
        try:
            rows = await search_egrul(inn)
            if rows:
                r = rows[0]
                egrul_name = r.name
                result["egrul"] = {
                    "name": r.name,
                    "inn": r.inn,
                    "ogrn": r.ogrn,
                    "address": r.address,
                    "status": r.status,
                }
                result["display_name"] = shorten_company_name(r.name) or r.name
        except EgrulError as exc:
            result["errors"].append(f"ЕГРЮЛ: {exc}")

    if isinstance(girbo_result, Exception):
        message = str(girbo_result)
        if isinstance(girbo_result, GirBoError):
            if message.startswith("ГИР БО:"):
                message = message[len("ГИР БО:") :].strip()
            if not girbo_offer_closed_contour(girbo_result, message):
                result["errors"].append(f"ГИР БО: {message}")
        girbo_no_public = girbo_offer_closed_contour(girbo_result, message)
    else:
        org = girbo_result
        girbo_okved = org.okved
        girbo_documents = org.documents
        girbo_stale_open = org.stale_open_reporting
        girbo_no_public = org.no_public_reporting
        has_girbo_audit = _has_girbo_audit_for_year(org.documents, report_year)
        if not girbo_stale_open:
            report_year = _report_year_from_docs(org.documents) or report_year
        if not egrul_name:
            result["display_name"] = (
                org.short_name or shorten_company_name(org.name) or org.name or inn
            )
        if not result.get("egrul"):
            result["egrul"] = {
                "name": org.name,
                "inn": inn,
                "ogrn": org.ogrn,
            }

    if not has_girbo_audit and not girbo_stale_open and not girbo_no_public:
        try:
            _, delivery = await lookup_fedresurs_audit_delivery(inn, report_year)
            has_fedresurs_audit = delivery is not None
        except Exception as exc:
            logger.info("Preview fedresurs audit hint inn=%s: %s", inn, exc)

    okved_info = resolve_okved(
        egrul_main=egrul_main,
        activities_type=activities_type,
        girbo_okved=girbo_okved,
    )
    result["okved"] = okved_info

    doc_kinds = sorted({d.kind for d in girbo_documents if d.kind})
    result["selection_hints"] = {
        "needs_okved_screenshot": activities_type == "applicant",
        "needs_fedresurs_publications_screenshot": (
            not has_girbo_audit and not has_fedresurs_audit
        ),
        "has_girbo_audit": has_girbo_audit,
        "has_fedresurs_audit": has_fedresurs_audit,
        "casebook_configured": casebook_configured(),
        "girbo_document_kinds": doc_kinds,
        "girbo_no_public_reporting": girbo_no_public,
        "girbo_stale_open_reporting": girbo_stale_open,
        "activities_type": activities_type,
    }
    return result


@app.get("/api/lookup")
async def lookup(
    inn: str = Query(..., min_length=10, max_length=12),
    egrul: str | None = Query(None),
    reporting: str | None = Query(None),
    casebook: str | None = Query(None),
    preview: str | None = Query(None),
) -> dict:
    inn = inn.strip()
    if not inn.isdigit():
        raise HTTPException(400, "ИНН должен содержать только цифры")

    want_egrul = _parse_bool_flag(egrul, default=True)
    want_reporting = _parse_bool_flag(reporting, default=True)
    want_casebook = _parse_bool_flag(casebook, default=True)
    is_preview = _parse_bool_flag(preview, default=False)

    if is_preview:
        return await _lookup_preview(inn)

    result: dict = {
        "inn": inn,
        "egrul": None,
        "girbo": None,
        "rmsp": None,
        "fedresurs": None,
        "okved": None,
        "description": None,
        "links": None,
        "errors": [],
        "selection": {
            "egrul": want_egrul,
            "reporting": want_reporting,
            "casebook": want_casebook,
        },
    }

    egrul_name = None
    egrul_address = None
    egrul_status = None
    org_id = None
    girbo_okved = None
    girbo_region = None
    girbo_city = None
    girbo_name = None
    girbo_short_name = None
    egrul_short_name = None
    girbo_documents: list[BoDocument] = []
    girbo_balance_rows: list[dict] = []
    girbo_financial_rows: list[dict] = []
    rmsp_name = None
    report_year = default_report_year()
    fedresurs_card_url = None
    fed_audit_delivery = None
    egrul_main = None
    activities_type = None

    async def _load_egrul_pdf_data() -> dict:
        # Карточка: без PDF-выписки (файл качается отдельно) — ускоряет /api/lookup.
        return {}

    async def _load_girbo_org():
        if not want_reporting and not want_egrul:
            return None
        try:
            return await asyncio.wait_for(get_organization(inn), timeout=40.0)
        except asyncio.TimeoutError:
            raise GirBoError("ГИР БО: сервис слишком долго отвечает")

    def _egrul_from_pdf(parsed: dict) -> dict | None:
        name = (parsed.get("name") or "").strip()
        ogrn = (parsed.get("ogrn") or "").strip()
        if not name and not ogrn:
            return None
        short_name = (parsed.get("short_name") or "").strip() or None
        return {
            "name": name or None,
            "short_name": short_name,
            "inn": parsed.get("inn") or inn,
            "ogrn": ogrn or None,
            "address": parsed.get("address"),
            "status": parsed.get("status"),
        }

    async def _load_rmsp():
        try:
            return await asyncio.wait_for(lookup_rmsp(inn), timeout=25.0)
        except asyncio.TimeoutError:
            return TimeoutError("Реестр МСП: таймаут")

    okved_raw, girbo_result, rmsp_result, bankruptcy_result = await asyncio.gather(
        _load_egrul_pdf_data(),
        _load_girbo_org(),
        _load_rmsp(),
        lookup_bankruptcy(inn),
        return_exceptions=True,
    )

    display_short = None
    egrul_from_pdf = okved_raw if isinstance(okved_raw, dict) else {}
    egrul_pdf_payload = _egrul_from_pdf(egrul_from_pdf) if egrul_from_pdf else None

    if egrul_pdf_payload:
        egrul_name = egrul_pdf_payload.get("name")
        egrul_address = egrul_pdf_payload.get("address")
        egrul_status = egrul_pdf_payload.get("status")
        result["egrul"] = {
            **egrul_pdf_payload,
            "alerts": egrul_from_pdf.get("alerts") or [],
        }
        if want_egrul:
            pass  # label added later
        else:
            result["egrul"].pop("label", None)
        egrul_short_name = (egrul_pdf_payload.get("short_name") or "").strip() or None
        if egrul_short_name:
            display_short = egrul_short_name
        elif egrul_name:
            display_short = shorten_company_name(egrul_name) or egrul_name
        else:
            filled_name = await _enrich_egrul_record_from_search(inn, result["egrul"])
            if filled_name:
                egrul_name = filled_name
                display_short = shorten_company_name(filled_name) or filled_name
    else:
        try:
            egrul_result = await search_egrul(inn)
        except Exception as exc:
            if isinstance(exc, EgrulError):
                result["errors"].append(f"ЕГРЮЛ: {exc}")
            else:
                result["errors"].append(f"ЕГРЮЛ: {exc}")
        else:
            if egrul_result:
                r = egrul_result[0]
                egrul_name = r.name
                egrul_address = r.address
                egrul_status = r.status
                result["egrul"] = {
                    "name": r.name,
                    "inn": r.inn,
                    "ogrn": r.ogrn,
                    "address": r.address,
                    "status": r.status,
                }
                display_short = shorten_company_name(r.name) or r.name

    if isinstance(okved_raw, Exception):
        okved_raw = {}

    if isinstance(rmsp_result, Exception):
        logger.info("Реестр МСП lookup error inn=%s: %s", inn, rmsp_result)
        result["rmsp"] = {
            "in_registry": False,
            "category": "Не удалось получить сведения",
            "name": None,
        }
    else:
        rmsp_name = rmsp_result.name or None
        result["rmsp"] = {
            "in_registry": rmsp_result.in_registry,
            "category": rmsp_result.category,
            "name": rmsp_name,
            "ogrn": rmsp_result.ogrn or None,
            "region": rmsp_result.region,
        }

    if isinstance(bankruptcy_result, Exception):
        logger.info("Банкротство lookup error inn=%s: %s", inn, bankruptcy_result)
        result["bankruptcy"] = {
            "found": False,
            "state": "unknown",
            "status": STATUS_UNKNOWN,
            "url": BANKRUPTCY_URL,
        }
    else:
        result["bankruptcy"] = {
            "found": bankruptcy_result.found,
            "state": bankruptcy_result.state,
            "status": bankruptcy_result.status,
            "url": bankruptcy_result.url,
        }

    if girbo_result is None:
        pass
    elif isinstance(girbo_result, Exception):
        message = str(girbo_result)
        if message.startswith("ГИР БО:"):
            message = message[len("ГИР БО:") :].strip()
        if girbo_offer_closed_contour(girbo_result, message):
            stub_name = egrul_name or rmsp_name
            result["girbo"] = {
                "name": stub_name,
                "ogrn": (result.get("egrul") or {}).get("ogrn"),
                "no_public_reporting": True,
                "bfo_card_url": bfo_closed_link_url(inn=inn),
                "open_notice": girbo_open_notice(message),
            }
        elif isinstance(girbo_result, GirBoError):
            result["errors"].append(f"ГИР БО: {message}")
        else:
            result["errors"].append(f"ГИР БО: неожиданная ошибка ({message})")
    else:
        org = girbo_result
        org_id = org.org_id
        girbo_okved = org.okved
        girbo_region = org.region
        girbo_city = org.city
        girbo_name = org.name
        girbo_short_name = org.short_name
        girbo_documents = org.documents
        girbo_balance_rows = org.latest_balance_rows
        girbo_financial_rows = org.latest_financial_rows
        report_year = default_report_year()
        stale_open = org.stale_open_reporting
        if not stale_open:
            report_year = _report_year_from_docs(org.documents) or report_year
        doc_short = org.short_name or shorten_company_name(egrul_name) or org.name
        display_short = doc_short or display_short
        open_notice = None
        if org.no_public_reporting:
            open_notice = GIRBO_OPEN_NO_REPORTING_NOTICE
        elif stale_open:
            open_notice = girbo_stale_open_notice(org.latest_period or report_year)
        result["girbo"] = {
            "name": org.name,
            "short_name": org.short_name,
            "org_id": org.org_id,
            "ogrn": org.ogrn,
            "card_url": org.card_url,
            "bfo_card_url": bfo_closed_link_url(inn=inn, org_id=org.org_id)
            if org.no_public_reporting or stale_open
            else None,
            "no_public_reporting": org.no_public_reporting,
            "stale_open_reporting": stale_open,
            "open_notice": open_notice,
            "okved": org.okved,
            "region": org.region,
            "city": org.city,
            "documents": [] if stale_open or not want_reporting else _lookup_girbo_documents(org, doc_short, report_year),
        }
        if girbo_balance_rows and not stale_open and want_reporting:
            validation = validate_reporting(girbo_balance_rows, girbo_financial_rows)
            result["report_check"] = {
                "ok": validation.ok,
                "status": validation.status,
                "messages": validation.messages,
                "flags": validation.flags,
                "period": org.latest_period or report_year,
            }
            if casebook_configured() and revenue_from_financial_rows(girbo_financial_rows) is not None:
                try:
                    metrics = load_cached_casebook_metrics(inn)
                    if metrics is None and os.getenv("CASEBOOK_FETCH_CLAIMS_ON_LOOKUP", "0") == "1":
                        metrics = await fetch_casebook_claim_metrics(inn)
                    claim_load = compute_claim_load(metrics, girbo_financial_rows)
                    if claim_load:
                        result["report_check"]["claim_load"] = claim_load
                except CasebookError as exc:
                    logger.info("Исковая нагрузка skipped inn=%s: %s", inn, exc)

    fedresurs_lookup_attempted = False
    if want_reporting and not _has_girbo_audit_for_year(girbo_documents, report_year):
        fedresurs_lookup_attempted = True
        try:
            fed_company, delivery = await asyncio.wait_for(
                lookup_fedresurs_audit_delivery(inn, report_year),
                timeout=35.0,
            )
            fed_audit_delivery = delivery
            if fed_company:
                fedresurs_card_url = fed_company.card_url
                fed_payload: dict = {
                    "guid": fed_company.guid,
                    "name": fed_company.name,
                    "inn": fed_company.inn,
                    "ogrn": fed_company.ogrn,
                    "card_url": fed_company.card_url,
                    "report_year": report_year,
                    "audit": None,
                }
                if delivery:
                    fed_audit = delivery.publication
                    if not _period_match(fed_audit.period, report_year):
                        logger.info(
                            "Fedresurs audit skipped wrong period inn=%s period=%s report_year=%s",
                            inn,
                            fed_audit.period,
                            report_year,
                        )
                        delivery = None
                        fed_audit_delivery = None
                if delivery:
                    fed_audit = delivery.publication
                    audit_name = (
                        display_short
                        or girbo_short_name
                        or shorten_company_name(egrul_name)
                        or girbo_name
                        or egrul_name
                        or fed_company.name
                        or inn
                    )
                    audit_label = (
                        f"АЗ {audit_name} {fed_audit.period}"
                        if delivery.mode == "file"
                        else f"Выдержка из Федресурса {audit_name}"
                    )
                    audit_format = "pdf" if delivery.mode == "file" else "png"
                    fed_payload["audit"] = {
                        "guid": fed_audit.guid,
                        "title": fed_audit.title,
                        "type_label": fed_audit.type_label,
                        "publication_type": fed_audit.publication_type,
                        "period": fed_audit.period,
                        "date_publish": fed_audit.date_publish,
                        "publication_url": fed_audit.publication_url,
                        "label": audit_label,
                        "delivery": delivery.mode,
                        "format": audit_format,
                    }
                    if delivery.mode == "screenshot":
                        fed_payload["audit_screenshot"] = (
                            f"/api/download/fedresurs-audit-screenshot"
                            f"?inn={inn}&guid={fed_audit.guid}&period={fed_audit.period}"
                        )
                    if result.get("girbo") is not None:
                        result["girbo"]["documents"].append(
                            {
                                "title": fed_audit.title,
                                "period": fed_audit.period,
                                "kind": "audit",
                                "format": audit_format,
                                "label": audit_label,
                                "source": "fedresurs",
                                "delivery": delivery.mode,
                                "publication_guid": fed_audit.guid,
                            }
                        )
                result["fedresurs"] = fed_payload
                if not delivery:
                    fed_payload["publications_screenshot"] = (
                        f"/api/download/fedresurs-publications?inn={inn}&period={report_year}"
                    )
        except asyncio.TimeoutError:
            logger.info("Fedresurs lookup timeout inn=%s", inn)
        except FedresursError as exc:
            logger.info("Fedresurs lookup failed inn=%s: %s", inn, exc)
            result["errors"].append(f"Федресурс: {exc}")
        except Exception as exc:
            logger.info("Fedresurs lookup error inn=%s: %s", inn, exc)
    elif want_reporting:
        try:
            connector = ipv4_connector(ssl=False)
            jar = aiohttp.CookieJar(unsafe=True)
            async with aiohttp.ClientSession(
                connector=connector,
                cookie_jar=jar,
                timeout=aiohttp.ClientTimeout(total=30),
                trust_env=False,
            ) as session:
                fed_company = await lookup_company(session, inn)
                if fed_company:
                    fedresurs_card_url = fed_company.card_url
                    result["fedresurs"] = {
                        "guid": fed_company.guid,
                        "name": fed_company.name,
                        "inn": fed_company.inn,
                        "ogrn": fed_company.ogrn,
                        "card_url": fed_company.card_url,
                        "report_year": report_year,
                        "audit": None,
                    }
        except Exception as exc:
            logger.info("Fedresurs card lookup error inn=%s: %s", inn, exc)

    if isinstance(okved_raw, Exception):
        logger.info("ЕГРЮЛ parse error inn=%s: %s", inn, okved_raw)
    elif isinstance(okved_raw, dict):
        egrul_main = okved_raw.get("main_okved")
        activities_type = okved_raw.get("activities_type")
        if result.get("egrul"):
            alerts = okved_raw.get("alerts") or []
            if alerts:
                result["egrul"]["alerts"] = alerts
            if okved_raw.get("needs_predecessor_egrul") and want_egrul:
                pred = okved_raw.get("predecessor") or {}
                pred_inn = pred.get("inn")
                if pred_inn:
                    pred_name = pred.get("name") or pred_inn
                    pred_short = shorten_company_name(pred_name) or pred_name
                    pred_payload: dict = {
                        "inn": pred_inn,
                        "name": pred_name,
                        "ogrn": pred.get("ogrn"),
                        "label": f"Выписка ЕГРЮЛ {pred_short}",
                        "alerts": [],
                    }
                    try:
                        pred_pdf = await download_egrul_pdf(pred_inn)
                        pred_parsed = parse_egrul_pdf(pred_pdf)
                        pred_payload["alerts"] = pred_parsed.get("alerts") or []
                    except Exception as exc:
                        logger.info(
                            "ЕГРЮЛ предшественник недоступен inn=%s pred_inn=%s: %s",
                            inn,
                            pred_inn,
                            exc,
                        )
                    result["egrul"]["predecessor"] = pred_payload

    okved_info = resolve_okved(
        egrul_main=egrul_main,
        activities_type=activities_type,
        girbo_okved=girbo_okved,
    )
    result["okved"] = okved_info

    region = extract_region(egrul_address, girbo_region)
    city = extract_city(
        egrul_address,
        girbo_city=girbo_city,
        girbo_region=girbo_region,
    )
    short_name = _company_short_name(
        girbo_short=girbo_short_name,
        egrul_short=egrul_short_name,
        egrul_name=egrul_name,
        girbo_name=girbo_name,
        rmsp_name=rmsp_name,
        inn=inn,
    )
    result["display_name"] = short_name
    result["company_about"] = build_company_about(
        name=egrul_name or girbo_name,
        region=region,
        address=egrul_address,
        city=city,
    )
    if result.get("egrul") and want_egrul:
        result["egrul"]["label"] = f"Выписка ЕГРЮЛ {result['display_name']}"
    if result.get("rmsp"):
        result["rmsp"]["label"] = "Реестр МСП"

    try:
        result["description"] = await build_company_description(
            short_name=short_name,
            city=city,
            name=egrul_name or girbo_name,
            inn=inn,
            region=region,
            status=egrul_status,
            address=egrul_address,
        )
    except Exception as exc:
        result["errors"].append(f"Описание: {exc}")

    try:
        result["links"] = (
            await build_links(inn, org_id=org_id, fedresurs_card=fedresurs_card_url)
        ).__dict__
        if not want_casebook and result.get("links"):
            result["links"]["casebook_screenshot"] = None
            result["links"]["casebook_enforcement_screenshot"] = None
    except Exception as exc:
        result["errors"].append(f"Ссылки: {exc}")
        result["links"] = {
            "inn": inn,
            "girbo_card": f"https://bo.nalog.gov.ru/organizations-card/{org_id}" if org_id else None,
            "spark_card": "https://spark-interfax.ru/system/#/dashboard",
            "fedresurs_card": fedresurs_card_url,
            "casebook_feed": "https://casebook.ru/app/feed",
            "casebook_screenshot": (
                f"/api/download/casebook?inn={inn}" if casebook_configured() and want_casebook else None
            ),
            "casebook_enforcement_screenshot": (
                f"/api/download/casebook-enforcement?inn={inn}"
                if casebook_configured() and want_casebook
                else None
            ),
            "bankruptcy_url": BANKRUPTCY_URL,
            "fsgs_info": FSGS_INFO_URL,
            "fsgs_download": f"/api/download/fsgs?inn={inn}",
        }
    if want_reporting:
        await _apply_audit_analysis(
            result,
            inn=inn,
            report_year=report_year,
            girbo_documents=girbo_documents,
            fed_delivery=fed_audit_delivery,
            org_name=egrul_name or girbo_name,
            short_name=result.get("display_name") or display_short,
            ogrn=((result.get("girbo") or {}).get("ogrn")) or ((result.get("egrul") or {}).get("ogrn")),
            skip_fedresurs_lookup=fedresurs_lookup_attempted,
        )
        if result.get("girbo") and isinstance(result["girbo"].get("documents"), list):
            result["girbo"]["documents"] = [
                doc
                for doc in result["girbo"]["documents"]
                if doc.get("kind") != "audit"
                or _period_match(str(doc.get("period") or ""), report_year)
            ]

    ogrn_for_meta = (
        ((result.get("girbo") or {}).get("ogrn"))
        or ((result.get("egrul") or {}).get("ogrn"))
        or ""
    )
    short_for_meta = short_name
    result["zip_filename"] = documents_zip_filename(ogrn_for_meta, short_for_meta)
    _DOWNLOAD_META_CACHE[inn] = (time.monotonic(), (ogrn_for_meta, short_for_meta))
    _FSGS_HINTS_CACHE[inn] = (
        time.monotonic(),
        FsgsMatchHints(
            ogrn=ogrn_for_meta or None,
            name=egrul_name or girbo_name,
            short_name=short_for_meta or None,
            address=egrul_address,
            city=city,
            is_branch_entity=detect_branch_entity(egrul_name or girbo_name),
        ),
    )
    return result


@app.get("/api/download/egrul")
async def api_download_egrul(inn: str = Query(..., min_length=10, max_length=15)) -> StreamingResponse:
    inn = inn.strip()
    try:
        pdf = await download_egrul_pdf(inn)
    except EgrulError as exc:
        raise HTTPException(400, str(exc)) from exc
    parsed = parse_egrul_pdf(pdf)
    ogrn, short_name = await _resolve_download_names(inn, egrul_parsed=parsed)
    filename = egrul_filename(ogrn, short_name)
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/girbo")
async def api_download_girbo(
    inn: str = Query(..., min_length=10, max_length=12),
    kind: str = Query("balance"),
    period: str | None = None,
) -> StreamingResponse:
    inn = inn.strip()
    try:
        org = await get_organization(inn)
        docs = [d for d in org.documents if d.kind == kind]
        if period:
            docs = [d for d in docs if d.period == period]
        if not docs:
            raise GirBoError("Документ не найден.")
        doc = docs[0]
        logger.info(
            "GIR BO download start inn=%s kind=%s period=%s url=%s fallbacks=%s",
            inn,
            kind,
            doc.period,
            doc.url,
            doc.fallback_urls,
        )
        data, _, media_type = await download_document(doc)
        ogrn, short_name = await _resolve_download_names(inn, org=org)
        ext = "xlsx" if media_type.endswith("spreadsheetml.sheet") else "pdf"
        filename = _girbo_download_filename(kind, ogrn, short_name, doc.period, ext)
    except GirBoError as exc:
        logger.error("GIR BO download failed inn=%s kind=%s period=%s: %s", inn, kind, period, exc)
        raise HTTPException(400, str(exc)) from exc

    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/fedresurs-publications")
async def api_download_fedresurs_publications(
    inn: str = Query(..., min_length=10, max_length=12),
    period: str | None = None,
) -> StreamingResponse:
    inn = inn.strip()
    report_year = period or default_report_year()
    fed_company, delivery = await lookup_fedresurs_audit_delivery(inn, report_year)
    if not fed_company:
        raise HTTPException(404, "Компания на Федресурсе не найдена.")
    if delivery:
        raise HTTPException(
            404,
            "На Федресурсе есть аудиторское заключение — используйте скачивание АЗ.",
        )
    screenshot = await capture_fedresurs_publications_png(
        fed_company.guid,
        inn=inn,
        year=current_calendar_year(),
        company_name=fed_company.name,
        company_ogrn=fed_company.ogrn,
    )
    if not screenshot:
        raise HTTPException(400, "Не удалось сделать скриншот публикаций Федресурса.")
    ogrn, short_name = await _download_meta(inn)
    filename = fedresurs_publications_screenshot_filename(ogrn, short_name)
    return StreamingResponse(
        io.BytesIO(screenshot),
        media_type="image/png",
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/fedresurs-audit")
async def api_download_fedresurs_audit(
    inn: str = Query(..., min_length=10, max_length=12),
    guid: str = Query(..., min_length=8),
    period: str | None = None,
) -> StreamingResponse:
    inn = inn.strip()
    guid = guid.strip()
    report_year = period or default_report_year()

    fed_company, delivery = await lookup_fedresurs_audit_delivery(inn, report_year)
    if not delivery or delivery.publication.guid != guid:
        raise HTTPException(404, "Аудиторское заключение на Федресурсе не найдено.")
    if delivery.mode != "file":
        raise HTTPException(
            404,
            "Аудиторское заключение на Федресурсе доступно только как скриншот публикации.",
        )

    if delivery.file_data:
        data, media_type = delivery.file_data, delivery.file_media_type or "application/pdf"
    else:
        connector = ipv4_connector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                cookie_jar=jar,
                timeout=aiohttp.ClientTimeout(total=90),
                trust_env=False,
            ) as session:
                data, media_type = await download_audit_file(session, delivery.publication)
        except FedresursError as exc:
            raise HTTPException(400, str(exc)) from exc

    ogrn, short_name = await _download_meta(inn)
    filename = audit_filename(ogrn, short_name, delivery.publication.period)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/fedresurs-audit-screenshot")
async def api_download_fedresurs_audit_screenshot(
    inn: str = Query(..., min_length=10, max_length=12),
    guid: str = Query(..., min_length=8),
    period: str | None = None,
) -> StreamingResponse:
    inn = inn.strip()
    guid = guid.strip()
    report_year = period or default_report_year()

    fed_company, delivery = await lookup_fedresurs_audit_delivery(inn, report_year)
    if not delivery or delivery.publication.guid != guid:
        raise HTTPException(404, "Публикация аудита на Федресурсе не найдена.")
    if delivery.mode != "screenshot":
        raise HTTPException(404, "Аудиторское заключение на Федресурсе доступно как файл.")

    pub = delivery.publication
    screenshot = await capture_fedresurs_publication_png(
        pub.publication_url,
        inn=inn,
        company_guid=fed_company.guid if fed_company else None,
    )
    if not screenshot:
        raise HTTPException(400, "Не удалось сделать скриншот публикации аудита на Федресурсе.")
    ogrn, short_name = await _download_meta(inn)
    filename = fedresurs_audit_screenshot_filename(ogrn, short_name)
    return StreamingResponse(
        io.BytesIO(screenshot),
        media_type="image/png",
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/casebook")
async def api_download_casebook(inn: str = Query(..., min_length=10, max_length=12)) -> StreamingResponse:
    inn = inn.strip()
    identity = await _casebook_identity(inn)
    identity_kwargs = {
        "company_name": identity.get("name"),
        "company_short_name": identity.get("short_name"),
        "company_address": identity.get("address"),
        "company_ogrn": identity.get("ogrn"),
    }
    try:
        screenshot = await capture_casebook_cases_screenshot(inn, **identity_kwargs)
    except CasebookError as exc:
        raise HTTPException(400, str(exc)) from exc
    ogrn, short_name = await _download_meta(inn)
    filename = casebook_screenshot_filename(ogrn, short_name)
    return StreamingResponse(
        io.BytesIO(screenshot),
        media_type="image/png",
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/casebook-enforcement")
async def api_download_casebook_enforcement(
    inn: str = Query(..., min_length=10, max_length=12),
) -> StreamingResponse:
    inn = inn.strip()
    identity = await _casebook_identity(inn)
    identity_kwargs = {
        "company_name": identity.get("name"),
        "company_short_name": identity.get("short_name"),
        "company_address": identity.get("address"),
        "company_ogrn": identity.get("ogrn"),
    }
    try:
        screenshot = await capture_casebook_enforcement_screenshot(inn, **identity_kwargs)
    except CasebookError as exc:
        raise HTTPException(400, str(exc)) from exc
    ogrn, short_name = await _download_meta(inn)
    filename = casebook_enforcement_screenshot_filename(ogrn, short_name)
    return StreamingResponse(
        io.BytesIO(screenshot),
        media_type="image/png",
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/rmsp")
async def api_download_rmsp(inn: str = Query(..., min_length=10, max_length=15)) -> StreamingResponse:
    inn = inn.strip()
    try:
        report, _record = await download_rmsp_report(inn)
    except RmspError as exc:
        raise HTTPException(400, str(exc)) from exc
    ogrn, short_name = await _download_meta(inn)
    filename = rmsp_filename(ogrn, short_name)
    return StreamingResponse(
        io.BytesIO(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/fsgs")
async def api_download_fsgs(inn: str = Query(..., min_length=10, max_length=15)) -> StreamingResponse:
    inn = inn.strip()
    cached_pdf = get_cached_fsgs_pdf(inn)
    if cached_pdf is not None:
        meta = _fsgs_filename_meta(inn)
        if meta is not None:
            ogrn, short_name = meta
        else:
            ogrn, short_name = await _download_meta(inn)
        filename = fsgs_filename(ogrn, short_name)
        return StreamingResponse(
            io.BytesIO(cached_pdf),
            media_type="application/pdf",
            headers=content_disposition_attachment(filename),
        )

    hints_task = asyncio.create_task(_fsgs_match_hints(inn))
    meta_task = asyncio.create_task(_download_meta(inn))
    try:
        pdf = await download_fsgs_pdf(inn, hints_task=hints_task)
    except FsgsError as exc:
        hints_task.cancel()
        meta_task.cancel()
        raise HTTPException(400, str(exc)) from exc
    meta = _fsgs_filename_meta(inn)
    if meta is not None:
        ogrn, short_name = meta
        meta_task.cancel()
    else:
        ogrn, short_name = await meta_task
    filename = fsgs_filename(ogrn, short_name)
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers=content_disposition_attachment(filename),
    )


@app.get("/api/download/all")
async def api_download_all(
    inn: str = Query(..., min_length=10, max_length=12),
    egrul: str | None = Query(None),
    reporting: str | None = Query(None),
    casebook: str | None = Query(None),
) -> StreamingResponse:
    inn = inn.strip()
    want_egrul = _parse_bool_flag(egrul, default=True)
    want_reporting = _parse_bool_flag(reporting, default=True)
    want_casebook = _parse_bool_flag(casebook, default=True)

    egrul_parsed: dict = {}
    org = None

    async def _fetch_egrul() -> bytes | None:
        if not (want_egrul or want_reporting or want_casebook):
            return None
        try:
            return await download_egrul_pdf(inn)
        except EgrulError:
            return None

    async def _fetch_org():
        if not (want_reporting or want_egrul):
            return None
        try:
            return await get_organization(inn)
        except GirBoError as exc:
            logger.info("ZIP GIR BO org fetch failed inn=%s: %s", inn, exc)
            return None

    try:
        egrul_pdf, org = await asyncio.gather(_fetch_egrul(), _fetch_org())
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.info("ZIP fetch network error inn=%s: %s", inn, exc)
        raise HTTPException(503, "Сайты ФНС/ГИР БО временно недоступны. Повторите через минуту.") from exc

    if egrul_pdf:
        egrul_parsed = parse_egrul_pdf(egrul_pdf)
        await _enrich_egrul_record_from_search(inn, egrul_parsed)

    try:
        ogrn, short_name = await _resolve_download_names(
            inn,
            egrul_parsed=egrul_parsed or None,
            org=org,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, EgrulError) as exc:
        logger.info("ZIP resolve names failed inn=%s: %s", inn, exc)
        raise HTTPException(503, "ЕГРЮЛ временно недоступен. Повторите через минуту.") from exc
    zip_folder = organization_folder_name(ogrn, short_name)
    zip_filename = documents_zip_filename(ogrn, short_name)
    buffer = io.BytesIO()

    async def _girbo_zip_entries() -> list[tuple[str, bytes]]:
        if not want_reporting and not _needs_okved_screenshot_for_selection(
            egrul_selected=want_egrul,
            activities_type=egrul_parsed.get("activities_type"),
        ):
            return []

        entries: list[tuple[str, bytes]] = []
        has_audit = False
        report_year = default_report_year()
        if want_reporting and org:
            try:
                if org.stale_open_reporting:
                    logger.info(
                        "GIR BO zip skip stale open reporting inn=%s period=%s",
                        inn,
                        org.latest_period,
                    )
                else:
                    report_year = _report_year_from_docs(org.documents) or report_year
                    has_audit = _has_girbo_audit_for_year(org.documents, report_year)
                    latest_docs = _latest_year_docs(org.documents)
                    doc_results = await asyncio.gather(
                        *[download_document(doc) for doc in latest_docs if doc.kind != "financial"],
                        return_exceptions=True,
                    )
                    doc_idx = 0
                    for doc in latest_docs:
                        if doc.kind == "financial":
                            continue
                        if doc.kind == "audit" and not _period_match(doc.period, report_year):
                            continue
                        result = doc_results[doc_idx]
                        doc_idx += 1
                        if isinstance(result, BaseException):
                            continue
                        data, _, media_type = result
                        ext = "xlsx" if media_type.endswith("spreadsheetml.sheet") else "pdf"
                        arcname = _girbo_download_filename(doc.kind, ogrn, short_name, doc.period, ext)
                        entries.append((arcname, data))
            except GirBoError:
                pass
        elif org and not org.stale_open_reporting:
            report_year = _report_year_from_docs(org.documents) or report_year
            has_audit = _has_girbo_audit_for_year(org.documents, report_year)

        activities_type = egrul_parsed.get("activities_type")
        if _needs_okved_screenshot_for_selection(
            egrul_selected=want_egrul,
            activities_type=activities_type,
        ):
            org_id_for_shot = org.org_id if org else await lookup_girbo_org_id(inn)
            if org_id_for_shot:
                screenshot = await capture_girbo_card_png(
                    org_id_for_shot, GIRBO_OKVED_PERIOD
                )
                if screenshot:
                    entries.append(
                        (
                            girbo_okved_screenshot_filename(ogrn, short_name, GIRBO_OKVED_PERIOD),
                            screenshot,
                        )
                    )
                else:
                    logger.info(
                        "GIR BO OKVED screenshot empty inn=%s org_id=%s period=%s",
                        inn,
                        org_id_for_shot,
                        GIRBO_OKVED_PERIOD,
                    )
            else:
                logger.info(
                    "GIR BO OKVED screenshot skipped inn=%s: org_id not found activities=%s",
                    inn,
                    activities_type,
                )

        if want_reporting:
            entries.extend(
                await _fedresurs_zip_entries(
                    inn,
                    ogrn,
                    short_name,
                    has_audit=has_audit,
                    report_year=report_year,
                )
            )
        return entries

    async def _casebook_zip_entries() -> list[tuple[str, bytes]]:
        if not casebook_configured():
            return []
        logger.info("Casebook zip start inn=%s", inn)
        entries: list[tuple[str, bytes]] = []
        identity: dict[str, str | None] = {
            "name": (egrul_parsed.get("name") or "").strip() or None,
            "address": (egrul_parsed.get("address") or "").strip() or None,
            "ogrn": (egrul_parsed.get("ogrn") or "").strip() or None,
            "short_name": (egrul_parsed.get("short_name") or "").strip() or None,
        }
        if not identity["short_name"] and identity["name"]:
            identity["short_name"] = shorten_company_name(identity["name"])
        if not identity["name"] or not identity["address"] or not identity["ogrn"]:
            fetched = await _casebook_identity(inn)
            identity["name"] = identity["name"] or fetched.get("name")
            identity["address"] = identity["address"] or fetched.get("address")
            identity["ogrn"] = identity["ogrn"] or fetched.get("ogrn")
            identity["short_name"] = identity["short_name"] or fetched.get("short_name")
        identity_kwargs = {
            "company_name": identity.get("name"),
            "company_address": identity.get("address"),
            "company_ogrn": identity.get("ogrn"),
            "company_short_name": identity.get("short_name"),
        }
        try:
            cases_png, enforcement_png = await capture_casebook_both_screenshots(inn, **identity_kwargs)
            if cases_png:
                entries.append((casebook_screenshot_filename(ogrn, short_name), cases_png))
            if enforcement_png:
                entries.append(
                    (
                        casebook_enforcement_screenshot_filename(ogrn, short_name),
                        enforcement_png,
                    )
                )
            return entries
        except CasebookError as exc:
            logger.info("Casebook both screenshots failed inn=%s: %s", inn, exc)
        for capture, filename_fn in (
            (capture_casebook_cases_screenshot, casebook_screenshot_filename),
            (
                capture_casebook_enforcement_screenshot,
                casebook_enforcement_screenshot_filename,
            ),
        ):
            try:
                screenshot = await capture(inn, **identity_kwargs)
                entries.append((filename_fn(ogrn, short_name), screenshot))
            except CasebookError as exc:
                logger.info("Casebook screenshot skipped inn=%s: %s", inn, exc)
        return entries

    girbo_task = (
        asyncio.create_task(_girbo_zip_entries())
        if want_reporting or want_egrul
        else None
    )
    casebook_task = (
        asyncio.create_task(_casebook_zip_entries())
        if casebook_configured() and want_casebook
        else None
    )

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if want_egrul and egrul_pdf:
            zf.writestr(
                zip_entry_path(zip_folder, egrul_filename(ogrn, short_name)),
                egrul_pdf,
            )
            if egrul_parsed.get("needs_predecessor_egrul") and want_egrul:
                pred = egrul_parsed.get("predecessor") or {}
                pred_inn = pred.get("inn")
                if pred_inn:
                    try:
                        pred_pdf = await download_egrul_pdf(pred_inn)
                        pred_short = (
                            shorten_company_name(pred.get("name"))
                            or pred.get("name")
                            or pred_inn
                        )
                        zf.writestr(
                            zip_entry_path(
                                zip_folder,
                                egrul_predecessor_filename(ogrn, pred_short),
                            ),
                            pred_pdf,
                        )
                    except EgrulError:
                        pass

        if girbo_task:
            girbo_entries = await girbo_task
            for arcname, data in girbo_entries:
                zf.writestr(zip_entry_path(zip_folder, arcname), data)

        if casebook_task:
            for arcname, data in await casebook_task:
                zf.writestr(zip_entry_path(zip_folder, arcname), data)

    buffer.seek(0)
    if buffer.getbuffer().nbytes < 100:
        raise HTTPException(404, "Не удалось скачать ни одного файла.")

    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers=content_disposition_attachment(zip_filename),
    )


def main() -> None:
    import os
    import uvicorn

    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "80"))
    if port == 80:
        logger.info("Откройте в браузере: http://crna/")
    else:
        logger.info("Откройте в браузере: http://crna:%s", port)
    uvicorn.run("web_app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
