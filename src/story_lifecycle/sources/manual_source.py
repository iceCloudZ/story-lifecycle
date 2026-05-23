from .base import SourceItem, StorySource


class ManualSource(StorySource):
    def fetch_pending(self) -> list[SourceItem]:
        return []

    def get_detail(self, item_id: str) -> SourceItem | None:
        return None

    def sync_status(self, item_id: str, status: str):
        pass

    def test_connection(self) -> bool:
        return True
