"""
agent.py — консольный кодовый агент (аналог Claude Code) поверх Alice.

Сам поднимает alice_adapter.py, ждёт его готовности и запускает чат-REPL
с инструментами для работы с файлами и shell. Запускается через run.bat.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import queue as _queue

import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

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

# Команды чата: имя → описание (для подсказок при вводе «/»).
_COMMANDS: list[tuple[str, str]] = [
    ("/help", "помощь по командам"),
    ("/clear", "новая сессия (очистить контекст)"),
    ("/resume", "вернуться к прошлой сессии"),
    ("/undo", "откатить последнюю правку файлов"),
    ("/trust", "уровень подтверждений: all / danger / none"),
    ("/login", "повторный вход в Яндекс (окно браузера)"),
    ("/queue", "показать очередь запросов"),
    ("/exit", "выход"),
]


class _SlashCompleter(Completer):
    """Выпадающие подсказки команд, когда строка начинается с «/» (как в
    claude code). Срабатывает только пока вводится сама команда (до пробела)."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name, desc in _COMMANDS:
            if name.startswith(text):
                yield Completion(name, start_position=-len(text),
                                 display=name, display_meta=desc)


def read_user_input() -> str:
    """Прочитать ввод пользователя. Поддерживает многострочную вставку."""
    global _input_session
    if _input_session is None:
        _input_session = PromptSession(history=InMemoryHistory(),
                                       refresh_interval=0.2,
                                       completer=_SlashCompleter(),
                                       complete_while_typing=True)
    return _input_session.prompt(_prompt_message)


def _default_ask(prompt_text: str) -> str:
    console.print(prompt_text)
    return read_user_input()


# Хук чтения одной строки для подтверждений / выбора сессии. В режиме очереди
# main() подменяет его на чтение из очереди ответов (чтобы не конфликтовать с
# фоновым приёмом ввода).
ASK = _default_ask


class _PTOutput:
    """Файл для rich: его ANSI-вывод печатаем через prompt_toolkit (print_formatted_text
    + ANSI). Так цвета сохраняются И вывод корректно ложится над строкой ввода под
    patch_stdout (обычный путь rich печатал бы ANSI-коды буквально)."""

    def write(self, data: str) -> int:
        if data:
            print_formatted_text(ANSI(data), end="")
        return len(data or "")

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


# Анимированный статус «Alice думает…» над строкой ввода. Кадр спиннера
# вычисляется по времени, а поток-тикер форсит перерисовку приглашения.
_status = {"busy": False, "asking": False, "text": "Alice думает"}


def _force_prompt_redraw() -> None:
    """Принудительно перерисовать приглашение (из любого потока). Нужно на
    переходе «занят→свободен»: тикер в этот момент уже не тикает, и строка
    спиннера осталась бы на экране до следующей клавиши."""
    sess = _input_session
    if sess is None:
        return
    try:
        app = sess.app
        loop = getattr(app, "loop", None)
        if getattr(app, "is_running", False) and loop is not None:
            loop.call_soon_threadsafe(app._redraw)
    except Exception:
        pass


def set_thinking(on: bool, text: str = "Alice думает") -> None:
    _status["busy"] = on
    if on:
        _status["text"] = text
    else:
        _force_prompt_redraw()  # убрать строку спиннера сразу, не дожидаясь клавиши


def _looks_like_answer(line: str) -> bool:
    """Похоже на ответ y/n/a (а не на новую задачу), чтобы во время подтверждения
    случайно не съесть набранную задачу как ответ."""
    a = line.strip().lower()
    return (a == "" or len(a) <= 4
            or a in ("yes", "no", "да", "нет", "ага", "ok", "ок", "всегда", "always"))


# Вращающийся бегунок из ASCII: кадры заведомо различимы в любом консольном шрифте
# (брайлевые точки ⠋⠙⠹… у многих выглядят одинаково и кажутся застывшими).
_SPINNER = "|/-\\"


def _prompt_message():
    """Текст приглашения. Пока агент занят — над строкой ввода рисуется
    анимированная строка статуса (как «· Thinking…» в Claude Code), слева, в
    потоке; refresh_interval перерисовывает её → анимация. Когда свободен —
    остаётся только зелёная стрелка. Строка статуса исчезает по завершении."""
    arrow = "\x1b[1;32m› \x1b[0m"
    # ждём ответ пользователя (подтверждение/выбор) — жёлтая стрелка, без спиннера
    if _status["asking"]:
        return ANSI("\x1b[1;33m❯ \x1b[0m")
    if not _status["busy"]:
        return ANSI(arrow)
    # кадр привязан к ВРЕМЕНИ (~10 кадров/с), а не к числу перерисовок — иначе
    # _prompt_message зовётся по много раз за один redraw и анимация «дрожит».
    # Точку-многоточие держим статичной (меняющаяся ширина .. → ... мерцала).
    sp = _SPINNER[int(time.time() / 0.1) % len(_SPINNER)]
    return ANSI(f"\x1b[36m{sp} {_status['text']}…\x1b[0m\n{arrow}")


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


def _safe_workdir(cwd: str) -> Path:
    """Рабочая папка для команд: внутри проекта и существующая, иначе PROJECT_DIR
    (не кидает ValueError на путь вне проекта — мягкий фолбэк)."""
    try:
        wd = _safe_path(cwd)
    except ValueError:
        return PROJECT_DIR
    return wd if wd.is_dir() else PROJECT_DIR


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
    before = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    _checkpoint([path])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _show_diff(before, content, path)
    return f"Записано: {path} ({len(content)} символов)"


