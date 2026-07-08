# 状态流程图（Mermaid）

> 现状还原，基于 STATE-MAP.md。GitHub/GitLab 原生渲染 Mermaid。
> 创建：2026-07-08。

---

## 1. 主状态转移图（status × 触发入口 → 新状态）

```mermaid
stateDiagram-v2
    [*] --> candidate: TAPD/GitHub 拉取
    candidate --> planning: promote (intake_state=ready)
    planning --> planning: /plan/stream\nLLM 规划\n写 _agent_actions
    planning --> active: /plan/confirm\n(正道: 启动自动链路)
    planning --> planning: /sessions/spawn\n⚠️旁路: 不改状态!\n(今天 bug 根因)

    active --> active: done file 出现\nconfirm=false\n推进下一 stage
    active --> paused: done file 出现\nconfirm=true\n(明天加: 确认闸)
    active --> failed: stage 失败\n(spawn/超时/解析)
    active --> implementing: recovery 换 adapter\n重试
    implementing --> active: 重试中
    active --> paused: 服务器重启\nrecover_orphan_stories

    paused --> active: /advance\n(人点继续)
    paused --> active: 1秒轮询\n发现 done ready\n⚠️自动resume\n(与确认闸冲突)

    failed --> implementing: recovery\nretry_new_adapter
    failed --> [*]: 放弃

    active --> completed: 最后 stage done\n+ verify gate pass
    completed --> [*]

    note right of planning
        真相源在此刻:
        DB=planning, 但 done file
        可能已有 design.json
        (手动跑的) → 四处打架
    end note

    note right of paused
        paused 三义:
        1. 确认闸 (有 _stage_gate)
        2. 重启孤儿恢复
        3. 父子任务等待
        → status 字段无区分
    end note
```

---

## 2. 5 个驱动入口（核心问题：无唯一调度者）

```mermaid
flowchart LR
    subgraph 入口["5 个驱动入口 (都能改 status/起进程)"]
        E1["/plan/confirm\n✅正道"]
        E2["/sessions/spawn\n⚠️旁路 不改status"]
        E3["/advance\nresume"]
        E4["/skip/{stage}\n跳过"]
        E5["1秒后台轮询\n自动resume"]
    end

    subgraph 故事["同一个 Story"]
        S["DB status\n(9个值)"]
        C["context_json\n_agent_actions\n_plan_confirmed\n_active_execution"]
        D["done files\n.stage/done/..."]
        P["PTY 注册表\n_ptys (内存)"]
    end

    E1 -->|改status+起链路| S
    E1 -->|spawn| P
    E2 -.->|只spawn 不改DB| P
    E3 -->|paused→active| S
    E3 -->|起链路| P
    E4 -->|→active 起链路| S
    E5 -->|→active 自动resume| S
    E5 -->|起链路| P

    S -.->|不一致| D
    C -.->|不一致| P
    D -.->|不一致| S

    style E2 fill:#fdd,stroke:#c33
    style E5 fill:#ffe,stroke:#cc3
    style S fill:#fef,stroke:#939
```

---

## 3. 4 处真相源（"story 进展到哪了"散在 4 处）

```mermaid
flowchart TB
    Q["❓ story 现在进展到哪了?"]

    Q --> F1["1. DB status\nplanning/active/paused/\nfailed/completed/...\n9 个值, 混 3 层语义"]
    Q --> F2["2. DB current_stage\ndesign/build/verify\n2 个写路径, 与 status 不同步"]
    Q --> F3["3. context_json._active_execution.stage\n仅自动链路写\n与 current_stage 100% 冗余"]
    Q --> F4["4. 文件系统 done file\n.stage/done/key/stage.json\nstage 真做完了吗的真相\n但 DB 无字段反映"]

    F1 -.->|引擎内部状态| X1["混了: 业务状态 + 引擎态 + 化石"]
    F2 -.->|X| X2["和 status 常矛盾"]
    F3 -.->|X| X3["孤立终端不写 → 缺失"]
    F4 -.->|X| X4["靠 poll, 无 DB 投影"]

    style Q fill:#fec,stroke:#c93
    style F1 fill:#fee
    style F2 fill:#fee
    style F3 fill:#fee
    style F4 fill:#fee
```

---

## 4. 归一目标：1 个真相源 + N 个派生视图

```mermaid
flowchart TB
    subgraph 归一["归一后"]
        T["单一真相源\ndriver层进度:\ncontext_json._completed_stages\n= [design, build]"]
        G["确认闸标记:\ncontext_json._stage_gate"]

        T --> D1["派生: 业务状态\n开发/测试/上线\n(读时算, 不另存)"]
        T --> D2["派生: current_stage\n= completed 之后第一个 launch"]
        T --> D3["派生: done 进度\n扫 done file 仅作输入"]

        S2["引擎执行态 status:\nactive / paused / failed\n(只表健康度, 不管业务)"]

        T -.->|正交| S2
        G -.->|关联| S2
    end

    style T fill:#cfc,stroke:#393
    style G fill:#cfc,stroke:#393
    style D1 fill:#eef
    style D2 fill:#eef
    style D3 fill:#eef
    style S2 fill:#fef
```

---

## 5. 三层分层（北极星，STATE-MAP 末尾）

```mermaid
flowchart TB
    subgraph 配置["配置层 (profile yaml) — 单一真相源"]
        CFG["阶段序列 / 转移规则 / 确认闸 / 重试上限\nconfirm, review, adversarial\n现状: 死配置(解析了从不读)\n目标: 代码不硬编码, 全从这读"]
    end

    subgraph 驱动["驱动层 (StageDriver) — 厚, 确定"]
        DRV["读配置 → 按阶段跑 → 推进状态机\n只管编排, 不碰 PTY 细节\n状态机: 设计done→开发→开发done→测试→...\n持有 _completed_stages (唯一真相源)"]
    end

    subgraph PTY["PTY 层 (PtyManager) — 薄, 无状态"]
        PTY["单进程生命周期: spawn/alive/kill/resume\n不知道 Story, 不知道阶段\n给命令就跑, 写 done 就算完"]
    end

    subgraph 业务["Story 业务状态 — 派生视图"]
        BIZ["开发 / 测试 / 上线\n从 _completed_stages 派生\n不另存字段"]
    end

    配置 -->|读| 驱动
    驱动 -->|起/读done| PTY
    驱动 -->|派生| 业务

    style 配置 fill:#fef,stroke:#939
    style 驱动 fill:#cfc,stroke:#393
    style PTY fill:#eef,stroke:#339
    style 业务 fill:#fec,stroke:#c93
```

---

## 6. 现状 vs 归一后对比（一句话总结）

```mermaid
flowchart LR
    subgraph 现在["现在: 一个函数跨 4 层"]
        N["continue_orchestrator_agent (500行)\n├─ 改 Story status\n├─ 推进 stage\n├─ spawn/kill PTY\n├─ poll done file\n└─ 起 supervisor 线程\n\n→ 厚协调器干了执行的活\n→ 4 处真相源常打架"]
    end

    subgraph 目标["目标: 三层各管各的"]
        G["StageDriver (驱动层)\n只管编排, 持唯一真相源\n\nPtyManager (PTY层)\n只管进程, 不知 Story\n\n业务状态 = 派生\n不另存"]
    end

    现在 -->|归一| 目标

    style 现在 fill:#fee,stroke:#c33
    style 目标 fill:#cfc,stroke:#393
```
