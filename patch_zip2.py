#!/usr/bin/env python3
"""Патч ZIP v2 — сначала ГИР БО, потом ЕГРЮЛ; защита от HTTP 500."""
from pathlib import Path

TARGET = Path("/opt/crnabot/web_app.py")

REPLACEMENTS: list[tuple[str, str]] = [
    (
        """    async def _fetch_egrul() -> bytes | None:
        if not (want_egrul or want_reporting or want_casebook):
            return None""",
        """    async def _fetch_egrul() -> bytes | None:
        if not want_egrul:
            return None""",
    ),
    (
        """    async def _fetch_org():
        if not (want_reporting or want_egrul):
            return None
        try:
            return await asyncio.wait_for(
                get_organization(inn, skip_playwright_okved=True),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            logger.info("ZIP GIR BO org fetch timeout inn=%s", inn)
            return None
        except GirBoError as exc:
            logger.info("ZIP GIR BO org fetch failed inn=%s: %s", inn, exc)
            return None

    egrul_pdf, org = await asyncio.gather(_fetch_egrul(), _fetch_org(), return_exceptions=True)
    if isinstance(egrul_pdf, BaseException):
        logger.info("ZIP egrul fetch error inn=%s: %s", inn, egrul_pdf)
        egrul_pdf = None
    if isinstance(org, BaseException):
        logger.info("ZIP girbo fetch error inn=%s: %s", inn, org)
        org = None""",
        """    async def _fetch_org():
        if not (want_reporting or want_egrul or want_casebook):
            return None
        try:
            return await asyncio.wait_for(
                get_organization(inn, skip_playwright_okved=True),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.info("ZIP GIR BO org fetch timeout inn=%s", inn)
            return None
        except GirBoError as exc:
            logger.info("ZIP GIR BO org fetch failed inn=%s: %s", inn, exc)
            return None
        except Exception as exc:
            logger.info("ZIP GIR BO org fetch error inn=%s: %s", inn, exc)
            return None

    org = await _fetch_org()
    if org is None and (want_reporting or want_egrul or want_casebook):
        logger.info("ZIP GIR BO org retry inn=%s", inn)
        org = await _fetch_org()

    egrul_pdf = await _fetch_egrul()
    if isinstance(egrul_pdf, BaseException):
        logger.info("ZIP egrul fetch error inn=%s: %s", inn, egrul_pdf)
        egrul_pdf = None""",
    ),
    (
        """        identity: dict[str, str | None] = {
            "name": (egrul_parsed.get("name") or "").strip() or None,
            "address": (egrul_parsed.get("address") or "").strip() or None,
            "ogrn": (egrul_parsed.get("ogrn") or "").strip() or None,
            "short_name": (egrul_parsed.get("short_name") or "").strip() or None,
        }
        if not identity["short_name"] and identity["name"]:""",
        """        identity: dict[str, str | None] = {
            "name": (egrul_parsed.get("name") or "").strip() or None,
            "address": (egrul_parsed.get("address") or "").strip() or None,
            "ogrn": (egrul_parsed.get("ogrn") or "").strip() or None,
            "short_name": (egrul_parsed.get("short_name") or "").strip() or None,
        }
        if org:
            identity["name"] = identity["name"] or org.name
            identity["ogrn"] = identity["ogrn"] or org.ogrn
            identity["short_name"] = identity["short_name"] or org.short_name
        if not identity["short_name"] and identity["name"]:""",
    ),
    (
        """        if girbo_task:
            girbo_entries = await girbo_task
            for arcname, data in girbo_entries:
                zf.writestr(zip_entry_path(zip_folder, arcname), data)

        if casebook_task:
            for arcname, data in await casebook_task:
                zf.writestr(zip_entry_path(zip_folder, arcname), data)

    buffer.seek(0)
    if buffer.getbuffer().nbytes < 100:
        raise HTTPException(404, "Не удалось скачать ни одного файла.")""",
        """        if girbo_task:
            try:
                girbo_entries = await girbo_task
            except Exception as exc:
                logger.exception("ZIP girbo task failed inn=%s: %s", inn, exc)
                girbo_entries = []
            for arcname, data in girbo_entries:
                zf.writestr(zip_entry_path(zip_folder, arcname), data)

        if casebook_task:
            try:
                casebook_entries = await casebook_task
            except Exception as exc:
                logger.exception("ZIP casebook task failed inn=%s: %s", inn, exc)
                casebook_entries = []
            for arcname, data in casebook_entries:
                zf.writestr(zip_entry_path(zip_folder, arcname), data)

    buffer.seek(0)
    zip_size = buffer.getbuffer().nbytes
    if zip_size < 100:
        logger.info(
            "ZIP empty inn=%s want_egrul=%s want_reporting=%s want_casebook=%s org=%s egrul_pdf=%s",
            inn,
            want_egrul,
            want_reporting,
            want_casebook,
            bool(org),
            bool(egrul_pdf),
        )
        raise HTTPException(404, "Не удалось скачать ни одного файла.")""",
    ),
]


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")
    if "ZIP GIR BO org retry inn=%s" in text:
        print("Уже v2:", TARGET)
        return
    if "ZIP egrul pdf network" not in text:
        raise SystemExit("Сначала запустите patch_zip.py (v1).")
    for idx, (old, new) in enumerate(REPLACEMENTS, start=1):
        if old not in text:
            raise SystemExit(f"Блок {idx} не найден — напишите в чат.")
        text = text.replace(old, new, 1)
    TARGET.write_text(text, encoding="utf-8")
    print("Готово v2:", TARGET)


if __name__ == "__main__":
    main()
