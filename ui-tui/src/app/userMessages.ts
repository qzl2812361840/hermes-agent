// User-facing wording for gateway/transport failures in the TUI. Pure functions
// so the copy — and the "what happened / what to do" shape — is unit-testable
// without rendering. Every slash command cited here exists in
// ui-tui/src/app/slash/commands (/logs, /retry, /model, /update, /resume,
// /sessions, /quit) and `hermes doctor` is a real subcommand.

import type { ErrorSurface } from '@hermes/shared/gateway-events'

/** JSON-RPC error codes the gateway answers with. */
export const RPC_INVALID_PARAMS = 4000
export const RPC_SESSION_NOT_FOUND = 4001
export const RPC_NOT_DISPATCHABLE = 4018
export const RPC_UNKNOWN_METHOD = -32601

const DETAIL_LIMIT = 300

interface RpcErrorShape {
  code?: number
  message?: string
}

const rpcShape = (err: unknown): RpcErrorShape =>
  err instanceof Error ? { code: (err as { code?: number }).code, message: err.message } : {}

const detailLine = (raw: string | undefined): string | null => {
  const text = (raw ?? '').replace(/\s+/g, ' ').trim()

  if (!text) {
    return null
  }

  return `Details: ${text.length > DETAIL_LIMIT ? `${text.slice(0, DETAIL_LIMIT - 1)}…` : text}`
}

// ── Backend process lifecycle ─────────────────────────────────────────────

export const BACKEND_RESTARTING =
  'Hermes 意外停止——正在重启并重新打开您的对话（进行中的回复已丢失）。'

export const BACKEND_RESTARTING_ACTIVITY = 'Hermes 意外停止 · 正在重启…'

// Attached (dashboard / embedded) mode: only the socket dropped; Hermes and any
// reply in progress are still alive on the backend and come back on reconnect.
export const CONNECTION_LOST = '与 Hermes 的连接已断开——正在重连并重新打开您的对话…'

export const CONNECTION_LOST_ACTIVITY = 'connection lost · reconnecting…'

export const backendGaveUp = (code: null | number, lastLine?: string): string => {
  const exit = code === null ? '' : ` (exit code ${code})`
  const detail = detailLine(lastLine)

  return [
    `Hermes stopped${exit} and could not be restarted. Your chat is saved.`,
    detail,
    'Hermes 会在后台持续重连，成功后自动重新打开本对话；若一直失败，请输入 /resume。',
    '输入 /logs 查看完整日志，或 /quit 后运行 `hermes doctor` 检查安装。'
  ]
    .filter(Boolean)
    .join('\n')
}

export const BACKEND_GAVE_UP_ACTIVITY = 'Hermes 已停止 · 详情见 /logs'

/** Last line of the backend log tail that is not our own [lifecycle]/[startup] bookkeeping. */
export const lastStderrLine = (tail: string): string | undefined =>
  tail
    .split('\n')
    .map(l => l.trim())
    .filter(l => l && !/^\[(?:lifecycle|startup|protocol|sidecar|spawn)\]/.test(l))
    .at(-1)

export const backendReconnecting = (attempt: number | undefined, delayMs: number | undefined): string => {
  const secs = Math.max(1, Math.round((delayMs ?? 1000) / 1000))
  const n = attempt && attempt > 0 ? ` (attempt ${attempt})` : ''

  return `retrying in ${secs}s${n}`
}

export const BACKEND_SLOW_START =
  'Hermes 启动比平时慢，仍在等待…若始终连不上：/logs 可看后端最后的输出；/quit 后运行 `hermes doctor`。'

export const BACKEND_SLOW_START_STATUS = 'still starting…'

// ── stderr noise ──────────────────────────────────────────────────────────

// Only real failures: a traceback, a CRITICAL log line, an `XxxError:` / `XxxException:`
// head, or the gateway's own turn/exit markers. Dependency `DeprecationWarning` /
// `UserWarning` lines are noise and stay in /logs only.
const STDERR_PROBLEM_RE = /Traceback|\b[A-Z][A-Za-z]*(?:Error|Exception)\b:|CRITICAL|\[gateway-turn\]|\[gateway-exit\]/

