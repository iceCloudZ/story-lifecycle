-- judge_feedback 表 DDL（迭代 4 C 线，人判 vs 机判校准）
-- 依据：docs/iteration-4-pass-accuracy-design.md §5
-- 机判数据在 orchestrator_decision 表（story_key/stage/trigger/decision/reason/decided_at）；
-- 本表只记人判侧（零 LLM 成本），混淆矩阵口径：
--   机判 approve + 人判 disagree = 漏拦；机判 reject/escalate + 人判 disagree = 误拦
CREATE TABLE IF NOT EXISTS judge_feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    story_key    TEXT NOT NULL,
    decision_id  INTEGER NOT NULL,           -- 关联 orchestrator_decision.id
    machine_decision TEXT NOT NULL DEFAULT '',  -- 冗余快照（机判 decision，join 时校验）
    human_decision   TEXT NOT NULL CHECK (human_decision IN ('agree', 'disagree')),
    note         TEXT NOT NULL DEFAULT '',
    decided_at   TEXT NOT NULL DEFAULT '',   -- 冗余快照（决策时间）
    created_at   TEXT NOT NULL,
    UNIQUE(story_key, decision_id)           -- 重复提交覆盖（最近人判为准）
);
CREATE INDEX IF NOT EXISTS idx_jf_story ON judge_feedback(story_key);
CREATE INDEX IF NOT EXISTS idx_jf_decision ON judge_feedback(decision_id);
CREATE INDEX IF NOT EXISTS idx_jf_created ON judge_feedback(created_at);
