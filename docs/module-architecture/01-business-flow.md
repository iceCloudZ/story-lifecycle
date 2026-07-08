# 01 · 端到端业务流程

> 一个需求(Story)从进入系统到交付,经过哪些业务步骤。
> 把 87 个 API 端点按调用时序串起来,得到这条主线。

## 主线:Story 生命周期

```
①接入    ②规划       ③准备         ④执行(可循环)      ⑤验证      ⑥交付     ⑦沉淀
─────────────────────────────────────────────────────────────────────────────
需求源 ──▶ 建 Story ──▶ 注入上下文 ──▶ AI CLI 跑阶段 ──▶ Gate 闸 ──▶ 交付包 ──▶ 复盘
TAPD     /api/story   /context      /plan/stream      /gate      /delivery  /done
GitHub   /sub         /release-     /plan/confirm     /quality   /context   → miner
手动     /intake      prompt        /pty/spawn        /findings  /pack      → 飞轮
                       /worktrees    /answer
                                     /clarify
                                     /wait              ↑
                                     └── retry ◀─ gate 拒 ┘
```

**核心 insight**:这本质是一条 **AI 驱动的 SDLC 流水线**,跟传统 CI/CD 的区别是——流水线的每个"工序"不是脚本,而是 **AI CLI 会话**,由编排引擎规划、执行、验证、推进。

## 业务流程图(Flowchart)

```mermaid
flowchart TD
    %% 接入
    SRC["需求源<br/>TAPD / GitHub / 手动"] --> INTAKE["① 需求接入<br/>建 Story + PRD"]
    INTAKE --> SUB{"复杂需求?"}
    SUB -->|是| DECOMP["子 Story 拆分<br/>带依赖 DAG"]
    SUB -->|否| STAGE
    DECOMP --> STAGE

    %% 上下文
    STAGE["选定 stage<br/>design/build/verify"] --> CTX["② 上下文装配<br/>resolver 聚合 + 知识注入"]
    KNOW[("知识飞轮<br/>scenario/playbook/failure")] -.注入.-> CTX
    MINER[("miner<br/>transcript 历史")] -.transcript_context.-> CTX

    %% 执行
    CTX --> MODE{"执行模式?"}
    MODE -->|全自动 FC| FC["planner FC 循环<br/>生成 actions"]
    MODE -->|交互式| INTER["claude 'query' 自动开跑<br/>人 watch+steer"]
    MODE -->|半自动| SEMI["release_prompt 渲染<br/>人拷给 CLI"]

    FC --> CONFIRM{"plan 确认<br/>(_plan_confirmed)"}
    CONFIRM -->|人放行| EXEC
    CONFIRM -. HITL .-> HUMAN
    INTER --> EXEC
    SEMI --> EXEC
    EXEC["③ 执行编排<br/>adapters + pty 起 AI CLI<br/>轮询 .done"]

    %% 验证循环
    EXEC --> GATE["④ 质量闸<br/>run_verify_gate"]
    GATE --> VERDICT{判定}
    VERDICT -->|advance| NEXT{"还有 stage?"}
    VERDICT -->|retry| FC
    VERDICT -->|fail / 超 max_retries| HUMAN["⑦ HITL 介入<br/>审批/澄清/steer"]
    HUMAN --> FC

    %% 交付 + 沉淀
    NEXT -->|是| STAGE
    NEXT -->|否| DELIVER["⑤ 交付收尾<br/>delivery-artifacts + worktree 清理<br/>+ 上游回写"]
    DELIVER --> DONE["story done"]
    DONE --> RETRO["⑥ 知识沉淀<br/>retrospect → miner"]
    RETRO -.反哺.-> KNOW

    %% 样式
    classDef intake fill:#e1f5e1,stroke:#2e7d32
    classDef exec fill:#fff3e0,stroke:#e65100
    classDef gate fill:#fce4ec,stroke:#c62828
    classDef human fill:#e8eaf6,stroke:#283593
    classDef flywheel fill:#f3e5f5,stroke:#6a1b9a
    class INTAKE,DECOMP intake
    class FC,EXEC,INTER,SEMI exec
    class GATE gate
    class HUMAN human
    class KNOW,RETRO,MINER flywheel
```

