"""项目配置：从 config.json 加载 db 路径与工作区列表。
workspaces 决定 Claude adapter 扫哪些 projects 编码目录、桥接扫哪些 .story/。"""
import os, json

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cfg = json.load(open(os.path.join(_PROJ, 'config.json'), encoding='utf-8'))


def _resolve(p):
    """Resolve a config path: env-var override > abs path > relative to package."""
    env_key = {'db_path': 'MINER_DB_PATH', 'cache_dir': 'MINER_CACHE_DIR'}.get(p)
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    val = _cfg.get(p)
    return val if (val and os.path.isabs(val)) else os.path.join(_PROJ, val or '')


DB_PATH = _resolve('db_path')
# 分析脚本（distill/explore/learn/toolopt/workload 等）读写中间产物的缓存目录：
# events.pkl / sessions.json（旧布局的 ingest 产物）+ 各脚本输出的 dN_*.md。
# 默认指向 hc-all 的 .claude/tmp/cache（保留既有使用场景），可用 config.json 的
# "cache_dir" 或环境变量 MINER_CACHE_DIR 覆盖，以分析非 hc-all 项目。
CACHE_DIR = _resolve('cache_dir')
WORKSPACES = _cfg['workspaces']

def claude_encoding(path):
    """工作区路径 -> Claude Code projects 目录编码。 D:/hc-all -> D--hc-all
    规则: ':' 与每个分隔符 '\\' '/' 各替换为一个 '-'。"""
    return path.replace(':', '-').replace('\\', '-').replace('/', '-')

CLAUDE_ENCODINGS = [claude_encoding(p) for p in WORKSPACES]
