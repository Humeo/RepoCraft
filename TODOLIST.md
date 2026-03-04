# RepoCraft v3 — Self-Proving Autonomous Code Maintainer

## What It Is

长时运行的自主代码库维护者。它不只是说"我修好了"，而是拿出证据证明——复现截图、测试报告、Before/After 对比。所有人机交互通过 GitHub Issues/PRs 进行。

**核心差异化**: Proof of Work（自证机制）。AI 经常自信地给出错误代码，人类要花大量时间验证。RepoCraft 自动采集证据链，人类 30 秒就能判断修复是否可信。

## Architecture

```
RepoCraft Daemon (宿主机, Python 长期进程)
├── Scheduler
│     ├── GitHub Poller (60s, ETag 缓存)
│     │     → 新 issue → triage activity
│     │     → PR review → respond_review activity
│     │     → @mention → task activity
│     ├── Patrol Timer (可配, 如 12h)
│     │     → patrol activity (限速: N issues / window)
│     └── Dispatcher
│           → per-repo asyncio.Lock (同 repo 串行)
│           → global asyncio.Semaphore (最多 M 个并发)
│
├── Store (SQLite WAL)
│     repos, activities, logs, processed_events, patrol_budget
│
└── Container Manager → Docker "repocraft-worker"
      ├── Python 3 + claude-agent-sdk    ← SDK 运行在容器内
      ├── Claude Code CLI (SDK 调用)
      ├── git, gh, node, uv
      ├── Playwright + Chromium (证据截图)
      ├── /repos/owner-repo/  (多 repo)
      └── 用户的 git identity (name + email)
```

### Claude Agent SDK 执行模型

SDK 运行在**容器内**，不在宿主机。流程：

```
宿主机 Dispatcher
  → docker exec repocraft-worker python3 /app/run_activity.py
  → (容器内) run_activity.py 使用 claude-agent-sdk:
      options = ClaudeAgentOptions(
          permission_mode="bypassPermissions",
          disallowed_tools=["AskUserQuestion", "EnterPlanMode"],
          max_turns=200,
          cwd=Path("/repos/owner-repo"),
      )
      async for msg in query(prompt=prompt, options=options):
          print(json.dumps(serialize(msg)), flush=True)  # 结构化输出到 stdout
  → 宿主机读 stdout，写入 SQLite logs
```

**为什么用 SDK 而不是 `claude -p`**:


|          | `claude -p` (当前)               | Agent SDK                                   |
| -------- | ------------------------------ | ------------------------------------------- |
| 工具禁用     | CLI flag `--disallowed-tools`  | `disallowed_tools=["AskUserQuestion"]` 原生参数 |
| 输出       | stream-json 字符串，手动解析 JSON      | 类型化对象：`ResultMessage`, `AssistantMessage`   |
| Shell 转义 | base64 编码 hack                 | Python 字符串，无需转义                             |
| 中断       | kill 进程                        | `client.interrupt()` 优雅中断                   |
| 安全拦截     | 无法从宿主端拦截                       | `PreToolUse` hook 拦截危险操作                    |
| 会话续接     | `--resume session_id` CLI flag | SDK API `resume`                            |
| 错误处理     | 解析 exit code                   | `ResultMessage.is_error` + 异常               |
| 成本跟踪     | 从 JSON 提取                      | `ResultMessage.total_cost_usd`              |


### 证据系统 (Proof of Work)

完全由 prompt 驱动，不需要自定义工具。Claude Code 自带 Bash/Read/Write/Grep 足够：

- **测试输出**: `Bash("pytest -v")` 捕获 stdout
- **错误日志**: `Bash("python app.py 2>&1")` 捕获 stderr
- **API 测试**: `Bash("curl -s localhost:8000/api/users | jq .")`
- **UI 截图**: `Bash("npx playwright screenshot ...")`
- **数据库状态**: `Bash("sqlite3 db.sqlite 'SELECT count(*) FROM users'")`
- **压力测试**: `Bash("wrk -t4 -c100 http://localhost:8000/api")`

