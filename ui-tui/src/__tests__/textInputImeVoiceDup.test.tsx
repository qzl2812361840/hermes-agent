import { EventEmitter } from 'events'

import { renderSync } from '@hermes/ink'
import React, { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TextInput } from '../components/textInput.js'

// 2026-10-28 主人实测 bug（手机输入法语音 → SSH → TUI）：
// 输入法中间稿整句 commit（多字符块），终稿再整句 commit（无 backspace、无 bracketed paste 包裹），
// textInput 把两块都当"追加"→ 输入框两遍。期望终稿替换中间稿。

class FakeTty extends EventEmitter {
  chunks: string[] = []
  columns = 80
  rows = 24
  isTTY = true
  isRaw = false
  private pendingReads: string[] = []
  ref(): void {}
  unref(): void {}
  read(): string | null { return this.pendingReads.shift() ?? null }
  send(chunk: string): void { this.pendingReads.push(chunk); this.emit('readable') }
  setEncoding(): this { return this }
  setRawMode(mode: boolean): this { this.isRaw = mode; return this }
  write(chunk: string | Uint8Array, cb?: (err?: Error | null) => void): boolean {
    this.chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    cb?.()
    return true
  }
}

const tick = () => new Promise<void>(resolve => setImmediate(resolve))

function Harness({ onValue }: { onValue: (v: string) => void }) {
  const [value, setValue] = useState('')
  return React.createElement(TextInput, {
    onChange: (next: string) => { setValue(next); onValue(next) },
    value
  })
}

describe('IME voice duplicate insert (2026-10-28)', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    ;(process.stdout as { isTTY?: boolean }).isTTY = undefined
  })

  it('voice intermediate commit followed by final commit must not duplicate', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'Date'] })
    ;(process.stdout as { isTTY?: boolean }).isTTY = true
    vi.spyOn(process.stdout, 'write').mockImplementation(() => true)

    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()
    let last = ''
    const instance = renderSync(React.createElement(Harness, { onValue: (v: string) => { last = v } }), {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    stdin.send('然后给我打包APP')
    await tick(); await tick(); await tick()
    stdin.send('然后给我打包APP，给我最新下载链接')
    await tick(); await tick(); await tick()
    instance.unmount()

    expect(last).toBe('然后给我打包APP，给我最新下载链接')
  })

  // 2026-11-03 回归：终稿以 bracketed paste 包裹到达（SSH 客户端/输入法升级后
  // 开始走粘贴通道），旧方案去重放在粘贴路由之后 → 双遍。去重必须先于粘贴路由。
  it('voice final arriving as bracketed paste must not duplicate', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'Date'] })
    ;(process.stdout as { isTTY?: boolean }).isTTY = true
    vi.spyOn(process.stdout, 'write').mockImplementation(() => true)

    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()
    let last = ''
    const instance = renderSync(React.createElement(Harness, { onValue: (v: string) => { last = v } }), {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    stdin.send('我们升级hermes不是有个规则么')
    await tick(); await tick(); await tick()
    stdin.send('\x1b[200~我们升级hermes不是有个规则么给我修改一下\x1b[201~')
    await tick(); await tick(); await tick()
    instance.unmount()

    expect(last).toBe('我们升级hermes不是有个规则么给我修改一下')
  })

  // 2026-11-03 回归：整段语音口述超 200 字，旧上限 200 直接漏判 → 双遍。
  it('voice final longer than 200 chars must still recompose (not append)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'Date'] })
    ;(process.stdout as { isTTY?: boolean }).isTTY = true
    vi.spyOn(process.stdout, 'write').mockImplementation(() => true)

    const base = '今天要做的第一件事是把服务器的备份脚本检查一遍然后看一下磁盘空间'
    const longText = base.repeat(12) // >200 chars
    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()
    let last = ''
    const instance = renderSync(React.createElement(Harness, { onValue: (v: string) => { last = v } }), {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    stdin.send(base)
    await tick(); await tick(); await tick()
    stdin.send(longText)
    await tick(); await tick(); await tick()
    instance.unmount()

    expect(last).toBe(longText)
    expect(last.length).toBeGreaterThan(200)
  })
})
