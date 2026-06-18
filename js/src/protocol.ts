// Протокол tool_call поверх Алисы: рендер истории в один промпт + разбор
// текстового ответа обратно в OpenAI-совместимые tool_calls. Порт с Python.
import { randomUUID } from "node:crypto";

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface Msg {
  role: string;
  content?: any;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

// Фенс-блок (тег любой) — внутри JSON-вызов.
const FENCE_RE = /```[^\n`]*\n([\s\S]*?)```/g;
// Тело аргумента: строка @@ключ@@ ... строка @@end@@ (или конец текста), дословно.
const BODY_RE =
  /^@@(?!end@@)([A-Za-z_]\w*)@@[ \t]*\r?\n([\s\S]*?)(?:^@@end@@[ \t]*\r?$|(?![\s\S]))/gm;
// Плейсхолдер тела в JSON: значение вида "@@ключ@@".
const PLACEHOLDER_RE = /^@@([A-Za-z_]\w*)@@$/;
// Тело целиком в одном ```-фенсе (модель часто оборачивает код).
const WRAP_RE = /^```[^\n`]*\r?\n([\s\S]*?)\r?\n?```\s*$/;

function looksLikeCall(o: any): boolean {
  return (
    !!o && typeof o === "object" && !Array.isArray(o) &&
    typeof o.name === "string" && o.name.length > 0 &&
    !!o.arguments && typeof o.arguments === "object" && !Array.isArray(o.arguments)
  );
}

// Терпимый JSON.parse: типографские кавычки -> ASCII, висячие запятые.
function loadsTolerant(s: string): any {
  const t = s.trim().replace(/[“”«»]/g, '"').replace(/[‘’]/g, "'");
  for (const cand of [t, t.replace(/,(\s*[}\]])/g, "$1")]) {
    try {
      return JSON.parse(cand);
    } catch {
      /* пробуем следующий вариант */
    }
  }
  return null;
}

function mkCall(o: any): ToolCall {
  return {
    id: "call_" + randomUUID().replace(/-/g, "").slice(0, 24),
    type: "function",
    function: { name: o.name, arguments: JSON.stringify(o.arguments ?? {}) },
  };
}

// Достаёт блоки @@ключ@@…@@end@@ -> {ключ: [тела по порядку]} + их позиции.
function extractBodies(text: string): { bodies: Map<string, string[]>; spans: [number, number][] } {
  const bodies = new Map<string, string[]>();
  const spans: [number, number][] = [];
  BODY_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = BODY_RE.exec(text)) !== null) {
    let body = m[2];
    if (body.endsWith("\n")) body = body.slice(0, -1);
    if (body.endsWith("\r")) body = body.slice(0, -1);
    const wrap = WRAP_RE.exec(body);
    if (wrap) body = wrap[1];
    if (!bodies.has(m[1])) bodies.set(m[1], []);
    bodies.get(m[1])!.push(body);
    spans.push([m.index, m.index + m[0].length]);
  }
  return { bodies, spans };
}

// Подставляет тела вместо плейсхолдеров "@@ключ@@" по порядку (для нескольких
// вызовов с одинаковым ключом — каждому своё тело).
function resolveBodies(call: any, bodies: Map<string, string[]>): any {
  const args = call.arguments ?? {};
  for (const k of Object.keys(args)) {
    const v = args[k];
    if (typeof v === "string") {
      const ph = PLACEHOLDER_RE.exec(v.trim());
      if (ph) {
        const lst = bodies.get(ph[1]);
        if (lst && lst.length) args[k] = lst.shift()!;
      }
    }
  }
  return call;
}

function stripSpans(text: string, spans: [number, number][]): string {
  if (!spans.length) return text.trim();
  const sorted = [...spans].sort((a, b) => a[0] - b[0]);
  let out = "";
  let prev = 0;
  for (const [s, e] of sorted) {
    if (s >= prev) {
      out += text.slice(prev, s);
      prev = e;
    }
  }
  out += text.slice(prev);
  return out.trim();
}

export function parseToolCalls(text: string): { clean: string; toolCalls: ToolCall[] } {
  const { bodies, spans: bodySpans } = extractBodies(text);
  const toolCalls: ToolCall[] = [];
  const callSpans: [number, number][] = [];
  FENCE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = FENCE_RE.exec(text)) !== null) {
    const obj = loadsTolerant(m[1]);
    const candidates = Array.isArray(obj) ? obj : [obj];
    const matched = candidates.filter(looksLikeCall);
    if (matched.length) {
      for (const c of matched) toolCalls.push(mkCall(resolveBodies(c, bodies)));
      callSpans.push([m.index, m.index + m[0].length]);
    }
  }
  if (toolCalls.length) {
    return { clean: stripSpans(text, [...callSpans, ...bodySpans]), toolCalls };
  }
  const obj = loadsTolerant(text);
  if (looksLikeCall(obj)) {
    return { clean: "", toolCalls: [mkCall(resolveBodies(obj, bodies))] };
  }
  return { clean: text.trim(), toolCalls: [] };
}

// Компактный рендер аргументов прошлого вызова: длинные строки -> «<N символов>».
export function abbrevArgs(argsJson: string, threshold = 500): string {
  let d: any;
  try {
    d = JSON.parse(argsJson || "{}");
  } catch {
    return (argsJson || "").slice(0, threshold);
  }
  if (typeof d !== "object" || d === null) return String(d).slice(0, threshold);
  return Object.entries(d)
    .map(([k, v]) =>
      typeof v === "string" && v.length > threshold
        ? `${k}=<${v.length} символов>`
        : `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`,
    )
    .join(", ");
}

function contentToText(content: any): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.filter((p) => p && typeof p === "object").map((p) => p.text ?? "").join("\n");
  }
  return "";
}

export function buildToolInstructions(tools: any[]): string {
  const lines = [
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
    "кавычками. Пример записи файла:",
    "```tool_call",
    '{"name": "write_file", "arguments": {"path": "app.py", "content": "@@content@@"}}',
    "```",
    "@@content@@",
    "import sys",
    'print("привет")',
    "@@end@@",
    "",
    "Простые инструменты (read_file, list_dir, glob, grep, run_command) вызывай",
    "обычным JSON без тел. Если инструмент не нужен — отвечай обычным текстом.",
    "Доступные инструменты:",
  ];
  for (const t of tools) {
    const fn = t.function ?? t;
    lines.push(
      `- ${fn.name}: ${fn.description ?? ""}\n  параметры (JSON Schema): ${JSON.stringify(fn.parameters ?? {})}`,
    );
  }
  return lines.join("\n");
}

