"""
agent.py — консольный кодовый агент (аналог Claude Code) поверх Alice.

Сам поднимает alice_adapter.py, ждёт его готовности и запускает чат-REPL
с инструментами для работы с файлами и shell. Запускается через run.bat.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


# ---------------------------------------------------------------------------
# Инструменты (изолированы рабочей папкой)
# ---------------------------------------------------------------------------

def _safe_path(rel: str) -> Path:
    p = (PROJECT_DIR / rel).resolve()
    if p != PROJECT_DIR and PROJECT_DIR not in p.parents:
        raise ValueError(f"Путь вне рабочей папки: {rel}")
    return p


def tool_read_file(path: str) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"Файл не найден: {path}"
    data = p.read_text(encoding="utf-8", errors="replace")
    return data[:60000] + "\n... [обрезано]" if len(data) > 60000 else data


def tool_write_file(path: str, content: str) -> str:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Записано: {path} ({len(content)} символов)"


def tool_edit_file(path: str, old: str, new: str) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"Файл не найден: {path}"
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        return "Фрагмент `old` не найден — нужно точное совпадение."
    if n > 1:
        return f"Фрагмент `old` встречается {n} раз — уточни, чтобы он был уникальным."
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"Отредактировано: {path}"


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


TOOLS_IMPL = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_dir": tool_list_dir,
    "run_command": tool_run_command,
}
DANGEROUS = {"write_file", "edit_file", "run_command"}  # требуют подтверждения

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Прочитать текстовый файл в рабочей папке.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Создать или перезаписать файл.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Заменить уникальный фрагмент `old` на `new` в существующем файле.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "old": {"type": "string"},
                                      "new": {"type": "string"}},
                       "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "Показать содержимое директории.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
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
    "Действуй пошагово: сначала осмотрись инструментами (list_dir, read_file), "
    "затем вноси небольшие правки (write_file, edit_file) и при необходимости "
    "запускай команды (run_command), проверяя результат. Никогда не выдумывай "
    "содержимое файлов — читай их. Когда задача выполнена, дай краткий итог "
    "обычным текстом, не вызывая инструменты."
)


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
# Подтверждение опасных операций
# ---------------------------------------------------------------------------

def confirm(name: str, args: dict, auto: set) -> bool:
    if name in auto:
        return True
    preview = json.dumps(args, ensure_ascii=False)
    if len(preview) > 400:
        preview = preview[:400] + "…"
    console.print(Panel(preview, title=f"[yellow]Выполнить {name}?[/]", border_style="yellow"))
    ans = Prompt.ask("[y]да / [n]нет / [a]разрешить всё до конца сессии",
                     choices=["y", "n", "a"], default="y")
    if ans == "a":
        auto.add(name)
        return True
    return ans == "y"


# ---------------------------------------------------------------------------
# Один ход агента: крутим модель, пока она вызывает инструменты
# ---------------------------------------------------------------------------

def agent_turn(client: OpenAI, messages: list, auto: set,
               dialog_id: Optional[str] = None) -> None:
    for _ in range(25):  # предохранитель от бесконечного цикла
        with console.status("[dim]Alice думает…[/]", spinner="dots"):
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS_SCHEMA,
                extra_body={"dialog_id": dialog_id} if dialog_id else {},
            )
        msg = resp.choices[0].message

        # финальный ответ без инструментов
        if not msg.tool_calls:
            if msg.content:
                console.print(Panel(Markdown(msg.content), title="Alice", border_style="cyan"))
            messages.append({"role": "assistant", "content": msg.content or ""})
            return

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

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            shown = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
            console.print(f"[dim]→ {name}({shown})[/]")

            if name in DANGEROUS and not confirm(name, args, auto):
                result = "Пользователь отклонил выполнение."
            else:
                fn = TOOLS_IMPL.get(name)
                try:
                    result = fn(**args) if fn else f"Неизвестный инструмент: {name}"
                except Exception as e:
                    result = f"Ошибка инструмента: {e}"

            preview = result.replace("\n", " ")
            console.print(f"[dim]  ↳ {preview[:200]}{'…' if len(result) > 200 else ''}[/]")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    console.print("[red]Достигнут лимит шагов агента за один запрос.[/]")


# ---------------------------------------------------------------------------
# Грубый guard под 32k-контекст: сносим самые старые сообщения после system
# ---------------------------------------------------------------------------

def trim_history(messages: list, max_chars: int = 80000) -> None:
    def size() -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    while size() > max_chars and len(messages) > 3:
        del messages[1]


# ---------------------------------------------------------------------------
# Сессии: один dialog_id на сеанс (→ один диалог на сайте Алисы), транскрипт
# на диске. Обычный запуск = новая сессия; /resume — вернуться в существующую.
# ---------------------------------------------------------------------------

SESSIONS_DIR = ROOT / ".alice_sessions"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def new_session() -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "dialog_id": str(uuid.uuid4()),
        "created": _now(),
        "updated": _now(),
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
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
        "[dim]/resume — прошлая сессия · /clear — новая · /exit — выход · /help[/]",
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
    sess = new_session()
    messages = sess["messages"]
    auto: set = set()
    console.print(f"[dim]Новая сессия {sess['id']}.[/]\n")

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
                sess = new_session()
                messages = sess["messages"]
                auto.clear()
                console.print(f"[dim]Новая сессия {sess['id']} (контекст очищен).[/]")
                continue
            if cmd == "/help":
                console.print(
                    "[dim]Опиши задачу обычным текстом. Многострочная вставка (Ctrl+V) "
                    "и история (↑/↓) поддерживаются.\n"
                    "/resume [id] — вернуться к прошлой сессии · /clear — новая сессия · "
                    "/exit — выход.[/]")
                continue
            if cmd == "/resume" or cmd.startswith("/resume "):
                chosen = pick_session(stripped[len("/resume"):].strip(),
                                      exclude_id=sess["id"])
                if chosen is not None:
                    sess = chosen
                    messages = sess["messages"]
                    auto.clear()
                    console.print(f"[green]Вернулся в сессию {sess['id']}[/] "
                                  f"[dim]({session_title(sess)})[/]")
                continue
            if not stripped:
                continue

            messages.append({"role": "user", "content": user})
            trim_history(messages)
            try:
                agent_turn(client, messages, auto, sess["dialog_id"])
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
