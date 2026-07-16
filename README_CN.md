# CoreCoder

> # CodeRover: Verify-driven Self-Healing Software Engineering Harness

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**CodeRover** 是一个基于 CoreCoder（Claude Code 的 Python 精简实现）构建的 **验证驱动的软件工程 Harness**。它通过 “执行 → 校验 → 反思 → 修复” 的闭环调度，让 AI Agent 能够自主完成代码修改、自动验证修改效果、在失败时自主反思并迭代，最终交付正确的修复。

> 与传统的“对话式”编程助手不同，CodeRover 是一个**任务式闭环系统** —— 它不依赖人类的实时判断，而是依靠**自动化质量门禁（Verifier）**来驱动决策，确保每一次修改都经过客观检验。

原项目 **CoreCoder** [he-yufeng/CoreCoder](https://github.com/he-yufeng/CoreCoder) 。

---

## 📌 项目目的

- **将 AI Agent 从“搜索者”转变为“执行者”**：通过 Harness 控制 Agent 的行为边界，使其专注于修复目标，而非漫无目的地探索。
- **构建工程化的质量门禁**：集成 `pytest`（功能测试）、`mypy`（类型检查）、`ruff`（代码规范），以客观的退出码和结构化报错驱动修复决策。
- **实现闭环自修复**：Agent 修改代码 → Verifier 校验 → 失败时 Reflector 分析错误并生成修复方案 → Agent 重新执行，直到 Verifier 通过或预算耗尽。
- **支持多种任务输入**：自然语言指令、GitHub Issue、交互式 REPL —— Harness 统一处理为 `Task`，并基于 Verifier 结果驱动循环。

---

## 🧠 核心设计理念

### 为什么需要 Verifier？—— 从“主观判断”到“客观验收”

| 方式 | 问题 | CodeRover 方案 |
|------|------|----------------|
| **Claude Code 自行读报错** | 50 行红字，Token 浪费，易误判 | Verifier 精准提取 `file:line` + `error_type`，压缩为 3 行 JSON |
| **大模型“觉得没问题”** | 靠 LLM 主观判断，可能漏判 | Verifier 只看 `returncode`，非 0 即失败，强制验收 |
| **修改后不验证** | 可能引入新 Bug | Verifier 自动运行全部测试，确保全绿才交卷 |

> **比喻**：CoreCoder 是“会看病的医生”，Verifier 是“CT 机和化验单”。没有 CT 机，医生也能治感冒，但要治复杂疾病，必须依赖精确的数据。

---

## 🔄 项目流程图



---

## 🧩 分模块设计

### 1. Adaptive Harness（`core/harness.py`）

核心调度器，管理整个闭环流程。主要职责：

- **状态机控制**：INIT → LOCALIZATION → PATCH_GEN → PATCH_REVIEW → VERIFICATION → SUCCESS/FAILED
- **自适应终止**：Verifier 通过、`max_retries` 耗尽、死循环检测、Reflector 置信度过低
- **错误定级**：只将 `pytest` 失败和高危 `mypy` 错误视为阻断，忽略 `override`、`F401` 等低危警告
- **Patch Scope 控制**：强制 Agent 仅修改候选文件列表内的文件，越界立即触发 Reflector
- **Diff-based Review**：检查 `git diff` 是否引入无关修改或过大的改动

### 2. Verifier（`verifier/verification.py`）

工程化质量门禁，集成三套工业标准工具：

| 工具 | 职责 | 解析输出 |
|------|------|----------|
| `pytest` | 单元测试执行 | 提取 `file:line`、`AssertionError`、上下文 |
| `mypy` | 静态类型检查 | 提取错误类型（`attr-defined`、`override` 等） |
| `ruff` | 代码规范检查 | 提取规则代码（`F401`、`E402` 等） |

**核心数据结构**：

```python
class VerifierError(BaseModel):
    tool: str        # "pytest" | "mypy" | "ruff"
    file: str
    line: int
    error_type: str
    message: str
    context: str

class VerifierResult(BaseModel):
    passed: bool                    # 阻断型错误（pytest + 高危 mypy）
    errors: List[VerifierError]     # 阻断错误列表
    quality_warnings: List[VerifierError]  # 非阻断警告（ruff + 低危 mypy）
    summary: str
    raw_outputs: Dict[str, str]



### 3. Reflector（`agents/reflector.py`）

错误根因分析与修复方案生成器。

- **输入**：`VerifierError` 列表 + 仓库路径
- **流程**：
  1. 按优先级排序错误（pytest 最高）
  2. 提取报错行的代码上下文
  3. 从 `FailureLibrary` 检索相似成功案例
  4. 调用 LLM 生成结构化的 `FixPlan` 列表
- **输出**：`ReflectorResult`（包含 `root_cause`、`fix_plans`、`confidence`）

---

### 4. Failure Pattern Library（`memory/failure_library.py`）

持久化存储历史修复经验，实现"经验驱动的迭代"。

- 每个案例包含：错误签名、根因、修复方案、结果（成功/失败）
- 新错误通过**归一化签名**（忽略 file/line，仅保留 tool + error_type + 消息模式）匹配相似案例
- 检索时优先返回成功案例，提升修复效率

---

### 5. Symbol Index（`tools/symbol_index.py`）

基于 Tree-sitter 的代码符号索引与调用图分析，为 Agent 提供"全局地图"。

- 支持查询：函数/类定义、调用者、被调用者、导入
- 非 ASCII 字符兼容（中文/emoji），正确处理字节偏移与字符偏移

---

### 6. CLI & GitHub Integration（`cli.py` + `github_client.py`）

- **CLI 子命令**：`coderover run`（正式入口）、`coderover test-harness`（调试）、`coderover`（交互式 REPL）
- **Issue 支持**：自动从 GitHub API 获取 Issue 标题和描述，构造任务
- **PR 自动创建**：修复成功后通过 GitHub REST API 创建带测试报告的 Pull Request

---

## 📊 测试结果（Benchmark）

我们基于 **axios、date-fns、core-web** 三个真实开源项目，构建了包含 30+ 个已关闭 Issue 的评测集，验证 CodeRover 的修复能力。

### 核心指标

| 指标 | 说明 | 数值 |
|------|------|------|
| **修复尝试率** | Agent 是否产生任何修改 | 100% |
| **文件命中率** | 修改的文件是否与官方 PR 一致 | 100% |
| **语义等价率** | Agent 的修复与官方 PR 在逻辑上等价（由 LLM 判断） | 36.4%（axios） |
| **平均耗时** | 每个 Issue 从启动到完成 | 267.8 秒（axios） |
| **Token 消耗** | 平均 Prompt Token / Issue | 99.6 万（axios） |

### 典型案例

| Issue | 问题 | Agent 修复 | 结果 |
|-------|------|-----------|------|
| axios#10851 | `??` 运算符兼容性 | 替换为三元表达式 | ✅ 语义等价 |
| axios#6928 | 进度事件 `undefined` | 增加 `if (!e) return` | ✅ 语义等价 |
| date-fns#3614 | `areIntervalsOverlapping` 逻辑错误 | 修正核心函数 + 回退 lock 文件 | ✅ 文件匹配（经 Patch Review 纠正）|

> **关键突破**：在 date-fns #3614 中，Agent 首次修改时误改了 `package-lock.json`，但 **Patch Review** 检测到"只改了 lock 文件，未改源码"并拒绝，**Reflector** 生成正确方案，Phase 2 执行修复，最终达成 `file_match` —— 这验证了多阶段防御机制的有效性。

---

## 🛠️ 安装与使用

### 安装

```bash
# 克隆项目
git clone https://github.com/your-username/CodeRover.git
cd CodeRover

# 创建虚拟环境并安装
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

# 配置 API Key（.env 文件）
echo "OPENAI_API_KEY=your-key" > .env
echo "OPENAI_BASE_URL=https://api.deepseek.com/v1" >> .env

### 使用

```bash
# 修复当前仓库
coderover run --repo . --task "修复测试失败"

# 从 GitHub Issue 修复
coderover run --issue "https://github.com/axios/axios/issues/10851" --repo ./axios

# 交互式 REPL
coderover

## 🏆 核心创新点

- **Verifier-driven 闭环**：不依赖 LLM 主观判断，以工具退出码为唯一依据，工程化质量门禁。
- **自适应终止**：死循环检测、资源预算、置信度阈值，避免无限消耗。
- **Patch Scope 策略**：硬性限制 Agent 修改范围，杜绝无关文件污染。
- **Diff-based Patch Review**：在 Verifier 之前预检 `git diff`，拦截无效或过大的改动。
- **经验复用**：FailureLibrary 让 Agent 从历史失败中学习，减少重复试错。

---

## 📌 未来计划

- **多语言支持**：扩展 Tree-sitter 到 Java、Go、Rust
- **多 Agent 协同**：Planner-Executor-Reviewer 分工
- **MCP 生态集成**：连接 Notion、Jira、Slack 等工具
- **Web UI**：可视化 Harness 执行过程，便于演示
