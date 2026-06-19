"""
agent.py — консольный кодовый агент (аналог Claude Code) поверх Alice.

Сам поднимает alice_adapter.py, ждёт его готовности и запускает чат-REPL
с инструментами для работы с файлами и shell. Запускается через run.bat.
"""

from __future__ import annotations

import fnmatch
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

import alice_session

# На Windows консоль может быть не в UTF-8 (cp1251/cp866) — тогда rich падает
# на символах → ↳ и рамках. Принудительно переключаем потоки на UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()
ROOT = Path(__file__).resolve().parent

# Ввод через prompt_toolkit: bracketed paste включён по умолчанию, поэтому
# многострочная вставка (Ctrl+V) попадает в буфер целиком — переводы строк
# внутри вставки не отправляют сообщение, отправка по Enter. Плюс история по
# стрелкам и нормальное редактирование строки.
# Сессию создаём лениво: на импорте консоли может не быть (падает на ряде
# окружений), а нужна она только в момент реального ввода.
_input_session: Optional[PromptSession] = None


def read_user_input() -> str:
    """Прочитать ввод пользователя. Поддерживает многострочную вставку."""
    global _input_session
    if _input_session is None:
        _input_session = PromptSession(history=InMemoryHistory())
    return _input_session.prompt(ANSI("\x1b[1;32m› \x1b[0m"))


# ---------------------------------------------------------------------------
# Конфиг / .env
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_dotenv(ROOT / ".env")

PORT = int(os.environ.get("PORT", "8787"))
MODEL = os.environ.get("ALICE_MODEL_NAME", "alice")
BASE_URL = f"http://127.0.0.1:{PORT}/v1"
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR") or os.getcwd()).resolve()
# Необязательная команда проверки после правок (тесты/линт/сборка). Пусто = выкл.
VERIFY_CMD = os.environ.get("ALICE_VERIFY_CMD", "").strip()


# ---------------------------------------------------------------------------
# Инструменты (изолированы рабочей папкой)
# ---------------------------------------------------------------------------

def _safe_path(rel: str) -> Path:
    p = (PROJECT_DIR / rel).resolve()
    if p != PROJECT_DIR and PROJECT_DIR not in p.parents:
        raise ValueError(f"Путь вне рабочей папки: {rel}")
    return p


def tool_read_file(path: str, start: Optional[int] = None,
                   end: Optional[int] = None) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"Файл не найден: {path}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    s = max(1, int(start)) if start else 1
    e = min(total, int(end)) if end else total
    out, size = [], 0
    for i in range(s, e + 1):
        row = f"{i}\t{lines[i - 1]}"  # номер строки + tab + текст
        out.append(row)
        size += len(row) + 1
        if size > 60000:
            out.append(f"... [обрезано на строке {i} из {total}; уточни start/end]")
            break
    return "\n".join(out) or "(пусто или диапазон вне файла)"


def tool_write_file(path: str, content: str) -> str:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Записано: {path} ({len(content)} символов)"


def tool_edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"Файл не найден: {path}"
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        return ("Фрагмент `old` не найден — нужно точное совпадение, включая отступы. "
                "Прочитай файл через read_file и скопируй фрагмент дословно. "
                f"В файле {len(text.splitlines())} строк.")
    if n > 1 and not replace_all:
        return (f"Фрагмент `old` встречается {n} раз. Добавь контекста для уникальности "
                "или передай replace_all=true, чтобы заменить все.")
    text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    return f"Отредактировано: {path}" + (f" ({n} замен)" if replace_all and n > 1 else "")


def tool_list_dir(path: str = ".") -> str:
    p = _safe_path(path)
    if not p.is_dir():
        return f"Не директория: {path}"
    rows = [("[d] " if i.is_dir() else "[f] ") + i.name for i in sorted(p.iterdir())]
    return "\n".join(rows) or "(пусто)"


def tool_run_command(command: str) -> str:
    try:
        r = subprocess.run(
            command, shell=True, cwd=PROJECT_DIR,
            capture_output=True, text=True, timeout=120, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "Команда превысила таймаут 120с."
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip() or "(нет вывода)"
    if len(out) > 30000:
        out = out[:30000] + "\n... [обрезано]"
    return f"exit={r.returncode}\n{out}"


# Папки-шум, которые поиск пропускает
_NOISE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
               ".alice_profile", ".alice_sessions", ".idea", ".mypy_cache",
               ".pytest_cache", ".ruff_cache", "dist", "build"}


