import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { config, loadSessions, newSession, saveSession, sessionTitle } from "../src/sessions";

let tmp: string;

beforeAll(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "alice-sess-"));
  config.sessionsDir = tmp;
});

afterAll(() => fs.rmSync(tmp, { recursive: true, force: true }));

describe("сессии", () => {
  it("сохранение/загрузка, разные dialog_id, заголовок", () => {
    const s1 = newSession("sys");
    s1.messages.push({ role: "user", content: "первая задача про файлы" });
    saveSession(s1);
    const s2 = newSession("sys");
    s2.messages.push({ role: "user", content: "вторая задача" });
    saveSession(s2);

    const loaded = loadSessions();
    expect(loaded.length).toBe(2);
    expect(s1.dialog_id).not.toBe(s2.dialog_id);
    expect(sessionTitle(s1).startsWith("первая задача")).toBe(true);
  });
});
