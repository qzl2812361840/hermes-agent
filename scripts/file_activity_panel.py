#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件活动面板：多会话协作的"留言板"——系统记录的事实，不是 AI 的留言。

tools/file_state.py 在会话读/写文件时生成节流快照
（HERMES_HOME/file-activity.<pid>.json，可用 HERMES_FILE_ACTIVITY_SNAPSHOT=0 关闭）。
本脚本把所有进程的快照汇总成一张表：哪个会话在改/看哪些文件、多久前。
给主人一眼掌握"现在谁在动什么"——协作判断由人做，不让两个 AI 互相聊天。

用法：
    HERMES_HOME=/d/父体/hermes-home python3 scripts/file_activity_panel.py
    python3 scripts/file_activity_panel.py --home /d/父体/hermes-home --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

_PREFIX = "file-activity."
_WRITE_WINDOW = 1800.0   # 写入事件 30 分钟内可见
_READ_WINDOW = 600.0     # 读取事件 10 分钟内可见


def _pid_alive(pid) -> bool | None:
    """Linux: /proc 探测；其他平台返回 None（未知→按存活处理，不做清理）。"""
    if pid is None:
        return None
    if sys.platform.startswith("linux"):
        return Path(f"/proc/{pid}").exists()
    return None


def _fmt_age(ts: float, now: float) -> str:
    age = max(0, int(now - ts))
    if age < 60:
        return f"{age}秒前"
    if age < 3600:
        return f"{age // 60}分钟前"
    return f"{age // 3600}小时前"


def main() -> int:
    ap = argparse.ArgumentParser(description="文件活动面板（多会话协作留言板）")
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME", "").strip() or None,
                    help="HERMES_HOME 目录（默认取环境变量，其次 ~/.hermes）")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument("--write-window", type=float, default=_WRITE_WINDOW)
    ap.add_argument("--read-window", type=float, default=_READ_WINDOW)
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    home = Path(args.home).expanduser() if args.home else Path.home() / ".hermes"
    files = sorted(glob.glob(str(home / f"{_PREFIX}*.json")))
    now = time.time()
    entries: list[dict] = []
    procs: list[dict] = []
    stale_files: list[str] = []

    for f in files:
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = data.get("pid")
        alive = _pid_alive(pid)
        procs.append({"pid": pid, "alive": alive, "updated": data.get("updated", 0)})
        fresh = False
        for e in data.get("entries", []):
            window = args.write_window if e.get("kind") == "write" else args.read_window
            if now - float(e.get("ts", 0)) <= window:
                fresh = True
                entries.append({**e, "pid": pid, "process_alive": alive})
        # 进程已退出、无新鲜条目、文件本身也放了一段时间 → 清掉陈旧快照
        if alive is False and not fresh and now - Path(f).stat().st_mtime > 600:
            stale_files.append(f)

    for f in stale_files:
        try:
            os.unlink(f)
        except OSError:
            pass

    if args.json:
        print(json.dumps({"home": str(home), "now": now,
                          "processes": procs, "entries": entries},
                         ensure_ascii=False, indent=1))
        return 0

    entries.sort(key=lambda e: float(e.get("ts", 0)), reverse=True)
    print("文件活动面板（多会话协作留言板 · 系统自动记录的事实，非 AI 留言）")
    print(f"  窗口: 写入≤{int(args.write_window // 60)}分钟 / 读取≤{int(args.read_window // 60)}分钟"
          f"   生成于 {time.strftime('%H:%M:%S')}")
    if procs:
        desc = ", ".join(
            f"{p['pid']}({'存活' if p['alive'] else ('未知' if p['alive'] is None else '已退出')})"
            for p in procs)
    else:
        desc = "无快照（尚无会话产生文件活动，或被 HERMES_FILE_ACTIVITY_SNAPSHOT=0 关闭）"
    print(f"  快照进程: {desc}")
    if not entries:
        print("  （窗口内暂无其他会话活动）")
        return 0
    for e in entries[: args.limit]:
        ts = float(e.get("ts", 0))
        who = str(e.get("task_id", "?"))
        if len(who) > 24:
            who = "…" + who[-22:]
        kind = "写入" if e.get("kind") == "write" else "读取"
        print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))}  {kind}  "
              f"{_fmt_age(ts, now):<8}{who}  {e.get('path', '')}")
    if len(entries) > args.limit:
        print(f"  …另有 {len(entries) - args.limit} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