def _is_noise(rel: Path) -> bool:
    return any(part in _NOISE_DIRS for part in rel.parts)


def tool_glob(pattern: str, path: str = ".") -> str:
    base = _safe_path(path)
    if not base.is_dir():
        return f"Не директория: {path}"
    rows = []
    for m in sorted(base.glob(pattern)):
        rel = m.relative_to(PROJECT_DIR)
        if _is_noise(rel):
            continue
        rows.append(("[d] " if m.is_dir() else "") + str(rel).replace("\\", "/"))
        if len(rows) >= 500:
            rows.append("... [обрезано: >500 совпадений]")
            break
    return "\n".join(rows) or "(ничего не найдено)"


def tool_grep(pattern: str, path: str = ".", include: str = None,
              ignore_case: bool = False) -> str:
    base = _safe_path(path)
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"Неверное регулярное выражение: {e}"
    files = [base] if base.is_file() else [
        f for f in base.rglob("*") if f.is_file()]
    hits, scanned = [], 0
    for f in files:
        rel = f.relative_to(PROJECT_DIR)
        if _is_noise(rel):
            continue
        if include and not fnmatch.fnmatch(f.name, include):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            data = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "\x00" in data[:1024]:  # похоже на бинарник — пропускаем
            continue
        scanned += 1
        relstr = str(rel).replace("\\", "/")
        for i, line in enumerate(data.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{relstr}:{i}: {line.strip()[:300]}")
                if len(hits) >= 200:
                    hits.append("... [обрезано: показаны первые 200 совпадений]")
                    return "\n".join(hits)
    if not hits:
        return f"Совпадений не найдено (просмотрено файлов: {scanned})."
    return "\n".join(hits)


TOOLS_IMPL = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_dir": tool_list_dir,
    "glob": tool_glob,
    "grep": tool_grep,
    "run_command": tool_run_command,
}
DANGEROUS = {"write_file", "edit_file", "run_command"}  # требуют подтверждения

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Прочитать текстовый файл (с номерами строк). Можно указать "
                       "диапазон строк start/end (1-based, включительно).",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "start": {"type": "integer"},
                                      "end": {"type": "integer"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Создать или перезаписать файл.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Заменить фрагмент `old` на `new` в файле (old — дословно, как в "
                       "read_file без номеров строк). По умолчанию old должен быть "
                       "уникальным; replace_all=true заменяет все вхождения.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "old": {"type": "string"},
                                      "new": {"type": "string"},
                                      "replace_all": {"type": "boolean"}},
                       "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "Показать содержимое директории.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "glob",
        "description": "Найти файлы по маске относительно рабочей папки. "
                       "Поддерживает ** (например, '**/*.py', 'src/**/test_*.py').",
        "parameters": {"type": "object",
                       "properties": {"pattern": {"type": "string"},
                                      "path": {"type": "string",
                                               "description": "подпапка, по умолчанию ."}},
                       "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Поиск по содержимому файлов (регулярное выражение). Возвращает "
                       "строки в формате путь:номер: текст. Пропускает бинарники и "
                       "служебные папки.",
        "parameters": {"type": "object",
                       "properties": {
                           "pattern": {"type": "string", "description": "regex"},
                           "path": {"type": "string", "description": "файл или подпапка, по умолчанию ."},
                           "include": {"type": "string", "description": "фильтр по имени файла, напр. '*.py'"},
                           "ignore_case": {"type": "boolean"}},
                       "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Выполнить команду в shell (Windows cmd) внутри рабочей папки.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
]

SYSTEM_PROMPT = (
    "Ты — кодовый агент в консоли, аналог Claude Code, работаешь на Windows.\n"
    f"Рабочая папка: {PROJECT_DIR}\n"
    "Действуй пошагово: сначала осмотрись инструментами (list_dir, glob — поиск "
    "файлов по маске, grep — поиск по содержимому, read_file), затем вноси "
    "небольшие правки (write_file, edit_file) и при необходимости "
    "запускай команды (run_command), проверяя результат. Никогда не выдумывай "
    "содержимое файлов — читай их. Когда задача выполнена, дай краткий итог "
    "обычным текстом, не вызывая инструменты."
)


def load_project_memory() -> str:
    """Подхватывает инструкции проекта из рабочей папки (если есть) в системный
    промпт — аналог CLAUDE.md."""
    for name in ("AGENTS.md", "CLAUDE.md", ".alice.md"):
        p = PROJECT_DIR / name
        if p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace")[:8000]
            return f"\n\n[Инструкции проекта из {name}]\n{txt}"
    return ""


# ---------------------------------------------------------------------------
# Вложения: Ctrl+V пути к файлу (из Проводника) или картинки из буфера обмена.
# Загружаются в Алису её протоколом (адаптер делает upload), она видит файл.
# ---------------------------------------------------------------------------

# Форматы, которые принимает веб-Алиса.
_ATTACH_EXT = {
    ".txt", ".text", ".md", ".markdown", ".js", ".mjs", ".ts", ".json", ".csv",
    ".xml", ".html", ".htm", ".shtml", ".shtm", ".ehtml", ".xhtml", ".css",
    ".xsl", ".xslt", ".xbl", ".vtt", ".ics", ".sh", ".dot", ".doc", ".docx",
    ".pdf", ".jpg", ".jpeg", ".jpe", ".jfif", ".pjp", ".pjpeg", ".png", ".webp",
    ".svg", ".gif",
}
_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".jfif": "image/jpeg", ".pjp": "image/jpeg", ".pjpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".gif": "image/gif", ".pdf": "application/pdf", ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword", ".md": "text/markdown", ".js": "text/javascript",
    ".mjs": "text/javascript", ".json": "application/json", ".xml": "text/xml",
    ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".txt": "text/plain", ".sh": "application/x-sh",
}
# Путь: в кавычках, либо абсолютный Windows (C:\…) / Unix (/…).
_PATH_RE = re.compile(r'"([^"]+)"|([A-Za-z]:\\[^\s"]+|/[^\s"]+)')


def _is_processing(text: str) -> bool:
    """Похоже на «файл ещё обрабатывается, спросите позже»."""
    t = (text or "").lower()
    return any(w in t for w in ("обрабатыва", "спросите поз", "попозже"))


def _guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _MIME:
        return _MIME[ext]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _att(path: str) -> dict:
    return {"path": path, "mime_type": _guess_mime(path), "title": os.path.basename(path)}


def detect_attachments(text: str) -> tuple[list, str]:
    """Находит в тексте пути к файлам поддерживаемых форматов; если их нет —
    пробует буфер обмена (картинка/файлы) через Pillow. Возвращает
    (список_вложений, текст_без_путей)."""
    atts, spans = [], []
    for m in _PATH_RE.finditer(text):
        cand = (m.group(1) or m.group(2) or "").strip().strip('"')
        ext = os.path.splitext(cand)[1].lower()
        if ext in _ATTACH_EXT and os.path.isfile(cand):
            atts.append(_att(cand))
            spans.append(m.span())
    if spans:
        out, prev = [], 0
        for s, e in spans:
            out.append(text[prev:s])
            prev = e
        out.append(text[prev:])
        text = " ".join("".join(out).split())

    if not atts:  # буфер обмена: скриншот или скопированные файлы
        try:
            from PIL import ImageGrab
            cb = ImageGrab.grabclipboard()
            if isinstance(cb, list):
                for p in cb:
                    if os.path.splitext(p)[1].lower() in _ATTACH_EXT and os.path.isfile(p):
                        atts.append(_att(p))
            elif cb is not None and hasattr(cb, "save"):
                tmp = os.path.join(tempfile.gettempdir(), f"alice_clip_{uuid.uuid4().hex[:8]}.png")
                cb.save(tmp, "PNG")
                atts.append(_att(tmp))
        except Exception:
            pass
    return atts, text


# ---------------------------------------------------------------------------
# Запуск и ожидание адаптера
# ---------------------------------------------------------------------------

def start_adapter() -> subprocess.Popen:
    log = open(ROOT / "adapter.log", "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "alice_adapter.py")],
        cwd=ROOT, stdout=log, stderr=log, env=os.environ.copy(),
    )


def wait_health(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# Уровень доверия и подтверждение операций
# ---------------------------------------------------------------------------

# all — спрашивать на ВСЕ вызовы; danger — только на опасные (по умолчанию);
# none — не запрашивать подтверждения вовсе. Переключается командой /trust.
TRUST_MODES = {
    "all": "подтверждать все операции",
    "danger": "подтверждать только опасные (запись/правка/команды)",
    "none": "не запрашивать подтверждения",
}


class Trust:
    def __init__(self, mode: str = "danger") -> None:
        self.mode = mode

    def needs_confirm(self, name: str) -> bool:
        if self.mode == "none":
            return False
        if self.mode == "all":
            return True
        return name in DANGEROUS  # режим danger


def _args_preview(arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    return ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())


def confirm_batch(calls: list) -> str:
    """Одно подтверждение сразу на все переданные вызовы. -> 'yes' | 'no' | 'always'."""
    lines = [f"• {tc.function.name}({_args_preview(tc.function.arguments)})"
             for tc in calls]
    title = "Выполнить операцию?" if len(calls) == 1 else f"Выполнить {len(calls)} операции?"
    console.print(Panel("\n".join(lines), title=f"[yellow]{title}[/]", border_style="yellow"))
    ans = Prompt.ask("[y]да / [n]нет / [a]больше не спрашивать в этой сессии",
                     choices=["y", "n", "a"], default="y")
    return {"y": "yes", "n": "no", "a": "always"}[ans]


# ---------------------------------------------------------------------------
# Один ход агента: крутим модель, пока она вызывает инструменты
# ---------------------------------------------------------------------------

# Осиротевшие маркеры тела (@@поле@@ / @@end@@ в начале строки) — признак того,
# что модель пыталась вызвать инструмент по нашему протоколу, но сломала формат.
_BROKEN_CALL_RE = re.compile(r"(?m)^@@(?:end|[A-Za-z_]\w*)@@\s*$")


def _looks_like_broken_call(text: str) -> bool:
    """Похоже на попытку вызова, которую парсер не смог распознать: остался фенс
    tool_call или осиротевшие маркеры тела."""
    if not text:
        return False
    return "```tool_call" in text or bool(_BROKEN_CALL_RE.search(text))


def agent_turn(client: OpenAI, messages: list, trust: "Trust",
               dialog_id: Optional[str] = None, attachments: Optional[list] = None) -> bool:
    """Прогоняет ход агента. Возвращает True, если файлы были изменены."""
    repairs_left = 2  # сколько раз просим Алису повторить сломанный вызов
    pending_att = attachments  # вложения шлём только на первом вызове хода
    modified = False  # были ли write_file/edit_file
    for _ in range(25):  # предохранитель от бесконечного цикла
        trim_history(messages)  # быстрая страховка от переполнения внутри хода
        extra: dict = {"dialog_id": dialog_id} if dialog_id else {}
        if pending_att:
            extra["attachments"] = pending_att
            pending_att = None
        with console.status("[dim]Alice думает…[/]", spinner="dots"):
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS_SCHEMA, extra_body=extra,
            )
        msg = resp.choices[0].message

        # ответ без инструментов
        if not msg.tool_calls:
            # битый вызов? просим повторить, а не завершаем молча
            if msg.content and repairs_left > 0 and _looks_like_broken_call(msg.content):
                repairs_left -= 1
                console.print("[yellow]Вызов инструмента сломан — прошу Алису повторить…[/]")
                messages.append({"role": "assistant", "content": msg.content})
                messages.append({"role": "user", "content":
                    "Твой вызов инструмента не распознан — формат сломан. Повтори его "
                    "СТРОГО блоком ```tool_call``` с корректным JSON (поля name и "
                    "arguments). Большой текст (content/old/new) выноси телом между "
                    "@@поле@@ и @@end@@, без обрамления тройными кавычками. "
                    "Ничего лишнего вокруг блока."})
                continue
            # финальный ответ
            if msg.content:
                console.print(Panel(Markdown(msg.content), title="Alice", border_style="cyan"))
            messages.append({"role": "assistant", "content": msg.content or ""})
            return modified

        # промежуточная "мысль" модели рядом с вызовом
        if msg.content:
            console.print(Markdown(msg.content))

        messages.append({
            "role": "assistant", "content": msg.content or None,
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls],
        })

        # одно подтверждение сразу на все вызовы, которым оно нужно по уровню доверия
        to_confirm = [tc for tc in msg.tool_calls if trust.needs_confirm(tc.function.name)]
        declined = False
        if to_confirm:
            decision = confirm_batch(to_confirm)
            declined = decision == "no"
            if decision == "always":
                trust.mode = "none"
                console.print("[dim]Уровень доверия → none: больше не спрашиваю в этой сессии.[/]")

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            console.print(f"[dim]→ {name}({_args_preview(tc.function.arguments)})[/]")

            if declined and tc in to_confirm:
                result = "Пользователь отклонил выполнение."
            else:
                fn = TOOLS_IMPL.get(name)
                try:
                    result = fn(**args) if fn else f"Неизвестный инструмент: {name}"
                except Exception as e:
                    result = f"Ошибка инструмента: {e}"

            if name in ("write_file", "edit_file") and result.startswith(
                    ("Записано", "Отредактировано")):
                modified = True
            preview = result.replace("\n", " ")
            console.print(f"[dim]  ↳ {preview[:200]}{'…' if len(result) > 200 else ''}[/]")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    console.print("[red]Достигнут лимит шагов агента за один запрос.[/]")
    return modified


