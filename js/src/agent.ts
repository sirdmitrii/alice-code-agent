// Консольный REPL-агент (аналог Claude Code) на Алисе. Порт agent.py.
import * as readline from "node:readline";
import OpenAI from "openai";
import { startServer } from "./adapter";
import { ensure, credsSummary } from "./session-capture";
import {
  loadSessions, newSession, saveSession, sessionTitle, type Session,
} from "./sessions";
import { argsPreview, Trust, TRUST_MODES } from "./trust";
import { DANGEROUS, SYSTEM_PROMPT, TOOLS_IMPL, TOOLS_SCHEMA } from "./tools";

const PORT = Number(process.env.PORT || "8787");
const MODEL = process.env.ALICE_MODEL_NAME || "alice";

let rl: readline.Interface;
const ask = (q: string): Promise<string> =>
  new Promise((res) => rl.question(q, (a) => res(a)));

function printAlice(text: string): void {
  const line = "─".repeat(60);
  console.log(`\n┌${line}┐\n${text}\n└${line}┘`);
}

const BROKEN_CALL_RE = /^@@(?:end|[A-Za-z_]\w*)@@\s*$/m;
function looksLikeBrokenCall(text: string): boolean {
  return !!text && (text.includes("```tool_call") || BROKEN_CALL_RE.test(text));
}

async function confirmBatch(calls: any[]): Promise<"yes" | "no" | "always"> {
  const title = calls.length === 1 ? "Выполнить операцию?" : `Выполнить ${calls.length} операции?`;
  console.log(`\n[${title}]`);
  for (const tc of calls) console.log(`  • ${tc.function.name}(${argsPreview(tc.function.arguments)})`);
  const ans = (await ask("[y]да / [n]нет / [a]больше не спрашивать: ")).trim().toLowerCase();
  return ans === "n" ? "no" : ans === "a" ? "always" : "yes";
}

async function agentTurn(
  client: OpenAI,
  messages: any[],
  trust: Trust,
  dialogId: string,
): Promise<void> {
  let repairsLeft = 2;
  for (let step = 0; step < 25; step++) {
    const resp = await client.chat.completions.create(
      { model: MODEL, messages: messages as any, tools: TOOLS_SCHEMA as any },
      { headers: dialogId ? { "x-alice-dialog-id": dialogId } : {} },
    );
    const msg = resp.choices[0].message;
    const toolCalls = msg.tool_calls;

    if (!toolCalls || !toolCalls.length) {
      if (msg.content && repairsLeft > 0 && looksLikeBrokenCall(msg.content)) {
        repairsLeft--;
        console.log("Вызов инструмента сломан — прошу Алису повторить…");
        messages.push({ role: "assistant", content: msg.content });
        messages.push({
          role: "user",
          content:
            "Твой вызов инструмента не распознан — формат сломан. Повтори его СТРОГО " +
            "блоком ```tool_call``` с корректным JSON (поля name и arguments). Большой " +
            "текст (content/old/new) выноси телом между @@поле@@ и @@end@@, без обрамления " +
            "тройными кавычками. Ничего лишнего вокруг блока.",
        });
        continue;
      }
      if (msg.content) printAlice(msg.content);
      messages.push({ role: "assistant", content: msg.content || "" });
      return;
    }

    if (msg.content) console.log(msg.content);
    messages.push({
      role: "assistant",
      content: msg.content || null,
      tool_calls: toolCalls.map((tc) => ({
        id: tc.id,
        type: "function",
        function: { name: tc.function.name, arguments: tc.function.arguments },
      })),
    });

    const toConfirm = toolCalls.filter((tc) => trust.needsConfirm(tc.function.name));
    let declined = false;
    if (toConfirm.length) {
      const decision = await confirmBatch(toConfirm);
      declined = decision === "no";
      if (decision === "always") {
        trust.mode = "none";
        console.log("Уровень доверия → none: больше не спрашиваю в этой сессии.");
      }
    }

    for (const tc of toolCalls) {
      const name = tc.function.name;
      let args: any = {};
      try {
        args = JSON.parse(tc.function.arguments || "{}");
      } catch {
        args = {};
      }
      console.log(`→ ${name}(${argsPreview(tc.function.arguments)})`);
      let result: string;
      if (declined && toConfirm.includes(tc)) {
        result = "Пользователь отклонил выполнение.";
      } else {
        const fn = TOOLS_IMPL[name];
        try {
          result = fn ? fn(args) : `Неизвестный инструмент: ${name}`;
        } catch (e) {
          result = `Ошибка инструмента: ${(e as Error).message}`;
        }
      }
      console.log(`  ↳ ${result.replace(/\n/g, " ").slice(0, 200)}`);
      messages.push({ role: "tool", tool_call_id: tc.id, content: result });
    }
  }
  console.log("Достигнут лимит шагов агента за один запрос.");
}

