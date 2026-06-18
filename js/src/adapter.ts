// Локальный OpenAI-совместимый HTTP-адаптер поверх WS-клиента Алисы. Порт.
import http from "node:http";
import { alice } from "./alice";
import { makeCompletion, renderMessagesToPrompt } from "./protocol";

const MODEL = process.env.ALICE_MODEL_NAME || "alice";

function sendJson(res: http.ServerResponse, code: number, obj: unknown): void {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
  res.end(body);
}

function readJson(req: http.IncomingMessage): Promise<any> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

async function handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  if (req.method === "GET" && req.url === "/health") {
    return sendJson(res, 200, { status: "ok" });
  }
  if (req.method === "GET" && req.url === "/v1/models") {
    return sendJson(res, 200, {
      object: "list",
      data: [{ id: MODEL, object: "model", created: 0, owned_by: "yandex" }],
    });
  }
  if (req.method === "POST" && req.url === "/v1/chat/completions") {
    const body = await readJson(req);
    const messages = body.messages || [];
    if (!messages.length) return sendJson(res, 400, { error: { message: "messages пуст" } });
    const tools = body.tools || null;
    // dialog_id агент передаёт заголовком (надёжнее, чем extra body)
    const dialogId = (req.headers["x-alice-dialog-id"] as string) || body.dialog_id;
    const prompt = renderMessagesToPrompt(messages, tools);
    const text = await alice.complete(prompt, dialogId);
    return sendJson(res, 200, makeCompletion(text, MODEL));
  }
  sendJson(res, 404, { error: { message: "not found" } });
}

export function startServer(port: number): Promise<http.Server> {
  const server = http.createServer((req, res) => {
    handle(req, res).catch((e) =>
      sendJson(res, 502, { error: { message: String((e as Error)?.message || e) } }),
    );
  });
  return new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve(server)));
}

// Запуск как отдельного процесса (необязательно — агент поднимает сервер сам).
const invokedDirectly =
  process.argv[1] && import.meta.url.endsWith(process.argv[1].split(/[\\/]/).pop() || "");
if (invokedDirectly) {
  const port = Number(process.env.PORT || "8787");
  startServer(port).then(() => console.log(`adapter on http://127.0.0.1:${port}`));
}