def run_verification(client: OpenAI, messages: list, trust: "Trust",
                     dialog_id: Optional[str]) -> None:
    """Запускает ALICE_VERIFY_CMD после правок; при ошибке отдаёт вывод Алисе
    и просит исправить (одна попытка)."""
    console.print(f"[dim]Проверка: {VERIFY_CMD}[/]")
    try:
        r = subprocess.run(VERIFY_CMD, shell=True, cwd=PROJECT_DIR,
                           capture_output=True, text=True, timeout=180, errors="replace")
    except subprocess.TimeoutExpired:
        console.print("[yellow]Проверка превысила таймаут 180с.[/]")
        return
    if r.returncode == 0:
        console.print("[green]✓ Проверка прошла.[/]")
        return
    out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()[:6000]
    console.print(f"[yellow]✗ Проверка упала (exit={r.returncode}) — прошу Алису исправить…[/]")
    messages.append({"role": "user", "content":
        f"Проверка `{VERIFY_CMD}` завершилась с ошибкой (exit={r.returncode}):\n{out}\n"
        "Исправь причину ошибки."})
    agent_turn(client, messages, trust, dialog_id)


# ---------------------------------------------------------------------------
# Грубый guard под 32k-контекст: сносим самые старые сообщения после system
# ---------------------------------------------------------------------------

