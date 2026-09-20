import type {
  BillingMutationResponse,
  BillingStateResponse,
  SubscriptionPreviewResponse,
  SubscriptionStateResponse,
  SubscriptionUpgradeResponse
} from '../../../gatewayTypes.js'
import { openExternalUrl } from '../../../lib/openExternalUrl.js'
import type { SubscriptionOverlayCtx } from '../../interfaces.js'
import { patchOverlayState } from '../../overlayStore.js'
import type { SlashCommand, SlashRunCtx } from '../types.js'

type Sys = (text: string) => void

/**
 * Build the manage-subscription URL locally from the loaded subscription state.
 *
 * Uses `portal_url` (the resolved portal base URL carried in the state) and
 * `org_id` to construct `{portal_base}/manage-subscription?org_id=<id>`.
 * `org_id` pins the page to the correct account in multi-org situations.
 * Falls back to bare `/manage-subscription` if org_id is absent.
 */
function buildManageUrl(s: SubscriptionStateResponse, tierId?: string): string | null {
  // portal_url is already an absolute URL resolved by resolve_portal_base_url()
  // on the Python side (e.g. https://portal.nousresearch.com/billing). Strip any
  // path so we can attach /manage-subscription cleanly.
  let base: string | null = null

  if (s.portal_url) {
    try {
      base = new URL(s.portal_url).origin
    } catch {
      // A malformed portal_url must not throw out of the Ink key handler
      // (it would crash the overlay) — treat it as "no manage URL".
      return null
    }
  }

  if (!base) {
    return null
  }

  const url = new URL('/manage-subscription', base)

  if (s.org_id) {
    url.searchParams.set('org_id', s.org_id)
  }

  if (tierId) {
    url.searchParams.set('plan', tierId)
  }

  return url.toString()
}

/**
 * Build the ctx the overlay uses to talk to the gateway + emit transcript
 * lines.  Mirrors topup.ts's buildOverlayCtx — all RPC + error-mapping logic
 * lives here (single source of truth); the overlay only renders + routes keys.
 */
const buildSubscriptionCtx = (
  ctx: SlashRunCtx,
  sys: Sys,
  initialState: SubscriptionStateResponse
): SubscriptionOverlayCtx => ({
  fetchCard: () =>
    ctx.gateway
      .rpc<BillingStateResponse>('billing.state', {})
      .then(r => (r?.ok ? (r.card ?? null) : null))
      .catch(() => null),
  openManageLink: (tierId?: string) => {
    const url = buildManageUrl(initialState, tierId)

    if (!url) {
      sys('无法生成管理链接——你的门户配置好了吗？')

      return Promise.resolve(false)
    }

    const opened = openExternalUrl(url)

    if (opened) {
      sys('正在浏览器中打开订阅页面——在那里完成后，再运行一次 /subscription。')
    } else {
      sys('无法打开浏览器——请手动访问你的订阅页面：' + url)
    }

    return Promise.resolve(opened)
  },
  openPortal: (url: string) => {
    if (openExternalUrl(url)) {
      sys('正在浏览器中打开门户——在那里完成后，再运行一次 /subscription。')
    } else {
      sys('无法打开浏览器——请访问 ' + url + ' 完成操作。')
    }
  },
  preview: tierId =>
    ctx.gateway
      .rpc<SubscriptionPreviewResponse>('subscription.preview', { subscription_type_id: tierId })
      .then(r => r ?? null)
      .catch(() => null),
  refreshState: () =>
    ctx.gateway
      .rpc<SubscriptionStateResponse>('subscription.state', {})
      .then(r => r ?? null)
      .catch(() => null),
  requestRemoteSpending: () =>
    ctx.gateway
      .rpc<BillingMutationResponse>('billing.step_up', { session_id: ctx.sid ?? undefined })
      // Carry the typed denial (session_revoked / remote_spending_revoked /
      // rate_limited / …) so the stepup screen shows the right recovery.
      .then(r => ({ error: r?.error, granted: !!(r && r.ok && r.granted), message: r?.message }))
      .catch(() => ({
        granted: false,
        message: '无法连接计费服务——请检查网络后重试。'
      })),
  resume: () =>
    ctx.gateway
      .rpc<BillingMutationResponse>('subscription.resume', {})
      .then(r => r ?? null)
      .catch(() => null),
  scheduleCancellation: () =>
    ctx.gateway
      .rpc<BillingMutationResponse>('subscription.change', { cancel: true })
      .then(r => r ?? null)
      .catch(() => null),
  scheduleChange: tierId =>
    ctx.gateway
      .rpc<BillingMutationResponse>('subscription.change', { subscription_type_id: tierId })
      .then(r => r ?? null)
      .catch(() => null),
  sys,
  upgrade: (tierId, idempotencyKey) =>
    ctx.gateway
      .rpc<SubscriptionUpgradeResponse>('subscription.upgrade', {
        subscription_type_id: tierId,
        ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {})
      })
      .then(r => r ?? null)
      .catch(() => null)
})

export const subscriptionCommands: SlashCommand[] = [
  {
    help: '查看或更改你的 Nous 订阅套餐',
    name: 'subscription',
    aliases: ['upgrade'],
    // ZERO sub-commands: bare `/subscription` fetches state and opens the
    // overlay's in-terminal change flow (only /upgrade's charge_now confirm
    // moves money, via the V3 upgrade route).
    run: (_arg, ctx) => {
      const sys: Sys = ctx.transcript.sys

      ctx.gateway
        .rpc<SubscriptionStateResponse>('subscription.state', {})
        .then(
          ctx.guarded<SubscriptionStateResponse>(s => {
            if (!s.logged_in) {
              sys('未登录 Nous 门户——请运行 /portal 登录，然后 /subscription。')

              return
            }

            patchOverlayState({
              subscription: {
                ctx: buildSubscriptionCtx(ctx, sys, s),
                screen: 'overview',
                state: s
              }
            })
          })
        )
        .catch(ctx.guardedErr)
    }
  }
]
