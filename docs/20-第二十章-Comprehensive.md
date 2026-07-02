# 第 20 章：Comprehensive

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s20_comprehensive.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s20_comprehensive.py)


## 本章解决什么问题？

从 第一章 到 第十九章，我每天给 Agent Harness 加一块能力：

- Agent Loop
- Tool Use
- Permission
- Hooks
- TodoWrite
- Subagent
- Skill Loading
- Context Compact
- Memory
- System Prompt 组装
- Error Recovery
- Task System
- Background Tasks
- Cron Scheduler
- Agent Teams
- Team Protocols
- Autonomous Agents
- Worktree Isolation
- MCP Plugin

到 第十九章，这台机器已经相当复杂。子系统很多：任务、团队、定时、隔离、自主运行、外部插件……每个都有自己的状态和查看工具。

但正因为它太复杂，出现了一个新问题：**没有统一入口能看到全局状态**。

用户想知道“现在 Harness 里到底在发生什么”，得分别调用 `task_list`、`cron_list`、`team_list`、`autonomous_list`、`worktree_list`、`mcp_list`、`read_memory`、`show_errors`、`show_system_prompt`。这很不方便。

所以 第二十章 作为终章，不再叠加新机制，而是做一件事：**综合集成与收尾**。

> 补一个 `system_status` 总览工具，把前 19 章的全部子系统集成进一个仪表盘，让 Harness 有一个统一的状态出口。

## 核心概念

第二十章 的核心是“仪表盘”思想：

```text
system_status
  -> tasks / teams / cron / worktrees / autonomy / mcp / memory / skills / errors / protocols
  -> 一份可读的 Harness 全局状态
```

这里有两个关键点。

第一，聚合而非新增。

`system_status` 不引入新数据源，它只是把分散在各子系统里的状态聚合起来，按计数和状态分类展示。

第二，单一入口。

主 Agent 或用户想知道全局情况时，只需要调用一次 `system_status`，而不需要挨个查询。

## 我的实现

完整实现见：`code/s20_comprehensive.py`

第二十章 的代码基于 第十九章 的完整实现。本章新增的只有一个工具：`system_status`。

### 状态聚合辅助

先写一个通用的状态计数函数：

```python
def count_status(records: dict[str, dict[str, Any]], field: str = "status") -> dict[str, int]:
    """按某个字段聚合计数，得到 {状态: 数量}。"""
    counts: dict[str, int] = {}
    for record in records.values():
        key = str(record.get(field, "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return counts
```

这样无论是 Task、Cron、Team、Autonomy 还是 Worktree，都可以用同一个函数按状态聚合计数。

### system_status

核心函数把所有子系统状态串起来：

```python
def run_system_status() -> str:
    """工具：输出 Harness 全局状态总览，串起前 19 章的全部子系统。"""
    ensure_mcp_plugins()
    with TASK_LOCK:
        tasks_total = len(TASKS)
        tasks_by_status = count_status(TASKS)
        threads_alive = sum(1 for thread in TASK_THREADS.values() if thread.is_alive())
    with CRON_LOCK:
        cron_total = len(CRON_JOBS)
        cron_by_status = count_status(CRON_JOBS)
    with TEAM_LOCK:
        teams_total = len(TEAM_RUNS)
        teams_by_status = count_status(TEAM_RUNS)
    with AUTONOMY_LOCK:
        autonomy_total = len(AUTONOMY_RUNS)
        autonomy_by_status = count_status(AUTONOMY_RUNS)
    with WORKTREE_LOCK:
        worktree_total = len(WORKTREES)
        worktree_by_status = count_status(WORKTREES)
    with MCP_LOCK:
        mcp_total = len(MCP_PLUGINS)
        mcp_enabled = sum(1 for plugin in MCP_PLUGINS.values() if plugin["enabled"])
    ...
```

输出大致是这样的形状：

```text
# Harness System Status

## Counts
- todos: 2
- skills loaded: 3
- memory entries: 5
- recent errors: 1/20
- tasks: 4 {'completed': 3, 'running': 1} (background threads alive: 1)
- cron jobs: 2 {'enabled': 2}
- team runs: 1 {'completed': 1}
- autonomous runs: 0 {}
- worktrees: 1 {'open': 1}
- mcp plugins: 1 (enabled: 1)
- protocols: 3

## Subsystems
- cron scheduler thread: running
- effective cwd: /path/to/workspace
- model: claude-...
- workspace: /path/to/workspace
```