def _hist_size(messages: list) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


def trim_history(messages: list, max_chars: int = 60000) -> None:
    """Быстрая страховка от переполнения: НЕ трогает system[0] и первый user-запрос
    (задачу) и последние 6 сообщений; удаляет самые старые из середины."""
    if _hist_size(messages) <= max_chars:
        return
    first_user = next((i for i, m in enumerate(messages)
                       if m.get("role") == "user"), None)
    while _hist_size(messages) > max_chars and len(messages) > 8:
        protected = {0, first_user} | set(range(len(messages) - 6, len(messages)))
        victim = next((i for i in range(1, len(messages)) if i not in protected), None)
        if victim is None:
            break
        del messages[victim]


def _msg_brief(m: dict) -> str:
    role = m.get("role")
    txt = m.get("content") if isinstance(m.get("content"), str) else ""
    if role == "assistant" and m.get("tool_calls"):
        calls = ", ".join(tc.get("function", {}).get("name", "") for tc in m["tool_calls"])
        return f"ассистент вызвал: {calls}" + (f" | {txt[:200]}" if txt else "")
    if role == "tool":
        return f"результат инструмента: {(txt or '')[:300]}"
    return f"{role}: {(txt or '')[:400]}"


def compact_history(client: OpenAI, messages: list, max_chars: int = 60000) -> None:
    """Сжимает старые ходы в краткую сводку (одним запросом к Алисе), сохраняя
    system, первый запрос и последние ходы. При сбое — откат к trim_history."""
    if _hist_size(messages) <= max_chars:
        return
    keep = 6
    if len(messages) <= keep + 2:
        trim_history(messages, max_chars)
        return
    system, old, recent = messages[0], messages[1:-keep], messages[-keep:]
    rendered = "\n".join(_msg_brief(m) for m in old)[:30000]
    try:
        with console.status("[dim]Сжимаю историю…[/]", spinner="dots"):
            resp = client.chat.completions.create(model=MODEL, messages=[
                {"role": "system", "content": "Ты кратко конспектируешь работу кодового агента."},
                {"role": "user", "content":
                    "Сожми в 5-12 пунктов: что сделано, какие файлы менялись/создавались, "
                    "важные факты и решения для продолжения. Только факты:\n\n" + rendered}])
        summary = (resp.choices[0].message.content or "").strip()
    except Exception:
        trim_history(messages, max_chars)
        return
    if not summary:
        trim_history(messages, max_chars)
        return
    messages[:] = ([system, {"role": "system",
                             "content": "[Сводка предыдущих шагов]\n" + summary}] + recent)
    console.print("[dim]Контекст сжат в сводку.[/]")