## 全自动 FC 模式时序(系统主路径)

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户
    participant API as service/api.py
    participant P as engine/planner.py<br/>(FC 循环)
    participant A as adapters/ + pty.py
    participant CLI as AI CLI<br/>(claude/codex)
    participant G as evaluation/gate.py
    participant DB as SQLite

    U->>API: POST /api/story/{key}/plan/stream
    API->>P: run_orchestrator_agent
    P->>P: LLM invoke_with_tools(规划)
    P->>DB: 写 _agent_actions 队列<br/>_plan_confirmed=False
    P-->>U: SSE 流(规划过程)

    Note over U,API: 暂停 — 等人确认(⑦ HITL)
    U->>API: POST /plan/confirm
    API->>P: graph.start_story_async
    P->>P: continue_orchestrator_agent

    loop 每个 action
        P->>A: launch(adapter, prompt)
        A->>CLI: spawn PTY + claude "query"
        CLI-->>A: 输出流(thinking/tools/diff)
        A->>A: 轮询 .done 握手
        CLI-->>A: 写 done 文件
        A-->>P: action 完成
    end

    P->>G: run_verify_gate(产出)
    G->>G: 检查 round_count vs max_retries
    alt advance
        G-->>P: 推进下一 stage
    else retry
        G-->>P: 拒绝 → planner 重插 launch action
        P->>A: 重新执行(LLM 自驱重试)
    else fail(超 max_retries)
        G-->>U: 硬闸强制 fail,等人(⑦)
    end

    P->>DB: 更新 story 状态
    P-->>U: /ws/story 推送最终状态
```

## 交互式终端模式时序(当前主方向)

> 最近 commits 的主线:`claude "query"` 取代 PTY 注入。

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户(前端)
    participant WS_FE as WebSocket 前端
    participant API as service/api.py
    participant PTY as infra/terminal/pty.py
    participant CLI as claude "query"

    U->>API: 点「启动终端」→ POST /sessions/spawn
    API->>API: _build_stage_launch_prompt(story)<br/>写完整 prompt 到文件
    API->>CLI: spawn ["claude", "<读文件指令>"]
    Note over CLI: claude 自己管 readiness<br/>加载完自动处理初始 prompt
    CLI-->>PTY: 自动开跑 design

    PTY-->>WS_FE: /ws/pty 实时推输出
    WS_FE-->>U: xterm 渲染

    Note over U,CLI: 人实时 watch<br/>Esc 打断 / 打字纠偏(steer)
    U->>WS_FE: 键盘输入
    WS_FE->>PTY: ws.send(输入)
    PTY->>CLI: 写 stdin

    CLI-->>CLI: Read PRD → 出设计 → 写 done
    CLI-->>API: 写 .story/done/<key>/<stage>.json
```

## 两条横向贯穿的轴

### 知识飞轮轴(模块⑥,跨包)

```mermaid
flowchart LR
    subgraph lifecycle [story-lifecycle 执行期]
        A1["adapters 写<br/>anchors.jsonl"] --> A2["context_provider<br/>注入 {transcript_context}"]
        A3["story done<br/>触发复盘"]
    end

    subgraph miner [story-miner 离线]
        M1["store 入库<br/>transcript → SQLite"]
        M2["link 读 anchors<br/>精确绑定 story↔session"]
        M3["挖掘脚本<br/>playbook/failure/scenario"]
    end

    subgraph knowledge [knowledge 契约包]
        K1["统一 INDEX<br/>scenario+playbook+failure"]
    end

    A1 --> M2
    M1 --> M2
    M2 --> M3
    M3 --> K1
    K1 -.SOFT 缝.-> A2
    A3 --> M3

    classDef l fill:#e3f2fd,stroke:#1565c0
    classDef m fill:#fff3e0,stroke:#e65100
    classDef k fill:#f3e5f5,stroke:#6a1b9a
    class A1,A2,A3 l
    class M1,M2,M3 m
    class K1 k
```

### HITL 轴(模块⑦,横切③④)

```mermaid
flowchart LR
    subgraph exec [③ 执行编排]
        E1[FC 规划]
        E2[AI 跑阶段]
    end
    subgraph gate [④ 质量闸]
        G1[判定]
    end

    H1["Plan 确认"] -.阻塞.-> E1
    H2["Clarify 逐问"] -.阻塞.-> E2
    H3["交互终端 steer"] -.实时.-> E2
    H4["Finding 裁决"] -.反馈.-> G1
    H5["Pattern 审批"] -.学经验.-> G1

    classDef hitl fill:#e8eaf6,stroke:#283593
    class H1,H2,H3,H4,H5 hitl
```

## 与业界参考架构的对照

对照 [Augment Code 的 AI SDLC 五层参考架构](https://www.augmentcode.com/guides/ai-sdlc-framework-reference-architecture):

| 业界层 | 本项目对应模块 |
|---|---|
| Governance(治理) | ④ 质量闸 + ⑦ HITL(Profile + Gate 硬闸 + 审批) |
| Agent Execution(agent 执行) | ③ adapters + pty |
| Orchestration(编排) | ③ planner FC 循环 + stage_graph |
| Platform/Knowledge(平台知识) | **⑥ 知识飞轮(本项目独有强项)** |
| Observability(观测) | observability + loop-trace + debug |

**差异化**:大多数同类系统(如 [ai-sdlc.io](https://ai-sdlc.io/))做编排但不闭环沉淀经验。本项目靠 miner + knowledge 契约 + SOFT 缝,把每次执行的经验反哺下次——这是 `dev-flywheel` 名字的实质。
