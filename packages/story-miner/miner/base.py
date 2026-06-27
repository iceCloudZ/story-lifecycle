"""adapter 抽象基类 + 注册表。

新增端的步骤（零改 store/分析层）：
  1. 在 adapters/ 下新建 <name>.py
  2. 写一个 SourceAdapter 子类，实现 name/discover()/parse()，用 @register_adapter 装饰
  3. 在 adapters/__init__.py 加一行 `from . import <name>`
store.py 和所有分析脚本只依赖 transcript.REGISTRY，不感知具体端。
"""
import abc

REGISTRY = []

def register_adapter(cls):
    """类装饰器：实例化并加入全局 REGISTRY。"""
    REGISTRY.append(cls())
    return cls

class SourceAdapter(abc.ABC):
    name = ''        # 稳定短标识: 'claude' / 'codex' / 'kimi' / 'cursor' ...
    label = ''       # 人类可读名

    def discover(self):
        """yield (abs_path, stable_sid) —— 该端每个会话源文件。
        sid 必须稳定（同文件重跑得到同 sid），用于增量更新。"""
        return []

    @abc.abstractmethod
    def parse(self, path, sid):
        """解析单个源文件 -> (meta_dict, [event_dict,...])，映射到 common.py 的统一 schema。
        meta 无有效内容时返回 (None, [])，store 会跳过。"""
        ...
