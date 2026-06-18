#!/usr/bin/env python3
"""Патч ZIP в web_app.py на сервере — без скачивания всего файла с GitHub."""
from pathlib import Path

TARGET = Path("/opt/crnabot/web_app.py")

OLD_BLOCK = """        except EgrulError:
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
        raise HTTPException(503, "Сайты ФНС/ГИР БО временно недоступны. Повторите через минуту.") from exc"""

NEW_BLOCK = """        except EgrulError as exc:
            logger.info("ZIP egrul pdf failed inn=%s: %s", inn, exc)
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.info("ZIP egrul pdf network inn=%s: %s", inn, exc)
            return None

    async def _fetch_org():
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
        org = None"""

OLD_RESOLVE = """    except (aiohttp.ClientError, asyncio.TimeoutError, EgrulError) as exc:
        logger.info("ZIP resolve names failed inn=%s: %s", inn, exc)
        raise HTTPException(503, "ЕГРЮЛ временно недоступен. Повторите через минуту.") from exc"""

NEW_RESOLVE = """    except Exception as exc:
        logger.info("ZIP resolve names failed inn=%s: %s", inn, exc)
        if org is not None:
            ogrn = (org.ogrn or inn).strip() or inn
            short_name = org.short_name or shorten_company_name(org.name) or org.name or inn
        else:
            ogrn = inn
            short_name = inn"""


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")
    if "ZIP egrul pdf network" in text:
        print("Уже исправлено:", TARGET)
        return
    if OLD_BLOCK not in text:
        raise SystemExit("Не нашёл старый код ZIP — напишите в чат, поможем вручную.")
    text = text.replace(OLD_BLOCK, NEW_BLOCK, 1)
    if OLD_RESOLVE not in text:
        raise SystemExit("Не нашёл блок resolve names.")
    text = text.replace(OLD_RESOLVE, NEW_RESOLVE, 1)
    TARGET.write_text(text, encoding="utf-8")
    print("Готово:", TARGET)


if __name__ == "__main__":
    main()