export function renderMessagesToPrompt(messages: Msg[], tools: any[] | null): string {
  const parts: string[] = [];
  const sys = messages.filter((m) => m.role === "system").map((m) => contentToText(m.content));
  if (sys.length) parts.push("[Системные инструкции]\n" + sys.join("\n"));
  if (tools) parts.push(buildToolInstructions(tools));

  const idToName = new Map<string, string>();
  for (const m of messages) {
    if (m.role === "system") continue;
    if (m.role === "user") {
      parts.push("[Пользователь]\n" + contentToText(m.content));
    } else if (m.role === "assistant") {
      const txt = contentToText(m.content);
      if (txt) parts.push("[Ассистент]\n" + txt);
      for (const tc of m.tool_calls ?? []) {
        idToName.set(tc.id, tc.function.name);
        parts.push(`[Ассистент вызвал] ${tc.function.name}(${abbrevArgs(tc.function.arguments)})`);
      }
    } else if (m.role === "tool") {
      const name = idToName.get(m.tool_call_id ?? "") ?? "";
      const head = name ? `[Результат: ${name}]` : "[Результат инструмента]";
      parts.push(head + "\n" + contentToText(m.content));
    }
  }
  parts.push("[Ассистент]");
  return parts.join("\n\n");
}

// OpenAI-совместимый ответ из текста Алисы.
export function makeCompletion(text: string, model: string): any {
  const { clean, toolCalls } = parseToolCalls(text);
  const message: any = { role: "assistant", content: clean || null };
  let finish = "stop";
  if (toolCalls.length) {
    message.tool_calls = toolCalls;
    finish = "tool_calls";
  }
  return {
    id: "chatcmpl-" + randomUUID().replace(/-/g, ""),
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, message, finish_reason: finish }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}
