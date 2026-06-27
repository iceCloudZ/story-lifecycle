"""项目配置：从 config.json 加载 db 路径与工作区列表。
workspaces 决定 Claude adapter 扫哪些 projects 编码目录、桥接扫哪些 .story/。"""
import os, json

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cfg = json.load(open(os.path.join(_PROJ, 'config.json'), encoding='utf-8'))

DB_PATH = _cfg['db_path'] if os.path.isabs(_cfg['db_path']) else os.path.join(_PROJ, _cfg['db_path'])
WORKSPACES = _cfg['workspaces']

def claude_encoding(path):
    """工作区路径 -> Claude Code projects 目录编码。 D:/hc-all -> D--hc-all
    规则: ':' 与每个分隔符 '\\' '/' 各替换为一个 '-'。"""
    return path.replace(':', '-').replace('\\', '-').replace('/', '-')

CLAUDE_ENCODINGS = [claude_encoding(p) for p in WORKSPACES]
