# 第 17 章：Autonomous Agents

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s17_autonomous_agents.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s17_autonomous_agents.py)


## 本章解决什么问题？

前面几章已经把 Agent 的很多基础设施搭起来了：Task System 可以跟踪子任务，Background Tasks 可以后台运行，Cron Scheduler 可以按时间触发，Agent Teams 和 Team Protocols 可以组织多个角色协作。

但这些能力仍然有一个共同点：它们大多是“被动执行”。用户说运行一次 task，它就运行一次；用户说跑一个 team，它就跑一个 team。每一步都需要用户或主 Agent 明确下达下一条指令。

真正的 Autonomous Agent 想解决的是另一件事：给它一个目标、预算和停止条件，它能围绕这个目标自主推进多个 step，并把每一步做了什么记录下来。

但这里也有危险：自主不等于无限自动化。没有边界的 Autonomous Agent 很容易变成无限循环、重复调用工具、超预算、甚至做出用户没授权的动作。

所以 第十七章 我实现的是一个**保守版 Autonomous Agents**：

> 自主运行必须有 `max_steps`、可观察的 `success_criteria`、可选时间预算，并且每一步都记录在 `AutonomyRun` 中，可暂停、可恢复、可查看。

## 核心概念

第十七章 的流程是：

```text
autonomous_start(goal, success_criteria, max_steps)
  -> AutonomyRun
  -> autonomous_step
  -> team_run(protocol="plan")
  -> record team/task ids
  -> stop when budget is reached
```

这里有三个关键概念。

第一，AutonomyRun。

自主运行不是一段聊天历史，而是一条结构化记录：目标、成功标准、状态、最大步数、已执行步数、deadline、notes、关联的 team/task、最终报告和错误。

第二，Step。

自主不是“一口气无限跑”。我把自主循环拆成一个个 step。每个 step 都可以被单独推进，也可以被暂停和恢复。

第三，Stop Condition。

自主运行必须有停止条件。本章实现了两个硬边界：

- `max_steps`：最多执行多少步，最多 10。
- `deadline_seconds`：可选时间预算，最多 3600 秒。

## 我的实现

完整实现见：`code/s17_autonomous_agents.py`

第十七章 继续基于 第十六章，所以 Team Protocols、Agent Teams、Cron Scheduler、Background Tasks、Task System、Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在自主运行记录和自主 step。

### AutonomyRun

先定义自主运行状态：

```python
AutonomyStatus = Literal["running", "paused", "completed", "failed"]
```

然后定义 `AutonomyRun`：

```python
class AutonomyRun(TypedDict):
    """一次有边界的自主运行记录。"""

    id: str
    goal: str
    success_criteria: str
    status: AutonomyStatus
    created_at: str
    updated_at: str
    max_steps: int
    step_count: int
    deadline_at: float | None
    notes: list[str]
    task_ids: list[str]
    team_ids: list[str]
    final_report: str
    error: str | None
```

它和 `TaskRecord`、`TeamRun` 是三层关系：

- `AutonomyRun`：长期目标。
- `TeamRun`：某一步采用团队协议做判断。
- `TaskRecord`：团队成员实际运行的子 Agent 任务。

### 创建自主运行

`new_autonomy_run()` 创建记录并保存到 `AUTONOMY_RUNS`：

```python
def new_autonomy_run(
    goal: str,
    success_criteria: str,
    max_steps: int = 3,
    deadline_seconds: float | None = None,
) -> AutonomyRun:
    """创建一次有边界的自主运行。"""
    global NEXT_AUTONOMY_ID
    run_id = f"auto-{NEXT_AUTONOMY_ID:04d}"
    NEXT_AUTONOMY_ID += 1
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    deadline_at = time.time() + deadline_seconds if deadline_seconds else None
    run: AutonomyRun = {
        "id": run_id,
        "goal": goal.strip(),
        "success_criteria": success_criteria.strip(),
        "status": "running",
        "created_at": now_text,
        "updated_at": now_text,
        "max_steps": max_steps,
        "step_count": 0,
        "deadline_at": deadline_at,
        "notes": [],
        "task_ids": [],
        "team_ids": [],
        "final_report": "",
        "error": None,
    }
    with AUTONOMY_LOCK:
        AUTONOMY_RUNS[run_id] = run
    return run
```

这里没有把自主运行持久化到磁盘。本章仍然保持“当前会话内可观察”的最小实现。

### 停止条件

停止条件集中在 `autonomy_should_stop()`：

