import type { PanelSection } from '../types.js'

export const SETUP_REQUIRED_TITLE = 'Setup Required'

export const buildSetupRequiredSections = (): PanelSection[] => [
  {
    text: 'TUI 开始会话前，Hermes 需要先配置一个模型提供方。'
  },
  {
    rows: [
      ['/setup', 'run the first-time setup wizard in-place (adds a provider)'],
      ['/model', 'pick a model (needs a session — add a provider first)'],
      ['Ctrl+C', 'exit and run `hermes setup` manually']
    ],
    title: 'Actions'
  },
  {
    text: '在网页控制台用「模型」页面设置档案默认值；桌面版则在「设置 -> 模型」里。'
  }
]
