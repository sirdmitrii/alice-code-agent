"""
alice_adapter.py
Локальный OpenAI-совместимый адаптер поверх веб-Алисы (протокол uniproxy).

Идея: агент (Claude-Code-аналог) ходит в обычный POST /v1/chat/completions,
а этот сервис превращает запрос в сессию WebSocket с uniproxy-шлюзом Алисы:
    connect ws  ->  System.SynchronizeState (auth_token)  ->  Vins.TextInput (текст)
                <-  DeferredAliceResponse ... base_response.text  (копится прогрессивно)
                <-  DeferredAliceResponse ... is_last: true        (конец потока)

Весь "костыль" авторизации заперт здесь. Поля берутся из devtools браузера:
открой чат Алисы -> F12 -> Network -> WS -> соединение uni.ws -> вкладка Messages.
Стартовый кадр SynchronizeState (↑) содержит auth_token / uuid / icookie.

Запуск:
    pip install fastapi uvicorn httpx websockets
    python alice_adapter.py

Указатель для агента:
    base_url = http://127.0.0.1:8787/v1
    api_key  = любой непустой (адаптер его игнорирует)
    model    = alice
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Any, AsyncIterator, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from websockets.asyncio.client import connect as ws_connect

import alice_session

# ============================================================================
# КОНФИГ. Секреты сессии (auth_token / куки / uuid / icookie / sae) больше не
# читаются здесь — их добывает и обновляет alice_session (Playwright-захват),
# либо они берутся из env как ручной override. Ниже — только не-секретные настройки.
# ============================================================================

# Шлюз uniproxy (обычно неизменен)
WS_URL = os.environ.get("ALICE_WS_URL", "wss://uniproxy.alice.yandex.ru/uni.ws")

# Необязательно: переиспользовать конкретный диалог. Пусто = новый на каждый вызов.
DIALOG_ID_FIXED = os.environ.get("ALICE_DIALOG_ID", "")

# Режим Алисы 2.0: "Pro" или "" (обычный). Из active_chat_dialog_context.
ALICE_MODE = os.environ.get("ALICE_MODE", "Pro")

ORIGIN = "https://alice.yandex.ru"
USER_AGENT = os.environ.get(
    "ALICE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 YaBrowser/26.4.0.0 Safari/537.36",
)
SPEECHKIT_VERSION = os.environ.get("ALICE_SPEECHKIT_VERSION", "4.16.7")

MODEL_NAME = os.environ.get("ALICE_MODEL_NAME", "alice")
PORT = int(os.environ.get("PORT", "8787"))
REQUEST_TIMEOUT = float(os.environ.get("ALICE_TIMEOUT", "120"))

# Списки из реального запроса. Влияют на режим (standalone_alice_2_0) и стриминг.
EXPERIMENTS = [
    "read_dialogs_for_unauthorized_users", "mm_allow_anonymous_request",
    "dont_skip_cancel_requests", "enable_parallel_requests_to_chats",
    "enable_external_skills_for_webdesktop_and_webtouch",
    "send_show_view_directive_on_supports_show_view_layer_content_interface",
    "standalone_alice_2_0", "mm_enable_protocol_scenario=WebAliceControls",
    "exp_flag_chat_dialog_history", "exp_flag_chat_dialog_history_main_context_save",
    "div2cards_in_external_skills_for_web_standalone", "enable_find_poi_standalone",
    "use_server_pings", "enable_onboarding_adaptive_size",
    "standalone_show_fullscreen_image_gallery_directive",
    "draw_picture_enable_controls", "alice_has_borders_div_paddings",
    "enable_new_colors_for_alice_chat",
    "erase_serialized_response_from_json_deferred_alice_response",
    "skills_standalone_use_div_render", "standalone_skill_card_cloud_ui",
]
SUPPORTED_FEATURES = [
    "background_response_streaming", "supports_bso_answer", "open_link",
    "server_action", "div2_cards", "supports_streaming_response",
    "supports_rich_json_cards", "supports_markdown_response",
    "print_text_in_message_view", "show_loader_directive",
    "supports_default_dialog_as_dedicated", "supports_multi_model_dialogs",
    "supports_unlimited_dialogs_creation",
]


# ============================================================================
# WebSocket-клиент к Алисе (uniproxy).
# ============================================================================

class AliceClient:
    def _headers(self, creds: "alice_session.Creds") -> dict[str, str]:
        h = {"Origin": ORIGIN, "User-Agent": USER_AGENT}
        if creds.cookies:
            h["Cookie"] = creds.cookies
        return h

    def _now(self) -> tuple[str, str]:
        ts = int(time.time())
        client_time = time.strftime("%Y%m%dT%H%M%S", time.gmtime(ts + 3 * 3600))
        return client_time, str(ts)

    def _sync_state(self, creds: "alice_session.Creds") -> dict[str, Any]:
        return {"event": {
            "header": {"namespace": "System", "name": "SynchronizeState",
                       "seqNumber": 1, "messageId": str(uuid.uuid4())},
            "payload": {
                "auth_token": creds.auth_token,
                "uuid": creds.uuid,
                "vins": {"application": {
                    "app_id": "ru.yandex.webstandalone.desktop",
                    "platform": "windows", "device_id": creds.uuid}},
                "supported_features": SUPPORTED_FEATURES,
                "request": {"experiments": EXPERIMENTS},
                "speechkitVersion": SPEECHKIT_VERSION,
                "icookie": creds.icookie,
                "sae_cookie": creds.sae_cookie,
                "yexp_cookie": "",
            }}}

    def _text_input(self, creds: "alice_session.Creds", text: str,
                    request_id: str, dialog_id: str) -> dict[str, Any]:
        client_time, ts = self._now()
        return {"event": {
            "header": {"namespace": "Vins", "name": "TextInput",
                       "seqNumber": 2, "messageId": str(uuid.uuid4())},
            "payload": {
                "application": {
                    "app_id": "ru.yandex.webstandalone.desktop",
                    "app_version": "unknown", "platform": "windows",
                    "os_version": USER_AGENT.lower(),
                    "uuid": creds.uuid, "device_id": creds.uuid,
                    "lang": "ru-RU", "client_time": client_time,
                    "timezone": "Europe/Moscow", "timestamp": ts},
                "header": {"request_id": request_id, "dialog_id": dialog_id,
                           "dialog_type": 2},
                "request": {
                    "event": {"type": "text_input", "text": text},
                    "voice_session": False,
                    "experiments": EXPERIMENTS,
                    "additional_options": {
                        "bass_options": {"user_agent": USER_AGENT,
                                         "screen_scale_factor": 1},
                        "origin_domain": "yandex.ru",
                        "supported_features": SUPPORTED_FEATURES,
                        "unsupported_features": [],
                        "icookie": creds.icookie},
                },
                "format": "audio/ogg;codecs=opus",
                "mime": "audio/webm;codecs=opus",
                "topic": "desktopgeneral", "punctuation": False,
                "alice_2_settings": {"preset": "", "mode": ALICE_MODE},
            }}}

    @staticmethod
    def _text_from_directive(payload: dict[str, Any]) -> Optional[str]:
        """Достаёт текст из DeferredAliceResponse (или VinsResponse-фолбэк)."""
        jr = payload.get("json_response")
        if isinstance(jr, dict):
            base = jr.get("base_response") or {}
            txt = base.get("text")
            if isinstance(txt, str) and txt:
                return txt
            for c in base.get("cards", []) or []:
                tc = (c or {}).get("text_card") or {}
                if isinstance(tc.get("text"), str) and tc["text"]:
                    return tc["text"]
        resp = payload.get("response")
        if isinstance(resp, dict):
            card = resp.get("card")
            if isinstance(card, dict) and isinstance(card.get("text"), str):
                return card["text"]
        return None

    async def _roundtrip(self, creds: "alice_session.Creds", prompt: str,
                         dialog_id: Optional[str] = None) -> str:
        dialog_id = dialog_id or DIALOG_ID_FIXED or str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        async with ws_connect(
            WS_URL, additional_headers=self._headers(creds),
            max_size=None, open_timeout=20, close_timeout=5,
        ) as ws:
            await ws.send(json.dumps(self._sync_state(creds), ensure_ascii=False))
            await ws.send(json.dumps(
                self._text_input(creds, prompt, request_id, dialog_id),
                ensure_ascii=False))
            return await self._read_response(ws)

    async def complete(self, prompt: str, dialog_id: Optional[str] = None) -> str:
        creds = await alice_session.get_credentials()
        try:
            return await self._roundtrip(creds, prompt, dialog_id)
        except HTTPException:
            # Возможно протухли токен/кука — тихо (headless) обновляем и повторяем.
            try:
                creds = await alice_session.refresh(interactive_ok=False)
            except Exception as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"Сессия Алисы недействительна и не обновилась "
                           f"автоматически: {e}. Перезапусти и залогинься заново.")
            return await self._roundtrip(creds, prompt, dialog_id)

    async def _read_response(self, ws) -> str:
        """Собирает текст из кадров. Работает в двух режимах:
        - Pro: поток DeferredAliceResponse, конец по json_response.is_last;
        - Base: синхронный VinsResponse с response.card.text, без is_last.
        Пока текста нет — ждём до REQUEST_TIMEOUT. Получив текст: в Base (нет
        is_last) возвращаем после короткой паузы; в Pro (поток) ждём is_last с
        большим idle, чтобы не обрезать длинный ответ на паузах генерации."""
        BASE_IDLE = 4.0      # Base: ответ синхронный, после него можно выходить
        STREAM_IDLE = 30.0   # Pro: пауза между чанками бывает большой — терпим
        latest = ""
        saw_stream = False
        last_frame_preview = ""
        while True:
            if not latest:
                timeout = REQUEST_TIMEOUT
            else:
                timeout = STREAM_IDLE if saw_stream else BASE_IDLE
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            except Exception:  # ConnectionClosed и пр.
                break

            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            last_frame_preview = (raw if isinstance(raw, str) else str(raw))[:600]

            directive = frame.get("directive") or {}
            header = directive.get("header") or {}
            name = header.get("name")
            payload = directive.get("payload") or {}

            if name in ("DeferredAliceResponse", "VinsResponse"):
                jr = payload.get("json_response") or {}
                # DeferredAliceResponse = потоковый ответ Pro (копится до is_last)
                if name == "DeferredAliceResponse":
                    saw_stream = True
                txt = self._text_from_directive(payload)
                if txt:
                    latest = txt
                if jr.get("is_last"):
                    return latest

        if latest:
            return latest
        raise HTTPException(
            status_code=502,
            detail="Поток Алисы закончился без текста ответа. Возможно, протух "
                   f"auth_token или куки. Последний кадр: {last_frame_preview}")


alice = AliceClient()


# ============================================================================
# OpenAI-сообщения -> один промпт + синтез tool_calls.
# Здесь компенсируется отсутствие native function calling: инструменты
# описываем в промпте, текстовый ответ парсим обратно в OpenAI tool_calls.
# Это самая хрупкая часть — тюнится под поведение модели.
# ============================================================================

# Фенс-блок (с языковым тегом или без) — внутри него JSON-вызов.
FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)

# Блок «тела» аргумента: строка @@ключ@@ ... строка @@end@@ (или конец текста).
# Тело берётся дословно — без экранирования и без конфликта с ```.
BODY_RE = re.compile(
    r"^@@(?!end@@)([A-Za-z_]\w*)@@[ \t]*\r?\n(.*?)(?:^@@end@@[ \t]*\r?$|\Z)",
    re.DOTALL | re.MULTILINE)

# Плейсхолдер тела внутри JSON: значение аргумента вида "@@ключ@@".
PLACEHOLDER_RE = re.compile(r"^@@([A-Za-z_]\w*)@@$")

# Тело целиком в одном ```-фенсе: модель часто оборачивает код, хотя не должна.
WRAP_RE = re.compile(r"\A```[^\n`]*\r?\n(.*?)\r?\n?```\s*\Z", re.DOTALL)

# Типографские кавычки -> ASCII (модель иногда подставляет их в JSON).
_QUOTE_FIX = {0x201c: '"', 0x201d: '"', 0x2018: "'", 0x2019: "'",
              0x00ab: '"', 0x00bb: '"'}


def _looks_like_call(obj: Any) -> bool:
    return (isinstance(obj, dict)
            and isinstance(obj.get("name"), str) and bool(obj.get("name"))
            and isinstance(obj.get("arguments"), dict))


def _loads_tolerant(s: str) -> Any:
    """Терпимый json.loads: чинит типографские кавычки и висячие запятые."""
    s = s.strip().translate(_QUOTE_FIX)
    for candidate in (s, re.sub(r",(\s*[}\]])", r"\1", s)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _extract_bodies(text: str) -> tuple[dict[str, list[str]], list[tuple[int, int]]]:
    """Достаёт блоки @@ключ@@…@@end@@ -> {ключ: [тела по порядку]} и их позиции.
    Список (а не одно значение) нужен для нескольких вызовов с одинаковым ключом
    (напр. два write_file, оба с content). Перевод строки перед @@end@@ — разделитель."""
    bodies: dict[str, list[str]] = {}
    spans: list[tuple[int, int]] = []
    for m in BODY_RE.finditer(text):
        body = m.group(2)
        if body.endswith("\n"):
            body = body[:-1]
        if body.endswith("\r"):
            body = body[:-1]
        wrap = WRAP_RE.match(body)  # снять обёртку ```…``` целиком, если есть
        if wrap:
            body = wrap.group(1)
        bodies.setdefault(m.group(1), []).append(body)
        spans.append(m.span())
    return bodies, spans


def _resolve_bodies(call: dict[str, Any], bodies: dict[str, list[str]]) -> dict[str, Any]:
    """Подставляет тела вместо плейсхолдеров "@@ключ@@". Тела для одного ключа
    раздаются ПО ПОРЯДКУ (первый вызов — первое тело, и т.д.), поэтому несколько
    вызовов с одинаковым ключом не перетирают друг друга."""
    args = call.get("arguments") or {}
    for k, v in list(args.items()):
        if isinstance(v, str):
            ph = PLACEHOLDER_RE.match(v.strip())
            if ph and bodies.get(ph.group(1)):
                args[k] = bodies[ph.group(1)].pop(0)
    return call


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text.strip()
    out, prev = [], 0
    for s, e in sorted(spans):
        if s >= prev:
            out.append(text[prev:s])
            prev = e
    out.append(text[prev:])
    return "".join(out).strip()


def _mk_call(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {
            "name": obj["name"],
            "arguments": json.dumps(obj.get("arguments", {}), ensure_ascii=False),
        },
    }


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # мультимодальные части OpenAI
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def build_tool_instructions(tools: list[dict[str, Any]]) -> str:
    lines = [
        "Тебе доступны инструменты. Чтобы ВЫПОЛНИТЬ действие (создать/изменить файл,",
        "запустить команду), нельзя просто показать код в ответе — нужно вызвать",
        "инструмент. Вызов — отдельный блок строго такого вида:",
        "```tool_call",
        '{"name": "имя", "arguments": {"параметр": "значение"}}',
        "```",
        "Внутри блока — только корректный JSON (поля name и arguments), без пояснений.",
        "",
        "ВАЖНО про большой текст (содержимое файла в content, а также old/new у",
        "edit_file): НЕ вставляй его прямо в JSON. Вместо значения поставь плейсхолдер",
        '"@@имяполя@@", а сам текст приведи ПОСЛЕ блока — дословно, между строками',
        "@@имяполя@@ и @@end@@. Тело приводи КАК ЕСТЬ, без обрамления тройными",
        "кавычками ``` — это содержимое файла, а не блок кода для показа. Тогда не",
        "нужно экранировать кавычки и переводы строк. Пример записи файла:",
        "```tool_call",
        '{"name": "write_file", "arguments": {"path": "app.py", "content": "@@content@@"}}',
        "```",
        "@@content@@",
        "import sys",
        'print("привет")',
        "@@end@@",
        "",
        "Пример правки (два тела — по именам аргументов old и new):",
        "```tool_call",
        '{"name": "edit_file", "arguments": {"path": "app.py", "old": "@@old@@", "new": "@@new@@"}}',
        "```",
        "@@old@@",
        "def foo(): pass",
        "@@end@@",
        "@@new@@",
        "def foo(): return 42",
        "@@end@@",
        "",
        "Простые инструменты (read_file, list_dir, glob, grep, run_command) вызывай",
        "обычным JSON без тел. Если инструмент не нужен — отвечай обычным текстом.",
        "Доступные инструменты:",
    ]
    for t in tools:
        fn = t.get("function", t)
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = json.dumps(fn.get("parameters", {}), ensure_ascii=False)
        lines.append(f"- {name}: {desc}\n  параметры (JSON Schema): {params}")
    return "\n".join(lines)


def _abbrev_args(arguments: str, threshold: int = 500) -> str:
    """Компактно рендерит аргументы прошлого вызова: длинные строки (тело файла и
    т.п.) заменяет на «<N символов>», чтобы не раздувать историю. Точный текст
    модель при необходимости перечитает через read_file."""
    try:
        d = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return (arguments or "")[:threshold]
    if not isinstance(d, dict):
        return str(d)[:threshold]
    out = []
    for k, v in d.items():
        if isinstance(v, str) and len(v) > threshold:
            out.append(f"{k}=<{len(v)} символов>")
        else:
            out.append(f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}")
    return ", ".join(out)


def render_messages_to_prompt(
    messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]]
) -> str:
    parts: list[str] = []

    system_chunks = [
        _content_to_text(m.get("content"))
        for m in messages
        if m.get("role") == "system"
    ]
    if system_chunks:
        parts.append("[Системные инструкции]\n" + "\n".join(system_chunks))
    if tools:
        parts.append(build_tool_instructions(tools))

    id_to_name: dict[str, str] = {}
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            parts.append("[Пользователь]\n" + _content_to_text(m.get("content")))
        elif role == "assistant":
            txt = _content_to_text(m.get("content"))
            if txt:
                parts.append("[Ассистент]\n" + txt)
            for tc in m.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                id_to_name[tc.get("id")] = name
                parts.append(f"[Ассистент вызвал] {name}({_abbrev_args(fn.get('arguments', ''))})")
        elif role == "tool":
            name = id_to_name.get(m.get("tool_call_id"), "")
            head = f"[Результат: {name}]" if name else "[Результат инструмента]"
            parts.append(head + "\n" + _content_to_text(m.get("content")))

    parts.append("[Ассистент]")
    return "\n\n".join(parts)


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Текст ответа -> (чистый_текст, [tool_calls]).

    Структура вызова — JSON в фенсе (тег любой; распознаём по полям name+arguments).
    Объёмные аргументы (content/old/new) могут приходить телом @@ключ@@…@@end@@ вне
    JSON (без экранирования и без конфликта с ```), а в JSON стоять плейсхолдером
    "@@ключ@@". JSON парсится терпимо. Если тело пришло прямо в JSON (старый формат)
    — тоже работает. Обычный код в ```python вызовом не считается."""
    bodies, body_spans = _extract_bodies(text)

    tool_calls: list[dict[str, Any]] = []
    call_spans: list[tuple[int, int]] = []
    for m in FENCE_RE.finditer(text):
        obj = _loads_tolerant(m.group(1))
        candidates = obj if isinstance(obj, list) else [obj]
        matched = [c for c in candidates if _looks_like_call(c)]
        if matched:
            for c in matched:
                tool_calls.append(_mk_call(_resolve_bodies(c, bodies)))
            call_spans.append(m.span())

    if tool_calls:
        return _strip_spans(text, call_spans + body_spans), tool_calls

    # фолбэк: весь ответ — голый JSON-вызов без фенса
    obj = _loads_tolerant(text)
    if _looks_like_call(obj):
        return "", [_mk_call(_resolve_bodies(obj, bodies))]
    return text.strip(), []


