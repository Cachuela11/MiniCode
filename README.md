# MiniCode

MiniCode 是一个最小可运行的 coding agent 框架。当前阶段的目标不是一次性做完整智能体，而是先把核心 runtime 跑通：

- LLM：DeepSeek API
- Sandbox：Docker CLI
- Agent loop：模型返回 JSON action，系统执行 tool，再把 observation 回传给模型
- 可观测性：结构化 run log、token、耗时、权限决策、文件修改记录
- Eval：内置一组简单任务，用来衡量后续 Skill、Memory、自我进化是否真的有效

后续还会继续扩展 context、harness、memory、skills 和 self-evolution。

## 运行要求

- Python 3.11+
- Docker
- DeepSeek API Key

## 快速开始

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
python -m pip install -e .
python -m minicode --check
python -m minicode "inspect the workspace and create a hello.txt file"
```

安装为 editable package 后，也可以直接运行：

```powershell
minicode "inspect the workspace and create a hello.txt file"
```

## Agent Loop

```mermaid
flowchart TD
    A[User query] --> B[Build prompt with context and tool descriptions]
    B --> C[LLM returns JSON action]
    C --> D{Decision}
    D -->|tool call| E[Execute tool]
    E --> F[Record step log]
    F --> G[Send observation back to model]
    G --> C
    D -->|finished| H[Run final test]
    H --> I[Write run log and print answer]
    D -->|max steps| I
```

```mermaid
flowchart TD
    A[ToolRegistry.execute] --> B{tool name}
    B -->|list_files / read_file / write_file| C[Validate workspace path]
    C --> D[Read or write host workspace directly]
    D --> Z[Return ToolResult]
    B -->|future API tools| E[Validate API policy and parameters]
    E --> F[Call external API directly]
    F --> Z
    B -->|run_tests / run_shell| G[DockerSandbox.run]
    G --> H[CommandPolicy.check]
    H -->|deny| I[Return blocked result]
    H -->|ask| J{approval mode}
    J -->|never or rejected| I
    J -->|ask accepted or always| K[Run command in Docker /workspace]
    H -->|allow| K
    K --> L[Capture stdout, stderr, exit code, duration]
    L --> Z
```

第一张图是 agent 主循环：用户任务进入后，MiniCode 构建 prompt，DeepSeek 返回一个 JSON action，系统执行对应 tool，并把 observation 再发回模型，直到模型返回 `finish` 或达到最大步数。

第二张图是 tool 执行路径：不是所有 tool 都走 Docker。文件类 tool 直接在本地 workspace 内执行路径校验和读写；`run_shell` / `run_tests` 才会进入 Docker sandbox；未来 API 类 tool 会走自己的 API 参数校验和权限策略。

## 项目文件架构

```text
MiniCode/
  pyproject.toml
  README.md
  .env.example
  .gitignore
  .skills/
    python_test_repair.md
    add_api.md
    refactor_function.md
    input_validation.md
    code_review.md
  minicode/
    __main__.py
    __init__.py
    cli.py
    agent.py
    llm.py
    tools.py
    sandbox.py
    permissions.py
    context.py
    observability.py
    eval.py
    harness.py
    memory.py
    dreaming.py
    retrieval/
      __init__.py
      schema.py
      ranking.py
      skill.py
      memory.py
    skills/
      __init__.py
      schema.py
      loader.py
      catalog.py
      router.py
      prompt.py
    evolution.py
