// WebSocket-клиент к Алисе (протокол uniproxy). Порт AliceClient с Python.
import { randomUUID } from "node:crypto";
import WebSocket from "ws";
import { getCredentials, refresh, type Creds } from "./session-capture";

const WS_URL = process.env.ALICE_WS_URL || "wss://uniproxy.alice.yandex.ru/uni.ws";
const ORIGIN = "https://alice.yandex.ru";
const USER_AGENT =
  process.env.ALICE_USER_AGENT ||
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/146.0.0.0 YaBrowser/26.4.0.0 Safari/537.36";
const SPEECHKIT_VERSION = process.env.ALICE_SPEECHKIT_VERSION || "4.16.7";
const ALICE_MODE = process.env.ALICE_MODE ?? "Pro";
const DIALOG_ID_FIXED = process.env.ALICE_DIALOG_ID || "";
const REQUEST_TIMEOUT = Number(process.env.ALICE_TIMEOUT || "120") * 1000;

const EXPERIMENTS = [
  "standalone_alice_2_0", "supports_streaming_response", "exp_flag_chat_dialog_history",
  "exp_flag_chat_dialog_history_main_context_save", "use_server_pings",
  "enable_new_colors_for_alice_chat",
];
const SUPPORTED_FEATURES = [
  "background_response_streaming", "supports_bso_answer", "open_link", "server_action",
  "div2_cards", "supports_streaming_response", "supports_rich_json_cards",
  "supports_markdown_response", "print_text_in_message_view", "show_loader_directive",
  "supports_default_dialog_as_dedicated", "supports_multi_model_dialogs",
  "supports_unlimited_dialogs_creation",
];

function nowStamp(): { clientTime: string; ts: string } {
  const t = Math.floor(Date.now() / 1000);
  const d = new Date((t + 3 * 3600) * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  const clientTime =
    `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}T` +
    `${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
  return { clientTime, ts: String(t) };
}

function syncState(creds: Creds): any {
  return {
    event: {
      header: { namespace: "System", name: "SynchronizeState", seqNumber: 1, messageId: randomUUID() },
      payload: {
        auth_token: creds.auth_token,
        uuid: creds.uuid,
        vins: { application: { app_id: "ru.yandex.webstandalone.desktop", platform: "windows", device_id: creds.uuid } },
        supported_features: SUPPORTED_FEATURES,
        request: { experiments: EXPERIMENTS },
        speechkitVersion: SPEECHKIT_VERSION,
        icookie: creds.icookie,
        sae_cookie: creds.sae_cookie,
        yexp_cookie: "",
      },
    },
  };
}

function textInput(creds: Creds, text: string, requestId: string, dialogId: string): any {
  const { clientTime, ts } = nowStamp();
  return {
    event: {
      header: { namespace: "Vins", name: "TextInput", seqNumber: 2, messageId: randomUUID() },
      payload: {
        application: {
          app_id: "ru.yandex.webstandalone.desktop", app_version: "unknown", platform: "windows",
          os_version: USER_AGENT.toLowerCase(), uuid: creds.uuid, device_id: creds.uuid,
          lang: "ru-RU", client_time: clientTime, timezone: "Europe/Moscow", timestamp: ts,
        },
        header: { request_id: requestId, dialog_id: dialogId, dialog_type: 2 },
        request: {
          event: { type: "text_input", text },
          voice_session: false,
          experiments: EXPERIMENTS,
          additional_options: {
            bass_options: { user_agent: USER_AGENT, screen_scale_factor: 1 },
            origin_domain: "yandex.ru",
            supported_features: SUPPORTED_FEATURES,
            unsupported_features: [],
            icookie: creds.icookie,
          },
        },
        format: "audio/ogg;codecs=opus",
        mime: "audio/webm;codecs=opus",
        topic: "desktopgeneral",
        punctuation: false,
        alice_2_settings: { preset: "", mode: ALICE_MODE },
      },
    },
  };
}

function textFromDirective(payload: any): string | null {
  const jr = payload.json_response;
  if (jr && typeof jr === "object") {
    const base = jr.base_response || {};
    if (typeof base.text === "string" && base.text) return base.text;
    for (const c of base.cards || []) {
      const tc = c?.text_card || {};
      if (typeof tc.text === "string" && tc.text) return tc.text;
    }
  }
  const resp = payload.response;
  if (resp && typeof resp === "object") {
    const card = resp.card;
    if (card && typeof card === "object" && typeof card.text === "string") return card.text;
  }
  return null;
}

class AliceClient {
  async complete(prompt: string, dialogId?: string): Promise<string> {
    let creds = await getCredentials();
    try {
      return await this.roundtrip(creds, prompt, dialogId);
    } catch (e) {
      // возможно протухли токен/кука — тихо обновляем и повторяем
      try {
        creds = await refresh(false);
      } catch {
        throw new Error(
          "Сессия Алисы недействительна и не обновилась автоматически. " +
            "Перезапусти и залогинься заново.",
        );
      }
      return await this.roundtrip(creds, prompt, dialogId);
    }
  }

  private roundtrip(creds: Creds, prompt: string, dialogId?: string): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      const did = dialogId || DIALOG_ID_FIXED || randomUUID();
      const reqId = randomUUID();
      const headers: Record<string, string> = { Origin: ORIGIN, "User-Agent": USER_AGENT };
      if (creds.cookies) headers["Cookie"] = creds.cookies;

      const ws = new WebSocket(WS_URL, { headers, maxPayload: 0 });
      let latest = "";
      let sawStream = false;
      let lastFrame = "";
      let finished = false;
      let idleTimer: NodeJS.Timeout | undefined;

      const finish = (err: Error | null, val?: string) => {
        if (finished) return;
        finished = true;
        if (idleTimer) clearTimeout(idleTimer);
        clearTimeout(overall);
        try {
          ws.close();
        } catch {
          /* ignore */
        }
        if (err) reject(err);
        else resolve(val ?? "");
      };

      const overall = setTimeout(
        () => (latest ? finish(null, latest) : finish(new Error("Таймаут запроса к Алисе"))),
        REQUEST_TIMEOUT + 60_000,
      );

      const resetIdle = () => {
        if (idleTimer) clearTimeout(idleTimer);
        const ms = latest ? (sawStream ? 30_000 : 4_000) : REQUEST_TIMEOUT;
        idleTimer = setTimeout(() => {
          if (latest) finish(null, latest);
          else finish(new Error("Поток Алисы без текста. Последний кадр: " + lastFrame));
        }, ms);
      };

      ws.on("open", () => {
        ws.send(JSON.stringify(syncState(creds)));
        ws.send(JSON.stringify(textInput(creds, prompt, reqId, did)));
        resetIdle();
      });
      ws.on("message", (data: WebSocket.RawData) => {
        const raw = data.toString();
        lastFrame = raw.slice(0, 600);
        let frame: any;
        try {
          frame = JSON.parse(raw);
        } catch {
          return;
        }
        const directive = frame.directive || {};
        const name = directive.header?.name;
        const payload = directive.payload || {};
        if (name === "DeferredAliceResponse" || name === "VinsResponse") {
          if (name === "DeferredAliceResponse") sawStream = true;
          const txt = textFromDirective(payload);
          if (txt) latest = txt;
          if (payload.json_response?.is_last) {
            finish(null, latest);
            return;
          }
        }
        resetIdle();
      });
      ws.on("error", (e: Error) => (latest ? finish(null, latest) : finish(e)));
      ws.on("close", () => (latest ? finish(null, latest) : finish(new Error("ws закрыт без ответа"))));
    });
  }
}

export const alice = new AliceClient();
