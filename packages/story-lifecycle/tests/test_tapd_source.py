from story_lifecycle.sources.tapd_source import TapdSource


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
