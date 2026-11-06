"""权限规则版本化（policy_version）——参考 dsh-superagent-prototype policyVersion/revalidate 设计（MIT），Python 自行实现。

语义：
  - 规则字典稳定序列化后哈希出版本号；版本号变了 = 政策变了
  - 政策变更不吊销在办任务：只通知（注入 policy-changed），任务继续时检查 mandate 是否需重验
  - revalidate 谓词：任务的 mandate 版本 != 当前版本 且 任务在续做边界（新输入/新分派）→ 要求重验
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["policy_version", "Mandate", "needs_revalidate", "policy_changed_notifications"]


def policy_version(rules: Dict[str, Any]) -> str:
    """规则字典 → 稳定哈希版本号（排序序列化，键序无关）。"""
    canonical = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class Mandate:
    """接单时冻结的规则集版本；变更政策不吊销活票，续做时重验。"""

    ticket_id: str
    version: str
    strength: str = "inferred"  # explicit / inferred


def needs_revalidate(mandate: Mandate, current_version: str) -> bool:
    """任务带旧版本 mandate 且与当前版本不一致 → 续做前需要重验。"""
    return mandate.version != current_version


def policy_changed_notifications(
    mandates: List[Mandate], current_version: str
) -> List[Dict[str, str]]:
    """政策变更后：为每个活任务生成通知（不吊销）；无需重验的跳过。"""
    notes = []
    for m in mandates:
        if needs_revalidate(m, current_version):
            notes.append(
                {
                    "ticket_id": m.ticket_id,
                    "type": "policy-changed",
                    "old_version": m.version,
                    "new_version": current_version,
                }
            )
    return notes
