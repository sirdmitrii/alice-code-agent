// Инструменты агента, изолированные рабочей папкой. Порт с Python.
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

let _projectDir = path.resolve(process.env.PROJECT_DIR || process.cwd());
export const projectDir = (): string => _projectDir;
export function setProjectDir(p: string): void {
  _projectDir = path.resolve(p);
}

const NOISE_DIRS = new Set([
  ".git", ".venv", "venv", "node_modules", "__pycache__", ".alice_profile",
  ".alice_sessions", ".idea", ".mypy_cache", ".pytest_cache", ".ruff_cache",
  "dist", "build",
]);

export function safePath(rel: string): string {
  const p = path.resolve(_projectDir, rel);
  const relToRoot = path.relative(_projectDir, p);
  if (p !== _projectDir && (relToRoot.startsWith("..") || path.isAbsolute(relToRoot))) {
    throw new Error(`Путь вне рабочей папки: ${rel}`);
  }
  return p;
}

function isNoise(relPosix: string): boolean {
  return relPosix.split("/").some((part) => NOISE_DIRS.has(part));
}

function relPosix(abs: string): string {
  return path.relative(_projectDir, abs).split(path.sep).join("/");
}

function* walk(dir: string): Generator<{ abs: string; dir: boolean }> {
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const e of entries) {
    const abs = path.join(dir, e.name);
    const isDir = e.isDirectory();
    yield { abs, dir: isDir };
    if (isDir) yield* walk(abs);
  }
}

function globToRegExp(pattern: string): RegExp {
  let re = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*") {
      if (pattern[i + 1] === "*") {
        re += ".*";
        i++;
        if (pattern[i + 1] === "/") i++; // ** / -> любое число папок
      } else {
        re += "[^/]*";
      }
    } else if (c === "?") re += "[^/]";
    else if ("\\^$.|+()[]{}".includes(c)) re += "\\" + c;
    else re += c;
  }
  return new RegExp("^" + re + "$");
}

// ---- инструменты ----

export function toolReadFile(args: { path: string }): string {
  const p = safePath(args.path);
  if (!fs.existsSync(p)) return `Файл не найден: ${args.path}`;
  const data = fs.readFileSync(p, "utf-8");
  return data.length > 60000 ? data.slice(0, 60000) + "\n... [обрезано]" : data;
}

export function toolWriteFile(args: { path: string; content: string }): string {
  const p = safePath(args.path);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, args.content ?? "", "utf-8");
  return `Записано: ${args.path} (${(args.content ?? "").length} символов)`;
}

export function toolEditFile(args: { path: string; old: string; new: string }): string {
  const p = safePath(args.path);
  if (!fs.existsSync(p)) return `Файл не найден: ${args.path}`;
  const text = fs.readFileSync(p, "utf-8");
  const n = args.old ? text.split(args.old).length - 1 : 0;
  if (n === 0) return "Фрагмент `old` не найден — нужно точное совпадение.";
  if (n > 1) return `Фрагмент \`old\` встречается ${n} раз — уточни, чтобы он был уникальным.`;
  fs.writeFileSync(p, text.replace(args.old, args.new), "utf-8");
  return `Отредактировано: ${args.path}`;
}