def tool_edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"Файл не найден: {path}"
    if not old:
        return ("Параметр `old` не может быть пустым: укажи фрагмент для замены "
                "(для создания/перезаписи файла используй write_file).")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return (f"Файл {path} не в кодировке UTF-8 — правка текстом невозможна "
                "(вероятно бинарный или cp1251). Используй другой подход.")
    n = text.count(old)
    if n == 0:
        return ("Фрагмент `old` не найден — нужно точное совпадение, включая отступы. "
                "Прочитай файл через read_file и скопируй фрагмент дословно. "
                f"В файле {len(text.splitlines())} строк.")
    if n > 1 and not replace_all:
        return (f"Фрагмент `old` встречается {n} раз. Добавь контекста для уникальности "
                "или передай replace_all=true, чтобы заменить все.")
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    _checkpoint([path])
    p.write_text(new_text, encoding="utf-8")
    _show_diff(text, new_text, path)
    return f"Отредактировано: {path}" + (f" ({n} замен)" if replace_all and n > 1 else "")


def tool_list_dir(path: str = ".") -> str:
    p = _safe_path(path)
    if not p.is_dir():
        return f"Не директория: {path}"
    rows = [("[d] " if i.is_dir() else "[f] ") + i.name for i in sorted(p.iterdir())]
    return "\n".join(rows) or "(пусто)"


def tool_run_command(command: str, timeout: int = 120, cwd: str = ".") -> str:
    timeout = max(1, min(int(timeout or 120), 600))
    workdir = _safe_workdir(cwd)
    try:
        r = subprocess.run(
            command, shell=True, cwd=str(workdir),
            capture_output=True, text=True, timeout=timeout, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return (f"Команда превысила таймаут {timeout}с. Для долгих процессов "
                "(серверы, watch) используй run_background.")
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip() or "(нет вывода)"
    if len(out) > 30000:
        out = out[:30000] + "\n... [обрезано]"
    note = ""
    if r.returncode != 0:
        note = ("\n[note] Если это программа с окном/GUI и пользователь просто закрыл "
                "окно — ненулевой код выхода и сообщения при закрытии нормальны, это не "
                "ошибка в коде.")
    return f"exit={r.returncode}\n{out}{note}"


# ---------------------------------------------------------------------------
# Новые инструменты v0.5.0: правки, файловые операции, фоновые процессы, git, web
# ---------------------------------------------------------------------------

# Стек снапшотов для /undo: каждый элемент — список (путь, прежние_байты | None).
_undo_stack: list = []


def _checkpoint(paths: list) -> None:
    snap = []
    for rel in paths:
        try:
            ap = _safe_path(rel)
        except ValueError:
            continue
        # снапшотим только файлы; директории read_bytes() ронял (IsADirectory/
        # PermissionError) и ломал move/delete/copy папок
        snap.append((str(ap), ap.read_bytes() if (ap.exists() and ap.is_file()) else None))
    if snap:
        _undo_stack.append(snap)
        if len(_undo_stack) > 50:
            _undo_stack.pop(0)


def _show_diff(before: str, after: str, path: str) -> None:
    if before == after:
        return
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                     fromfile=f"{path} (было)", tofile=f"{path} (стало)",
                                     lineterm="", n=2))
    if not diff:
        return
    shown = diff[:60]
    for line in shown:
        color = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
        console.print(f"[{color}]{line}[/]")
    if len(diff) > 60:
        console.print(f"[dim]… ещё {len(diff) - 60} строк диффа[/]")


def tool_multi_edit(path: str, edits: list) -> str:
    """Несколько правок в одном файле за вызов. edits: [{old, new, replace_all?}]."""
    p = _safe_path(path)
    if not p.exists():
        return f"Файл не найден: {path}"
    try:
        before = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return (f"Файл {path} не в кодировке UTF-8 — правка текстом невозможна "
                "(вероятно бинарный или cp1251).")
    text = before
    applied = 0
    for i, ed in enumerate(edits or [], 1):
        old, new = ed.get("old", ""), ed.get("new", "")
        ra = bool(ed.get("replace_all"))
        if not old:
            return f"Правка {i}: параметр `old` пустой — так нельзя. Применено до этого: {applied}."
        cnt = text.count(old)
        if cnt == 0:
            return f"Правка {i}: фрагмент `old` не найден. Применено до этого: {applied}."
        if cnt > 1 and not ra:
            return f"Правка {i}: фрагмент встречается {cnt} раз — уточни или replace_all=true."
        text = text.replace(old, new) if ra else text.replace(old, new, 1)
        applied += 1
    _checkpoint([path])
    p.write_text(text, encoding="utf-8")
    _show_diff(before, text, path)
    return f"Отредактировано: {path} ({applied} правок)"


def tool_make_dir(path: str) -> str:
    p = _safe_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Создана директория: {path}"


def tool_move(src: str, dst: str) -> str:
    s, d = _safe_path(src), _safe_path(dst)
    if not s.exists():
        return f"Не найдено: {src}"
    _checkpoint([src, dst])
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return f"Перемещено: {src} -> {dst}"


def tool_copy(src: str, dst: str) -> str:
    s, d = _safe_path(src), _safe_path(dst)
    if not s.exists():
        return f"Не найдено: {src}"
    _checkpoint([dst])
    d.parent.mkdir(parents=True, exist_ok=True)
    if s.is_dir():
        shutil.copytree(str(s), str(d), dirs_exist_ok=True)
    else:
        shutil.copy2(str(s), str(d))
    return f"Скопировано: {src} -> {dst}"


