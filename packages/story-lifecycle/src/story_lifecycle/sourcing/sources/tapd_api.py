from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CLI_TAPD_PATH = Path.home() / ".claude" / "scripts" / "cli_tapd.py"


def _load_cli_tapd_module():
    """Dynamically load cli_tapd.py to access TAPDClient and cmd_ functions."""
    if not _CLI_TAPD_PATH.exists():
        raise FileNotFoundError(f"cli_tapd.py not found: {_CLI_TAPD_PATH}")
    import importlib.util

    spec = importlib.util.spec_from_file_location("cli_tapd", str(_CLI_TAPD_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TapdApi:
    def __init__(self, workspace_id: str, **kwargs):
        self.workspace_id = int(workspace_id) if workspace_id else 0
        self._mod = None
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return
        self._mod = _load_cli_tapd_module()
        self._client = self._mod.TAPDClient()

    def _call(self, command: str, params: dict) -> Any:
        self._ensure_client()
        func_name = f"cmd_{command.replace('-', '_')}"
        func = getattr(self._mod, func_name, None)
        if func is None:
            log.warning(f"TAPD command not found: {func_name}")
            return None
        try:
            return func(self._client, self.workspace_id, params)
        except Exception as e:
            log.warning(f"TAPD API error ({command}): {e}")
            return None

    def get_stories(self, params: dict) -> list[dict]:
        result = self._call("get_stories", params)
        if isinstance(result, dict):
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return result if isinstance(result, list) else []

    def get_bugs(self, params: dict) -> list[dict]:
        result = self._call("get_bug", params)
        if isinstance(result, dict):
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return result if isinstance(result, list) else []

    def get_story_detail(self, story_id: str) -> dict | None:
        result = self._call("get_stories", {"entity_type": "stories", "id": story_id})
        if isinstance(result, dict):
            data = result.get("data", [])
            return data[0] if data else None
        return None

    def get_bug_detail(self, bug_id: str) -> dict | None:
        result = self._call("get_bug", {"id": bug_id})
        if isinstance(result, dict):
            data = result.get("data", [])
            return data[0] if data else None
        return None

    def update_story(self, story_id: str, fields: dict) -> bool:
        result = self._call("update_story", {"id": story_id, **fields})
        return result is not None

    def update_bug(self, bug_id: str, fields: dict) -> bool:
        result = self._call("update_bug", {"id": bug_id, **fields})
        return result is not None

    def get_comments(self, entry_id: str) -> list[dict]:
        result = self._call("get_comments", {"entry_id": entry_id, "entry_type": "bug"})
        if isinstance(result, dict):
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return result if isinstance(result, list) else []

    def get_entity_relations(self, entity_id: str) -> list[dict]:
        result = self._call("entity_relations", {"entity_id": entity_id})
        if isinstance(result, dict):
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return result if isinstance(result, list) else []

    def get_related_bugs(self, story_id: str) -> list[dict]:
        """Bugs linked to a story (TAPD stories/get_related_bugs)."""
        result = self._call("get_related_bugs", {"story_id": story_id})
        if isinstance(result, dict):
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return result if isinstance(result, list) else []

    def get_status_map(self, system: str = "story") -> dict:
        """工作流状态字典(status_N → 中文名)。自定义工作流的 status 字段返回
        不透明 status_N ID,展示层需要此字典译名(2026-08-28 实测)。

        不带 workitem_type_id 返回全类型并集(需求自定义态 + 任务三态),
        一份字典同时覆盖 story 和 subtask 展示。system=bug 单独取缺陷枚举。
        """
        result = self._call("get-workflows-status-map", {"system": system})
        if isinstance(result, dict):
            data = result.get("data", {})
            return data if isinstance(data, dict) else {}
        return {}