### 工具注册

`system_status` 注册为只读工具，主 Agent 和 Subagent 都能用：

```python
"system_status": lambda **kwargs: run_system_status(),
```

这样无论任务跑得多深，Agent 都能随时回到仪表盘，确认 Harness 当前的整体状态。

## 全书能力清单

第二十章 是一个合适的收尾点。让我把前 20 章造出来的能力按 Part 整理一遍。

### Part 1：地基 —— 让 Agent 动起来

| 章节 | 能力 |
|---|---|
| 第一章 Agent Loop | 模型 → 工具 → 模型的循环 |
| 第二章 Tool Use | 让模型调用结构化工具 |
| 第三章 Permission | 工具执行前的权限确认 |
| 第四章 Hooks | PreToolUse / PostToolUse / UserPromptSubmit |

### Part 2：记忆与规划

| 章节 | 能力 |
|---|---|
| 第五章 TodoWrite | 可见任务列表 |
| 第六章 Subagent | 独立上下文的子 Agent |
| 第七章 Skill Loading | 按需加载技能 |
| 第八章 Context Compact | 长对话的上下文压缩 |
| 第九章 Memory | 跨会话的持久化记忆 |
| 第十章 System Prompt | 结构化组装系统提示词 |
| 第十一章 Error Recovery | 错误分类与恢复建议 |

### Part 3：长期运行

| 章节 | 能力 |
|---|---|
| 第十二章 Task System | 子任务生命周期 |
| 第十三章 Background Tasks | 后台非阻塞执行 |
| 第十四章 Cron Scheduler | 时间触发任务 |

### Part 4：多 Agent 协作

| 章节 | 能力 |
|---|---|
| 第十五章 Agent Teams | 角色化小队 |
| 第十六章 Team Protocols | 可复用协作流程 |
| 第十七章 Autonomous Agents | 有边界的自主运行 |
| 第十八章 Worktree Isolation | 隔离工作目录 |

### Part 5：扩展与集成

| 章节 | 能力 |
|---|---|
| 第十九章 MCP Plugin | 外部能力插件化 |
| 第二十章 Comprehensive | 全局状态总览 |

## 我踩的坑

### 坑 1：差一点又想加新机制

写终章时最大的诱惑是“再塞一个能力进去”。

但我意识到：如果 第二十章 还在加新机制，就违背了“收尾”的定位。终章的价值是让已有的复杂系统变得可观察、可理解，而不是再增加复杂度。

所以我只加了 `system_status`，把精力放在“把前面所有东西串起来”。

### 坑 2：状态分散在很多锁里

`system_status` 要读 Task、Cron、Team、Autonomy、Worktree、MCP 六个子系统的状态，每个都有自己的锁。

我没有新建一个全局大锁（那会破坏各子系统的独立性），而是在每个子系统自己的锁范围内分别快照，再拼到一起。这保持了锁的粒度，也避免了长时间持锁。

### 坑 3：仪表盘要克制

一开始我想让 `system_status` 输出每个任务的详细内容。

但那样输出会很长，失去了“总览”的意义。所以最终版本只输出计数和状态分布，需要细节时再用 `task_read`、`team_read` 等专用工具。

## 小结

第二十章 的关键词是：**收尾与可观察性**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类工具里，这类总览能力通常体现在：

- 启动时的环境摘要。
- 状态面板（todos、上下文用量、权限模式、当前目录）。
- 工具列表和可用 Skill / MCP server 摘要。
- 诊断命令（查看 memory、errors、配置）。
- 可观测性和遥测面板。

我这个最小实现对应的是：

```text
all subsystems -> aggregated snapshot -> single readable overview
```

和真实系统相比，它还很简单：

- 没有实时刷新或事件流。
- 没有图形化面板。
- 没有资源用量（token、内存、API 配额）。
- 没有持久化运行日志。
- 没有按时间维度的历史趋势。

但核心思想已经出现：

> 一个复杂的 Harness 需要一个统一的观察出口，否则连开发者都看不清自己在维护什么。


20 天前，我从“让模型调用一个工具”开始；20 天后，这台机器有了任务系统、后台执行、定时调度、团队协作、自主运行、工作隔离和外部插件。

终章没有再造新轮子，而是给整台机器装了一个仪表盘。这也回到了这本书一直强调的观点：

> Agent 不是模型，Agent = Model + Harness。而 Harness 的价值，恰恰在于把这些工程能力一层层组织起来。