# ---------------------------------------------------------------------------
# Сессии: один dialog_id на сеанс (→ один диалог на сайте Алисы), транскрипт
# на диске. Обычный запуск = новая сессия; /resume — вернуться в существующую.
# ---------------------------------------------------------------------------

SESSIONS_DIR = ROOT / ".alice_sessions"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def new_session(system_prompt: str = SYSTEM_PROMPT) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "dialog_id": str(uuid.uuid4()),
        "created": _now(),
        "updated": _now(),
        "messages": [{"role": "system", "content": system_prompt}],
    }


def save_session(sess: dict) -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)
    sess["updated"] = _now()
    (SESSIONS_DIR / f"{sess['id']}.json").write_text(
        json.dumps(sess, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sessions() -> list:
    if not SESSIONS_DIR.is_dir():
        return []
    out = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda s: s.get("updated", ""), reverse=True)
    return out


def session_title(sess: dict) -> str:
    for m in sess.get("messages", []):
        if m.get("role") == "user":
            t = (m.get("content") or "").strip().replace("\n", " ")
            return (t[:50] + "…") if len(t) > 50 else t
    return "(пусто)"


def pick_session(arg: str, exclude_id: str = "") -> Optional[dict]:
    """Выбрать сессию для /resume: по id-префиксу (arg) или из нумерованного списка."""
    sessions = [s for s in load_sessions() if s.get("id") != exclude_id]
    if not sessions:
        console.print("[dim]Нет сохранённых сессий для возврата.[/]")
        return None
    if arg:
        for s in sessions:
            if s["id"].startswith(arg):
                return s
        console.print(f"[yellow]Сессия '{arg}' не найдена.[/]")
        return None
    console.print("[bold]Прошлые сессии:[/]")
    shown = sessions[:15]
    for i, s in enumerate(shown, 1):
        console.print(f"  [cyan]{i:>2}[/] [dim]{s.get('updated', '')}[/]  "
                      f"{s['id']}  {session_title(s)}")
    ans = Prompt.ask("[bold]Номер сессии[/] (Enter — отмена)", default="").strip()
    if not ans:
        return None
    if ans.isdigit() and 1 <= int(ans) <= len(shown):
        return shown[int(ans) - 1]
    for s in sessions:
        if s["id"].startswith(ans):
            return s
    console.print("[yellow]Отмена.[/]")
    return None


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def main() -> None:
    console.print(Panel.fit(
        f"[bold cyan]Alice Code[/]   модель: {MODEL}\n"
        f"папка: {PROJECT_DIR}\n"
        "[dim]/resume · /clear · /trust · /exit · /help[/]",
        border_style="cyan"))

    # Сессия Алисы: при первом запуске откроется окно браузера для входа в Яндекс
    # (логин сохранится в профиле). Дальше токен обновляется сам.
    console.print("[dim]Проверяю сессию Алисы…[/]")
    try:
        creds = alice_session.ensure_sync()
    except Exception as e:
        console.print(f"[red]Не удалось подготовить сессию Алисы: {e}[/]")
        return
    if creds.logged_in:
        console.print("[green]Сессия готова — режим Pro.[/]")
    else:
        console.print("[yellow]Вход не выполнен — работаю в режиме Base. "
                      "Для Pro перезапусти и залогинься в окне браузера.[/]")

    console.print("[dim]Поднимаю адаптер…[/]")
    adapter = start_adapter()
    if not wait_health():
        console.print("[red]Адаптер не поднялся за 25с. Смотри adapter.log.[/]")
        adapter.terminate()
        return
    console.print("[green]Готово, можно работать.[/]\n")

    client = OpenAI(base_url=BASE_URL, api_key="local")
    project_mem = load_project_memory()
    sys_prompt = SYSTEM_PROMPT + project_mem
    if project_mem:
        console.print("[dim]Подхвачены инструкции проекта (CLAUDE.md/AGENTS.md).[/]")
    sess = new_session(sys_prompt)
    messages = sess["messages"]
    trust = Trust()
    console.print(f"[dim]Новая сессия {sess['id']}. Уровень доверия: {trust.mode} "
                  f"({TRUST_MODES[trust.mode]}).[/]\n")

    try:
        while True:
            try:
                user = read_user_input()
            except (EOFError, KeyboardInterrupt):
                break

            stripped = user.strip()
            cmd = stripped.lower()
            if cmd in ("/exit", "/quit", "exit", "quit"):
                break
            if cmd == "/clear":
                sess = new_session(sys_prompt)
                messages = sess["messages"]
                console.print(f"[dim]Новая сессия {sess['id']} (контекст очищен).[/]")
                continue
            if cmd == "/help":
                console.print(
                    "[dim]Опиши задачу обычным текстом. Многострочная вставка (Ctrl+V) "
                    "и история (↑/↓) поддерживаются.\n"
                    "Прикрепить файл: Ctrl+V путь к файлу (скопируй файл в Проводнике) "
                    "или скриншот из буфера — Алиса его прочитает.\n"
                    "/resume [id] — вернуться к прошлой сессии · /clear — новая сессия · "
                    "/trust — уровень доверия (подтверждения) · /exit — выход.[/]")
                continue
            if cmd == "/trust" or cmd.startswith("/trust "):
                arg = stripped[len("/trust"):].strip().lower()
                if arg in TRUST_MODES:
                    trust.mode = arg
                    console.print(f"[green]Уровень доверия: {arg}[/] "
                                  f"[dim]— {TRUST_MODES[arg]}[/]")
                else:
                    console.print(f"[bold]Текущий уровень доверия:[/] {trust.mode} "
                                  f"[dim]— {TRUST_MODES[trust.mode]}[/]")
                    for k, v in TRUST_MODES.items():
                        console.print(f"  [cyan]/trust {k}[/] — {v}")
                continue
            if cmd == "/resume" or cmd.startswith("/resume "):
                chosen = pick_session(stripped[len("/resume"):].strip(),
                                      exclude_id=sess["id"])
                if chosen is not None:
                    sess = chosen
                    messages = sess["messages"]
                    console.print(f"[green]Вернулся в сессию {sess['id']}[/] "
                                  f"[dim]({session_title(sess)})[/]")
                continue
            if not stripped:
                continue

            atts, cleaned = detect_attachments(user)
            if atts:
                console.print("[dim]📎 прикреплено: "
                              + ", ".join(a["title"] for a in atts) + "[/]")
                messages.append({"role": "user",
                                 "content": cleaned.strip() or "Посмотри прикреплённый файл."})
            else:
                messages.append({"role": "user", "content": user})
            compact_history(client, messages)
            try:
                modified = agent_turn(client, messages, trust, sess["dialog_id"],
                                      attachments=atts or None)
                # файл загружается асинхронно: если Алиса «обрабатывает» —
                # подождём и переспросим один раз
                if atts and messages and messages[-1].get("role") == "assistant" \
                        and _is_processing(messages[-1].get("content", "")):
                    console.print("[dim]Файл ещё обрабатывается — жду и переспрашиваю…[/]")
                    time.sleep(6)
                    messages.append({"role": "user",
                                     "content": "Файл обработан? Ответь по его содержимому."})
                    modified = agent_turn(client, messages, trust, sess["dialog_id"]) or modified
                # верификация после правок (если задана ALICE_VERIFY_CMD)
                if modified and VERIFY_CMD:
                    run_verification(client, messages, trust, sess["dialog_id"])
            except Exception as e:
                console.print(f"[red]Ошибка запроса: {e}[/]")
            save_session(sess)
    finally:
        console.print("\n[dim]Останавливаю адаптер…[/]")
        adapter.terminate()
        try:
            adapter.wait(timeout=5)
        except Exception:
            adapter.kill()


if __name__ == "__main__":
    main()