/** Only lines that look like a failure earn an activity row; the rest stay in /logs. */
export const stderrLooksLikeProblem = (line: string): boolean => STDERR_PROBLEM_RE.test(line)

export const stderrProblemActivity = (line: string): string => {
  const m = /([A-Z][A-Za-z]*(?:Error|Exception)):/.exec(line)
  const what = m ? ` (${m[1]})` : ''

  return `Something went wrong inside Hermes${what} · /logs for details`
}

// ── RPC errors ────────────────────────────────────────────────────────────

const VERSION_SKEW_RE = /Extra inputs are not permitted|^unknown method:/

/** The Ink bundle and the Python backend disagree on the wire: stale dist or an older attached backend. */
export const isVersionSkewError = (err: unknown): boolean => {
  const { code, message } = rpcShape(err)

  return (
    code === RPC_UNKNOWN_METHOD ||
    (code === RPC_INVALID_PARAMS && VERSION_SKEW_RE.test(message ?? '')) ||
    (code === undefined && VERSION_SKEW_RE.test(message ?? ''))
  )
}

export const VERSION_SKEW_MESSAGE =
  '终端界面与 Hermes 后端版本不一致。请运行 /update，或退出后运行 `hermes update`，再重新打开界面。'

const SESSION_NOT_FOUND_RE = /session not found/i
const NOT_CONNECTED_RE = /^gateway not (?:connected|running)\b/
const TIMED_OUT_RE = /^request timed out after (\d+)s/

type RpcErrorRow = [
  matcher: (code: number | undefined, text: string) => RegExpExecArray | boolean | null,
  render: (m: RegExpExecArray | null) => string
]

// Ordered: first matching row wins. 4001 is reused by the backend for unrelated
// refusals ("no active session", "slug is required", NOT_OWNER), so the code
// alone must not trigger the /resume copy — only the "session not found" text.
const RPC_ERROR_ROWS: RpcErrorRow[] = [
  [
    (code, text) => (code === RPC_SESSION_NOT_FOUND || code === undefined) && SESSION_NOT_FOUND_RE.test(text),
    () =>
      '本对话已与后端断开（闲置过久或后端重启）。记录已保存：输入 /resume 重新打开。'
  ],
  [
    (_code, text) => NOT_CONNECTED_RE.test(text),
    () =>
      '当前未连接 Hermes，消息没有发出去。会自动重连，稍等片刻再试；若持续如此请输入 /logs。'
  ],
  [
    (_code, text) => TIMED_OUT_RE.exec(text),
    m =>
      `Hermes did not answer within ${m?.[1] ?? '?'}s. Try again; if it keeps happening, type /logs and report the last lines.`
  ]
]

let rpcErrorLogSink: ((line: string) => void) | null = null

/** Where describeRpcError records the raw wire text it replaced (the /logs buffer). */
export const setRpcErrorLogSink = (sink: ((line: string) => void) | null): void => {
  rpcErrorLogSink = sink
}

const logReplacedWireText = (code: number | undefined, text: string): void => {
  rpcErrorLogSink?.(`[rpc] ${code === undefined ? '' : `code=${code} `}${text}`)
}

/** Rewrite transport/session errors into plain words; other errors pass through. */
export const describeRpcError = (err: unknown): string => {
  const { code, message } = rpcShape(err)
  const text = message ?? (typeof err === 'string' && err.trim() ? err : 'request failed')

  if (isVersionSkewError(err)) {
    logReplacedWireText(code, text)

    return VERSION_SKEW_MESSAGE
  }

  for (const [matcher, render] of RPC_ERROR_ROWS) {
    const m = matcher(code, text)

    if (m) {
      logReplacedWireText(code, text)

      return render(m === true ? null : m)
    }
  }

  return text
}