```python
def autonomy_should_stop(run: AutonomyRun) -> str | None:
    """检查自主运行是否已经触达停止条件。"""
    if run["status"] != "running":
        return f"run is {run['status']}"
    if run["step_count"] >= run["max_steps"]:
        return "max_steps reached"
    if run["deadline_at"] is not None and time.time() >= run["deadline_at"]:
        return "deadline reached"
    return None
```

这段逻辑很简单，但意义很大：自主运行不是模型自己说“我继续”，而是 Harness 每步都检查预算。

### 执行一个 step

`run_autonomous_step()` 是核心：

```python
def run_autonomous_step(run: AutonomyRun) -> str:
    """执行一个保守的自主 step：用 plan 协议生成下一步报告。"""
    stop_reason = autonomy_should_stop(run)
    if stop_reason:
        run["status"] = "completed" if stop_reason == "max_steps reached" else run["status"]
        return f"Autonomy {run['id']} stopped: {stop_reason}."

    run["step_count"] += 1
    run["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    objective = (
        f"Autonomous goal:\n{run['goal']}\n\n"
        f"Success criteria:\n{run['success_criteria']}\n\n"
        f"Step {run['step_count']} of {run['max_steps']}: decide and document the next bounded action."
    )
    report = run_team(objective, protocol="plan")
```

我让每个自主 step 复用 第十六章 的 `plan` 协议。这样 Autonomous Agent 不会随意乱跑，而是每步都经过：

```text
research -> plan -> review
```

之后把生成的 TeamRun 记录到 `team_ids`：

```python
with TEAM_LOCK:
    latest_team_id = next(reversed(TEAM_RUNS)) if TEAM_RUNS else None
if latest_team_id:
    run["team_ids"].append(latest_team_id)
    run["notes"].append(f"step {run['step_count']}: created {latest_team_id}")
```

最后，如果达到 `max_steps`，就标记完成。

### autonomous 工具

第十七章 新增一组工具：

- `autonomous_start`：启动一次自主运行。
- `autonomous_step`：推进一个 step。
- `autonomous_list`：列出自主运行。
- `autonomous_read`：读取某次运行详情。
- `autonomous_pause`：暂停运行。
- `autonomous_resume`：恢复运行。

`autonomous_start` 的输入限制很关键：

```python
steps = max(1, min(int(max_steps), 10))
deadline = None if deadline_seconds is None else max(5.0, min(float(deadline_seconds), 3600.0))
```

也就是说，模型不能创建一个 10 万步的自主循环，也不能设一个离谱的超长 deadline。

## 我踩的坑

### 坑 1：Autonomous 不等于 while True

最直觉的写法是：

```python
while not done:
    ask_model_next_action()
```

但这很危险。模型可能永远判断自己还没完成，也可能重复调用同一个工具。

所以本章没有做无限循环，而是把自主运行拆成 `autonomous_step`。每次 step 都由 Harness 检查预算，用户也可以暂停、查看、再继续。

### 坑 2：必须复用已有机制

一开始我想给 Autonomous Agent 写一套新循环，让它直接调用模型和工具。

但这样会绕过前面已经做好的 Task、Team、Error Recovery、Protocol 记录。

所以我让每个 autonomous step 调用 `run_team(..., protocol="plan")`。这样自主运行仍然使用标准 TeamRun 和 TaskRecord，可观察性不会丢。

### 坑 3：成功标准必须显式输入

如果只给 goal，不给 success criteria，Agent 很难知道什么时候停。

所以 `autonomous_start` 强制要求 `success_criteria`。这不是形式主义，而是给停止条件一个可观察的目标。

## 小结

第十七章 的关键词是：**有边界的自主性**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类系统里，Autonomous Agents 往往不是单个函数，而是多种机制组合：

- todo / plan 作为短期目标管理。
- task / subagent 作为分工执行单元。
- background task 作为长任务载体。
- error recovery 防止失败后盲目重复。
- memory / summary 保存跨轮上下文。
- permission / policy 限制自主行为边界。

我这个最小实现对应的是：

```text
bounded autonomous run -> repeated planned steps -> team/task records -> inspectable state
```

和真实系统相比，它还很简单：

- 每个 step 固定使用 `plan` 协议。
- 没有让模型真正选择工具序列。
- 没有自动判断 success criteria 是否满足。
- 没有持久化自主运行状态。
- 没有后台 autonomous loop。

但核心思想已经出现：

> Autonomous Agent 的关键不是“自动运行”，而是“带边界、可观察、可暂停、可恢复地自动推进”。


没有预算和停止条件的自主 Agent 是危险的；没有记录的自主 Agent 是不可调试的。

本章通过 `AutonomyRun`、`autonomous_start`、`autonomous_step`、`autonomous_pause` 和 `autonomous_read`，把自主循环变成了 Harness 可管理的一等对象。