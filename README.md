<div align="center">

<img src="frontend/public/logo.svg" width="80" height="80" alt="CatoCode" />

# CatoCode

### 长时运行的代码库维护助手——自动处理 issue、修复 bug、审查 PR，7×24 不间断

[![CI](https://github.com/humeo/cato-code/actions/workflows/ci.yml/badge.svg)](https://github.com/humeo/cato-code/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-required-blue)](https://www.docker.com/)

[托管服务](#-托管服务) · [快速开始](#-快速开始) · [工作原理](#-工作原理) · [CLI 参考](#-cli-参考) · [贡献](#-贡献)

</div>

---

> [!IMPORTANT]
> **CatoCode 会做什么：**
> - 在本地运行 Docker 容器（隔离执行环境）
> - 调用 Anthropic API（代码片段会发送给 Anthropic 用于分析）
> - 向 GitHub 发送 comment、创建 PR
>
> **CatoCode 不会做什么：**
> - 不会将代码发送给除 Anthropic 以外的第三方
> - 不会在没有你 `/approve` 的情况下自动提交代码
>
> **如何停止：** `docker compose down` 立即停止所有活动。

---

## 问题

你的 GitHub 仓库每天都在产生新 issue、新 PR、新的潜在 bug。

每处理一个 bug，流程都一样：读 issue → 本地拉取 → 复现问题 → 定位原因 → 写修复 → 跑测试 → 提 PR。重复、耗时，而且你不在的时候没人处理。

AI 代码补全工具（Copilot、Cursor）能帮你写代码，但它们是被动的——你不问它不动，也不会主动监听你的仓库。Devin 这类全自动工具走向另一个极端——你不知道它改了什么，也不知道该不该信任结果。

CatoCode 的定位在中间：**持续运行，主动响应，但每一步都留证据，你来决定是否合并。**

---

## 托管服务

不想自己部署？直接用我们的托管版本：

**[www.catocode.com](https://www.catocode.com)** — 连接你的 GitHub 仓库，5 分钟上手，无需配置服务器。

---

## 快速开始

```bash
git clone https://github.com/humeo/cato-code.git
cd cato-code
cp .env.example .env          # 填入 ANTHROPIC_API_KEY 和 GITHUB_TOKEN
docker compose up -d
docker compose exec catocode catocode watch https://github.com/owner/repo
```

服务启动后持续运行，自动响应新 issue 和 PR。详细步骤见 [部署指南](#-部署指南)。

---

## 看它工作

Issue 开启 → CatoCode 自动拉取代码、复现问题、分析原因 → 在 issue 下留评论 → 你回复 `/approve` → 自动修复、验证、创建 PR。

```
# 1. 监听你的 repo（之后持续运行，无需干预）
$ docker compose exec catocode catocode watch https://github.com/alice/myproject
✓ Watching https://github.com/alice/myproject

# 2. 有人开了一个 issue：
#    "Bug: calculate_average() crashes when list is empty"

# 3. CatoCode 自动拉取代码，复现 bug，在 issue 下评论：
#
#    已复现此 bug。
#
#    根本原因：calculate_average() 未处理空列表，触发 ZeroDivisionError。
#    复现命令：python -c "from stats import calculate_average; calculate_average([])"
#    输出：ZeroDivisionError: division by zero
#
#    建议修复：在函数开头加空列表检查，返回 0 或 None。
#    回复 /approve 让我执行修复。

# 4. 你回复 /approve

# 5. CatoCode 自动修复、跑测试、创建 PR，PR 描述包含完整证据：
#
#    ## Evidence
#    | Check           | Before                    | After       |
#    |-----------------|---------------------------|-------------|
#    | 复现命令        | ❌ ZeroDivisionError       | ✅ 返回 0   |
#    | 完整测试套件    | 11 passed, 1 failed       | 12 passed   |

$ docker compose exec catocode catocode status
done  88b7ce3b  fix_issue  issue:42  $0.68
```

你只需要审一个带完整证据的 PR，30 秒验证结果。

---

## 部署指南

Docker Compose 是推荐方式，不需要本地安装 Python 或 uv。

### 1. 配置

```bash
git clone https://github.com/humeo/cato-code.git
cd cato-code
cp .env.example .env
```

编辑 `.env`：

| 变量 | 必填 | 说明 |
|------|------|------|
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API 密钥 |
| `GITHUB_TOKEN` | ✅ | GitHub PAT（需要 `repo` 权限） |
| `PORT` | | 服务端口（默认 `8000`） |
| `GIT_USER_NAME` | | 提交用户名（默认 `CatoCode`） |
| `GIT_USER_EMAIL` | | 提交邮箱（默认 `catocode@bot.local`） |
| `MAX_CONCURRENT` | | 最大并发任务数（默认 `3`） |
| `CATOCODE_MEM` | | Worker 容器内存限制（默认 `8g`） |
| `CATOCODE_CPUS` | | Worker 容器 CPU 限制（默认 `4`） |
| `CATOCODE_PATROL_MAX_ISSUES` | | 每个巡检窗口最大 issue 数（默认 `5`） |
| `CATOCODE_PATROL_WINDOW_HOURS` | | 巡检滚动窗口（默认 `12` 小时） |

完整变量列表见 [`.env.example`](.env.example)。

### 2. 启动

```bash
docker compose up -d
```

首次启动自动构建 Docker 镜像（约 5–10 分钟），后续复用缓存。服务启动后**持续运行**，自动处理新事件。

### 3. 监听 Repo

```bash
docker compose exec catocode catocode watch https://github.com/owner/repo
```

可以监听多个 repo，每个 repo 独立管理。

### 4. 查看状态

```bash
# 查看所有监听的 repo 和最近活动
docker compose exec catocode catocode status

# 查看容器日志
docker compose logs -f catocode
```

### 5. Dashboard（可选）

可视化查看 repo 状态、活动历史和费用：

```bash
cd frontend
cp .env.example .env.local
bun install && bun dev
# 打开 http://localhost:3000
```

### 6. 配置 Webhook（可选）

没有 webhook，CatoCode 通过定时 patrol 巡检工作。配置 webhook 后可以**实时**响应新 issue 和 PR，延迟从分钟级降到秒级。

```bash
# 创建公网隧道（macOS）
brew install cloudflare/cloudflare/cloudflared
cloudflared tunnel --url http://localhost:8000
```

在 GitHub 仓库 Settings → Webhooks 中添加：
- **URL**: `https://<tunnel-id>.trycloudflare.com/webhook/github/{owner-repo}`
- **Content type**: `application/json`
- **Events**: Issues, Issue comments, Pull requests, Pull request reviews

> `{owner-repo}` 格式为 `owner-repo`，例如 `alice-myproject`

---

## 工作原理

CatoCode 持续运行，监听 GitHub 事件，在隔离的 Docker 容器里执行 Claude Agent 完成实际工作。

```
┌─ Host Process ──────────────────────────────────────┐
│  Daemon                                              │
│  ├── Scheduler（审批检查、patrol 巡检、任务分发）    │
│  ├── Webhook Server（/webhook/github/{repo_id}）     │
│  └── Store（SQLite at /data/catocode.db）            │
└──────────────────┬──────────────────────────────────┘
                   │ Docker API
┌─ Worker Container ──────────────────────────────────┐
│  catocode-worker                                    │
│  ├── Claude Agent SDK + Claude Code CLI             │
│  ├── 开发工具（git, gh, python, node, uv）          │
│  └── /repos/{owner-repo}/（克隆的仓库）             │
└─────────────────────────────────────────────────────┘
```

**事件流：**

1. GitHub issue 开启 → webhook 触发（或 patrol 定时扫描）
2. Decision engine 分类事件，选择对应 skill
3. Worker 容器拉取最新代码，执行 Claude Agent
4. Agent 复现问题、分析原因、发布评论，等待 `/approve`
5. 收到 `/approve` → 自动修复 → 跑测试 → 创建 PR，附带 Proof of Work 证据

<details>
<summary><b>Skills 详情</b> — 每种任务的触发条件和行为</summary>

Skills 是 Markdown 提示模板，存放在 `src/catocode/container/skills/`，可以直接编辑定制，无需改代码。

| Skill | 触发条件 | 行为 |
|-------|---------|------|
| `analyze_issue` | Issue 开启 | 拉取代码、复现问题、分析根因、发布评论，等待 `/approve` |
| `fix_issue` | `/approve` 后 | 复现 → 修复 → 验证 → 创建 PR（含 Proof of Work 证据表） |
| `review_pr` | PR 开启 | 审查代码质量、安全性、测试覆盖，发布 review 评论 |
| `respond_review` | PR review 评论 | 处理 review 反馈，推送更新 |
| `triage` | Issue 开启 | 分类并打标签，检查重复 issue |
| `patrol` | 定时触发 | 主动扫描代码库，发现潜在 bug 和安全问题，有 budget 限制防止刷 issue |

</details>

<details>
<summary><b>Proof of Work 协议</b> — 为什么每次修复都可信</summary>

`fix_issue` skill 强制执行两层证据协议：

**Layer 1 — 复现证据（必须）**

修复前先证明问题存在：
```bash
pytest tests/test_foo.py::test_bar 2>&1 | tee /tmp/evidence-before.txt
```

如果无法复现，不执行修复，直接在 issue 下说明原因。

**Layer 2 — 验证证据（必须）**

修复后用相同步骤验证：
```bash
pytest tests/test_foo.py::test_bar 2>&1 | tee /tmp/evidence-after.txt
pytest 2>&1 | tee /tmp/test-suite-after.txt  # 完整测试套件，确认无回归
```

PR 描述里包含完整的 before/after 对比表，你可以在 30 秒内验证结果，不需要本地跑测试。

</details>

---

## CLI 参考

```bash
# 监听一个 repo（注册并开始持续监听）
catocode watch https://github.com/owner/repo

# 停止监听
catocode unwatch https://github.com/owner/repo

# 立即修复一个 issue（无需 webhook，阻塞执行）
catocode fix https://github.com/owner/repo/issues/42

# 查看监听的 repo 和最近活动
catocode status

# 查看某个 activity 的详细日志
catocode logs <activity_id>
catocode logs <activity_id> --follow   # 实时跟踪
```

> **Docker Compose 部署时**在命令前加 `docker compose exec catocode`。
> **本地开发时**使用 `uv run catocode`。

---

## GitHub App 模式（进阶）

适合团队和组织：自动覆盖 org 下所有 repo，无需逐个配置 webhook 和 PAT。

见 [docs/GITHUB_APP_SETUP.md](docs/GITHUB_APP_SETUP.md)。

---

## 开发

```bash
uv sync --dev                              # 安装依赖
uv run pytest                             # 运行测试
uv run pytest --cov=src/catocode         # 带覆盖率
uv run pytest -m integration             # 集成测试（需要 Docker）
uv run ruff check src/ --fix             # Lint
cd frontend && bun install && bun dev    # 前端
```

---

## 贡献

1. Fork 仓库
2. 创建 feature 分支：`git checkout -b feature/amazing-feature`
3. 修改并添加测试：`uv run pytest`
4. 提交：`git commit -m "feat: add amazing feature"`
5. 开 PR

---

## License

Apache License 2.0 — 详见 [LICENSE](LICENSE)。

---

<div align="center">

[托管服务 →](https://www.catocode.com) · [快速开始](#-快速开始) · [CLI 参考](#-cli-参考) · [贡献](#-贡献)

</div>
