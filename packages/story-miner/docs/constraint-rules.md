# 约束库规则表（从 transcript 沉淀）

> 来源：story-miner 挖掘的 transcript 中真实用户指令（ucmd）里含「必须/禁止/不要/不能」等强制语气的片段。
> 本表把高频、可 grep 执行的约束沉淀为检查项，接入 `code-standards-check` skill。

## 统计

- 去重约束片段：**301**
- 聚类主题数：**8**
- 已沉淀为 grep 可执行规则：**8** 条

## 可执行约束规则

| 主题 | 严重级 | 规则 | 检查命令（grep/ripgrep） | 样例来源 |
|---|---|---|---|---|
| 分支/git | P0 | doc/设计稿/中间产物不要提交进 git | `rg -n '\.docx?&#124;\.tmp&#124;\.log&#124;/tmp/&#124;\.claude/tmp&#124;\.story/.*\.json&#124;\.pyc&#124;node_modules' --type java --type ts -g '!**/test/**'` | doc 不要提交 git；中间产物不要提交 |
| 配置/安全 | P0 | 禁止在测试分支/test 环境直接改生产配置或数据库 | `rg -n 'nacos&#124;application-.*\.yml&#124;update .* set&#124;delete from' --type java -g '!**/test/**' &#124; rg -i 'test&#124;staging&#124;dev'` | 不要在 test 分支直接改生产配置 |
| 代码质量 | P0 | 硬编码业务 ID / 实验 ID / token / 手机号必须清掉 | `rg -n 'experiment_id&#124;activity_id&#124;token&#124;phone&#124;mobile&#124;\b\d{10,}\b' --type java --type ts -g '!**/test/**'` | 禁止硬编码实验 ID、活动 ID、token |
| 代码质量 | P0 | TODO/FIXME/HACK/占位符常量不能进生产 | `rg -n 'TODO&#124;FIXME&#124;HACK&#124;_TEST_&#124;_PLACEHOLDER&#124;_DUMMY&#124;_TEMP&#124; System\.out&#124;printStackTrace&#124;console\.log' -g '!**/test/**'` | TODO/FIXME/占位符不能留到生产 |
| 文档/规范 | P0 | 对客可见的中文必须走 i18n（菲项对客只能是英文/菲语） | `rg -n '\p{Han}' --type java -g '!**/test/**' ; rg -n '\p{Han}' frontends/hc-admin/src` | 对客不要出现中文 |
| 代码质量 | P0 | 外部数据直接 Enum.valueOf 必须加防御 | `rg -n '\.valueOf\(' --type java -g '!**/test/**'` | 外部数据 valueOf 要有 try-catch/校验 |
| skill/流程 | P1 | 不要跳过 skill 流程 / MCP 链式调用约束 | `rg -n 'orchestrate&#124;mcp&#124;provider&#124;skill' --type java --type ts -g '!**/test/**' &#124; rg -i '跳过&#124;bypass&#124;直接调用'` | 不要跳过 skill 流程，不要直接调用内部 MCP |
| 数据库/SQL | P1 | SQL/数据操作必须有备份或回滚说明 | `rg -n 'delete from&#124;update .* set&#124;drop table&#124;truncate' --type sql --type java -g '!**/test/**'` | 删数据/改数据前确认可回滚 |

## 约束主题聚类（样本）

### 分支/git（32 条）

- 应该是saveBatch不支持fill吧 按A改，然后/deploy-test
- 你等等，hc-user应该也写了，看看在不在别的分支上
- 协议的分支，应该是 feature/1064993
- 稍等，我理解你是不是看错了，你应该拿源分支 和 master分支 ... 比较
- 不对啊，hc-admim,应该在7天免息的分支上改
- hc_user 应该用...和master比较
- FastMCP 版本不支持 tags 参数 ,那不能升级版本吗
- 改动：plan_stage_node 只做状态标记（如设置 status = "skipping"），绝不调用其他节点函数

### 部署/上线（3 条）

- 不要，帮我确认一下，目前的代码，如果上线了，会影响老包行为吗
- - 部署：aiops-mcp 的 Dockerfile 由平台托管、不可改
- 部署好了，看看odps能不能调通

### 数据库/SQL（23 条）

- 你的刷数据不对啊，发起方2718462 ，他的状态就是删除，不应该恢复啊
- 把hc-admin的datalock删了，不要提交上去
- 直接帮你写 ToolGrant 授予的 SQL/接口调用，看看能不能调通
- 所有状态变更（包括 UI 触发的 skip/new/fail）必须通过调用 LangGraph 的 graph.update_state() 来完成，而不是直接写 DB
- LLM 极易产生过度泛化的毒化规则（如“永远不要改路由”），必须人工审核 + 严格限定 applies_to 作用域才能入库
- 我要用新的需求来测试全流程，目前的埋点数据/日志够吗，出现问题能不能快速定位
- 修正：应将 Open Decisions 1 改为“决定：P0 必须由脚本 checkout”，并删除 0.5.0 中的“不要求真实 checkout”
- 澄清：必须在“解析成功后、删除源文件前”进行复制

