"""C5 接线测试：WhitelistStore 规则版本化（版本哈希+变更历史+重验谓词）。"""
import json

import pytest

from agent.shell_policy import WhitelistStore


@pytest.fixture()
def store(tmp_path):
    return WhitelistStore(path=str(tmp_path / "wl.json"))


def test_version_stable_and_order_insensitive(store):
    """同一规则集键序无关哈希一致；内容变了版本才变。"""
    store.set_safe({"ls", "cat"})
    v1 = store.current_version()
    assert store.current_version() == v1  # 幂等
    store.set_safe({"cat", "ls"})  # 同内容不同顺序
    assert store.current_version() == v1
    store.set_safe({"cat", "ls", "curl"})  # 内容变化
    assert store.current_version() != v1


def test_version_history_recorded(store):
    """规则变更留历史：时间+新版本号，最多 10 条，新在前。"""
    versions = []
    for cmd in ({"ls"}, {"ls", "cat"}, {"ls", "cat", "curl"}, {"ls"}):
        store.set_safe(cmd)
        versions.append(store.current_version())
    hist = store.version_history()
    assert [h["version"] for h in hist] == versions[::-1]  # 新在前
    assert all("at" in h for h in hist)
    # 元数据不进哈希：连续 _save 不再追加历史
    before = len(store.version_history())
    store._save()
    assert len(store.version_history()) == before


def test_needs_revalidate(store):
    """接单时冻结的版本 != 当前版本 → 要求重验；一致则放行。"""
    store.set_safe({"ls"})
    old = store.current_version()
    assert store.needs_revalidate(old) is False
    store.set_safe({"ls", "cat"})
    assert store.needs_revalidate(old) is True


def test_metadata_not_in_version(tmp_path):
    """手工塞 version 元数据的旧文件加载后，版本只由规则内容决定。"""
    path = tmp_path / "wl.json"
    path.write_text(json.dumps({"safe": ["ls"], "version": "stale", "version_history": []}), encoding="utf-8")
    store = WhitelistStore(path=str(path))
    fresh = WhitelistStore(path=str(tmp_path / "wl2.json"))
    fresh.set_safe({"ls"})
    assert store.current_version() == fresh.current_version()
