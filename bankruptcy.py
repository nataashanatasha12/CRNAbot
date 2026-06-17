from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

from services.inn_utils import inn_equal, normalize_inn

logger = logging.getLogger("crnabot.bankruptcy")

BANKROT_SITE = "https://bankrot.fedresurs.ru"
FEDRESURS_SITE = "https://fedresurs.ru"
BANKRUPTCY_URL = (
    "https://bankrot.fedresurs.ru/?utm_referrer=https:%2F%2Ffedresurs.ru%2F"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
LOOKUP_TIMEOUT_SEC = 28.0
CACHE_TTL_SEC = 3600

STATUS_FOUND = "Имеется информация о банкротстве"
STATUS_NOT_FOUND = "Информации о банкротстве не найдено"
STATUS_UNKNOWN = "Не удалось проверить банкротство"

BankruptcyState = Literal["found", "not_found", "unknown"]
ProbeResult = Literal["found", "not_found", "failed"]

_cache: dict[str, tuple[float, "BankruptcyCheck"]] = {}


class BankruptcyError(Exception):
    pass


@dataclass
class BankruptcyCheck:
    found: bool
    status: str
    state: BankruptcyState = "not_found"
    url: str = BANKRUPTCY_URL


def _headers(referer: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": referer,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _search_filter(inn: str) -> dict[str, Any]:
    if len(inn) == 12:
        return {"startRowIndex": "0", "pageSize": "15", "fizCode": inn}
    return {"startRowIndex": "0", "pageSize": "15", "orgCode": inn}


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("pageData", "records", "content", "rows"):
        bucket = payload.get(key)
        if isinstance(bucket, list):
            return [row for row in bucket if isinstance(row, dict)]
    return []


def _extract_found(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("found", "total_count", "totalCount", "count", "total"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _row_inn(row: dict[str, Any]) -> str:
    for key in ("inn", "debtorInn", "INN", "orgInn", "orgCode", "code", "fizCode"):
        value = row.get(key)
        if value not in (None, ""):
            return normalize_inn(value)
    return ""


def _rows_match_inn(rows: list[dict[str, Any]], inn: str) -> bool:
    if not rows:
        return False
    return any(_row_inn(row) and inn_equal(_row_inn(row), inn) for row in rows)


def _classify_payload(payload: Any, inn: str) -> ProbeResult:
    if _rows_match_inn(_extract_rows(payload), inn):
        return "found"
    found = _extract_found(payload)
    if found == 0:
        return "not_found"
    rows = _extract_rows(payload)
    if found is not None and found > 0 and rows:
        return "found" if _rows_match_inn(rows, inn) else "not_found"
    if rows:
        return "found" if _rows_match_inn(rows, inn) else "not_found"
    if found is not None:
        return "not_found"
    return "not_found"


async def _fetch_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    referer: str,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any | None:
    headers = _headers(referer, json_body=json_body is not None)
    try:
        async with session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            allow_redirects=True,
            ssl=False,
        ) as resp:
            if resp.status in {404, 405}:
                return None
            if resp.status >= 400:
                logger.debug("Банкротство HTTP %s %s", resp.status, url)
                return None
            text = await resp.text()
            if not text.strip():
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                logger.debug("Банкротство JSON parse failed %s: %s", url, exc)
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.debug("Банкротство request failed %s: %s", url, exc)
        return None
    except Exception as exc:
        logger.debug("Банкротство unexpected error %s: %s", url, exc)
        return None


def _candidate_requests(
    inn: str,
) -> list[tuple[str, str, str, dict[str, str] | None, dict[str, Any] | None]]:
    """method, base, path, params, json_body"""
    requests: list[
        tuple[str, str, str, dict[str, str] | None, dict[str, Any] | None]
    ] = []
    params_variants = [
        {"limit": "15", "offset": "0", "code": inn},
        {"limit": "15", "offset": "0", "searchString": inn},
        {"limit": "15", "offset": "0", "inn": inn},
    ]
    for base in (BANKROT_SITE, FEDRESURS_SITE):
        for params in params_variants:
            requests.append(("GET", base, "/backend/cmpbankrupts", params, None))
        requests.append(
            (
                "POST",
                base,
                "/backend/cmpbankrupts/search",
                None,
                {"entitySearchFilter": _search_filter(inn)},
            )
        )
    return requests


async def _lookup_bankruptcy_inner(inn: str) -> BankruptcyCheck:
    from services.net import ipv4_connector

    connector = ipv4_connector(ssl=False, limit=4)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=REQUEST_TIMEOUT,
        trust_env=False,
    ) as session:

        async def _probe(
            method: str,
            base: str,
            path: str,
            params: dict[str, str] | None,
            json_body: dict[str, Any] | None,
        ) -> ProbeResult:
            payload = await _fetch_json(
                session,
                method,
                f"{base}{path}",
                referer=f"{base}/",
                params=params,
                json_body=json_body,
            )
            if payload is None:
                return "failed"
            return _classify_payload(payload, inn)

        found_hit = False
        not_found_hit = False
        for method, base, path, params, body in _candidate_requests(inn):
            result = await _probe(method, base, path, params, body)
            if result == "found":
                found_hit = True
                break
            if result == "not_found":
                not_found_hit = True

    if found_hit:
        return BankruptcyCheck(found=True, status=STATUS_FOUND, state="found")
    if not_found_hit:
        return BankruptcyCheck(found=False, status=STATUS_NOT_FOUND, state="not_found")
    logger.info("Банкротство: все запросы неудачны inn=%s", inn)
    return BankruptcyCheck(found=False, status=STATUS_UNKNOWN, state="unknown")


async def lookup_bankruptcy(inn: str) -> BankruptcyCheck:
    inn = normalize_inn(inn.strip())
    if not inn.isdigit() or len(inn) not in (10, 12):
        raise BankruptcyError("Нужен корректный ИНН (10 или 12 цифр).")

    cached = _cache.get(inn)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SEC:
        return cached[1]

    try:
        result = await asyncio.wait_for(
            _lookup_bankruptcy_inner(inn),
            timeout=LOOKUP_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.info("Банкротство timeout inn=%s", inn)
        result = BankruptcyCheck(found=False, status=STATUS_UNKNOWN, state="unknown")

    _cache[inn] = (time.monotonic(), result)
    return result