CLAUDE.md 模板定义证据采集规范和输出格式。

### Git Identity

用户身份，不是 bot 身份。提交显示：

```
Author: Kolten Luca <kolten@email.com>
Co-Authored-By: Claude <noreply@anthropic.com>
```

通过环境变量 `GIT_USER_NAME` + `GIT_USER_EMAIL` 配置。

---

## Decisions


| 决策       | 选择                                    | 理由                            |
| -------- | ------------------------------------- | ----------------------------- |
| Agent 引擎 | Claude Agent SDK (容器内)                | 类型化输出、无 shell hack、hooks、优雅中断 |
| 隔离       | 单 Docker 容器, 多 repo                   | Pattern 2: OS 级隔离, 简单         |
| 权限       | `permission_mode="bypassPermissions"` | 容器内安全                         |
| 人机界面     | GitHub Issues/PRs                     | 唯一界面, 无 CLI chat, 无 Telegram  |
| 核心价值     | Proof of Work 证据链                     | prompt 驱动, CLAUDE.md 定义标准     |
| 巡检限速     | max N issues / T hours                | SQLite patrol_budget 表        |
| Git 身份   | 用户 name + email                       | 环境变量, Co-Authored-By Claude   |
| 并发       | asyncio.Semaphore + per-repo Lock     | 全局上限 + repo 串行                |


---

## Unknowns & Open Questions

### U1: SDK 在容器非 root 用户下是否正常工作？ [需验证]

当前容器用 `repocraft` 用户 (uid 1001)。`claude-agent-sdk` 内部调用 `claude` CLI，需验证 `permission_mode="bypassPermissions"` 在非 root 下的行为。
**验证方式**: 在容器内跑 `python3 -c "from claude_agent_sdk import query; ..."`

### U2: SDK 的 `query()` 输出是否可以 stream 到 stdout？ [需验证]

SDK 的 `async for msg in query()` 返回类型化对象。需要确认能否实时序列化到 stdout，而不是等全部完成。
**验证方式**: 写一个最小脚本测试 streaming

### U3: Playwright 截图在容器内非 root 用户下是否能用？ [需验证]

Chromium 在 Docker 里需要 `--no-sandbox` 等参数。非 root 可能有额外限制。
**验证方式**: 容器内 `npx playwright screenshot https://example.com test.png`

### U4: GitHub 图片上传到 Issue [需研究]

Claude 截图保存为本地文件。如何嵌入到 GitHub issue/PR？

- 方案 A: `gh` CLI 不直接支持图片上传，需要用 GitHub API
- 方案 B: 把截图 push 到 repo 分支，用相对路径引用
- 方案 C: 用 GitHub Release assets 或 Gist 做图床
**需要确定最佳方案**

### U5: 巡检质量——Claude 能发现真正的 bug 还是只产出噪音？ [需实验]

核心产品风险。如果 patrol 发现的 "bug" 大部分是误报，用户会关闭此功能。
**缓解**: 严格的 "确认后才提 issue" 规则 + 限速 + 先 reproduce 再报告

### U6: 多 repo 同容器的磁盘和内存压力 [需监控]

10+ repos 克隆到同一容器，加上 Node.js + Python + Playwright，内存可能不够。
**缓解**: 8GB 默认限制 + `git clone --depth=50` + 定期清理

### U7: `--resume` 在 SDK 中如何传递 session_id？ [需查 SDK 文档]

respond_review 需要续接之前的 fix_issue session。SDK 是否支持 resume？参数怎么传？

### U8: 容器内 claude-agent-sdk 的安装方式 [需确认]

容器里已有 `uv`。是否可以 `uv pip install --system claude-agent-sdk`？还是需要 venv？
需要确认 SDK 依赖是否和容器内现有 Python 环境兼容。