def tool_delete(path: str) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"Не найдено: {path}"
    _checkpoint([path])
    if p.is_dir():
        shutil.rmtree(str(p))
    else:
        p.unlink()
    return f"Удалено: {path}"


def tool_apply_patch(patch: str) -> str:
    """Применяет unified diff. Сначала пробует `git apply`, иначе — ошибка с
    подсказкой использовать multi_edit."""
    tmp = Path(tempfile.gettempdir()) / f"alice_patch_{uuid.uuid4().hex[:8]}.diff"
    tmp.write_text(patch, encoding="utf-8")
    # снапшот целевых файлов (из заголовков +++/---), чтобы /undo мог откатить патч
    targets = []
    for m in re.finditer(r"(?m)^(?:\+\+\+|---)\s+(?:[ab]/)?(\S+)", patch):
        f = m.group(1)
        if f and f != "/dev/null" and f not in targets:
            targets.append(f)
    if targets:
        _checkpoint(targets)
    try:
        for args in (["git", "apply", "--whitespace=nowarn", str(tmp)],
                     ["git", "apply", "-p0", "--whitespace=nowarn", str(tmp)]):
            r = subprocess.run(args, cwd=str(PROJECT_DIR), capture_output=True,
                               text=True, timeout=60, errors="replace")
            if r.returncode == 0:
                return "Патч применён (git apply)."
        return ("Не удалось применить патч: " + (r.stderr or "").strip()[:500] +
                "\nПопробуй edit_file/multi_edit вместо патча.")
    except FileNotFoundError:
        return "git не найден — применить патч нельзя. Используй edit_file/multi_edit."
    finally:
        tmp.unlink(missing_ok=True)


# --- фоновые процессы ---
_bg_procs: dict = {}


def _bg_reader(bid: str, proc: subprocess.Popen) -> None:
    buf = _bg_procs[bid]["output"]
    try:
        for line in proc.stdout:  # type: ignore
            buf.append(line)
            if len(buf) > 2000:
                del buf[:1000]
    except Exception:
        pass