export function toolListDir(args: { path?: string }): string {
  const p = safePath(args.path ?? ".");
  if (!fs.statSync(p, { throwIfNoEntry: false })?.isDirectory()) return `Не директория: ${args.path}`;
  const items = fs.readdirSync(p, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  const rows = items.map((i) => (i.isDirectory() ? "[d] " : "[f] ") + i.name);
  return rows.join("\n") || "(пусто)";
}

export function toolGlob(args: { pattern: string; path?: string }): string {
  const base = safePath(args.path ?? ".");
  if (!fs.statSync(base, { throwIfNoEntry: false })?.isDirectory()) return `Не директория: ${args.path}`;
  const rx = globToRegExp(args.pattern);
  const rows: string[] = [];
  const matches: { abs: string; dir: boolean }[] = [];
  for (const ent of walk(base)) {
    const relToBase = path.relative(base, ent.abs).split(path.sep).join("/");
    if (rx.test(relToBase)) matches.push(ent);
  }
  matches.sort((a, b) => a.abs.localeCompare(b.abs));
  for (const ent of matches) {
    const rel = relPosix(ent.abs);
    if (isNoise(rel)) continue;
    rows.push((ent.dir ? "[d] " : "") + rel);
    if (rows.length >= 500) {
      rows.push("... [обрезано: >500 совпадений]");
      break;
    }
  }
  return rows.join("\n") || "(ничего не найдено)";
}

export function toolGrep(args: {
  pattern: string;
  path?: string;
  include?: string;
  ignore_case?: boolean;
}): string {
  const base = safePath(args.path ?? ".");
  let rx: RegExp;
  try {
    rx = new RegExp(args.pattern, args.ignore_case ? "i" : "");
  } catch (e) {
    return `Неверное регулярное выражение: ${(e as Error).message}`;
  }
  const stat = fs.statSync(base, { throwIfNoEntry: false });
  const files: string[] = [];
  if (stat?.isFile()) files.push(base);
  else for (const ent of walk(base)) if (!ent.dir) files.push(ent.abs);

  const hits: string[] = [];
  let scanned = 0;
  const incRx = args.include ? globToRegExp(args.include) : null;
  for (const f of files) {
    const rel = relPosix(f);
    if (isNoise(rel)) continue;
    if (incRx && !incRx.test(path.basename(f))) continue;
    let data: string;
    try {
      if (fs.statSync(f).size > 2_000_000) continue;
      data = fs.readFileSync(f, "utf-8");
    } catch {
      continue;
    }
    if (data.slice(0, 1024).includes("\x00")) continue; // бинарник
    scanned++;
    const lines = data.split("\n");
    for (let i = 0; i < lines.length; i++) {
      if (rx.test(lines[i])) {
        hits.push(`${rel}:${i + 1}: ${lines[i].trim().slice(0, 300)}`);
        if (hits.length >= 200) {
          hits.push("... [обрезано: показаны первые 200 совпадений]");
          return hits.join("\n");
        }
      }
    }
  }
  return hits.length ? hits.join("\n") : `Совпадений не найдено (просмотрено файлов: ${scanned}).`;
}

export function toolRunCommand(args: { command: string }): string {
  const r = spawnSync(args.command, {
    shell: true,
    cwd: _projectDir,
    timeout: 120_000,
    encoding: "utf-8",
    maxBuffer: 50 * 1024 * 1024,
  });
  if (r.error && (r.error as any).code === "ETIMEDOUT") return "Команда превысила таймаут 120с.";
  let out = (r.stdout || "") + (r.stderr ? "\n[stderr]\n" + r.stderr : "");
  out = out.trim() || "(нет вывода)";
  if (out.length > 30000) out = out.slice(0, 30000) + "\n... [обрезано]";
  return `exit=${r.status ?? "?"}\n${out}`;
}

export const TOOLS_IMPL: Record<string, (args: any) => string> = {
  read_file: toolReadFile,
  write_file: toolWriteFile,
  edit_file: toolEditFile,
  list_dir: toolListDir,
  glob: toolGlob,
  grep: toolGrep,
  run_command: toolRunCommand,
};

export const DANGEROUS = new Set(["write_file", "edit_file", "run_command"]);

export const TOOLS_SCHEMA = [
  { type: "function", function: { name: "read_file", description: "Прочитать текстовый файл в рабочей папке.", parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } } },
  { type: "function", function: { name: "write_file", description: "Создать или перезаписать файл.", parameters: { type: "object", properties: { path: { type: "string" }, content: { type: "string" } }, required: ["path", "content"] } } },
  { type: "function", function: { name: "edit_file", description: "Заменить уникальный фрагмент `old` на `new` в существующем файле.", parameters: { type: "object", properties: { path: { type: "string" }, old: { type: "string" }, new: { type: "string" } }, required: ["path", "old", "new"] } } },
  { type: "function", function: { name: "list_dir", description: "Показать содержимое директории.", parameters: { type: "object", properties: { path: { type: "string" } } } } },
  { type: "function", function: { name: "glob", description: "Найти файлы по маске относительно рабочей папки (поддержка **).", parameters: { type: "object", properties: { pattern: { type: "string" }, path: { type: "string" } }, required: ["pattern"] } } },
  { type: "function", function: { name: "grep", description: "Поиск по содержимому файлов (regex). Возвращает путь:строка: текст.", parameters: { type: "object", properties: { pattern: { type: "string" }, path: { type: "string" }, include: { type: "string" }, ignore_case: { type: "boolean" } }, required: ["pattern"] } } },
  { type: "function", function: { name: "run_command", description: "Выполнить команду в shell внутри рабочей папки.", parameters: { type: "object", properties: { command: { type: "string" } }, required: ["command"] } } },
];

export const SYSTEM_PROMPT =
  "Ты — кодовый агент в консоли, аналог Claude Code.\n" +
  `Рабочая папка: ${_projectDir}\n` +
  "Действуй пошагово: сначала осмотрись (list_dir, glob, grep, read_file), затем " +
  "вноси небольшие правки (write_file, edit_file) и при необходимости запускай " +
  "команды (run_command), проверяя результат. Никогда не выдумывай содержимое " +
  "файлов — читай их. Когда задача выполнена, дай краткий итог обычным текстом.";
