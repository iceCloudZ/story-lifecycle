from story_lifecycle.sourcing.sources.tapd_source import TapdSource
from story_lifecycle.sourcing.sources.tapd_source import _paginated, MAX_PAGES


class FakeTapdApi:
    workspace_id = 44381896

    def get_story_detail(self, item_id):
        return {
            "Story": {
                "id": "1144381896001065618",
                "name": "授信提现展示拒绝原因",
                "description": "提现被拒绝时展示拒绝原因",
                "status": "status_3",
            }
        }

    def get_bug_detail(self, item_id):
        return {
            "Bug": {
                "id": "123",
                "title": "缺陷标题",
                "description": "缺陷描述",
                "status": "new",
            }
        }


def test_get_detail_flattens_story_wrapper(monkeypatch):
    source = TapdSource({"workspace_id": "44381896"})
    source._api = FakeTapdApi()

    item = source.get_detail("1065618")

    assert item is not None
    assert item.id == "1144381896001065618"
    assert item.title == "授信提现展示拒绝原因"
    assert item.description == "提现被拒绝时展示拒绝原因"


def test_get_detail_flattens_bug_wrapper(monkeypatch):
    source = TapdSource({"workspace_id": "44381896"})
    source._api = FakeTapdApi()

    item = source.get_detail("bug_123")

    assert item is not None
    assert item.id == "bug_123"
    assert item.title == "缺陷标题"


# --- 分页翻页(_paginated)测试 -------------------------------------------------
# 回归:此前 _fetch_bugs 写死 limit=20 且不翻页,bug 超过 20 个被静默截断,
# 导致已流转的 bug 状态永远刷不到。_paginated 靠"返回不满页判末页"翻页。


def _make_pages(specs):
    """造一个分页 fetcher:按 page 编号返回不同长度的页。specs = {page: count}。"""

    def fetcher(params):
        page = params.get("page", 1)
        count = specs.get(page, 0)
        return [{"page": page, "i": i} for i in range(count)]

    return fetcher


def test_paginated_stops_on_partial_page():
    """第二页不满 → 末页,停止翻页。"""
    fetcher = _make_pages({1: 200, 2: 50})  # 第一页满 200,第二页 50(< 200)
    rows = _paginated(fetcher, {"status": "new"}, limit=200)
    assert len(rows) == 250  # 200 + 50


def test_paginated_continues_on_full_pages():
    """连续满页 → 一直翻,直到不满页。"""
    fetcher = _make_pages({1: 200, 2: 200, 3: 10})
    rows = _paginated(fetcher, {}, limit=200)
    assert len(rows) == 410  # 200*2 + 10


def test_paginated_single_page_when_under_limit():
    """首页就不满 → 只拉一页,不浪费调用。"""
    fetcher = _make_pages({1: 5})
    rows = _paginated(fetcher, {}, limit=200)
    assert len(rows) == 5


def test_paginated_empty_first_page():
    """空结果 → 单次调用即停。"""
    fetcher = _make_pages({})
    rows = _paginated(fetcher, {}, limit=200)
    assert rows == []


def test_paginated_safety_cap_on_runaway_full_pages(monkeypatch):
    """TAPD 异常持续返回满页 → MAX_PAGES 安全阀防死循环。"""
    fetcher = _make_pages({p: 200 for p in range(1, MAX_PAGES + 5)})
    rows = _paginated(fetcher, {}, limit=200)
    # 恰好 MAX_PAGES 页(不触发无限循环)
    assert len(rows) == 200 * MAX_PAGES


def test_paginated_passes_base_params_each_page():
    """每页都带 base_params(status/current_owner 等),不被分页参数冲掉。"""
    seen = []

    def fetcher(params):
        seen.append(dict(params))
        return []  # 空页立即停

    _paginated(fetcher, {"status": "resolved", "current_owner": "alice"}, limit=200)
    assert seen[0]["status"] == "resolved"
    assert seen[0]["current_owner"] == "alice"
    assert seen[0]["page"] == 1
    assert seen[0]["limit"] == 200