def tool_run_background(command: str, cwd: str = ".") -> str:
    workdir = _safe_workdir(cwd)
    bid = uuid.uuid4().hex[:6]
    proc = subprocess.Popen(
        command, shell=True, cwd=str(workdir), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    _bg_procs[bid] = {"proc": proc, "output": [], "command": command}
    t = threading.Thread(target=_bg_reader, args=(bid, proc), daemon=True)
    t.start()
    # проверяем ~1.5с, не упал ли процесс сразу (нет модуля, синтаксис, краш на
    # старте), но выходим как только он явно жив — не блокируем очередь зря
    rc = None
    for _ in range(15):
        rc = proc.poll()
        if rc is not None:
            break
        time.sleep(0.1)
    if rc is not None:
        out = "".join(_bg_procs[bid]["output"])[-4000:].strip() or "(нет вывода)"
        return (f"Процесс id={bid} завершился СРАЗУ с кодом {rc} — окно не открылось/не "
                f"осталось работать, это ошибка запуска (не пиши пользователю, что окно "
                f"открылось). Разбери вывод и устрани причину (напр. поставь зависимость "
                f"через pip install …, потом запусти снова):\n{out}")
    return (f"Запущен фоновый процесс id={bid}: {command} — работает. "
            f"Вывод смотри через read_output(id={bid}), останови через kill_process.")


def tool_read_output(id: str) -> str:
    info = _bg_procs.get(id)
    if not info:
        return f"Нет фонового процесса id={id}."
    rc = info["proc"].poll()
    status = "завершён, exit=" + str(rc) if rc is not None else "выполняется"
    out = "".join(info["output"])[-12000:] or "(пока нет вывода)"
    return f"[{status}]\n{out}"


def _reap(proc: subprocess.Popen) -> None:
    """Завершить процесс и закрыть его пайп (без утечки дескрипторов/зомби)."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    except Exception:
        pass
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass


def tool_kill_process(id: str) -> str:
    info = _bg_procs.get(id)
    if not info:
        return f"Нет фонового процесса id={id}."
    _reap(info["proc"])
    return f"Процесс id={id} остановлен."


def _kill_all_bg() -> None:
    for info in _bg_procs.values():
        try:
            _reap(info["proc"])
        except Exception:
            pass


# --- git ---
def _git(args: list, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(PROJECT_DIR), capture_output=True,
                           text=True, timeout=timeout, errors="replace")
    except FileNotFoundError:
        return "git не установлен."
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    return out.strip() or "(нет вывода)"


def tool_git_status() -> str:
    return _git(["status", "--short", "--branch"])


def tool_git_diff(path: str = "") -> str:
    args = ["diff"] + ([path] if path else [])
    out = _git(args)
    return out[:30000] + "\n... [обрезано]" if len(out) > 30000 else out


def tool_git_commit(message: str) -> str:
    _git(["add", "-A"])
    return _git(["commit", "-m", message])


# --- web ---
def tool_web_fetch(url: str) -> str:
    """Скачивает страницу и возвращает текст (теги вырезаны)."""
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
    except Exception as e:
        return f"Ошибка запроса: {e}"
    text = r.text
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if len(text) > 20000:
        text = text[:20000] + "\n... [обрезано]"
    return f"[{r.status_code}] {url}\n{text}"


# --- план / todo ---
_todo: list = []  # элементы {"text", "done"}


def tool_update_todo(items: list) -> str:
    """Обновляет список задач (план). items: [{text, done}]. Печатает план."""
    global _todo
    _todo = [{"text": str(it.get("text", "")), "done": bool(it.get("done"))}
             for it in (items or []) if it.get("text")]
    if not _todo:
        return "План очищен."
    lines = [("[x] " if it["done"] else "[ ] ") + it["text"] for it in _todo]
    console.print(Panel("\n".join(lines), title="[cyan]План[/]", border_style="cyan"))
    return "План обновлён:\n" + "\n".join(lines)


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
    "multi_edit": tool_multi_edit,
    "list_dir": tool_list_dir,
    "glob": tool_glob,
    "grep": tool_grep,
    "make_dir": tool_make_dir,
    "move": tool_move,
    "copy": tool_copy,
    "delete": tool_delete,
    "apply_patch": tool_apply_patch,
    "run_command": tool_run_command,
    "run_background": tool_run_background,
    "read_output": tool_read_output,
    "kill_process": tool_kill_process,
    "git_status": tool_git_status,
    "git_diff": tool_git_diff,
    "git_commit": tool_git_commit,
    "web_fetch": tool_web_fetch,
    "update_todo": tool_update_todo,
}
# Требуют подтверждения (меняют файлы/репозиторий или выполняют код):
DANGEROUS = {"write_file", "edit_file", "multi_edit", "make_dir", "move", "copy",
             "delete", "apply_patch", "run_command", "run_background", "git_commit"}

# Инструменты, меняющие файлы → префикс успешного результата. Нужно, чтобы флаг
# modified (и ALICE_VERIFY_CMD после хода) срабатывал на ВСЕ правки, не только
# write_file/edit_file.
_MUTATOR_OK = {
    "write_file": ("Записано",), "edit_file": ("Отредактировано",),
    "multi_edit": ("Отредактировано",), "move": ("Перемещено",),
    "copy": ("Скопировано",), "delete": ("Удалено",),
    "apply_patch": ("Патч применён",),
}

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
        "description": "Выполнить команду в shell внутри рабочей папки (блокирующе). "
                       "timeout — секунды (по умолч. 120, макс 600), cwd — подпапка.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"},
                                      "timeout": {"type": "integer"},
                                      "cwd": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "multi_edit",
        "description": "Несколько правок одного файла за вызов. edits — список "
                       "объектов {old, new, replace_all?}, применяются по порядку.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "edits": {"type": "array", "items": {"type": "object",
                                          "properties": {"old": {"type": "string"},
                                                         "new": {"type": "string"},
                                                         "replace_all": {"type": "boolean"}}}}},
                       "required": ["path", "edits"]}}},
    {"type": "function", "function": {
        "name": "make_dir", "description": "Создать директорию (с родителями).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "move", "description": "Переместить/переименовать файл или папку.",
        "parameters": {"type": "object",
                       "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
                       "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "copy", "description": "Скопировать файл или папку.",
        "parameters": {"type": "object",
                       "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
                       "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "delete", "description": "Удалить файл или папку (рекурсивно).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "apply_patch",
        "description": "Применить unified diff (git-патч) к файлам рабочей папки.",
        "parameters": {"type": "object", "properties": {"patch": {"type": "string"}},
                       "required": ["patch"]}}},
    {"type": "function", "function": {
        "name": "run_background",
        "description": "Запустить долгий процесс в фоне (dev-сервер, watch). Вернёт id; "
                       "вывод — read_output(id), остановка — kill_process(id).",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_output", "description": "Прочитать накопленный вывод фонового процесса.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                       "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "kill_process", "description": "Остановить фоновый процесс по id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                       "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "git_status", "description": "git status (короткий) рабочей папки.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "git_diff", "description": "git diff (опц. по пути).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "git_commit", "description": "git add -A и git commit с сообщением.",
        "parameters": {"type": "object", "properties": {"message": {"type": "string"}},
                       "required": ["message"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Скачать веб-страницу и вернуть её текст (без тегов).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "update_todo",
        "description": "Обновить план задач (TODO). Используй для многошаговых задач: "
                       "items — список {text, done}. Отмечай выполненные.",
        "parameters": {"type": "object",
                       "properties": {"items": {"type": "array", "items": {"type": "object",
                           "properties": {"text": {"type": "string"},
                                          "done": {"type": "boolean"}}}}},
                       "required": ["items"]}}},
]

SYSTEM_PROMPT = (
    "Ты — кодовый агент в консоли, аналог Claude Code, работаешь на Windows.\n"
    f"Рабочая папка: {PROJECT_DIR}\n"
    "Действуй пошагово: сначала осмотрись инструментами (list_dir, glob — поиск "
    "файлов по маске, grep — поиск по содержимому, read_file), затем вноси "
    "небольшие правки (write_file, edit_file) и при необходимости "
    "запускай команды (run_command), проверяя результат. Никогда не выдумывай "
    "содержимое файлов — читай их.\n"
    "Для многошаговых задач сначала составь план через update_todo и по ходу "
    "отмечай выполненные пункты. Несколько правок одного файла делай за один "
    "multi_edit. Долгие процессы (серверы, watch) запускай через run_background и "
    "смотри вывод через read_output. Доступны файловые операции (make_dir, move, "
    "copy, delete), git (git_status/git_diff/git_commit) и web_fetch для доков.\n"
    "Программы с окном/GUI (игры, tkinter, pygame, браузерные), серверы и любые "
    "интерактивные процессы запускай через run_background, а НЕ run_command: "
    "run_command блокируется, пока пользователь не закроет окно. Когда пользователь "
    "закрывает окно — это нормальное завершение, НЕ ошибка; ненулевой код выхода или "
    "сообщение при закрытии окна не считай багом и не пытайся «чинить» рабочий код.\n"
    "Зависимости ставишь САМ: если при запуске не хватает модуля (ModuleNotFoundError), "
    "выполни run_command('pip install <пакет>') и запусти снова — НЕ проси пользователя "
    "ставить пакеты вручную, ты это умеешь. Но по возможности для простых игр/GUI "
    "выбирай стандартную библиотеку (tkinter, curses) — она уже есть, ничего ставить не "
    "нужно; внешние пакеты (pygame и т.п.) тяни только если без них правда никак.\n"
    "НЕ повторяй один и тот же вызов с теми же аргументами — это ничего не меняет. "
    "Финальную сводку давай ОДИН раз и ТОЛЬКО когда инструменты больше не нужны "
    "(в этом ответе не должно быть вызовов). Если действие выполнено — просто заверши "
    "текстом, не вызывая инструменты снова.\n"
    "Когда задача выполнена, дай краткий итог обычным текстом, не вызывая инструменты."
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
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "alice_adapter.py")],
        cwd=ROOT, stdout=log, stderr=log, env=os.environ.copy(),
    )
    proc._alice_log = log  # type: ignore[attr-defined]  # чтобы закрыть при остановке
    return proc


def stop_adapter(proc: subprocess.Popen) -> None:
    """Остановить адаптер и закрыть его лог-файл (без утечки дескриптора)."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log = getattr(proc, "_alice_log", None)
    if log is not None:
        try:
            log.close()
        except Exception:
            pass


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
    ans = ASK("[yellow](y)[/] да   [yellow](n)[/] нет   [yellow](a)[/] всегда   "
              "[dim](Enter — нет)[/]: ").strip().lower()
    first = ans[:1]
    # fail-safe: подтверждаем ТОЛЬКО на явное «да»; пустой ответ/отмена/непонятное
    # → «нет» (раньше любое не-n/a трактовалось как «да» — опасно при отмене/закрытии)
    if first in ("a", "а"):                       # латинская a и кириллическая а
        return "always"
    if first in ("y", "д") or ans in ("yes", "да", "ага", "ok", "ок"):
        return "yes"
    return "no"


# ---------------------------------------------------------------------------
# Один ход агента: крутим модель, пока она вызывает инструменты
# ---------------------------------------------------------------------------

# Осиротевшие маркеры тела (@@поле@@ / @@end@@ в начале строки) — признак того,
# что модель пыталась вызвать инструмент по нашему протоколу, но сломала формат.
_BROKEN_CALL_RE = re.compile(r"(?m)^@@(?:end|[A-Za-z_]\w*)@@\s*$")
# Алиса иногда «рассказывает» о вызове словами вместо реального tool_call:
# «[Ассистент вызвал] read_file(...)», «Вызываю read_file(...)» и т.п.
_NARRATED_CALL_RE = re.compile(
    r"\[\s*(?:ассистент|assistant)[^\]]*?(?:вы[зч]|call)", re.IGNORECASE)
# Строка, которая ЦЕЛИКОМ выглядит как голый вызов «toolname(...)» (и ничего
# больше) — признак «рассказанного» вызова. Требуем закрывающую скобку в конце
# строки, чтобы не ловить обычную прозу вроде «read_file(path) читает файл».
_TOOLCALL_LINE_RE = re.compile(
    r"(?m)^\s*(?:" + "|".join(re.escape(k) for k in TOOLS_IMPL) + r")\s*\([^\n]*\)\s*$")


def _looks_like_broken_call(text: str) -> bool:
    """Похоже на попытку вызова, которую парсер не смог распознать: остался фенс
    tool_call, осиротевшие маркеры тела, или вызов «рассказан» текстом."""
    if not text:
        return False
    return ("```tool_call" in text
            or bool(_BROKEN_CALL_RE.search(text))
            or bool(_NARRATED_CALL_RE.search(text))
            or bool(_TOOLCALL_LINE_RE.search(text)))


def _create_with_retry(client: OpenAI, **kw):
    """Вызов модели с ретраями на временных сетевых ошибках: адаптер/WS Алисы
    иногда роняет соединение. До 4 попыток с нарастающей паузой, чтобы ход не
    терялся из-за разрыва."""
    last = None
    for attempt in range(4):
        try:
            return client.chat.completions.create(**kw)
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = (isinstance(e, (APIConnectionError, APITimeoutError))
                         or "connection" in msg or "timeout" in msg
                         or "remote" in msg or "reset" in msg)
            if not transient or attempt == 3:
                raise
            wait = 1.5 * (attempt + 1)
            set_thinking(True, "переподключаюсь")
            console.print(f"[yellow]Соединение прервалось — повтор через {wait:.0f}с "
                          f"(попытка {attempt + 2}/4)…[/]")
            time.sleep(wait)
    raise last


def agent_turn(client: OpenAI, messages: list, trust: "Trust",
               dialog_id: Optional[str] = None, attachments: Optional[list] = None) -> bool:
    """Прогоняет ход агента. Возвращает True, если файлы были изменены."""
    repairs_left = 2  # сколько раз просим Алису повторить сломанный вызов
    pending_att = attachments  # вложения шлём только на первом вызове хода
    modified = False  # были ли write_file/edit_file
    prev_sig = None  # сигнатура прошлого батча вызовов — для детекта зацикливания
    for _ in range(25):  # предохранитель от бесконечного цикла
        trim_history(messages)       # страховка от переполнения внутри хода
        _repair_tool_pairs(messages)  # гарантируем валидную парность перед запросом
        extra: dict = {"dialog_id": dialog_id} if dialog_id else {}
        if pending_att:
            extra["attachments"] = pending_att
        set_thinking(True, "Alice думает")
        resp = _create_with_retry(
            client, model=MODEL, messages=messages, tools=TOOLS_SCHEMA, extra_body=extra,
        )
        msg = resp.choices[0].message

        # «битый» вызов (рассказан текстом / сломан формат)? попросим повторить
        broken = bool(not msg.tool_calls and msg.content and repairs_left > 0
                      and _looks_like_broken_call(msg.content))
        if not broken:
            pending_att = None  # ответ принят — вложения больше не досылаем

        # ответ без инструментов
        if not msg.tool_calls:
            if broken:
                repairs_left -= 1
                console.print("[yellow]Вызов инструмента сломан — прошу Алису повторить…[/]")
                messages.append({"role": "assistant", "content": msg.content})
                messages.append({"role": "user", "content":
                    "Твой вызов инструмента не распознан. НЕ описывай вызов словами и не "
                    "пиши «[Ассистент вызвал] …» или «toolname(...)» текстом — это не "
                    "выполняется. Сделай НАСТОЯЩИЙ вызов: строго блоком ```tool_call``` с "
                    "корректным JSON (поля name и arguments). Большой текст "
                    "(content/old/new) выноси телом между @@поле@@ и @@end@@, без "
                    "обрамления тройными кавычками. Ничего лишнего вокруг блока."})
                continue
            # финальный ответ
            if msg.content:
                console.print(Panel(Markdown(msg.content), title="🤖 Alice",
                                    border_style="cyan", title_align="left"))
            messages.append({"role": "assistant", "content": msg.content or ""})
            return modified

        # стоп-гард от зацикливания: модель часто шлёт финальную сводку ВМЕСТЕ с
        # вызовами и повторяет тот же батч раз за разом (напр. write_file одного и
        # того же файла). Два одинаковых батча подряд → считаем готовым, выходим.
        sig = tuple((tc.function.name, tc.function.arguments) for tc in msg.tool_calls)
        if sig == prev_sig:
            console.print("[yellow]Те же вызовы повторяются — завершаю ход (готово), "
                          "чтобы не зациклиться.[/]")
            if msg.content:
                console.print(Panel(Markdown(msg.content), title="🤖 Alice",
                                    border_style="cyan", title_align="left"))
            messages.append({"role": "assistant", "content": msg.content or ""})
            return modified
        prev_sig = sig

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
            set_thinking(True, f"{name}")
            console.print(f"[dim]→ {name}({_args_preview(tc.function.arguments)})[/]")

            if declined and tc in to_confirm:
                result = "Пользователь отклонил выполнение."
            else:
                fn = TOOLS_IMPL.get(name)
                try:
                    result = fn(**args) if fn else f"Неизвестный инструмент: {name}"
                except Exception as e:
                    result = f"Ошибка инструмента: {e}"

            ok_prefix = _MUTATOR_OK.get(name)
            if ok_prefix and result.startswith(ok_prefix):
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
    _repair_tool_pairs(messages)


def _repair_tool_pairs(messages: list) -> None:
    """Чинит парность assistant.tool_calls ↔ tool, чтобы API не отверг список
    (400 'tool must follow tool_calls'). Удаление сообщений в trim/compact могло
    оставить осиротевший tool или tool_calls без ответов — нормализуем оба случая."""
    declared = set()  # id вызовов, объявленных в assistant.tool_calls
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                declared.add(tc.get("id"))
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    out = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            if m.get("tool_call_id") in declared:   # есть «родитель» — оставляем
                out.append(m)
            # иначе осиротевший результат — выкидываем
        elif role == "assistant" and m.get("tool_calls"):
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in answered]
            if kept:
                nm = dict(m)
                nm["tool_calls"] = kept
                out.append(nm)
            elif (m.get("content") or "").strip():
                out.append({"role": "assistant", "content": m["content"]})
            # пустой assistant без ответов на вызовы — пропускаем
        else:
            out.append(m)
    if len(out) != len(messages):
        messages[:] = out


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
    # не начинаем «хвост» с tool-сообщения и не разрываем группу tool_calls↔tool:
    # сдвигаем границу назад, пока recent[0] — это tool (его assistant ушёл бы в old)
    cut = len(messages) - keep
    while cut > 1 and messages[cut].get("role") == "tool":
        cut -= 1
    system, old, recent = messages[0], messages[1:cut], messages[cut:]
    rendered = "\n".join(_msg_brief(m) for m in old)[:30000]
    try:
        console.print("[dim]Сжимаю историю…[/]")
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
    _repair_tool_pairs(messages)
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


