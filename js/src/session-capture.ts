// Автозахват и обновление сессии веб-Алисы через браузер (Playwright). Порт
// alice_session.py. Браузер открывается лишь на момент захвата и закрывается.
import * as fs from "node:fs";
import * as path from "node:path";

const PROFILE_DIR = path.resolve(".alice_profile");
const CREDS_FILE = path.resolve(".alice_creds.json");
const ALICE_URL = "https://alice.yandex.ru/";
const WS_MATCH = "uniproxy.alice.yandex.ru";
// auth_token у веб-Алисы — константа приложения (одинакова для всех); личность
// задаётся куками. Используем как фолбэк, если кадр SynchronizeState не пойман.
const AUTH_TOKEN_FALLBACK = "effd5a3f-fd42-4a18-83a1-61766a6d0924";
const LOGIN_TIMEOUT = Number(process.env.ALICE_LOGIN_TIMEOUT || "300") * 1000;
const FRAME_TIMEOUT = Number(process.env.ALICE_FRAME_TIMEOUT || "25") * 1000;

export interface Creds {
  auth_token: string;
  uuid: string;
  icookie: string;
  sae_cookie: string;
  cookies: string;
  logged_in: boolean;
  captured_at: number;
}

let cache: Creds | null = null;
let refreshing: Promise<Creds> | null = null;

function envCreds(): Creds | null {
  const token = (process.env.ALICE_AUTH_TOKEN || "").trim();
  if (!token) return null;
  const cookies = (process.env.ALICE_COOKIES || "").trim();
  return {
    auth_token: token,
    uuid: (process.env.ALICE_UUID || "").trim(),
    icookie: (process.env.ALICE_ICOOKIE || "").trim(),
    sae_cookie: (process.env.ALICE_SAE_COOKIE || "").trim(),
    cookies,
    logged_in: !!cookies,
    captured_at: Date.now() / 1000,
  };
}

function load(): Creds | null {
  if (!fs.existsSync(CREDS_FILE)) return null;
  try {
    return JSON.parse(fs.readFileSync(CREDS_FILE, "utf-8")) as Creds;
  } catch {
    return null;
  }
}

function save(c: Creds): void {
  fs.writeFileSync(CREDS_FILE, JSON.stringify(c, null, 2), "utf-8");
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function capture(headless: boolean, loginTimeout = 0): Promise<Creds | null> {
  const { chromium } = await import("playwright");
  let syncPayload: any = null;
  let resolveFrame: (() => void) | null = null;
  const framePromise = new Promise<void>((r) => (resolveFrame = r));

  const ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless,
    args: ["--disable-blink-features=AutomationControlled"],
    viewport: { width: 1100, height: 820 },
  });
  try {
    const page = ctx.pages()[0] ?? (await ctx.newPage());
    page.on("websocket", (ws) => {
      if (!ws.url().includes(WS_MATCH)) return;
      ws.on("framesent", (ev) => {
        const txt = typeof ev.payload === "string" ? ev.payload : ev.payload.toString("utf-8");
        let obj: any;
        try {
          obj = JSON.parse(txt);
        } catch {
          return;
        }
        if (obj?.event?.header?.name === "SynchronizeState") {
          syncPayload = obj.event.payload || {};
          resolveFrame?.();
        }
      });
    });

    await page.goto(ALICE_URL, { waitUntil: "domcontentloaded", timeout: 30_000 }).catch(() => {});
    await Promise.race([framePromise, sleep(FRAME_TIMEOUT)]);

    const loggedIn = async () =>
      (await ctx.cookies()).some((c) => c.name === "Session_id");

    if (loginTimeout > 0 && !(await loggedIn())) {
      console.log("  → Залогинься в открывшемся окне Яндекса. Жду…");
      let waited = 0;
      while (waited < loginTimeout && !(await loggedIn())) {
        await sleep(1500);
        waited += 1500;
      }
      if (await loggedIn()) {
        syncPayload = null;
        let resolve2: (() => void) | null = null;
        const p2 = new Promise<void>((r) => (resolve2 = r));
        page.once("websocket", (ws) => {
          if (!ws.url().includes(WS_MATCH)) return;
          ws.on("framesent", (ev) => {
            const txt = typeof ev.payload === "string" ? ev.payload : ev.payload.toString("utf-8");
            try {
              const obj = JSON.parse(txt);
              if (obj?.event?.header?.name === "SynchronizeState") {
                syncPayload = obj.event.payload || {};
                resolve2?.();
              }
            } catch {
              /* ignore */
            }
          });
        });
        await page.reload({ waitUntil: "domcontentloaded", timeout: 30_000 }).catch(() => {});
        await Promise.race([p2, sleep(FRAME_TIMEOUT)]);
      }
    }

    const cookies = await ctx.cookies();
    const logged = cookies.some((c) => c.name === "Session_id");
    const cookieStr = cookies
      .filter((c) => c.domain.endsWith("yandex.ru"))
      .map((c) => `${c.name}=${c.value}`)
      .join("; ");
    const cookieVal = (name: string) => cookies.find((c) => c.name === name)?.value || "";

    if (!syncPayload && !cookieStr) return null;
    return {
      auth_token: syncPayload?.auth_token || AUTH_TOKEN_FALLBACK,
      uuid: syncPayload?.uuid || cookieVal("alice_uuid"),
      icookie: syncPayload?.icookie || cookieVal("i"),
      sae_cookie: syncPayload?.sae_cookie || cookieVal("sae"),
      cookies: cookieStr,
      logged_in: logged,
      captured_at: Date.now() / 1000,
    };
  } finally {
    await ctx.close();
  }
}

export async function refresh(interactiveOk = true): Promise<Creds> {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      let creds = await capture(true);
      if ((!creds || !creds.logged_in) && interactiveOk) {
        const visible = await capture(false, LOGIN_TIMEOUT);
        if (visible) creds = visible;
      }
      if (!creds) {
        throw new Error(
          "Не удалось получить сессию Алисы через Playwright (нет кадра SynchronizeState).",
        );
      }
      cache = creds;
      save(creds);
      return creds;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export async function getCredentials(force = false): Promise<Creds> {
  const env = envCreds();
  if (env) return env;
  if (!force && cache) return cache;
  const disk = load();
  if (!force && disk) {
    cache = disk;
    return disk;
  }
  return refresh(true);
}

export async function ensure(preferLogin = true): Promise<Creds> {
  const env = envCreds();
  if (env) return env;
  const disk = load();
  if (disk && disk.logged_in) {
    cache = disk;
    return disk;
  }
  return refresh(preferLogin);
}

export function credsSummary(c: Creds): string {
  const mode = c.logged_in ? "Pro (залогинен)" : "Base (аноним)";
  return `${mode}; cookies=${c.cookies.length} симв.; uuid=${c.uuid.slice(0, 12)}…`;
}

// CLI: `tsx session-capture.ts login|show`
const invokedDirectly = process.argv[1] && import.meta.url.endsWith(path.basename(process.argv[1]));
if (invokedDirectly) {
  const cmd = process.argv[2] || "login";
  if (cmd === "show") {
    const c = load();
    console.log(c ? credsSummary(c) : "Кэш пуст (.alice_creds.json нет).");
  } else if (cmd === "login") {
    console.log("Открываю браузер для входа в Яндекс…");
    refresh(true)
      .then((c) => console.log("Готово:", credsSummary(c)))
      .catch((e) => {
        console.error(e.message);
        process.exit(1);
      });
  } else {
    console.log(`Неизвестная команда: ${cmd}. Доступно: login | show`);
  }
}
