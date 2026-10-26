/**
 * Identity of THIS browser tab's keep-alive PTY, sent as `?attach=`.
 *
 * The dashboard's PTY registry maps one attach token to exactly one PTY, so a
 * token two tabs present makes the second tab take the first one's terminal
 * over (the first is closed 4409 and goes dead — the reported multi-tab
 * interference). localStorage is shared per ORIGIN, so every tab read the same
 * value, and Chrome's "Duplicate tab" clones sessionStorage into the new tab,
 * so neither storage alone isolates the tabs. The token therefore lives in
 * sessionStorage *and* is claimed with a Web Lock: a tab whose token is
 * already claimed by a live document mints its own instead of sharing one.
 * See #115304.
 *
 * ── 2026-09-30 嘎妞补丁（主人要求：关掉浏览器再打开，像没中断过一样）──
 * 原实现只把 token 存在 sessionStorage —— 关掉整个浏览器就清空，重开时
 * mint 全新 token，服务端的 attach 键（`token\0profile\0resume`，见
 * hermes_cli/web_routers/chat_ws.py:511）必然 miss，于是新起一个 PTY，
 * 原来那个还在跑任务的终端永远接不回来（服务端其实一直活着，pty_session
 * .attach() 也支持重放缓冲 + 恢复实时）。
 * 现在 token 同时落到 localStorage（`hermes.pty.persist.chat`）：关掉整个
 * 浏览器再打开时 localStorage 仍在 → 同一个 token → 同一个键 → attach
 * 命中原 PTY，缓冲重放 + 实时续流 = 用户看到「像没中断过一样」。
 * 多标签隔离不受影响：claim() 的 Web Lock 仍在，被活标签占用的 token
 * claim 不到就另 mint 一个新的（老标签继续用自己那个）。
 */

const PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";
/** Cross-browser-session keep-alive identity (survives a full browser quit). */
const PTY_PERSIST_TOKEN_KEY = "hermes.pty.persist.chat";

/** The token this document claimed — a reconnect must not re-request the lock
 *  (Web Locks are not reentrant, so asking twice in one document deadlocks). */
let claimedToken: string | null = null;

function tabStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null; /* private mode / storage blocked */
  }
}

function persistStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null; /* private mode / storage blocked */
  }
}

function mint(): string {
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Write the token to both stores, so a browser restart can still find it. */
function remember(token: string): void {
  try {
    tabStorage()?.setItem(PTY_ATTACH_TOKEN_KEY, token);
  } catch {
    /* ignore */
  }
  try {
    persistStorage()?.setItem(PTY_PERSIST_TOKEN_KEY, token);
  } catch {
    /* ignore */
  }
}

/** Never settles, so the lock stays held for this document's lifetime. */
const HOLD: Promise<void> = new Promise<void>(() => {});

/**
 * Claim `token` for this document. True when we now hold it; the browser
 * releases it when the document unloads, so a reload of this tab finds the
 * token free again while a live second tab does not. Browsers without the Web
 * Locks API fall back to sessionStorage isolation only.
 */
async function claim(token: string): Promise<boolean> {
  const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
  if (!locks) return true;
  return new Promise<boolean>((resolve) => {
    void locks.request(`hermes.pty.attach.${token}`, { ifAvailable: true }, (lock) => {
      resolve(lock !== null);
      return lock ? HOLD : Promise.resolve();
    });
  });
}

/**
 * This tab's `?attach=` token, minted when `rotate` starts a fresh session (the
 * old keep-alive PTY must NOT be reattached) or when another live tab already
 * claimed both the tab-local and the persisted one.
 *
 * Order of preference: this document's already-claimed token → this tab's
 * sessionStorage token → the localStorage token left by a previous browser
 * session → a freshly minted one.
 */
export async function ptyAttachToken(rotate = false): Promise<string> {
  if (!rotate) {
    if (claimedToken) return claimedToken;
    const candidates = [
      tabStorage()?.getItem(PTY_ATTACH_TOKEN_KEY) ?? "",
      persistStorage()?.getItem(PTY_PERSIST_TOKEN_KEY) ?? "",
    ];
    for (const candidate of candidates) {
      if (!candidate) continue;
      if (await claim(candidate)) {
        claimedToken = candidate;
        remember(candidate);
        return candidate;
      }
    }
  }
  const token = mint();
  remember(token);
  await claim(token);
  claimedToken = token;
  return token;
}

/**
 * True when this browser kept a PTY identity from an earlier visit — i.e. the
 * user probably has a terminal still running server-side (detached PTYs keep
 * running; see hermes_cli/web_routers/chat_ws.py). Used to decide whether a
 * fresh `/chat` load should re-enter the last conversation instead of silently
 * starting an empty one.
 */
export function hasPersistedPtyToken(): boolean {
  try {
    return Boolean(persistStorage()?.getItem(PTY_PERSIST_TOKEN_KEY));
  } catch {
    return false;
  }
}
