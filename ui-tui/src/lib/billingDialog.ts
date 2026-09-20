import type { BillingBlock } from '@hermes/shared/billing'

export interface BillingDialogCopy {
  cancelLabel: string
  confirmLabel: string
  detail: string
  title: string
}

/**
 * Copy for the out-of-credits confirm dialog (the TUI's billing wall). The
 * dialog is the actionable layer — the full provider guidance already lands in
 * the transcript — so `detail` stays to one concise, non-truncating line and the
 * confirm button carries the recovery: Nous → `/topup`, other providers → their
 * billing page (or `/model` to switch when we have no URL). Pure + exported so
 * the wording is unit-tested without driving the gateway.
 */
export function billingDialogCopy(block: BillingBlock): BillingDialogCopy {
  if (block.is_nous) {
    return {
      cancelLabel: 'Dismiss',
      confirmLabel: 'Top up',
      detail: '你的 Nous 余额已用尽——充值后可继续。',
      title: 'Nous 余额不足'
    }
  }

  const label = block.provider_label || 'your provider'

  return {
    cancelLabel: 'Dismiss',
    confirmLabel: block.billing_url ? '打开计费页面' : '切换提供方',
    detail: `${label} reports your credits or billing are exhausted.`,
    title: `Out of credits · ${label}`
  }
}