```

- `pyproject.toml`：Python 项目配置，定义包名、版本、Python 版本要求和 `minicode` 命令行入口。
- `.env.example`：环境变量示例，包含 DeepSeek、Docker、日志和 eval 相关配置。
- `.skills/`：Skill 内容库。这里的 Markdown 文件是给模型看的任务工作流说明，不是 Python 执行代码。
- `.gitignore`：忽略 `.minicode/`、Python 缓存和安装元数据。`.minicode/` 是本机持久运行数据目录，但默认不提交到 Git。
- `minicode/__main__.py`：支持 `python -m minicode` 的入口文件，只负责转发到 CLI。
- `minicode/cli.py`：命令行入口，解析参数，创建 `DeepSeekClient`、`DockerSandbox`、`CodingAgent`，并处理 `--check`、`--eval`、`--run-log` 等模式。
- `minicode/agent.py`：agent 主循环。它负责构建 prompt、调用模型、解析 JSON action、执行 tool、记录 step log，并在结束时运行可选 final test。
- `minicode/llm.py`：DeepSeek API client。调用 OpenAI-compatible `/chat/completions`，返回模型内容、token 用量和耗时。
- `minicode/tools.py`：Tool runtime。注册并执行当前支持的 tools，统一返回 `ToolResult`。
- `minicode/sandbox.py`：Docker sandbox。负责把命令放进 Docker 的 `/workspace` 中执行，并收集 stdout、stderr、exit code、耗时和权限信息。
- `minicode/permissions.py`：命令权限策略。对危险命令做 `allow`、`ask`、`deny` 判断，并支持 `never`、`ask`、`always` 三种审批模式。
- `minicode/context.py`：多层上下文构建与压缩。负责 L0-L3 层说明、初始文件索引、大 observation 外置、占位预览、结构化 notes 和历史超限压缩。
- `minicode/observability.py`：结构化日志模型。记录每一步的模型输入摘要、action、tool 参数、权限决策、输出、修改文件、token、耗时和 retrieval trace。
- `minicode/eval.py`：内置 eval 任务集和指标汇总。用于衡量任务成功率、测试通过率、tool 调用次数、危险命令等。
- `minicode/harness.py`：后续 harness 占位。未来用于自动判断项目类型、运行验证命令和驱动修复循环。
- `minicode/memory.py`：文件型 memory store。管理 `.minicode/memory` 下的 Markdown/Text 记忆，支持检索、写入、归档和索引更新。
- `minicode/dreaming.py`：离线 memory dreaming。负责判断触发条件、精确去重、调用 DeepSeek 合并摘要，并把旧长期记忆归档。
- `minicode/retrieval/`：统一检索基础层。提供 `RetrievalCandidate`、`RetrievalResult`、`RetrievalTrace` 等共享结构；`search_skills` 和 `search_memory` 的 action 仍然分开，但 run log 使用统一 trace 记录检索过程。
- `minicode/skills/schema.py`：定义 `Skill`、`SelectedSkill`、`SkillRoute` 等数据结构。
- `minicode/skills/loader.py`：读取 `.skills/*.md`，解析 frontmatter 和正文。
- `minicode/skills/catalog.py`：管理已加载的 skill，提供按名称查询和枚举能力。
- `minicode/skills/router.py`：Skill 路由总控。先做元信息粗召回，再交给 DeepSeek 精排候选 skill。
- `minicode/skills/prompt.py`：把选中的 skill 渲染成 prompt 文本，注入给模型。
- `minicode/evolution.py`：记忆沉淀触发器。当前实现“session 摘要 -> 规则信号初筛 -> DeepSeek 长期记忆分类 -> 写入 memory”。

## Skill 体系

Skill 是给模型看的工作手册，不是可执行函数。Tool 负责真正执行动作，Skill 负责指导模型按什么步骤使用 tools。

```mermaid
flowchart TD
    A[User task] --> B[Load .skills/*.md]
    B --> C[Stage 1: metadata recall topK<br/>triggers +5<br/>intents +3<br/>tags +2<br/>name +1<br/>description +1]
    C --> D[Stage 2: DeepSeek rerank topN]
    D --> E{Selected skills?}
    E -->|yes| F[Inject selected skill docs]
    E -->|no| G[Inject tool list only]
    F --> H[Build agent prompt]
    G --> H
    D --> I[Record skill_route in run log]
    H --> J[Start agent loop]
```

当前实现两阶段路由：

- MiniCode 本地读取 `.skills/*.md`，解析 frontmatter 和正文。
- 第一阶段 `MetadataSkillRetriever` 用任务文本匹配 `triggers`、`tags`、`intents`、skill 名称和 description，粗召回 topK。
- 第二阶段 `LlmSkillRanker` 把候选 skill 的压缩元信息交给 DeepSeek 精排，选出最终注入 prompt 的 topN。
- 如果 LLM 精排失败，会退回本地规则精排并在 run log 的 `rerank_error` 中记录原因。
- 默认最多注入 `2` 个 skill，可用 `--max-skills` 调整。
- 默认粗召回 `8` 个候选，可用 `--skill-recall-k` 调整。
- 没有命中 skill 时，仍然会注入完整 tool 列表，模型依然能调用 tools。
- run log 会记录 `skill_route.recalled`、`skill_route.selected`、`reranker` 和精排 token 用量，方便后续 eval 对比 skill 是否有效。

**统一 Retriever**

当前没有合并模型可见的检索 action，模型仍然分别调用 `search_skills` 和 `search_memory`。底层新增 `minicode/retrieval/` 作为共享检索基础层，统一候选、结果、阶段和 trace 的数据结构。

```text
search_skills -> SkillToolRetriever  -> RetrievalTrace(kind=skill)
search_memory -> MemoryToolRetriever -> RetrievalTrace(kind=memory)
```

- `schema.py`：定义 `RetrievalCandidate`、`RetrievalResult`、`RetrievalStage`、`RetrievalTrace`。
- `skill.py`：包装现有 skill 元信息召回，不做 memory 降权。
- `memory.py`：包装现有 memory 检索，后续动态权重、衰减、usage feedback 和 diversity rerank 会在这里实现。
- `ranking.py`：放通用文本规范化和 reason 处理。
- 每次 `search_skills` / `search_memory` 的结果都会写入 step log 的 `retrieval_trace`，方便后续 eval 对比检索质量。

## 多层 Context

当前 context 设计为 L0-L3。前三层在第一次调用模型前构造，L3 在 agent loop 中持续更新。

Agent loop 中的 context 工作流程：

```mermaid
flowchart TD
    A[User task] --> B[Route initial skills]
    B --> C[Build first messages<br/>L0 + L1 + L2]
    C --> D[Before model call:<br/>compact messages if over budget]
    D --> E[LLM returns JSON action]
    E --> F{Action type}
    F -->|finish| G[Write run log<br/>including context state]
    F -->|tool call| H[Execute tool]
    H --> I[Record internal step note]
    I --> J{Observation large?}
    J -->|no| K[Inline observation]
    J -->|yes| L[Write artifact<br/>return placeholder + preview]
    K --> M[Append action + observation to messages]
    L --> M
    M --> D
```

L0-L3 分层架构：

```mermaid
flowchart TB
    L0["L0 Runtime Contract<br/>system role · JSON action protocol · tool list · context policy"]
    L1["L1 Workspace File Index<br/>Docker pwd · bounded file paths · no file content"]
    L2["L2 Initial Skills<br/>two-stage router result · selected skill docs · before first action"]
    L3["L3 Dynamic Working Memory<br/>action JSON · observation · inline small output · artifact placeholder · structured notes"]

    L0 ~~~ L1
    L1 ~~~ L2
    L2 ~~~ L3

    classDef layer fill:#f8fafc,stroke:#cbd5e1,color:#0f172a,stroke-width:1px
    class L0,L1,L2,L3 layer
```

层级说明：

- `L0 runtime contract`：固定系统规则、JSON action 协议、tool 列表和 context 使用策略。
- `L1 workspace file index`：运行开始时在 Docker `/workspace` 里读取当前工作目录和最多 200 个文件路径，不包含文件内容。
- `L2 initial skills`：运行开始前通过两阶段 skill router 选择少量 skill 注入 prompt。
- `L3 dynamic working memory`：agent loop 中持续更新的动态上下文，只包含完整 action JSON 和 observation。`read_file`、`run_tests`、`load_skill`、`load_memory`、`read_context_artifact` 等 tool 的返回内容都只是 observation 的不同来源。

当前策略：

- tool 执行后先处理本次 observation：小结果直接 inline，大结果写入 artifact 后用占位符和预览替换。
- 模型调用前检查整段 messages：如果超过 `MINICODE_CONTEXT_HISTORY_CHAR_LIMIT`，早期 action / observation 会脱离成 structured notes。
- artifact 占位符不是单独的 context 类型，它是大 observation 被外置后留在 observation 里的引用。notes 也不是单独的 context 类型，它是旧 action / observation 被移出 prompt 后的摘要。
- 大 observation 的原文会保存在 `.minicode/context-artifacts`，后续可通过 `read_context_artifact` 按行读回。
- 小 observation 当前不会额外写 artifact；如果后续历史超限，它会从 prompt 原文中脱离，只在 notes 中保留摘要。
- memory 默认来自 `.minicode/memory` 下的 `.md` / `.txt` 文件；任务结束后会自动沉淀 `session_memory`，命中规则后再沉淀长期记忆。
- run log 的 `context` 字段会记录 context layers、artifact、notes、compaction 事件。

## 记忆触发闭环

当前实现包含两段：在线记忆沉淀和第一版离线 dreaming。记忆沉淀发生在一次 agent run 结束之后，不在每个 step 后触发；dreaming 在沉淀之后做阈值检查，命中后才整理已有 memory。

```mermaid
flowchart TD
    A[Agent run finished] --> B[Build session summary<br/>and source trace]
    B --> C[Write session_memory<br/>with trace metadata]
    C --> D[Regex prefilter on session summary]
    D --> E{Hit long-term signals?}
    E -->|no| F[Stop after session memory]
    E -->|yes| G[DeepSeek classify into<br/>project/procedural/experience]
    G --> H{Long-term candidates?}
    H -->|yes| I[Write long-term memory]
    H -->|no| J[Skip long-term write]
    F --> K[Update index.json]
    I --> K
    J --> K
    K --> L[Write run_log.memory_evolution]
    L --> M[Check dreaming triggers]
    M -->|hit| N[Deduplicate / consolidate memories]
    M -->|miss| O[Skip dreaming]
    N --> P[Write run_log.memory_dreaming]
    O --> P
```

四类记忆：

- `session_memory`：session 层记忆。原始 run 摘要记录任务、结果、关键文件、工具使用和测试状态；dreaming 后也可以生成 `subtype=session_summary` 的压缩 session 摘要。
- `project_memory`：项目事实、架构约定、文件组织、设计决策。
- `procedural_memory`：可复用的修复流程、测试流程、工具使用经验。
- `experience_memory`：明确表达过的协作经验和稳定工作偏好。

当前执行逻辑：

- run 结束后，`SelfEvolution` 先生成一条本地 `session_memory` 摘要。
- `session_memory` 会先写入 `.minicode/memory/sessions/`，保证每次 run 都有可检索的情景记录。
- 然后 MiniCode 只对这条 session 摘要做正则初筛，判断是否出现长期沉淀信号。
- 规则信号来自 session 摘要里的任务、最终答案、修改文件、tool 使用、测试结果、危险/无效命令等。
- 如果没有命中长期信号，本次记忆沉淀到 session memory 为止，不调用 DeepSeek 做长期分类。
- 如果命中长期信号，DeepSeek 只负责把 session 摘要精判并分类为 `project_memory`、`procedural_memory`、`experience_memory` 三类候选。
- 在线沉淀阶段的原始 `session_memory` 始终由本地摘要生成；dreaming 阶段可以生成 `subtype=session_summary` 的压缩版 session memory。
- 如果 DeepSeek 长期分类失败，已经写入的 `session_memory` 会保留，不会让主任务失败。
- 记忆写入结果会记录到 run log 的 `memory_evolution` 字段。
- 每条新 memory 会写入 `source_run_id`、`source_trace_ids`、`source_step_ids`、`source_tool_names`、`source_modified_files` 等来源字段。
- dreaming 生成的 `session_summary` 和长期记忆都会通过 `parent_memory_ids` 指向来源 session，方便回查证据链。

存储结构：

```text
.minicode/memory/
  project/
  procedural/
  experience/
  sessions/
  _archive/
  index.json
  dreaming-state.json
```

写入规则：

- `MINICODE_MEMORY_TRIGGER=on` 是默认模式，会写入 `session_memory`，命中规则后写入长期记忆。
- `MINICODE_MEMORY_TRIGGER=off` 会关闭记忆沉淀。
- 四类记忆都直接写入对应目录，并参与 `search_memory`。
- `session_memory` 始终写入 `sessions/` 目录，并参与 `search_memory`。
- 原始 `session_memory` 当前检索分数乘以 `0.6`，`subtype=session_summary` 乘以 `0.75`，避免 session 层压过长期记忆。
- `index.json` 是记忆目录和元数据索引，记录 `id`、`type`、`subtype`、`title`、`tags`、`path`、`source_run_id`、`source_trace_ids`、`parent_memory_ids`。当前检索仍直接扫描 active Markdown/Text 文件，`index.json` 主要服务人工查看、后续 context memory index 和 dreaming 批处理。

## Dreaming

**四种触发时机**

- 精确重复触发：当前层 eligible memory 中出现完全重复内容。
- 数量阈值触发：当前层 eligible memory 数量达到阈值。
- Token 阈值触发：当前层 eligible memory 估算 token 总量达到阈值。
- 时间间隔触发：已有上次 dreaming 记录，超过配置时间间隔，且当前层存在 eligible memory。

`session_memory` 层只处理超过热窗口的原始 session，数量阈值使用 `MINICODE_DREAM_SESSION_THRESHOLD`，token 阈值使用 `MINICODE_DREAM_SESSION_TOKEN_THRESHOLD`。`project_memory`、`procedural_memory`、`experience_memory` 层使用 `MINICODE_DREAM_MEMORY_THRESHOLD` 和 `MINICODE_DREAM_MEMORY_TOKEN_THRESHOLD`。

**四层 Dreaming 流转**

```mermaid
flowchart TD
    A[Dreaming start] --> B[Load active memories]
    B --> C[Read dreaming-state.json]
    C --> D[Build eligible items by layer]

    D --> E1[session_memory<br/>only raw sessions older than hot window]
    D --> E2[project_memory]
    D --> E3[procedural_memory]
    D --> E4[experience_memory]

    E1 --> F[Check four triggers per layer]
    E2 --> F
    E3 --> F
    E4 --> F

    F --> G{Any layer triggered?}
    G -->|no| H[Skip dreaming]
    G -->|yes| K[Archive exact duplicates in eligible scope]
    K --> I[Build layers_to_process]
    I --> J[Run triggered layers serially<br/>session -> project -> procedural -> experience]
    J --> L[Select current layer batch]
    L --> M[DeepSeek layer dreaming]
    M --> N[Write same-layer consolidation]
    N --> O{Promote to next layer?}
    O -->|yes| P[Write next-layer memory]
    O -->|no| Q[No promotion]
    P --> R[Archive superseded source memories]
    Q --> R
    R --> S{More triggered layers?}
    S -->|yes| L
    S -->|no| T[Update index.json and dreaming-state.json]
    T --> U[Write run_log.memory_dreaming]
```

- 触发方式：手动 `python -m minicode --dream` 强制执行；自动模式下在 run 结束后检查阈值。
- 自动触发规则按层计算，四层分别是 `session_memory -> project_memory -> procedural_memory -> experience_memory`。
- 热窗口规则：默认近 `2` 天的原始 `session_memory` 不参与 dreaming、不去重、不归档，继续完整参与检索。
- 处理内容：先本地归档可处理范围内的精确重复 memory；再按层把命中的 batch 交给 DeepSeek 做本层合并、摘要，并判断是否需要向上一层写入本次 dreaming 结果。
- 写入策略：`session_memory` 层可以写入 `subtype=session_summary`，并可判断是否上升为 `project_memory`；`project_memory` 可上升为 `procedural_memory`；`procedural_memory` 可上升为 `experience_memory`；`experience_memory` 是当前最高层，只做本层合并。
- 归档策略：原始旧 session 只有在对应 `session_summary` 成功写入后才归档；被 LLM 合并且完全被新长期记忆替代的旧长期 memory 也会归档。普通检索和 `index.json` 不读取 `_archive/`。
- 状态文件：`dreaming-state.json` 记录上次 dreaming 时间、已处理 session id 和已处理 memory id。

## 当前支持的 Tools

模型每轮必须返回一个 JSON object，例如：

```json
{"thought":"short reasoning","action":"list_files","args":{"path":".","max_depth":2}}
```

或者结束任务：

```json
{"thought":"done","action":"finish","args":{"answer":"summary for the user"}}
```

当前 tools：

- `list_files`
  - 参数：`path`、`max_depth`、`limit`
  - 作用：列出 workspace 内文件。
  - 执行位置：本地 host workspace。
  - 安全策略：会校验路径不能逃出 workspace。

- `read_file`
  - 参数：`path`、`start_line`、`limit`
  - 作用：读取文件的指定行范围。
  - 执行位置：本地 host workspace。
  - 安全策略：会校验路径不能逃出 workspace。

- `write_file`
  - 参数：`path`、`content`、`overwrite`
  - 作用：写入文件。默认不覆盖已有文件，除非 `overwrite=true`。
  - 执行位置：本地 host workspace。
  - 安全策略：会校验路径不能逃出 workspace。

- `run_tests`
  - 参数：`command`
  - 默认命令：`python -m pytest`
  - 作用：在 Docker sandbox 中运行测试命令。
  - 执行位置：Docker `/workspace`。
  - 安全策略：先经过 `CommandPolicy`，再决定是否执行。

- `read_context_artifact`
  - 参数：`artifact_id`、`start_line`、`limit`
  - 作用：按行读取被外置化的大型 tool observation。
  - 执行位置：本地 context artifact 存储。
  - 安全策略：只能读取本次运行中由 MiniCode 生成的 artifact id，不能传任意文件路径。

- `search_skills`
  - 参数：`query`、`limit`
  - 作用：在 `.skills/*.md` 中按元信息粗召回相关 skill。
  - 执行位置：本地 skill catalog。
  - 使用方式：先搜索候选，再用 `load_skill` 加载完整 workflow。

- `load_skill`
  - 参数：`name`、`max_chars`
  - 作用：把指定 skill 的完整说明作为 observation 注入后续上下文。
  - 执行位置：本地 skill catalog。

- `search_memory`
  - 参数：`query`、`limit`
  - 作用：在 `.minicode/memory` 的长期记忆和 `session_memory` 中搜索相关项目经验。
  - 执行位置：本地 memory store。
  - 使用方式：先搜索候选，再用 `load_memory` 加载完整记忆。

- `load_memory`
  - 参数：`memory_id`、`max_chars`
  - 作用：把指定 memory 作为 observation 注入后续上下文。
  - 执行位置：本地 memory store。

- `run_shell`
  - 参数：`command`
  - 作用：兜底 shell tool，用于结构化 tool 不够用的情况。
  - 执行位置：Docker `/workspace`。
  - 安全策略：先经过 `CommandPolicy`，危险命令会被拒绝或要求审批。

- `finish`
  - 参数：`answer`
  - 作用：结束 agent loop，返回最终答案。
  - 执行位置：不执行外部操作。

## 配置

环境变量：

- `MINICODE_MODEL`：DeepSeek 模型名，默认 `deepseek-v4-flash`
- `DEEPSEEK_API_KEY`：DeepSeek API Key
- `MINICODE_DEEPSEEK_URL`：DeepSeek API base URL，默认 `https://api.deepseek.com`
- `MINICODE_LLM_TIMEOUT`：单次 LLM 响应超时时间，默认 `120`
- `MINICODE_MAX_TOKENS`：API provider 的最大输出 token，默认 `4096`
- `MINICODE_WORKSPACE`：挂载到 Docker 的 workspace，默认当前目录
- `MINICODE_DOCKER_IMAGE`：sandbox 镜像，默认 `python:3.12-slim`
- `MINICODE_MAX_STEPS`：agent 最大循环步数，默认 `8`
- `MINICODE_APPROVAL`：风险命令审批模式，默认 `never`
- `MINICODE_RUN_LOG`：结构化运行日志输出目录或文件路径，默认 `.minicode/runs`
- `MINICODE_FINAL_TEST_COMMAND`：agent 结束后运行的最终测试命令
- `MINICODE_EVAL_OUTPUT`：eval 报告输出路径，默认 `.minicode/eval-report.json`
- `MINICODE_SKILLS_DIR`：Skill Markdown 目录，默认 `.skills`
- `MINICODE_MAX_SKILLS`：每次最多注入的 skill 数量，默认 `2`
- `MINICODE_SKILL_RECALL_K`：精排前粗召回的 skill 候选数量，默认 `8`
- `MINICODE_CONTEXT_ARTIFACT_DIR`：大 observation 外置存储目录，默认 `.minicode/context-artifacts`
- `MINICODE_OBSERVATION_INLINE_LIMIT`：tool observation 小于等于该字符数时直接进入上下文，默认 `6000`
- `MINICODE_OBSERVATION_PREVIEW_CHARS`：大 observation 外置后保留在 prompt 中的预览字符数，默认 `1200`
- `MINICODE_CONTEXT_HISTORY_CHAR_LIMIT`：消息历史超过该字符数后触发脱离压缩，默认 `24000`
- `MINICODE_CONTEXT_KEEP_RECENT_MESSAGES`：历史脱离时保留最近消息数，默认 `6`
- `MINICODE_CONTEXT_NOTE_CHAR_LIMIT`：结构化 notes 摘要最大字符数，默认 `6000`
- `MINICODE_MEMORY_DIR`：本地 memory Markdown/Text 目录，默认 `.minicode/memory`
- `MINICODE_MEMORY_TRIGGER`：记忆沉淀模式，`off` / `on`，默认 `on`
- `MINICODE_MEMORY_MIN_CONFIDENCE`：候选记忆最低置信度，默认 `0.7`
- `MINICODE_MEMORY_MAX_CANDIDATES`：每次反思最多生成的候选记忆数，默认 `5`
- `MINICODE_DREAMING`：run 结束后的 dreaming 模式，`off` / `auto`，默认 `auto`
- `MINICODE_DREAM_SESSION_THRESHOLD`：新增多少条 `session_memory` 后自动触发 dreaming，默认 `8`
- `MINICODE_DREAM_SESSION_TOKEN_THRESHOLD`：超过热窗口的原始 session memory 估算 token 总量达到多少后触发，默认 `12000`
- `MINICODE_DREAM_MEMORY_THRESHOLD`：单个长期 memory 层新增多少条 active memory 后自动触发 dreaming，默认 `40`
- `MINICODE_DREAM_MEMORY_TOKEN_THRESHOLD`：单个长期 memory 层估算 token 总量达到多少后触发，默认 `12000`
- `MINICODE_DREAM_INTERVAL_HOURS`：距离上次 dreaming 超过多少小时且某层存在 eligible memory 时触发，默认 `24`
- `MINICODE_DREAM_MAX_BATCH_SIZE`：单次 dreaming 最多处理多少条 memory，默认 `20`
- `MINICODE_DREAM_MIN_CONFIDENCE`：dreaming 写入长期记忆的最低置信度，默认 `0.75`
- `MINICODE_DREAM_SESSION_HOT_DAYS`：原始 session memory 保持完整 active 的天数，默认 `2`

示例：

```powershell
$env:MINICODE_MODEL = "deepseek-v4-pro"
python -m minicode "list files"
```

手动执行一次 memory dreaming：

```powershell
python -m minicode --dream
```

审批模式：

- `never`：需要审批的命令直接阻止
- `ask`：在控制台询问是否允许
- `always`：自动允许需要审批的命令

示例：

```powershell
python -m minicode --approval ask "run tests and fix failures"
```

## 运行日志

MiniCode 默认会为每次运行写一个结构化 JSON 日志。日志是本地持久数据，不会因为下一次运行而覆盖。

```powershell
python -m minicode --final-test-command "python -m unittest discover -s tests" "fix the failing test"
```

默认日志目录：

```text
.minicode/runs/
```

日志文件名会包含时间戳和任务摘要，例如：

```text
.minicode/runs/20260621-221530-fix-the-failing-test.json
```

也可以手动指定目录：

```powershell
python -m minicode --run-log .minicode/my-runs "list files"
```

如果手动指定固定文件名，MiniCode 也不会覆盖旧文件，而是自动追加编号：

```text
.minicode/run-log.json
.minicode/run-log-1.json
.minicode/run-log-2.json
```

每一步会记录：

- 模型输入摘要
- 模型生成的 action
- tool 名称和参数
- 权限决策
- stdout、stderr、exit code
- 修改文件
- token 消耗
- 运行耗时
- 是否出现危险或无效命令

如果设置了 `--final-test-command`，最终测试结果也会写入 run log。

## Eval

运行内置 eval：

```powershell
python -m minicode --eval --approval never
```

eval 会在 `.minicode/eval-runs` 下创建隔离 workspace，并统计：

- 任务成功率
- 测试通过率
- 平均 tool 调用次数
- 无效命令次数
- 修改文件数量
- token 用量
- 总耗时
- 危险命令次数

当前 eval 任务：

- 修复一个失败的单元测试
- 补充一个 API
- 修复类型错误
- 重构一个函数
- 增加输入校验
