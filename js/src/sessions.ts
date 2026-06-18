// Сессии: один dialog_id на сеанс (один диалог на сайте Алисы), транскрипт на
// диске. Обычный запуск = новая сессия; /resume — вернуться в существующую.
import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { Msg } from "./protocol";

export interface Session {
  id: string;
  dialog_id: string;
  created: string;
  updated: string;
  messages: Msg[];
}

// Каталог сессий вынесен в config, чтобы тесты могли его переопределить.
export const config = { sessionsDir: path.resolve(".alice_sessions") };

function now(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function newSession(systemPrompt: string): Session {
  return {
    id: randomUUID().replace(/-/g, "").slice(0, 8),
    dialog_id: randomUUID(),
    created: now(),
    updated: now(),
    messages: [{ role: "system", content: systemPrompt }],
  };
}

export function saveSession(sess: Session): void {
  fs.mkdirSync(config.sessionsDir, { recursive: true });
  sess.updated = now();
  fs.writeFileSync(
    path.join(config.sessionsDir, `${sess.id}.json`),
    JSON.stringify(sess, null, 2),
    "utf-8",
  );
}

export function loadSessions(): Session[] {
  if (!fs.existsSync(config.sessionsDir)) return [];
  const out: Session[] = [];
  for (const f of fs.readdirSync(config.sessionsDir)) {
    if (!f.endsWith(".json")) continue;
    try {
      out.push(JSON.parse(fs.readFileSync(path.join(config.sessionsDir, f), "utf-8")));
    } catch {
      /* пропускаем битый файл */
    }
  }
  out.sort((a, b) => (b.updated || "").localeCompare(a.updated || ""));
  return out;
}

export function sessionTitle(sess: Session): string {
  for (const m of sess.messages ?? []) {
    if (m.role === "user") {
      const t = (typeof m.content === "string" ? m.content : "").trim().replace(/\n/g, " ");
      return t.length > 50 ? t.slice(0, 50) + "…" : t;
    }
  }
  return "(пусто)";
}
