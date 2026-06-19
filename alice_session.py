"""
alice_session.py — автоматический захват и обновление сессии веб-Алисы.

Открывает управляемый Chromium (Playwright), грузит alice.yandex.ru и
перехватывает кадр System.SynchronizeState из websocket uniproxy, попутно
читая куки (включая httpOnly Session_id/sessionid2). Браузер открывается
только на момент захвата и тут же закрывается — постоянно открытым не висит.

Логин сохраняется в постоянном профиле .alice_profile/, поэтому логинишься
один раз. Креды кэшируются в .alice_creds.json. Когда токен/кука протухают —
короткий headless-перезахват; если логин умер — всплывает видимое окно.

Найдено эмпирически: auth_token у веб-Алисы — константа приложения (одинакова
для всех), личность задаётся куками. Поэтому главная ценность захвата — куки.

Использование:
    python alice_session.py login   # принудительный видимый вход
    python alice_session.py show     # показать, что в кэше
    import alice_session; await alice_session.get_credentials()
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / ".alice_profile"
CREDS_FILE = ROOT / ".alice_creds.json"

ALICE_URL = "https://alice.yandex.ru/"
WS_MATCH = "uniproxy.alice.yandex.ru"
# Константа приложения (см. docstring). Используется как фолбэк, если кадр
# SynchronizeState почему-то не пойман.
AUTH_TOKEN_FALLBACK = "effd5a3f-fd42-4a18-83a1-61766a6d0924"

# Сколько ждём, пока пользователь залогинится в видимом окне.
LOGIN_TIMEOUT = float(os.environ.get("ALICE_LOGIN_TIMEOUT", "300"))
# Сколько ждём появления кадра SynchronizeState после загрузки.
FRAME_TIMEOUT = float(os.environ.get("ALICE_FRAME_TIMEOUT", "25"))


@dataclass
class Creds:
    auth_token: str
    uuid: str
    icookie: str
    sae_cookie: str
    cookies: str
    logged_in: bool
    captured_at: float

    def short(self) -> str:
        mode = "Pro (залогинен)" if self.logged_in else "Base (аноним)"
        return (f"{mode}; cookies={len(self.cookies)} симв.; "
                f"uuid={self.uuid[:12]}…; снято "
                f"{int((time.time() - self.captured_at) / 60)} мин назад")


# ---------------------------------------------------------------------------
# Кэш / диск / env-override
# ---------------------------------------------------------------------------

_CACHE: Optional[Creds] = None
_LOCK: Optional[asyncio.Lock] = None


def _lock() -> asyncio.Lock:
    # Лениво создаём Lock внутри уже работающего loop (безопасно для asyncio.run).
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


def _env_creds() -> Optional[Creds]:
    """Ручной override: если в окружении заданы ALICE_AUTH_TOKEN — используем его
    и не трогаем браузер вовсе."""
    token = os.environ.get("ALICE_AUTH_TOKEN", "").strip()
    if not token:
        return None
    return Creds(
        auth_token=token,
        uuid=os.environ.get("ALICE_UUID", "").strip(),
        icookie=os.environ.get("ALICE_ICOOKIE", "").strip(),
        sae_cookie=os.environ.get("ALICE_SAE_COOKIE", "").strip(),
        cookies=os.environ.get("ALICE_COOKIES", "").strip(),
        logged_in=bool(os.environ.get("ALICE_COOKIES", "").strip()),
        captured_at=time.time(),
    )


def _load() -> Optional[Creds]:
    if not CREDS_FILE.exists():
        return None
    try:
        data = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[alice_session] не прочитать {CREDS_FILE.name}: {e}", file=sys.stderr)
        return None
    fields = getattr(Creds, "__dataclass_fields__", {})
    known = {k: v for k, v in data.items() if k in fields}
    missing = [k for k in fields if k not in known]
    if missing:
        # схема кред поменялась (старый/новый формат файла) — не молчим, чтобы
        # «постоянно требует логин» не выглядело загадкой
        print(f"[alice_session] {CREDS_FILE.name}: нет полей {missing} — нужен повторный вход",
              file=sys.stderr)
        return None
    try:
        return Creds(**known)
    except Exception as e:
        print(f"[alice_session] неверные креды в {CREDS_FILE.name}: {e}", file=sys.stderr)
        return None


def _save(creds: Creds) -> None:
    CREDS_FILE.write_text(
        json.dumps(asdict(creds), ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Захват через Playwright
# ---------------------------------------------------------------------------

async def _capture(headless: bool, login_timeout: float = 0.0) -> Optional[Creds]:
    """Открывает браузер, ловит SynchronizeState и куки, возвращает Creds.
    login_timeout > 0 — ждём, пока пользователь залогинится (видимое окно)."""
    from playwright.async_api import async_playwright

    sync_payload: dict = {}
    got_frame = asyncio.Event()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1100, "height": 820},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        def on_ws(ws):
            if WS_MATCH not in ws.url:
                return

            def on_sent(payload):
                txt = payload if isinstance(payload, str) else \
                    payload.decode("utf-8", "ignore")
                try:
                    obj = json.loads(txt)
                except Exception:
                    return
                hdr = (obj.get("event") or {}).get("header") or {}
                if hdr.get("name") == "SynchronizeState":
                    sync_payload.clear()
                    sync_payload.update((obj.get("event") or {}).get("payload") or {})
                    got_frame.set()

            ws.on("framesent", on_sent)

        page.on("websocket", on_ws)

        try:
            await page.goto(ALICE_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        # первый кадр (анонимный или уже авторизованный)
        try:
            await asyncio.wait_for(got_frame.wait(), timeout=FRAME_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        async def is_logged_in() -> bool:
            return any(c["name"] == "Session_id" for c in await ctx.cookies())

        # интерактивный вход: ждём, пока появится Session_id, затем перезагружаем
        # страницу, чтобы поймать уже авторизованный SynchronizeState
        if login_timeout > 0 and not await is_logged_in():
            print("  → Залогинься в открывшемся окне Яндекса. Жду…", flush=True)
            waited = 0.0
            while waited < login_timeout and not await is_logged_in():
                await asyncio.sleep(1.5)
                waited += 1.5
            if await is_logged_in():
                got_frame.clear()
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                    await asyncio.wait_for(got_frame.wait(), timeout=FRAME_TIMEOUT)
                except Exception:
                    pass

        cookies = await ctx.cookies()
        logged = any(c["name"] == "Session_id" for c in cookies)
        cookie_str = "; ".join(
            f'{c["name"]}={c["value"]}'
            for c in cookies if c["domain"].endswith("yandex.ru"))

        await ctx.close()

    if not sync_payload and not cookie_str:
        return None

    def cookie_val(name: str) -> str:
        for c in cookies:
            if c["name"] == name:
                return c["value"]
        return ""

    return Creds(
        auth_token=sync_payload.get("auth_token") or AUTH_TOKEN_FALLBACK,
        uuid=sync_payload.get("uuid") or cookie_val("alice_uuid"),
        icookie=sync_payload.get("icookie") or cookie_val("i"),
        sae_cookie=sync_payload.get("sae_cookie") or cookie_val("sae"),
        cookies=cookie_str,
        logged_in=logged,
        captured_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

async def refresh(interactive_ok: bool = True) -> Creds:
    """Перезахватить креды. Сперва тихий headless; если не залогинен и
    interactive_ok — видимое окно для входа. Результат кэшируется и пишется на диск."""
    global _CACHE
    async with _lock():
        creds = await _capture(headless=True)
        if (creds is None or not creds.logged_in) and interactive_ok:
            visible = await _capture(headless=False, login_timeout=LOGIN_TIMEOUT)
            if visible is not None:
                creds = visible
        if creds is None:
            raise RuntimeError(
                "Не удалось получить сессию Алисы через Playwright "
                "(нет кадра SynchronizeState). Проверь сеть и доступ к alice.yandex.ru.")
        _CACHE = creds
        _save(creds)
        return creds


async def relogin() -> Creds:
    """Принудительный повторный вход: ВСЕГДА открывает видимое окно браузера
    (в отличие от refresh, который сперва пробует тихий headless). Результат
    кэшируется и пишется на диск."""
    global _CACHE
    async with _lock():
        creds = await _capture(headless=False, login_timeout=LOGIN_TIMEOUT)
        if creds is None:
            raise RuntimeError(
                "Не удалось получить сессию Алисы через Playwright "
                "(нет кадра SynchronizeState). Проверь сеть и доступ к alice.yandex.ru.")
        _CACHE = creds
        _save(creds)
        return creds


def relogin_sync() -> Creds:
    return asyncio.run(relogin())


async def get_credentials(force: bool = False) -> Creds:
    """Лучшие известные креды: env-override → кэш → диск → захват."""
    global _CACHE
    env = _env_creds()
    if env is not None:
        return env
    if not force and _CACHE is not None:
        return _CACHE
    disk = _load()
    if not force and disk is not None:
        _CACHE = disk
        return disk
    return await refresh(interactive_ok=True)


async def ensure(prefer_login: bool = True) -> Creds:
    """Старт сессии: если нет залогиненных кред — открыть окно для входа.
    Если пользователь не залогинился — деградируем до Base (что поймали)."""
    env = _env_creds()
    if env is not None:
        return env
    disk = _load()
    if disk is not None and disk.logged_in:
        global _CACHE
        _CACHE = disk
        return disk
    return await refresh(interactive_ok=prefer_login)


def ensure_sync(prefer_login: bool = True) -> Creds:
    return asyncio.run(ensure(prefer_login=prefer_login))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    if cmd == "show":
        c = _load()
        print(c.short() if c else "Кэш пуст (.alice_creds.json нет).")
        return
    if cmd == "login":
        print("Открываю браузер для входа в Яндекс…")
        creds = asyncio.run(refresh(interactive_ok=True))
        print("Готово:", creds.short())
        return
    print(f"Неизвестная команда: {cmd}. Доступно: login | show")


if __name__ == "__main__":
    _main()
