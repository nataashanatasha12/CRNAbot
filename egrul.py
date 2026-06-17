from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from services.net import ipv4_connector

logger = logging.getLogger("crnabot.egrul")

EGRUL_BASE = "https://egrul.nalog.ru"
POLL_INTERVAL_SEC = 1.5
SEARCH_POLL_ATTEMPTS = 20
PDF_POLL_ATTEMPTS = 40

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{EGRUL_BASE}/index.html",
    "X-Requested-With": "XMLHttpRequest",
}

SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=90, connect=20, sock_read=45)
PDF_TIMEOUT = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)


@dataclass
class EgrulRecord:
    inn: str
    ogrn: str
    name: str
    address: str
    status: str
    reg_date: str
    pdf_token: str
    raw: dict[str, Any]


class EgrulError(Exception):
    pass


def _pick(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _parse_row(row: dict[str, Any]) -> EgrulRecord:
    return EgrulRecord(
        inn=_pick(row, "i", "inn"),
        ogrn=_pick(row, "o", "ogrn"),
        name=_pick(row, "n", "name", "c"),
        address=_pick(row, "a", "address"),
        status=_pick(row, "e", "status"),
        reg_date=_pick(row, "r", "reg_date"),
        pdf_token=_pick(row, "t"),
        raw=row,
    )


async def _open_session(timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
    session = aiohttp.ClientSession(
        timeout=timeout,
        headers=HEADERS,
        connector=ipv4_connector(),
    )
    await session.get(f"{EGRUL_BASE}/index.html")
    return session


async def _read_body(resp: aiohttp.ClientResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in resp.content.iter_chunked(64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


async def _get_pdf_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    retries: int = 3,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return b""
                return await _read_body(resp)
        except (aiohttp.ClientError, ConnectionResetError, asyncio.TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
    if last_error:
        raise last_error
    return b""


async def _request_search_token(session: aiohttp.ClientSession, query: str) -> str:
    data = {
        "vyp3CaptchaToken": "",
        "page": "",
        "query": query,
        "region": "",
        "PreventChromeAutocomplete": "",
    }
    async with session.post(f"{EGRUL_BASE}/", data=data) as resp:
        resp.raise_for_status()
        payload = await resp.json(content_type=None)

    if payload.get("captchaRequired"):
        raise EgrulError(
            "ФНС требует капчу. Откройте выписку вручную: "
            f"{EGRUL_BASE}/index.html"
        )

    token = payload.get("t")
    if not token:
        raise EgrulError("ФНС не выдала токен поиска. Попробуйте позже.")
    return token


async def _poll_search_rows(session: aiohttp.ClientSession, token: str) -> list[dict[str, Any]]:
    for _ in range(SEARCH_POLL_ATTEMPTS):
        await asyncio.sleep(POLL_INTERVAL_SEC)
        async with session.get(f"{EGRUL_BASE}/search-result/{token}") as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        if payload.get("status") == "wait":
            continue

        rows = payload.get("rows") or []
        if rows:
            return rows
        break

    return []


async def search_egrul(query: str) -> list[EgrulRecord]:
    query = query.strip()
    if not query:
        raise EgrulError("Пустой запрос")

    session = await _open_session(SEARCH_TIMEOUT)
    try:
        token = await _request_search_token(session, query)
        rows = await _poll_search_rows(session, token)
        if not rows:
            raise EgrulError(
                f"По запросу «{query}» ничего не найдено в ЕГРЮЛ/ЕГРИП. "
                "Проверьте правильность ИНН (возможна опечатка в цифрах)."
            )
        return [_parse_row(row) for row in rows]
    finally:
        await session.close()


async def _try_direct_pdf(session: aiohttp.ClientSession, token: str) -> bytes | None:
    url = f"{EGRUL_BASE}/vyp-download/{token}"
    logger.info("Пробую прямую загрузку PDF: %s", url[:80])
    data = await _get_pdf_with_retry(session, url)
    if data.startswith(b"%PDF"):
        logger.info("PDF скачан напрямую, размер %s байт", len(data))
        return data
    return None


async def _download_pdf_by_token(session: aiohttp.ClientSession, pdf_token: str) -> bytes:
    direct = await _try_direct_pdf(session, pdf_token)
    if direct:
        return direct

    async with session.get(f"{EGRUL_BASE}/vyp-request/{pdf_token}") as resp:
        if resp.status != 200:
            text = await resp.text()
            raise EgrulError(
                "ФНС отклонила запрос PDF. "
                f"Скачайте вручную: {EGRUL_BASE}/index.html"
                + (f"\nОтвет: {text[:200]}" if text else "")
            )
        payload = await resp.json(content_type=None)

    token = payload.get("t") or pdf_token

    ready = False
    for attempt in range(PDF_POLL_ATTEMPTS):
        await asyncio.sleep(POLL_INTERVAL_SEC)
        async with session.get(f"{EGRUL_BASE}/vyp-status/{token}") as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        status = payload.get("status")
        logger.info("Статус PDF (%s/%s): %s", attempt + 1, PDF_POLL_ATTEMPTS, status)

        if status == "wait":
            continue
        if status == "error":
            raise EgrulError(payload.get("message") or "Ошибка формирования выписки.")
        ready = True
        break

    if not ready:
        raise EgrulError(
            "ФНС долго формирует PDF. Попробуйте через минуту или скачайте на egrul.nalog.ru."
        )

    data = await _get_pdf_with_retry(session, f"{EGRUL_BASE}/vyp-download/{token}")
    if data.startswith(b"%PDF"):
        logger.info("PDF скачан после ожидания, размер %s байт", len(data))
        return data

    raise EgrulError(
        "ФНС вернула не PDF. Попробуйте скачать вручную на egrul.nalog.ru."
    )


async def download_egrul_pdf(query: str) -> bytes:
    """query — ОГРН, ИНН или другой идентификатор для поиска."""
    query = query.strip()
    if not query:
        raise EgrulError("ОГРН/ИНН не указан")

    session = await _open_session(PDF_TIMEOUT)
    try:
        token = await _request_search_token(session, query)
        rows = await _poll_search_rows(session, token)
        if not rows:
            raise EgrulError("Организация не найдена для выписки PDF.")

        pdf_token = rows[0].get("t")
        if not pdf_token:
            raise EgrulError("ФНС не вернула токен для PDF.")

        return await _download_pdf_by_token(session, pdf_token)
    except (aiohttp.ClientError, ConnectionResetError, asyncio.TimeoutError) as exc:
        raise EgrulError(
            "ФНС слишком долго отвечает. Подождите и повторите, "
            "или скачайте выписку на egrul.nalog.ru."
        ) from exc
    finally:
        await session.close()