def _select_from_list(prompt_text: str, items: list) -> Optional[object]:
    """Интерактивный выбор из списка: стрелками ↑/↓ по выпадающему меню (Enter —
    выбрать), либо ввести номер. items: список (текст_для_показа, значение).
    Читает ВВОД НАПРЯМУ́Ю (вызывается из потока-читателя — без очереди ответов,
    иначе дедлок). Возвращает значение или None (Enter на пустом — отмена)."""
    labels = [f"{i:>2}. {disp}" for i, (disp, _v) in enumerate(items, 1)]

    class _LC(Completer):
        def get_completions(self, document, complete_event):
            t = document.text_before_cursor.strip().lower()
            for idx, label in enumerate(labels, 1):
                if not t or str(idx).startswith(t) or t in label.lower():
                    yield Completion(str(idx), start_position=-len(document.text_before_cursor),
                                     display=label)

    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        buff = event.current_buffer
        cs = buff.complete_state
        if cs is not None and cs.current_completion is not None:
            buff.apply_completion(cs.current_completion)
        buff.validate_and_handle()

    sess: PromptSession = PromptSession(completer=_LC(), complete_while_typing=True,
                                        key_bindings=kb)

    def _pre_run() -> None:
        sess.default_buffer.start_completion(select_first=False)

    try:
        ans = sess.prompt(prompt_text, pre_run=_pre_run).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if ans.isdigit() and 1 <= int(ans) <= len(items):
        return items[int(ans) - 1][1]
    return None


