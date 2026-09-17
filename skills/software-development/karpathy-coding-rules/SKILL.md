---
name: karpathy-coding-rules
description: Minimal coding rules: think first, keep simple, surgical changes, verify.
version: "1.0"
author: multica-ai/andrej-karpathy-skills (public domain) / adapted for Hermes
license: MIT
metadata:
  hermes:
    tags: ["coding", "rules", "behavior", "karpathy"]
    category: software-development
---

# Karpathy Coding Rules (65行 → Hermes技能)

摘自 Andrej Karpathy 的 LLM 编程痛点观察，提炼为行为规则。
源: multica-ai/andrej-karpathy-skills (⭐19万)

## 1. 先想再写

**不假设，不藏困惑，亮出取舍。**

动手前：
- 显式声明你的假设。不确定就问。
- 有多种理解 → 全部呈列，不自己默默选一种。
- 有更简单的做法 → 说出来，该反驳就反驳。
- 有不清楚的地方 → 停下来，说哪里不懂，问。

## 2. 最简优先

**恰好解决问題的最小代码。零猜测性代码。**

- 只做用户要求的，不加额外功能。
- 单次使用的代码不抽象。
- 不要"将来可能需要"的灵活性/可配置性。
- 不可能发生的场景不写错误处理。
- 写了 200 行发现 50 行就够 → 重写成 50 行。

自问："资深工程师看了会说这太复杂吗？" 会 → 化简。

## 3. 外科手术式修改

**只碰必须碰的。只收拾自己弄乱的。**

- 不"顺带优化"相邻代码/注释/格式。
- 没坏的不重构。
- 风格不同也照原样写，别按你的偏好改。
- 发现无关的死代码 → 提一句，别删。

只因为你的改动造成的新 orphan → 清掉（import/变量/函数）。原有的死代码不动。

检验：每行改动都能追溯到用户的请求。

## 4. 目标驱动

**定义成功标准。循环到验过。**

把任务变成可验证的目标：
- "加验证" → "先写测非法输入的用例，再让它们Pass"
- "修bug" → "先写复现用例，再让Pass"
- "重构X" → "确保重构前后测试全Pass"

多步任务给出简单计划：
```
1. [步骤] → 验: [检验]
2. [步骤] → 验: [检验]
3. [步骤] → 验: [检验]
```

强的成功标准让你能自己循环。弱的标准（"搞对就行"）需要不断问用户确认。