# ============================================================================
# OpenAI-совместимые ответы
# ============================================================================

def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def make_completion(text: str) -> dict[str, Any]:
    clean, tool_calls = parse_tool_calls(text)
    message: dict[str, Any] = {"role": "assistant", "content": clean or None}
    finish = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish = "tool_calls"
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def stream_completion(text: str) -> AsyncIterator[str]:
    cid = _completion_id()
    created = int(time.time())

    def chunk(delta: dict[str, Any], finish: Optional[str] = None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": MODEL_NAME,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    clean, tool_calls = parse_tool_calls(text)
    yield chunk({"role": "assistant"})

    if tool_calls:
        delta_calls = [{"index": i, **tc} for i, tc in enumerate(tool_calls)]
        yield chunk({"tool_calls": delta_calls})
        yield chunk({}, finish="tool_calls")
    else:
        # Имитируем стрим, нарезая готовый текст (апстрим собираем целиком).
        for piece in re.findall(r"\S+\s*", clean):
            yield chunk({"content": piece})
        yield chunk({}, finish="stop")

    yield "data: [DONE]\n\n"


# ============================================================================
# FastAPI
# ============================================================================

app = FastAPI(title="Alice OpenAI-compatible adapter (uniproxy)")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "yandex"}
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages пуст")
    tools = body.get("tools")
    stream = bool(body.get("stream", False))

    prompt = render_messages_to_prompt(messages, tools)
    # dialog_id передаёт агент (extra_body) — чтобы вся сессия шла в один диалог
    dialog_id = body.get("dialog_id")
    text = await alice.complete(prompt, dialog_id=dialog_id)  # один ws-роундтрип

    if stream:
        return StreamingResponse(
            stream_completion(text), media_type="text/event-stream"
        )
    return JSONResponse(make_completion(text))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
