import { pick } from '../lib/text.js'

export const PLACEHOLDERS = [
  '问我任何事…',
  '试试「讲讲这个代码库」',
  '试试「给…写个测试」',
  '试试「重构认证模块」',
  '输入 /help 查看命令',
  '试试「修掉这些 lint 错误」',
  '试试「配置加载器是怎么工作的？」'
]

export const PLACEHOLDER = pick(PLACEHOLDERS)
