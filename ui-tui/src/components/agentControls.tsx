import { Box, Text, useInput } from '@hermes/ink'
import { useEffect, useState } from 'react'

import type { GatewayClient } from '../gatewayClient.js'
import { asRpcResult } from '../lib/rpc.js'
import type { Theme } from '../theme.js'

import { TextInput } from './textInput.js'

export function rosterViewport(height: number, count: number, cursor: number) {
  const timelineRows = height >= 32 ? Math.min(4, count) : 0
  const rows = Math.max(1, height - 7 - (timelineRows ? timelineRows + 4 : 0))
  const start = Math.max(0, Math.min(Math.max(0, count - rows), cursor - Math.floor(rows / 2)))

  return { rows, start, timelineRows }
}

export async function sendAgentSteer(gw: GatewayClient, sid: string, id: string, text: string) {
  const result = asRpcResult<{ status: string }>(
    await gw.request('subagent.steer', { session_id: sid, subagent_id: id, text })
  )

  const accepted = result?.status === 'queued'

  return {
    accepted,
    message: accepted
      ? '已排队给子代理——将在下一个工具边界应用。'
      : '未入队：子代理已结束或不再接受指导。'
  }
}

interface ControlProps {
  gw: GatewayClient
  sid: string
  id: string
  t: Theme
}

export function AgentSteerForm({
  gw,
  sid,
  id,
  t,
  cols,
  onClose
}: ControlProps & { cols: number; onClose: () => void }) {
  const [text, setText] = useState('')
  const [feedback, setFeedback] = useState('')
  const [pending, setPending] = useState(false)
  useInput((_ch, key) => {
    if (key.escape && !pending) {
      onClose()
    }
  })

  const submit = async () => {
    if (!text.trim() || pending) {
      return
    }

    setPending(true)

    try {
      const result = await sendAgentSteer(gw, sid, id, text)
      setFeedback(result.message)

      if (result.accepted) {
        setText('')
      }
    } catch (error) {
      setFeedback(`Not queued: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setPending(false)
    }
  }

  return (
    <Box flexDirection="column" flexGrow={1}>
      <Text bold color={t.color.accent} wrap="truncate-end">
        Steer {id}
      </Text>
      <Text color={t.color.muted}>Guidance queues at the next tool boundary; current work is not interrupted.</Text>
      <Box marginTop={1}>
        <Text color={t.color.accent}>❯ </Text>
        <TextInput
          color={t.color.text}
          columns={Math.max(1, cols - 6)}
          focus={!pending}
          onChange={setText}
          onSubmit={() => void submit()}
          value={text}
        />
      </Box>
      <Text color={t.color.muted}>{pending ? 'Queueing…' : feedback}</Text>
      <Text color={t.color.muted}>Enter queue · Esc back · main composer draft is preserved</Text>
    </Box>
  )
}

export function AgentLiveTail({ gw, sid, id, t }: ControlProps) {
  const [tail, setTail] = useState('正在加载实时记录…')
  useEffect(() => {
    let active = true
    let pending = false

    const refresh = async () => {
      if (pending) {
        return
      }

      pending = true

      try {
        const result = asRpcResult<{ available: boolean; text: string; truncated: boolean }>(
          await gw.request('subagent.tail', { session_id: sid, subagent_id: id })
        )

        if (active) {
          setTail(
            result?.available
              ? `${result.truncated ? '[last 16 KiB]\n' : ''}${result.text}`
              : '实时记录不可用；子代理可能已结束。进度和输出仍在下方。'
          )
        }
      } catch {
        if (active) {
          setTail('无法刷新实时记录。')
        }
      } finally {
        pending = false
      }
    }

    void refresh()
    const timer = setInterval(() => void refresh(), 1500)

    return () => {
      active = false
      clearInterval(timer)
    }
  }, [gw, sid, id])

  return (
    <Box flexDirection="column">
      <Text bold color={t.color.accent}>
        Live transcript
      </Text>
      <Text color={t.color.text} wrap="wrap">
        {tail}
      </Text>
    </Box>
  )
}