### skill/流程（38 条）

- 这种情况应该怎么解决，别人是怎么做的，我理解应该有类似的skill
- 你现在没有 MCP codegraph 工具可用，只能用 Grep、Glob、Read、Bash
- 那我是不是应该做一个skill，来增强mcp的深度
- D:\java-agent 和 D:\hc-all 这俩都是公司项目，发布，查日志等，都是一套，应该弄到公司级skill，只是项目id不同，每个项目的agents.md 应该维护项目id啥的
- 等等，按照规范，脚本应该跟着skill文件夹啊，而不是固定在同一个目录
- 不能写流程去看公司级吗，这样不用到处维护
- 看看别人是怎么干的，按skill规范，应该放在skill文件夹内部
- 我理解，不能把逻辑放在mcp，再拓展的话，非常困难

### 配置/安全（14 条）

- hc-config, 上传文件应该有问题，好像没有oss配置
- 4000 token够吗，先不要限制token
- 那我应该如何设计，能用一行cli 跑SWE-bench
- 从 agent_no_patch 到 review_false_positive 再到 state_machine_stuck，这套分类法把 SWE-bench 从一个“只能看 pass rate 的黑盒”变成了“诊断 Agen
- 传统的 Benchmark 只给一个 Resolve Rate (0.0 ~ 1.0)，这是标量，只能告诉你好不好，不能告诉你怎么变好
- hc-all 里的邮件审批状态机，绝对不应该污染 SWE-bench 里对 Django 问题的判断
- SWE-bench 里“无脑重试跑测试”的策略，也绝对不能直接用于生产 hotfix
- 亮点：之前的 Dependency Graph 只能解决“先后顺序”，解决不了“同时修改同一个文件”的逻辑冲突

### 代码质量（47 条）

- - 你经常在指令中写"必须"、"禁止"、"不要"
- 把左边图标的浮层往下放，不要遮住柱子头部的柱子
- 把左边图标的浮层往下放，不要遮住柱子头部的文字
- 把hc-admin的datalock删了，不要提交上去
- acl的设计，要不要参考下java代码
- 在101服务器上，用claude code，用goal开始开发剩余的issue，每做完一个，再做下一个， 不要堵塞
- 建议定义两个概念：terminal 和 successful，不要混用“完成/结
- 4000 token够吗，先不要限制token

### 文档/规范（19 条）

- 上网查一查，别人是怎么干的，有没有类似的开源项目，这种data-map，如何做比较合适，文档结构应该怎么组织
- 等等，按照规范，脚本应该跟着skill文件夹啊，而不是固定在同一个目录
- 看看别人是怎么干的，按skill规范，应该放在skill文件夹内部
- 应该有个自动注册功能的，看一下D:\java-agent\ys-agent的session文档
- 1 读一下D:\java-agent\ys-agent\docs\generic-risk-stratification  这个skill 看看能不能跑通 2 创建新的skill，将opds连接，切换到{
- 3 的话，我目前已经改了呀，doctor，不能帮用户一键安装吗
- doc不要提交到git
- 建议在文档中明确区分（如称Plan Loop为In-node Loop，Code Loop为Cross-node Iterative Retry），避免后续维护者误在review_stage_node内写while循环

### 任务管理（29 条）

- 看一下这个需求，有个bug, 清分不对，999****这个用户，还了3800，超还应该是100.
- 有bug，新建的时候，不能选类型
- 有bug，7天免息活动保存时，没有把发起用户只能为新客保存
- feature/zzh/7days_free_interest_0504 还有个bug，cid:**** 7天免息资格校验的接口返回true，但是提交借款的借款拦截了，用户应该没有资格参与，测试环境看一下
- 另外第一步创建story，应该就让llm问/或者找文件路径的
- 如果 story 已存在且 stage 有 .done，n 应该报错并提示使用 r，还是直接降级为 r 的行为
- 建议 n 仅负责“从零创建”，若检测到 story 已存在，直接拦截并提示，不要让它隐式退化
- 2. .story-done 是否应该作为最高优先级状态信号

## 使用方式

1. 在 `code-standards-check` skill 的「速查」区按主题加入上表命令。
2. 提交/合并/上线前对本次变更文件跑一遍对应主题的命令，逐条核对上下文。
3. 命中结果按 `category | severity | file:line | 问题 | 建议` 记录。