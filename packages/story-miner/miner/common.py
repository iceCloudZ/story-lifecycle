"""公共工具 + 统一 schema 契约（所有 adapter 必须映射到此）。

统一 event schema (dict):
  sid, src, ws, ts, kind, name, cmd, code, ok, text, path
  kind 枚举:
    ucmd    用户指令（真实人输入，已过滤系统注入）
    utext   用户其他文本
    atext   助手文本输出
    tool    工具/函数调用  (name; cmd 仅 Bash/shell)
    result  工具执行结果   (ok: bool)
    code    写入的代码 diff (name=Edit/Write; path=目标文件)
    think   推理/思考片段
统一 session meta (dict):
  sid, src, ws, ts, title, turns, ntools, nerrs, cwd, branch, first_ucmd
"""
import os, re, datetime

SYS_PREFIX = ('<task-notification','<system-reminder','<command-name','<command-message',
              '<local-command','<bash-input','<bash-stdout','<environment_context','<task-')

WS_KEYWORDS = ['hc-all','java-agent','github','story-lifecycle','baoxian','ys-agent',
               'aiops-mcp','pitch-oracle','stock-research','lifestyle','lifeops']

def ws_of(cwd):
    """从 cwd 提取工作区标签。"""
    if not cwd: return '?'
    c=cwd.replace('\\','/').lower()
    for k in WS_KEYWORDS:
        if k in c: return k
    return os.path.basename(cwd.replace('\\','/').rstrip('/')) or '?'

INJECT_PREFIX = ('# AGENTS.md', '# CLAUDE.md', '# RTK', 'Base directory for this skill',
                 '## 任务', '## 任务书', '## 任务信息', '## 背景',
                 '你是一个 headless', '对需求进行分析', '根据设计文档进行编码')

def real_user(s):
    """是否真实用户输入（排除系统注入标签 + AGENTS.md/skill 正文/任务书模板等注入）。"""
    s=(s or '').strip()
    if not s: return False
    if s[0]=='<' and any(s.startswith(p) for p in SYS_PREFIX): return False
    if any(s.startswith(p) for p in INJECT_PREFIX): return False
    return True

def mask(s):
    """脱敏：手机号/长数字/邮箱。蒸馏或导出前必须再人工复核 PII。"""
    if not s: return s
    s=re.sub(r'\b09\d{9}\b','09*********',s)
    s=re.sub(r'\+63\d{10}','+63**********',s)
    s=re.sub(r'\b\d{11,}\b',lambda m:m.group()[:3]+'*'*8,s)
    s=re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+','***@***.***',s)
    return s

def full_ts(o, fallback=''):
    """从事件 dict 提取完整 ISO 时间戳(Claude/Codex 的 timestamp;Kimi 的 time 毫秒)。"""
    ts = o.get('timestamp')
    if ts:
        return str(ts)
    t_ms = o.get('time')
    if t_ms:
        try:
            return datetime.datetime.fromtimestamp(
                t_ms / 1000, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(t_ms) % 1000:03d}Z'
        except Exception:
            return fallback
    return fallback