/** The slash worker (built-in command helper) failed; name the command, not the helper. */
export const describeSlashExecError = (command: string, err: unknown): string => {
  const { message } = rpcShape(err)
  const text = message ?? ''

  if (/slash worker timed out/.test(text)) {
    return `/${command} did not finish: the command helper timed out. Try again; if it keeps happening, type /logs and report the last lines.`
  }

  if (/slash worker (?:exited|closed pipe|start failed)/.test(text)) {
    const detail = detailLine(text.replace(/^slash worker (?:exited|closed pipe:?|start failed:?)\s*/, ''))

    return [`/${command} did not finish: the command helper crashed. Try again; type /logs for the trace.`, detail]
      .filter(Boolean)
      .join('\n')
  }

  return describeRpcError(err)
}

// slash.exec answers 4018 with exactly these texts when it does NOT own the
// command (tui_gateway/methods_tools.py). Every other 4018 came from a
// command.dispatch handler slash.exec already forwarded to (/retry, /undo,
// /compress, /queue, bundles): re-dispatching would run a mutating command twice.
const NOT_MINE_REFUSAL_RE = /^skill command: use command\.dispatch for \/|use command\.dispatch for \/snapshot restore/

/** command.dispatch is only a fallback for "slash.exec does not own this command" refusals. */
export const shouldFallbackToDispatch = (err: unknown): boolean => {
  const { code, message } = rpcShape(err)

  if (code === RPC_NOT_DISPATCHABLE) {
    return NOT_MINE_REFUSAL_RE.test(message ?? '')
  }

  if (code !== undefined) {
    return false
  }

  // Legacy/attached backends without a code: keep the historical behaviour
  // unless the text is unmistakably a helper failure.
  return !/slash worker|timed out|not connected|not running/.test(message ?? '')
}

// ── Turn failures (message.complete status=error) ─────────────────────────

const TURN_CODE_COPY: Record<string, [string, string]> = {
  auth: ['模型提供方拒绝了 API 密钥', '请用 /model 修复密钥，然后 /retry 重试。'],
  auth_permanent: ['模型提供方拒绝了 API 密钥', '请用 /model 修复密钥，然后 /retry 重试。'],
  billing: ['模型提供方报告余额不足', '请充值或用 /model 切换模型。'],
  billing_unverified: ['模型提供方报告余额不足', '请充值或用 /model 切换模型。'],
  content_policy_blocked: ['模型提供方拒绝了这条请求（内容政策）', '换个说法再发一次。'],
  context_overflow: ['对话对这个模型来说太长了', '请运行 /compress，然后 /retry 重试。'],
  format_error: ['模型提供方拒绝了请求格式', '请 /retry 重试；若持续出现，用 /model 切换模型。'],
  model_not_found: ['模型提供方不认识这个模型', '请用 /model 选择其他模型。'],
  overloaded: ['模型提供方负载过高', '稍等片刻，然后 /retry 重试。'],
  payload_too_large: ['请求对这个模型来说太大了', '请运行 /compress，然后 /retry 重试。'],
  provider_policy_blocked: ['模型提供方拒绝了这条请求（账号策略）', '请用 /model 切换模型。'],
  rate_limit: ['模型提供方正在限制请求频率', '稍等片刻，然后 /retry 重试。'],
  server_error: ['模型提供方内部错误', '稍等片刻，然后 /retry 重试。'],
  ssl_cert_verification: [
    '无法验证与模型提供方的连接（TLS）',
    "请检查该端点的证书，然后 /retry 重试。"
  ],
  timeout: ['模型提供方没有及时应答', '请 /retry 重试；若反复出现，用 /model 切换模型。'],
  upstream_blocked: [
    '模型提供方前面的防火墙/CDN 拦截了请求',
    "请在该提供方的 extra_headers 里设置 User-Agent，或用 /model 切换模型。"
  ],
  upstream_rate_limit: ['模型提供方正在限制请求频率', '稍等片刻，然后 /retry 重试。']
}

