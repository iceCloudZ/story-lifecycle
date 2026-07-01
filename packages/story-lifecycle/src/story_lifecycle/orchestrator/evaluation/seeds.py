"""Seed data for quality flywheel: findings and learned patterns.

Seed data is NEVER auto-loaded in production. Call `seed_all()` explicitly
during dev setup or from the CLI. All functions are idempotent.
"""

from __future__ import annotations

from ...db import models as db

# -------- Seed Findings --------

SEED_FINDINGS = [
    {
        "severity": "high",
        "category": "error_handling",
        "description": "异步操作缺少 try/except 包裹，网络/文件 IO 异常未处理会导致静默失败",
        "location": "src/**/*.py (async functions)",
        "recommendation": "所有 async 函数内的 IO 操作包裹 try/except，记录日志后向上抛出或降级处理",
        "root_cause": "AI 生成代码倾向关注 happy path，异常路径常被忽略",
    },
    {
        "severity": "high",
        "category": "input_validation",
        "description": "API 入口未校验用户输入类型和范围，可能导致 downstream 崩溃或注入",
        "location": "src/**/api.py (endpoint handlers)",
        "recommendation": "使用 Pydantic model 声明请求体，或手动校验关键字段（type/range/length）",
        "root_cause": "AI 倾向假定输入合法，缺少 'never trust user input' 的防御性思维",
    },
    {
        "severity": "medium",
        "category": "sql_injection",
        "description": "SQL 拼接使用 f-string 或 .format() 而非参数化查询",
        "location": "src/**/models.py (raw SQL queries)",
        "recommendation": "始终使用 ? 占位符传递用户数据，禁止字符串拼接 SQL",
        "root_cause": "AI 在生成 demo/快速原型时倾向于最简单的字符串拼接方式",
    },
    {
        "severity": "medium",
        "category": "state_leak",
        "description": "模块级可变全局变量在不同 story 间共享状态，并发执行时产生竞态",
        "location": "src/**/*.py (module-level dict/list/set)",
        "recommendation": "将可变状态移入函数参数或类实例；全局只保留不可变常量",
        "root_cause": "AI 不感知多 story 并发执行场景，假设单线程顺序运行",
    },
    {
        "severity": "medium",
        "category": "missing_log",
        "description": "关键路径（DB 写入/外部 API 调用/状态变更）缺少结构化日志",
        "location": "src/**/*.py (write/network paths)",
        "recommendation": "在 IO 边界添加 `log.info(msg, extra={...})` 记录关键参数和结果",
        "root_cause": "AI 生成功能代码但忽略可观测性，导致线上排障困难",
    },
]

SEED_STORY_KEY = "__seed_findings__"


def seed_findings() -> int:
    """Load seed findings. Idempotent — skips if already seeded. Returns count loaded."""
    existing = db.get_open_findings(SEED_STORY_KEY, min_severity="low")
    if existing:
        return 0

    count = 0
    for f in SEED_FINDINGS:
        db.create_finding(
            story_key=SEED_STORY_KEY,
            stage="__seed__",
            source="seed",
            severity=f["severity"],
            category=f["category"],
            description=f["description"],
            location=f.get("location"),
            recommendation=f.get("recommendation"),
            root_cause=f.get("root_cause"),
        )
        count += 1
    return count


# -------- Seed Learned Patterns --------

SEED_PATTERNS = [
    {
        "pattern": "设计文档缺少验收标准",
        "applies_to": ["design"],
        "rule": "设计阶段产出必须包含可验证的 acceptance criteria。缺失则 design stage DoD 不通过。",
        "confidence": "high",
    },
    {
        "pattern": "模块耦合过高",
        "applies_to": ["design", "implement"],
        "rule": "新增功能应通过接口/适配器扩展核心模块，而非直接修改核心模块内部逻辑。",
        "confidence": "high",
    },
    {
        "pattern": "缺少回滚方案",
        "applies_to": ["design"],
        "rule": "涉及 schema 变更或数据迁移的设计，必须包含回滚步骤和风险评估。",
        "confidence": "medium",
    },
    {
        "pattern": "数据迁移遗漏",
        "applies_to": ["design", "implement"],
        "rule": "schema 变更（新增字段/修改表结构）必须附带 migration 脚本和验证步骤。",
        "confidence": "high",
    },
    {
        "pattern": "未声明外部依赖",
        "applies_to": ["design"],
        "rule": "新增的第三方库、外部服务调用、环境变量依赖必须在设计文档中显式列出。",
        "confidence": "medium",
    },
]


def seed_learned_patterns() -> int:
    """Load seed learned patterns as proposed. Idempotent — skips if any patterns exist. Returns count loaded."""
    existing = db.get_active_learned_patterns(limit=50)
    if not existing:
        existing = db.get_proposed_learned_patterns(limit=50)
    if existing:
        return 0

    count = 0
    for p in SEED_PATTERNS:
        db.create_learned_pattern(
            pattern=p["pattern"],
            applies_to=p["applies_to"],
            rule=p["rule"],
            source_findings=[],
            confidence=p["confidence"],
        )
        # Seed patterns stay as "proposed" — human must approve + activate
        count += 1
    return count


def seed_all() -> dict:
    """Run all seeds. Returns counts per category."""
    return {
        "findings": seed_findings(),
        "learned_patterns": seed_learned_patterns(),
    }
