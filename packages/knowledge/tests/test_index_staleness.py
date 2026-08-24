"""B3 回归:KnowledgeIndex 索引新鲜度自愈。

任何知识文件新于 INDEX.json → 加载时自动重建,不依赖调用方"记得" refresh。
这是飞轮"回写→召回"闭合的兜底:reflection 落盘(写后重建是主路径)之外,
外部脚本/手工改动绕路写文件也能在下次检索时自愈。
"""

import json
import os

from knowledge.generator import write_index
from knowledge.index import KnowledgeIndex


def _mk_playbook(kdir, name, title):
    path = kdir / "playbooks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: playbook:{name[:-3]}\ntitle: {title}\n---\n# {title}\n",
        encoding="utf-8",
    )
    return path


def _bump_mtime(path, delta_s=10.0):
    """把文件 mtime 拨到指定基准之后(规避 Windows mtime 粒度)。"""
    base = os.path.getmtime(path) + delta_s
    os.utime(path, (base, base))


def test_index_selfheals_when_file_newer_than_index(tmp_path):
    kdir = tmp_path / "k"
    _mk_playbook(kdir, "debug.md", "排查")
    write_index(str(kdir))
    assert (kdir / "INDEX.json").exists()

    # 绕路新增知识文件(不经 write_index),mtime 晚于 INDEX
    new = _mk_playbook(kdir, "deploy.md", "部署")
    _bump_mtime(new)

    idx = KnowledgeIndex(str(kdir))  # 不调 refresh()
    assert idx.get("playbook:deploy") is not None, "新文件应触发自愈重建并可召回"


def test_index_not_rebuilt_when_nothing_newer(tmp_path, monkeypatch):
    """所有文件都旧于 INDEX → 不重建(构造器不得有写副作用)。"""
    kdir = tmp_path / "k"
    _mk_playbook(kdir, "debug.md", "排查")
    write_index(str(kdir))
    before = (kdir / "INDEX.json").read_text(encoding="utf-8")

    import knowledge.index as idx_mod

    monkeypatch.setattr(idx_mod, "write_index", lambda d: (_ for _ in ()).throw(
        AssertionError("不应触发重建")
    ))
    idx = KnowledgeIndex(str(kdir))
    assert idx.get("playbook:debug") is not None
    assert (kdir / "INDEX.json").read_text(encoding="utf-8") == before


def test_index_missing_is_generated(tmp_path):
    """无 INDEX.json 的目录 → 构造时生成(既有行为,回归保护)。"""
    kdir = tmp_path / "k"
    _mk_playbook(kdir, "debug.md", "排查")
    idx = KnowledgeIndex(str(kdir))
    assert (kdir / "INDEX.json").exists()
    assert idx.get("playbook:debug") is not None


def test_staleness_scan_ignores_non_knowledge_files(tmp_path):
    """非 .md/.json 文件(cache 等)不触发重建 —— INDEX 内容不变的证据。"""
    kdir = tmp_path / "k"
    _mk_playbook(kdir, "debug.md", "排查")
    write_index(str(kdir))
    payload_before = json.loads((kdir / "INDEX.json").read_text(encoding="utf-8"))

    cache = kdir / "cache" / "tmp.bin"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"\x00")
    _bump_mtime(cache)

    idx = KnowledgeIndex(str(kdir))
    assert idx.get("playbook:debug") is not None
    payload_after = json.loads((kdir / "INDEX.json").read_text(encoding="utf-8"))
    assert payload_after["entries"] == payload_before["entries"]
