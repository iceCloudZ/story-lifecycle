> ⚠️ **历史快照（归档于 2026-07）**：描述的架构可能已被后续演进取代。当前架构见 [../../ARCHITECTURE.md](../../ARCHITECTURE.md)。本文件保留作决策记录（ADR），正文未修改。

---

# 多模态 Intake PRD 生成流程

```mermaid
flowchart TD
    A[用户在前端点击<br/>新建/开始 或 生成 PRD] --> B[前端 POST /api/intake/preview<br/>source_id=xxx]
    B --> C[api_intake_preview<br/>从 TAPD 拉取 story 详情]
    C --> D[构造 StorySourceSnapshot<br/>包含 title/description/url 等]
    D --> E[prd_generator.generate_prd_from_source]
    E --> F[_extract_image_urls<br/>从 HTML/Markdown 提取图片 URL/路径]
    F --> G{是否存在图片?}
    G -->|否| H[调用主 LLM 文本生成<br/>get_llm().invoke_structured]
    G -->|是| I[_prepare_images<br/>处理图片]
    I --> J{图片类型}
    J -->|data URL| K[直接使用]
    J -->|本地路径| L[透传给 Kimi CLI]
    J -->|tapd.cn URL| M[通过 TAPD client<br/>下载并转 base64]
    J -->|公网 URL| N[httpx 下载<br/>转 base64 data URL]
    K --> O{STORY_VISION_PROVIDER<br/>或模型判断}
    L --> O
    M --> O
    N --> O
    O -->|kimi-cli /<br/>kimi-for-coding| P[KimiCliClient.invoke_vision<br/>headless 调用本地 kimi CLI]
    O -->|openai-compatible| Q[LLMClient.invoke_vision<br/>OpenAI 兼容多模态 API]
    O -->|未配置 vision| R[记录警告<br/>降级为文本生成]
    P --> S[解析 stream-json<br/>提取 assistant content]
    Q --> T[解析 HTTP response<br/>提取 content]
    R --> H
    S --> U[_parse_json_or_none<br/>解析为 PrdGenerationResult]
    T --> U
    H --> U
    U --> V[返回 IntakePreview<br/>给前端]
    V --> W[前端展示 PRD / 待确认问题 / 人工下载提示]
```

## 关键分支说明

- **图片发现**：同时支持 HTML `<img src="...">` 和 Markdown `![alt](url)`。
- **TAPD 图片**：TAPD 图片需要登录，因此通过 `TapdApi` 下载后转为 base64 data URL。
- **公网图片**：为了避免 LLM 服务端无法访问内网/受限图片，默认下载后 inline 为 base64。
- **Kimi CLI**：`kimi-for-coding` 不能直接通过 OpenAI API 调用，所以走本地 `kimi -p ... --output-format stream-json`。
- **降级策略**：若来源包含图片但未配置 vision，则记录 warning 并继续用纯文本主 LLM 生成。