function trimHistory(messages: any[], maxChars = 80000): void {
  const size = () => messages.reduce((s, m) => s + JSON.stringify(m).length, 0);
  while (size() > maxChars && messages.length > 3) messages.splice(1, 1);
}

async function pickSession(arg: string, excludeId: string): Promise<Session | null> {
  const sessions = loadSessions().filter((s) => s.id !== excludeId);
  if (!sessions.length) {
    console.log("Нет сохранённых сессий для возврата.");
    return null;
  }
  if (arg) {
    const found = sessions.find((s) => s.id.startsWith(arg));
    if (!found) console.log(`Сессия '${arg}' не найдена.`);
    return found || null;
  }
  console.log("Прошлые сессии:");
  const shown = sessions.slice(0, 15);
  shown.forEach((s, i) => console.log(`  ${i + 1}  ${s.updated}  ${s.id}  ${sessionTitle(s)}`));
  const a = (await ask("Номер сессии (Enter — отмена): ")).trim();
  if (!a) return null;
  const n = Number(a);
  if (Number.isInteger(n) && n >= 1 && n <= shown.length) return shown[n - 1];
  return sessions.find((s) => s.id.startsWith(a)) || null;
}

async function main(): Promise<void> {
  console.log(`Alice Code   модель: ${MODEL}\n/resume · /clear · /trust · /exit · /help`);

  console.log("Проверяю сессию Алисы…");
  let creds;
  try {
    creds = await ensure(true);
  } catch (e) {
    console.error("Не удалось подготовить сессию Алисы:", (e as Error).message);
    return;
  }
  console.log(creds.logged_in ? "Сессия готова — режим Pro." : "Режим Base (вход не выполнен).");

  console.log("Поднимаю адаптер…");
  const server = await startServer(PORT);
  console.log("Готово, можно работать.\n");

  const client = new OpenAI({ baseURL: `http://127.0.0.1:${PORT}/v1`, apiKey: "local" });
  rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  let sess = newSession(SYSTEM_PROMPT);
  let messages = sess.messages as any[];
  const trust = new Trust();
  console.log(`Новая сессия ${sess.id}. Уровень доверия: ${trust.mode} (${TRUST_MODES[trust.mode]}).`);

  try {
    while (true) {
      const user = await ask("\n› ");
      const stripped = user.trim();
      const cmd = stripped.toLowerCase();
      if (["/exit", "/quit", "exit", "quit"].includes(cmd)) break;
      if (cmd === "/clear") {
        sess = newSession(SYSTEM_PROMPT);
        messages = sess.messages as any[];
        console.log(`Новая сессия ${sess.id} (контекст очищен).`);
        continue;
      }
      if (cmd === "/help") {
        console.log(
          "Опиши задачу обычным текстом. /resume [id] — прошлая сессия · /clear — новая · " +
            "/trust [all|danger|none] — подтверждения · /exit — выход.",
        );
        continue;
      }
      if (cmd === "/trust" || cmd.startsWith("/trust ")) {
        const arg = stripped.slice("/trust".length).trim().toLowerCase();
        if (arg in TRUST_MODES) {
          trust.mode = arg;
          console.log(`Уровень доверия: ${arg} — ${TRUST_MODES[arg]}`);
        } else {
          console.log(`Текущий уровень доверия: ${trust.mode} — ${TRUST_MODES[trust.mode]}`);
          for (const [k, v] of Object.entries(TRUST_MODES)) console.log(`  /trust ${k} — ${v}`);
        }
        continue;
      }
      if (cmd === "/resume" || cmd.startsWith("/resume ")) {
        const chosen = await pickSession(stripped.slice("/resume".length).trim(), sess.id);
        if (chosen) {
          sess = chosen;
          messages = sess.messages as any[];
          console.log(`Вернулся в сессию ${sess.id} (${sessionTitle(sess)})`);
        }
        continue;
      }
      if (!stripped) continue;

      messages.push({ role: "user", content: user });
      trimHistory(messages);
      try {
        await agentTurn(client, messages, trust, sess.dialog_id);
      } catch (e) {
        console.error("Ошибка запроса:", (e as Error).message);
      }
      saveSession(sess);
    }
  } finally {
    rl.close();
    server.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