def pick_session(arg: str, exclude_id: str = "") -> Optional[dict]:
    """Выбрать сессию для /resume: по id-префиксу (arg) или из списка (стрелки/номер)."""
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
    shown = sessions[:15]
    console.print("[bold]Прошлые сессии[/] [dim](↑/↓ + Enter, либо номер; Enter на пустом — отмена):[/]")
    items = [(f"[{s.get('updated', '')}]  {s['id']}  {session_title(s)}", s) for s in shown]
    chosen = _select_from_list("Сессия: ", items)
    if chosen is None:
        console.print("[yellow]Отмена.[/]")
    return chosen


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def main() -> None:
    global ASK, console
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
        stop_adapter(adapter)
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

    # ---- очередь ввода: читатель (этот поток) копит ввод, обработчик берёт по
    # порядку. Вывод не перетирает строку ввода благодаря patch_stdout. ----
    input_q: _queue.Queue = _queue.Queue()
    answer_q: _queue.Queue = _queue.Queue()
    confirm_mode = threading.Event()  # обработчик ждёт ответ (подтверждение/выбор)
    stop = threading.Event()
    state = {"sess": sess, "messages": messages}  # владелец — обработчик

    def ask_via_queue(prompt_text: str) -> str:
        # флаг ставим ДО печати приглашения — иначе строка, набранная сразу после
        # появления вопроса, могла уйти как задача, а не как ответ (TOCTOU)
        confirm_mode.set()
        _status["asking"] = True  # prompt покажет «ждёт ответа», не спиннер
        console.print(prompt_text)
        try:
            while not stop.is_set():
                try:
                    return answer_q.get(timeout=0.3)
                except _queue.Empty:
                    continue
            return ""  # выключение во время ожидания → confirm_batch трактует как «нет»
        finally:
            confirm_mode.clear()
            _status["asking"] = False
            _force_prompt_redraw()

    ASK = ask_via_queue

    def do_command(line: str) -> bool:
        """Обрабатывает слэш-команды СРАЗУ (в потоке-читателе), не дожидаясь
        очереди задач. Возвращает True, если строка была командой."""
        stripped = line.strip()
        cmd = stripped.lower()
        base = cmd.split(" ", 1)[0]
        # команды, меняющие состояние/блокирующие, нельзя выполнять посреди активной
        # задачи или ожидания подтверждения — иначе гонки за state/_undo_stack,
        # перезапуск адаптера под запросом и дедлоки на вложенных промптах
        if base in ("/clear", "/resume", "/undo", "/login") and (
                _status["busy"] or _status["asking"] or confirm_mode.is_set()):
            console.print("[yellow]⏳ Агент сейчас занят — команда "
                          f"{base} будет доступна, когда он освободится. "
                          "Дождись завершения текущей задачи и повтори.[/]")
            return True
        if cmd == "/queue":
            n = input_q.qsize()
            console.print(f"[dim]В очереди: {n}[/]" if n else "[dim]Очередь пуста.[/]")
            return True
        if cmd == "/clear":
            state["sess"] = new_session(sys_prompt)
            state["messages"] = state["sess"]["messages"]
            console.print(f"[dim]Новая сессия {state['sess']['id']} (контекст очищен).[/]")
            return True
        if cmd == "/undo":
            if not _undo_stack:
                console.print("[dim]Нечего откатывать.[/]")
                return True
            snap = _undo_stack.pop()
            for path_str, prev in snap:
                pp = Path(path_str)
                if prev is None:
                    if pp.exists():
                        pp.unlink()
                else:
                    pp.parent.mkdir(parents=True, exist_ok=True)
                    pp.write_bytes(prev)
            console.print(f"[green]Откат выполнен ({len(snap)} файлов).[/]")
            return True
        if cmd == "/help":
            console.print(
                "[dim]Опиши задачу обычным текстом. Команды срабатывают сразу, даже "
                "пока агент работает. Можно печатать следующие задачи во время работы — "
                "они встанут в очередь. Ctrl+V — вставка пути к файлу/скриншота.\n"
                "/queue — очередь · /resume [id] — прошлая сессия · /clear — новая · "
                "/undo — откат правки · /trust — подтверждения · /login — повторный вход · "
                "/exit — выход.[/]")
            return True
        if cmd == "/login":
            do_login()
            return True
        if cmd == "/trust" or cmd.startswith("/trust "):
            arg = stripped[len("/trust"):].strip().lower()
            if arg in TRUST_MODES:
                trust.mode = arg
                console.print(f"[green]Уровень доверия: {arg}[/] [dim]— {TRUST_MODES[arg]}[/]")
            else:
                console.print(f"[bold]Текущий уровень доверия:[/] {trust.mode} "
                              f"[dim]— {TRUST_MODES[trust.mode]}[/]")
                for k, v in TRUST_MODES.items():
                    console.print(f"  [cyan]/trust {k}[/] — {v}")
            return True
        if cmd == "/resume" or cmd.startswith("/resume "):
            chosen = pick_session(stripped[len("/resume"):].strip(),
                                  exclude_id=state["sess"]["id"])
            if chosen is not None:
                state["sess"] = chosen
                state["messages"] = chosen["messages"]
                console.print(f"[green]Вернулся в сессию {chosen['id']}[/] "
                              f"[dim]({session_title(chosen)})[/]")
            return True
        return False

    def do_login() -> None:
        """Принудительный повторный вход в Яндекс (откроется окно браузера),
        затем перезапуск адаптера с новой сессией."""
        nonlocal adapter
        console.print("[dim]Открываю браузер для повторного входа в Яндекс…[/]")
        try:
            creds = alice_session.relogin_sync()
        except Exception as e:
            console.print(f"[red]Не удалось войти: {e}[/]")
            return
        if creds.logged_in:
            console.print("[green]Вход выполнен — режим Pro. Перезапускаю адаптер…[/]")
        else:
            console.print("[yellow]Окно закрыто без входа — остаюсь в Base. "
                          "Перезапускаю адаптер…[/]")
        stop_adapter(adapter)
        adapter = start_adapter()
        if wait_health():
            console.print("[green]Адаптер перезапущен с новой сессией.[/]")
        else:
            console.print("[red]Адаптер не поднялся за 25с. Смотри adapter.log.[/]")

    def handle_line(line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return

        # блок «мой ввод» — серая заливка-маркер (как выделение текста), отделяет
        # задачу от вывода Алисы
        console.print(f"\n[default on grey35] {stripped[:300]} [/]")
        sess_, messages_ = state["sess"], state["messages"]
        atts, cleaned = detect_attachments(line)
        if atts:
            console.print("[dim]📎 прикреплено: " + ", ".join(a["title"] for a in atts) + "[/]")
            messages_.append({"role": "user",
                              "content": cleaned.strip() or "Посмотри прикреплённый файл."})
        else:
            messages_.append({"role": "user", "content": line})
        try:
            compact_history(client, messages_)
            modified = agent_turn(client, messages_, trust, sess_["dialog_id"],
                                  attachments=atts or None)
            if atts and messages_ and messages_[-1].get("role") == "assistant" \
                    and _is_processing(messages_[-1].get("content", "")):
                console.print("[dim]Файл ещё обрабатывается — жду и переспрашиваю…[/]")
                time.sleep(6)
                messages_.append({"role": "user",
                                  "content": "Файл обработан? Ответь по его содержимому."})
                modified = agent_turn(client, messages_, trust, sess_["dialog_id"]) or modified
            if modified and VERIFY_CMD:
                run_verification(client, messages_, trust, sess_["dialog_id"])
        finally:
            set_thinking(False)
        save_session(sess_)

    def worker() -> None:
        while not stop.is_set():
            try:
                line = input_q.get(timeout=0.3)
            except _queue.Empty:
                continue
            if line is None:
                break
            try:
                handle_line(line)
            except Exception as e:
                console.print(f"[red]Ошибка: {e}[/]")
        stop.set()

    def ticker() -> None:
        # Гоняем перерисовку приглашения, пока агент занят: под patch_stdout на
        # Windows авто-refresh prompt_toolkit во время «тихого» ожидания ответа
        # модели не тикает (спиннер замирал). Форсим redraw из внешнего потока.
        while not stop.is_set():
            sess = _input_session
            if sess is not None and (_status["busy"] or _status["asking"]):
                try:
                    app = sess.app
                    loop = getattr(app, "loop", None)
                    if getattr(app, "is_running", False) and loop is not None:
                        loop.call_soon_threadsafe(app._redraw)
                except Exception:
                    pass
            time.sleep(0.1)

    wt = threading.Thread(target=worker, daemon=True)
    tk = threading.Thread(target=ticker, daemon=True)
    try:
        with patch_stdout():
            # rich пишет ANSI в _PTOutput -> prompt_toolkit интерпретирует цвета и
            # печатает над строкой ввода (цвета сохраняются, без мешанины).
            console = Console(file=_PTOutput(), force_terminal=True,
                              color_system="256",
                              width=shutil.get_terminal_size((100, 30)).columns)
            wt.start()
            tk.start()
            while not stop.is_set():
                try:
                    line = read_user_input()
                except (EOFError, KeyboardInterrupt):
                    break
                s = line.strip().lower()
                if s in ("/exit", "/quit", "exit", "quit"):
                    break
                # пока агент ждёт подтверждения/выбора — короткий ввод это ответ;
                # длинную строку считаем новой задачей (ставим в очередь, не теряем,
                # и не трактуем как «да»)
                if confirm_mode.is_set():
                    if _looks_like_answer(line):
                        answer_q.put(line)
                    else:
                        input_q.put(line)
                        console.print("[dim](поставил в очередь; ответь на подтверждение выше — y/n)[/]")
                    continue
                # команды обрабатываем сразу, не кладя в очередь задач
                if s.startswith("/") and do_command(line):
                    continue
                input_q.put(line)
                # если агент сейчас занят — запрос ждёт отправки; показываем его
                # как «ожидающий» (приглушённая заливка), как в claude code
                if _status["busy"]:
                    console.print(f"[grey50 on grey19] {line.strip()[:200]} [/] "
                                  "[dim]· ожидает[/]")
    finally:
        stop.set()
        input_q.put(None)
        _kill_all_bg()
        console.print("\n[dim]Останавливаю адаптер…[/]")
        stop_adapter(adapter)


if __name__ == "__main__":
    main()
