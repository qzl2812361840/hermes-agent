import { readFileSync } from 'node:fs'

import type { ConfigSetResponse } from '../../../gatewayTypes.js'
import { patchUiState } from '../../uiStore.js'
import type { SlashCommand } from '../types.js'

/**
 * litellm 代理的模型池配置文件（嘎妞服务器固定路径）。
 * 这里只做只读解析，绝不修改该文件。
 */
const LITELLM_CONFIG = '/opt/hermes/.hermes/scripts/litellm_config.yaml'

/**
 * 不能拿来聊天的模型（名字片段匹配）：嵌入 / 重排 / 语音 / 出图。
 * 这类模型接不了对话请求，必须排除在切换池之外。
 */
const NON_CHAT_MODEL_RE = /(?:^|[-_])(embed|bge|m3e|rerank|reranker|tts|whisper|asr|dall|flux|imagegen|speech)(?:[-_]|$)/i

/**
 * 从 litellm_config.yaml 解析出可聊天模型的轮换池（保持出现顺序、去重）。
 *
 * 主人口径（2026-10-23 20:26）：「包括所有带文本的模型，也包括多模态的」。
 * 所以判定标准只有一个：**能不能处理文本**。
 *   · 纯文本模型   → 收
 *   · 多模态 / VL（视觉语言）模型 → 也收（它们能处理纯文本，看图只是附加能力）
 *   · 嵌入 / 重排 / 语音 / 出图  → 排除（接不了对话）
 *
 * 不再参考 model_info.supports_vision：实测本配置 21 个被标记的模型里
 * 有 18 个是纯文本模型被误标（glm 全家桶、deepseek-flash、doubao-ark-flash 等），
 * 该字段在本机不可信；且主人明确要求多模态也要能切，按它过滤等于反向误删。
 */
export function readTextModelPool(): string[] {
  const raw = readFileSync(LITELLM_CONFIG, 'utf8')
  const pool: string[] = []

  for (const line of raw.split('\n')) {
    const m = line.match(/^\s*- model_name: (\S+)/)
    if (m) pool.push(m[1])
  }

  // 去重（litellm 里同一 model_name 常配多个账号）后，只滤掉不能对话的模型
  return [...new Set(pool)].filter(name => !NON_CHAT_MODEL_RE.test(name))
}

/** 从当前模型出发，在轮换池里取下一个（循环取模；找不到则从头开始）。 */
function nextModel(current: string, pool: string[]): string {
  const idx = pool.indexOf(current)
  if (idx === -1) return pool[0] ?? ''
  return pool[(idx + 1) % pool.length]
}

export const nextModelCommands: SlashCommand[] = [
  {
    help: 'switch to the next text model (litellm pool)',
    name: 'next-model',
    aliases: ['nextmodel'],
    run: (arg, ctx) => {
      const ui = ctx.ui
      // 当前生效模型优先取实时轮询的 modelState，其次取会话快照 info.model
      const current = ui.modelState?.model || ui.info?.model
      if (!current) {
        return ctx.transcript.sys('error: no current model (run /model first)')
      }

      let pool: string[]
      try {
        pool = readTextModelPool()
      } catch (err) {
        return ctx.transcript.sys(`error: read litellm config failed: ${String(err)}`)
      }

      if (!pool.length) {
        return ctx.transcript.sys('error: empty text model pool (check litellm config)')
      }

      const next = nextModel(current, pool)
      if (!next || next === current) {
        return ctx.transcript.sys(`model: already at ${current} (pool has 1 text model)`)
      }

      const switchModel = (confirmExpensiveModel = false) =>
        ctx.gateway
          .rpc<ConfigSetResponse>('config.set', {
            confirm_expensive_model: confirmExpensiveModel,
            key: 'model',
            session_id: ctx.sid,
            value: next
          })
          .then(
            ctx.guarded<ConfigSetResponse>(r => {
              if (r.confirm_required) {
                // 高价模型需确认：用户要求任何模型都能切，带上 confirm 标志自动确认重试一次
                if (!confirmExpensiveModel) {
                  return switchModel(true)
                }
                return ctx.transcript.sys(
                  `model: ${r.value} still requires confirmation — run /model ${r.value} to pick it explicitly`
                )
              }

              if (!r.value) {
                return ctx.transcript.sys('error: invalid response: model switch')
              }

              ctx.transcript.sys(
                r.deferred ? `model → ${r.value} (applies next turn)` : `model → ${r.value}`
              )
              ctx.local.maybeWarn(r)

              // 立即同步界面展示：状态栏 modelState + 会话快照 info.model
              patchUiState(state => ({
                ...state,
                info: state.info
                  ? { ...state.info, model: r.value! }
                  : { model: r.value!, skills: {}, tools: {} },
                // 2026-10-23 修正：切换后尚未产生实际调用，必须清空"实际执行模型"记录。
                // 旧代码把 actual_model 也写成新模型、at 写成当前时间，等于伪造了一条
                // 「已实际执行」的记录；配合跨会话兜底的历史 fallback，轮询一回来就把
                // 用户刚选的模型覆盖成上一次真正跑过的模型（表现为跳回英伟达）。
                modelState: {
                  model: r.value!,
                  provider: state.modelState?.provider ?? '',
                  actual_model: '',
                  actual_backend: '',
                  request_model: r.value!,
                  fallback: false,
                  fallback_active: false,
                  fallback_chain: [],
                  at: 0
                }
              }))
            })
          )

      switchModel()
    }
  }
]