const TURN_LAYER_COPY: Record<string, [string, string]> = {
  auth: ['模型提供方拒绝了凭据', '请用 /model 修复，然后 /retry 重试。'],
  billing: ['模型提供方报告余额不足', '请充值或用 /model 切换模型。'],
  disk: ['磁盘已满，Hermes 无法保存这一轮', '请清理空间，然后 /retry 重试。'],
  endpoint: ['你的自定义模型端点没有应答', '请确认端点正在运行，然后 /retry 重试。'],
  gateway: ['Hermes 在运行这一轮时发生内部错误', '请发送 /retry；输入 /logs 查看堆栈。'],
  provider: ['模型提供方返回了错误', '请发送 /retry，或用 /model 切换模型。'],
  streaming: ['与模型提供方的连接在回复中途断开', '请发送 /retry。']
}

const TURN_DEFAULT_COPY: [string, string] = ['请求失败', '请发送 /retry，或用 /model 切换模型。']

export interface TurnFailure {
  error?: null | string
  error_surface?: ErrorSurface | null | Record<string, unknown>
  recoverable?: boolean | null
}

/** Plain title + dimmed detail + next step for a failed turn with no reply text. */
export const describeTurnFailure = (payload: TurnFailure): string => {
  const surface = (payload.error_surface ?? {}) as { code?: unknown; layer?: unknown; provider?: unknown }
  const code = typeof surface.code === 'string' ? surface.code : ''
  const layer = typeof surface.layer === 'string' ? surface.layer : ''
  const provider = typeof surface.provider === 'string' && surface.provider ? ` (${surface.provider})` : ''
  const [title, hint] = TURN_CODE_COPY[code] ?? TURN_LAYER_COPY[layer] ?? TURN_DEFAULT_COPY
  // The backend always sets recoverable=true on a turn error; error_surface.retryable
  // is the signal that actually says whether /retry can help.
  const retryable = (surface as { retryable?: unknown }).retryable !== false && payload.recoverable !== false
  const nextStep = retryable ? hint : hint.replace(/(?:请发送|发送|试试)?\\s*\/retry[^，。]*[，。]?/, '请用 /model 选择其他模型。')
  const raw = (payload.error ?? '').replace(/^Error:\s*/, '')

  return [`${title}${provider}. Your message was not answered.`, detailLine(raw), nextStep].filter(Boolean).join('\n')
}

/** True when the assistant slot carries nothing but the backend's "Error: …" fallback text. */
export const isBareErrorText = (text: string, error: null | string | undefined): boolean => {
  const t = text.trim()

  return !t || t === `Error: ${error ?? ''}`.trim() || t === (error ?? '').trim()
}

// ── Withdrawn password / secret prompts ───────────────────────────────────

const PROMPT_TIMEOUT_COPY: Record<string, string> = {
  secret:
    '密钥输入已关闭：超时未作答，需要它的那一步被跳过了。准备好输入时请重新发起请求。',
  sudo: '密码输入已关闭：超时未作答，命令被跳过了。准备好输入时请重新发起请求。',
  'vault.code':
    '验证码输入已关闭：超时未作答，登录被跳过了。拿到验证码后请重新发起请求。',
  'vault.save_login':
    '保存登录已关闭：超时未作答，未保存任何内容。准备好时请重新发起请求。',
  'vault.unlock_prompt':
    '解锁已关闭：超时未作答，密码库保持锁定。准备解锁时请重新发起请求。'
}

export const promptTimeoutNotice = (method: string | undefined, reason: string | undefined): null | string =>
  reason === 'timeout' && method ? (PROMPT_TIMEOUT_COPY[method] ?? null) : null

// ── session.info warnings ─────────────────────────────────────────────────

const MISSING_KEY_RE = /^No API key configured for provider '([^']*)'/

/** The backend's credential warning names the break; add the fix (/model saves a key in place). */
export const describeCredentialWarning = (warning: string): string => {
  const m = MISSING_KEY_RE.exec(warning)

  if (!m) {
    return warning
  }

  const provider = m[1] || 'the current provider'

  return `No API key is set for ${provider}, so messages will fail. Type /model, pick ${provider}, and paste a key (or run /setup).`
}

// ── Empty states ──────────────────────────────────────────────────────────

export const NO_SKILLS_INSTALLED =
  '还没有安装任何技能。输入 /skills browse 查看目录，或 /skills install <名称> 安装。'