---

## File Structure (target)

```
src/repocraft/
├── __init__.py
├── cli.py                      # watch, unwatch, daemon, fix, status, logs
├── config.py                   # env vars, URL parsing, git identity, patrol config
├── store.py                    # SQLite: repos, activities, logs, events, budget
├── scheduler.py                # daemon loop: poller + patrol + dispatch
├── dispatcher.py               # docker exec → SDK runner → log streaming
├── container/
│   ├── __init__.py
│   ├── Dockerfile              # Ubuntu + git + gh + node + python + SDK + playwright
│   ├── manager.py              # Docker lifecycle, exec, exec_stream
│   ├── image_builder.py        # proxy helpers (keep)
│   └── scripts/
│       └── run_activity.py     # 容器内 SDK runner (接收 prompt, 输出结构化 JSON)
├── github/
│   ├── __init__.py
│   ├── issue_fetcher.py        # fetch issue details (keep)
│   └── poller.py               # event polling with ETag caching
└── templates/
    ├── __init__.py
    ├── user_claude_md.py        # CLAUDE.md: identity + proof-of-work protocol (核心)
    ├── init_prompt.py           # repo exploration prompt
    └── prompts.py               # patrol, fix, review, triage prompts with evidence rules
```

**13 个 Python 文件**。一个人一下午读完。

### 要删除的文件 (v1 死代码)

```
src/repocraft/agent/__init__.py
src/repocraft/agent/orchestrator.py
src/repocraft/agent/prompts.py
src/repocraft/tools/__init__.py
src/repocraft/tools/container_tools.py
src/repocraft/tools/evidence_tools.py
src/repocraft/evidence/__init__.py
src/repocraft/evidence/models.py
src/repocraft/evidence/collector.py
src/repocraft/evidence/reporter.py
```

---

## TODOLIST

### Phase 0: Clean Slate + Verify Unknowns

- 0.1 删除 v1 死代码 (10 个文件: agent/*, tools/*, evidence/*)
- 0.2 验证 U1: SDK 在容器非 root 下工作
  - 容器内 `pip install claude-agent-sdk`
  - 跑最小脚本 `query(prompt="echo hello")`
  - 确认 `bypassPermissions` 正常
- 0.3 验证 U2: SDK streaming 输出
  - 容器内跑 SDK，确认 `async for msg in query()` 可实时 flush 到 stdout
- 0.4 验证 U3: Playwright 截图
  - 容器内 `npx playwright install --with-deps chromium`
  - `npx playwright screenshot https://example.com test.png`
  - 确认非 root 用户下能工作
- 0.5 研究 U4: GitHub 图片上传最佳方案
  - 测试 `gh api` 上传图片
  - 确定最终方案
- 0.6 验证 U8: SDK 安装方式
  - `uv pip install --system claude-agent-sdk` 还是 venv？
  - 确认依赖兼容

### Phase 1: 容器 + SDK Runner + Identity

- 1.1 更新 Dockerfile
  - 添加 `python3-pip` 或用 `uv` 安装 SDK
  - 安装 `claude-agent-sdk`
  - 安装 `playwright` + chromium
  - `git config --global --add safe.directory '*'`
  - 保留 git identity 为运行时配置 (不在 build 时写死)
- 1.2 创建容器内 SDK runner: `container/scripts/run_activity.py`
  - 接收: prompt (stdin), max_turns (arg), cwd (arg)
  - 使用 SDK `query()` 执行
  - 每条 message 序列化为 JSON 写 stdout (flush)
  - 最终输出 `ResultMessage` 包含: result, is_error, total_cost_usd, num_turns, session_id
  - 格式:
    ```jsonl
    {"type": "assistant", "text": "..."}
    {"type": "tool_use", "name": "Bash", "input": {"command": "..."}}
    {"type": "tool_result", "output": "..."}
    {"type": "result", "result": "...", "is_error": false, "cost_usd": 1.23, "session_id": "abc"}
    ```
- 1.3 config.py 更新
  - 添加 `get_git_user_name()`, `get_git_user_email()`
  - 添加 `get_patrol_config()` → `PatrolConfig(max_issues=5, window_hours=12)`
  - 删除 `RepoCraftConfig` dataclass
- 1.4 container/manager.py 更新
  - `_configure_git_identity()`: 写用户 name + email 到容器 git config
  - `ensure_running()` 中调用 `_configure_git_identity()`
  - `_write_user_claude_md()` 路径确认为 `/home/repocraft/.claude/CLAUDE.md`
- 1.5 pyproject.toml 更新
  - 移除 `claude-agent-sdk` 宿主机依赖 (SDK 只在容器内)
  - 保留: docker, httpx, rich

### Phase 2: Proof of Work 模板 (产品灵魂)

- 2.1 重写 `templates/user_claude_md.py`
  - **Identity**: 自证式代码维护者, 所有声明必须有证据
  - **Proof of Work Protocol**:
    - Layer 1 — 复现: 运行应用/测试, 捕获失败证据 (日志、截图、数据状态)
    - Layer 2 — 修复验证: 同样步骤, 证明 bug 消失
    - 证据格式: markdown collapsible sections, before/after 表格
  - **Patrol Rules**: 限速感知 ("剩余 budget: N issues"), 优先级 (安全>崩溃>逻辑>质量)
  - **Non-Interactive**: 不调用 AskUserQuestion/EnterPlanMode, 自主决策
  - **GitHub Output**: issue/PR 用 markdown 格式化证据, 人类可读
  - **Git Discipline**: 用户身份提交, Co-Authored-By Claude
- 2.2 创建 `templates/prompts.py`
  - `patrol_prompt(repo_id, budget_remaining, last_areas)` — 巡检 prompt with 证据要求
  - `fix_issue_prompt(issue)` — 修复 prompt with before/after 证据模板
  - `review_pr_prompt(pr_number)` — PR review prompt with 结构化意见格式
  - `respond_review_prompt(pr_number)` — 回应 review prompt
  - `triage_prompt(issue)` — 分类 + 回复 prompt
  - 每个 prompt 都包含证据采集指令
- 2.3 更新 `templates/init_prompt.py`
  - 增加 "如何复现 bug" 章节 (dev server 启动方式, 测试命令, demo 数据)
  - 增加 "如何截图" (playwright 命令, 如果是 web 项目)

### Phase 3: Dispatcher 重写

- 3.1 重写 `dispatcher.py`
  - 改用 `docker exec python3 /app/run_activity.py` 而非 `docker exec claude -p`
  - Prompt 通过 stdin pipe 传入 (不再需要 base64)
  - 读取 stdout 的结构化 JSON lines
  - 提取 `ResultMessage` 中的: result, is_error, cost_usd, session_id
  - session_id 存入 activities 表 (用于 respond_review --resume)
- 3.2 活动感知超时
  - 每行输出重置计时器
  - 空闲超时: 10 分钟无输出 → kill
  - 硬超时: 2 小时 → kill
  - 两层防护
- 3.3 巡检预算注入
  - patrol activity 时, 在 prompt 末尾注入: "Budget remaining: N issues in this window"
  - 从 patrol_budget 表读取
- 3.4 会话续接
  - respond_review 时, 查找原 fix activity 的 session_id
  - 传给 SDK runner 作为 resume 参数
  - 失败则 fallback 新 session
- 3.5 日志批量写入
  - 攒 50 行或 2 秒后批量 commit 到 SQLite
  - 减少 I/O 压力

### Phase 4: Store 扩展

- 4.1 repos 表扩展
  ```sql
  ALTER TABLE repos ADD COLUMN watch INTEGER DEFAULT 0;
  ALTER TABLE repos ADD COLUMN last_etag TEXT;
  ALTER TABLE repos ADD COLUMN last_poll_at TEXT;
  ALTER TABLE repos ADD COLUMN patrol_interval_hours INTEGER DEFAULT 12;
  ```
- 4.2 新增 processed_events 表
  ```sql
  CREATE TABLE processed_events (
      repo_id TEXT NOT NULL,
      event_id TEXT NOT NULL,
      event_type TEXT,
      processed_at TEXT NOT NULL,
      UNIQUE(repo_id, event_id)
  );
  ```
- 4.3 新增 patrol_budget 表
  ```sql
  CREATE TABLE patrol_budget (
      repo_id TEXT PRIMARY KEY,
      window_start TEXT NOT NULL,
      issues_filed INTEGER DEFAULT 0,
      max_issues INTEGER DEFAULT 5,
      window_hours INTEGER DEFAULT 12
  );
  ```
- 4.4 批量日志写入方法
  ```python
  def add_logs_batch(self, activity_id: str, lines: list[str]) -> None:
  ```
- 4.5 schema migration 机制
  - 简单版: 启动时检测 column 是否存在, 不存在则 ALTER TABLE
  - 不需要 alembic, 表很少

### Phase 5: GitHub Poller + Scheduler

- 5.1 创建 `github/poller.py`
  - `EventPoller` class
  - `poll_events(owner, repo, last_etag, github_token)` → events + new_etag
  - ETag 缓存: `If-None-Match` header, 304 不消耗 rate limit
  - 检测事件类型: IssuesEvent(opened), PullRequestReviewEvent, IssueCommentEvent
  - @mention 检测: 扫描 comment body 中的 `@repocraft`
  - Rate limit 感知: 读取 `X-RateLimit-Remaining`, 低于阈值降速
- 5.2 创建 `scheduler.py`
  - `Scheduler.__init__(store, container_mgr, config)`
  - `async run()`: 三个并发循环:
    - `_poll_loop()`: 每 60s 检查 watched repos 的 GitHub 事件
    - `_patrol_loop()`: 按 patrol_interval 创建 patrol activity (检查 budget)
    - `_dispatch_loop()`: 每 5s 检查 pending activities, 分配执行
  - Per-repo `asyncio.Lock` (同 repo 串行)
  - Global `asyncio.Semaphore(max_concurrent)` (跨 repo 并发上限)
  - Graceful shutdown (SIGTERM/SIGINT)
  - 启动恢复: status=running 的 activities 标记为 failed (上次 crash)
- 5.3 CLI 重写
  - `repocraft watch <repo_url>` — clone + init + set watch=true
  - `repocraft unwatch <repo_url>` — set watch=false
  - `repocraft daemon` — 前台启动 scheduler (长期运行)
  - `repocraft fix <issue_url>` — 一次性: 创建 activity + dispatch + 等待
  - `repocraft status [target]` — 显示 repo/activity 状态
  - `repocraft logs <activity_id> [-f]` — 流式日志
  - 删除: submit, ask (改为通过 @mention 在 GitHub 上交互)

### Phase 6: End-to-End 验证

- 6.1 创建测试 repo
  - 简单 Python 项目 + 几个 known bugs
  - 有 tests, 有 CI
- 6.2 验证 patrol
  - `repocraft watch <test_repo>` + `repocraft daemon`
  - 等待 patrol 运行
  - 检查: issue 是否包含复现证据? 限速是否生效?
- 6.3 验证 fix_issue + evidence
  - 手动在 test_repo 创建 issue
  - 等待 triage + fix
  - 检查: PR 是否包含 Before/After 证据表?
- 6.4 验证 review_pr
  - 提一个 PR → daemon 检测 → Claude review
  - 检查: review comment 是否有结构化意见?
- 6.5 验证 respond_review
  - 在 Claude 的 PR 上留 review comment
  - daemon 检测 → Claude respond (用 --resume)
  - 检查: 是否正确续接上下文?
- 6.6 验证 @mention
  - 在 issue 中 @repocraft → daemon 检测 → 执行

---

## Tests

### Unit Tests (`tests/test_unit.py`)

不需要 Docker, 不需要网络, 不需要 API key。纯逻辑测试。

```python
# --- Config ---
def test_parse_issue_url():
    """标准 GitHub issue URL 解析"""
def test_parse_issue_url_invalid():
    """非法 URL 抛出 ValueError"""
def test_repo_id_from_url():
    """repo URL → slug (owner-repo)"""
def test_repo_id_from_url_with_git_suffix():
    """.git 后缀被去掉"""
def test_get_git_user_name_from_env(monkeypatch):
    """GIT_USER_NAME 环境变量"""
def test_get_git_user_name_default():
    """未设置时的默认值"""
def test_get_git_user_email_from_env(monkeypatch):
    """GIT_USER_EMAIL 环境变量"""
def test_get_anthropic_api_key_from_auth_token(monkeypatch):
    """ANTHROPIC_AUTH_TOKEN 作为 fallback"""
def test_get_anthropic_api_key_missing():
    """两个都没设置 → RuntimeError"""

# --- Store ---
def test_store_add_repo(tmp_path):
    """添加 repo 并读取"""
def test_store_add_repo_idempotent(tmp_path):
    """重复 add 不报错 (INSERT OR IGNORE)"""
def test_store_add_activity(tmp_path):
    """创建 activity 返回 UUID"""
def test_store_update_activity_status(tmp_path):
    """更新 status 和 summary"""
def test_store_get_pending_activities(tmp_path):
    """只返回 status=pending 的"""
def test_store_add_log(tmp_path):
    """写入日志行"""
def test_store_get_logs_ordered(tmp_path):
    """日志按 id 排序"""
def test_store_add_logs_batch(tmp_path):
    """批量写入日志"""
def test_store_patrol_budget_fresh(tmp_path):
    """新 repo 的 budget = max_issues"""
def test_store_patrol_budget_decrement(tmp_path):
    """每 file issue 后 budget 减 1"""
def test_store_patrol_budget_window_reset(tmp_path):
    """超过 window_hours 后 budget 重置"""
def test_store_processed_event_dedup(tmp_path):
    """重复 event_id 被忽略"""
def test_store_schema_migration(tmp_path):
    """旧 schema DB 能正常升级"""

# --- Poller ---
def test_poller_parse_issues_event():
    """解析 IssuesEvent JSON"""
def test_poller_parse_pr_review_event():
    """解析 PullRequestReviewEvent JSON"""
def test_poller_detect_mention():
    """@repocraft 在 comment body 中被检测到"""
def test_poller_detect_mention_negative():
    """普通 comment 不触发"""
def test_poller_etag_304_returns_empty():
    """304 响应返回空事件列表"""

# --- Prompts ---
def test_patrol_prompt_includes_budget():
    """patrol prompt 包含 budget 数字"""
def test_fix_issue_prompt_includes_evidence_template():
    """fix prompt 包含 Before/After 模板"""
def test_triage_prompt_includes_issue_content():
    """triage prompt 包含 issue title + body"""

# --- Dispatcher helpers ---
def test_extract_summary_from_result_json():
    """从 {type: result, result: '...'} 提取 summary"""
def test_extract_summary_fallback():
    """无 result 行时 fallback 到最后几行"""
def test_slugify():
    """标题转 URL-safe slug"""
def test_slugify_length_limit():
    """超长标题截断到 50 字符"""

# --- Scheduler ---
def test_scheduler_creates_triage_for_new_issue():
    """新 issue 事件 → triage activity"""
def test_scheduler_skips_processed_event():
    """已处理的 event_id 不重复创建 activity"""
def test_scheduler_patrol_respects_budget():
    """budget 为 0 时不创建 patrol activity"""
def test_scheduler_patrol_resets_after_window():
    """window 过期后 budget 重置, 可以 patrol"""
```

### Integration Tests (`tests/test_integration.py`)

需要 Docker 运行。需要 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`。
用 `@pytest.mark.integration` 标记, 默认跳过, `pytest -m integration` 运行。

```python
@pytest.fixture(scope="session")
def container_mgr():
    """启动 repocraft-worker 容器, 测试结束后停止"""

@pytest.fixture
def store(tmp_path):
    """临时 SQLite DB"""

# --- Container ---
@pytest.mark.integration
def test_container_starts_and_runs(container_mgr):
    """容器能启动, exec 返回正确"""
    result = container_mgr.exec("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout

@pytest.mark.integration
def test_container_claude_installed(container_mgr):
    """claude CLI 可用"""
    result = container_mgr.exec("claude --version")
    assert result.exit_code == 0

@pytest.mark.integration
def test_container_sdk_installed(container_mgr):
    """claude-agent-sdk 可 import"""
    result = container_mgr.exec('python3 -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)"')
    assert result.exit_code == 0

@pytest.mark.integration
def test_container_playwright_works(container_mgr):
    """playwright 截图正常"""
    result = container_mgr.exec("npx playwright screenshot https://example.com /tmp/test.png")
    assert result.exit_code == 0

@pytest.mark.integration
def test_container_git_identity(container_mgr):
    """git 身份正确配置"""
    result = container_mgr.exec("git config --global user.name")
    assert result.stdout.strip() != "RepoCraft"  # 应该是用户身份

# --- SDK Runner ---
@pytest.mark.integration
def test_sdk_runner_simple_prompt(container_mgr):
    """SDK runner 能执行简单 prompt 并返回结构化 JSON"""
    # docker exec -i ... python3 /app/run_activity.py 5 /tmp <<< "What is 2+2?"
    # 验证 stdout 最后一行是 {type: result, ...}

@pytest.mark.integration
def test_sdk_runner_disallowed_tools(container_mgr):
    """AskUserQuestion 被禁用"""
    # 验证工具列表中不包含 AskUserQuestion

# --- End-to-End (需要 GITHUB_TOKEN) ---
@pytest.mark.integration
@pytest.mark.e2e
def test_fix_issue_end_to_end(container_mgr, store):
    """完整 fix 流程: clone → init → fix → PR with evidence"""
    # 用一个 test repo with known bug
    # 验证: activity status=done, PR 已创建, PR body 包含证据

@pytest.mark.integration
@pytest.mark.e2e
def test_patrol_creates_issue_with_evidence(container_mgr, store):
    """patrol 发现 bug 并创建带证据的 issue"""
    # 用一个 test repo with known vulnerability
    # 验证: issue 已创建, body 包含复现步骤和证据
```

### Test 配置 (`conftest.py`)

```python
def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires Docker")
    config.addinivalue_line("markers", "e2e: requires Docker + GitHub token")

def pytest_collection_modifyitems(config, items):
    if not config.getoption("-m"):
        skip_integration = pytest.mark.skip(reason="use -m integration to run")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
```

---

## Milestones

```
P0 清理+验证    ██░░░░░░░░  删 v1 死代码, 验证 SDK/Playwright 在容器内工作
P1 容器+SDK     ████░░░░░░  Dockerfile 更新, SDK runner, git identity, config
P2 Proof of Work ████████░░  CLAUDE.md 重写, 所有 prompt 模板 (产品核心)
P3 Dispatcher   ████░░░░░░  重写用 SDK runner, 活动超时, session resume
P4 Store 扩展   ██░░░░░░░░  新表, migration, 批量日志
P5 Scheduler    ██████░░░░  poller, patrol timer, daemon, CLI 重写
P6 E2E 验证     ████░░░░░░  在真实 repo 上测试全流程
```

