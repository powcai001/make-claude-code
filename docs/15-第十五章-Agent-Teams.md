# 第 15 章：Agent Teams

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s15_agent_teams.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s15_agent_teams.py)


## 本章解决什么问题？

第十二章 到 第十四章 逐步把单个子 Agent 做成了可跟踪、可后台运行、可定时触发的 Task System。但这些能力仍然围绕“一个子 Agent 做一件事”。

真实复杂任务经常不是一个视角能解决的。比如改一个关键模块时，我希望有人负责找相关代码，有人负责审查风险，有人负责想测试方案。如果只派一个通用子 Agent，它可能会把这些工作混在一起，报告看起来全面，但每个角度都不够深入。

所以 第十五章 我实现一个最小版 Agent Teams：

> 把多个带角色的子 Agent 编组成团队，围绕同一个目标分别调查，再由 Harness 汇总成一份团队报告。

## 核心概念

第十五章 的流程是：

```text
team_run(objective)
  -> TeamRun
  -> researcher task
  -> reviewer task
  -> tester task
  -> aggregate team report
```

这里有三个关键概念。

第一，TeamMember。

团队成员不是新的模型类型，而是带有不同角色提示词的子 Agent。默认团队包含：

- `researcher`：找事实、文件、实现证据。
- `reviewer`：审查风险、边界情况、回归点。
- `tester`：提出验证步骤和成功标准。

第二，TeamRun。

团队运行本身也需要记录：目标、成员、关联的 `task_ids`、最终报告、错误状态。

第三，汇总。

团队不是让多个 Agent 各说各话。Harness 需要把每个成员的报告按角色组织起来，形成一个可读、可追踪的总报告。

## 我的实现

完整实现见：`code/s15_agent_teams.py`

第十五章 继续基于 第十四章，所以 Cron Scheduler、Background Tasks、Task System、Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在团队成员、团队运行记录和报告聚合。

### TeamMember 和 TeamRun

我先定义两个结构：

```python
class TeamMember(TypedDict):
    """Agent Team 中一个带角色的成员。"""

    name: str
    role: str
    prompt: str


class TeamRun(TypedDict):
    """一次团队协作运行记录。"""

    id: str
    objective: str
    members: list[TeamMember]
    status: TaskStatus
    created_at: str
    finished_at: str | None
    task_ids: list[str]
    report: str
    error: str | None
```

`TeamRun` 和 `TaskRecord` 是两层关系：

- `TeamRun` 记录一次团队协作。
- `TaskRecord` 记录每个成员实际运行的子任务。

### 默认团队

默认团队是三人小队：

```python
def default_team_members() -> list[TeamMember]:
    return [
        {"name": "researcher", "role": "Researcher", "prompt": "Find relevant files..."},
        {"name": "reviewer", "role": "Reviewer", "prompt": "Review risks..."},
        {"name": "tester", "role": "Tester", "prompt": "Suggest validation steps..."},
    ]
```

如果用户传入 `members`，会通过 `normalize_team_members()` 规范化，最多取 5 个成员，避免团队过大导致调用成本失控。

### 运行团队

核心函数是 `run_team()`：

```python
def run_team(objective: str, members: list[dict[str, Any]] | None = None) -> str:
    """工具：顺序运行多个带角色的子 Agent，并汇总团队报告。"""
    if not isinstance(objective, str) or not objective.strip():
        return "Error: objective must be a non-empty string."
    team_members = normalize_team_members(members)
    run = new_team_run(objective, team_members)
    outputs: list[tuple[TeamMember, str, str]] = []

    try:
        for member in team_members:
            member_prompt = (
                f"Team objective:\n{objective.strip()}\n\n"
                f"Your role: {member['role']}\n"
                f"Role instructions:\n{member['prompt']}\n\n"
                "Return a concise role-specific report."
            )
            record = new_task_record(...)
            run["task_ids"].append(record["id"])
            report = run_subagent(record)
            outputs.append((member, record["id"], report))

        final_report = aggregate_team_report(run, outputs)
        run["status"] = "completed"
        run["report"] = final_report
        return f"Team {run['id']} completed.\n\n{final_report}"
```

这个版本故意顺序执行，而不是并发。因为 第十五章 的目标是先把“团队抽象”和“结果汇总”跑通。并发团队可以以后复用 第十三章 的后台任务机制继续扩展。

### 聚合报告

聚合函数很朴素：

```python
def aggregate_team_report(run: TeamRun, member_outputs: list[tuple[TeamMember, str, str]]) -> str:
    lines = [
        f"# Team report: {run['objective']}",
        "",
        "## Members",
    ]
    for member, task_id, report in member_outputs:
        lines.append(f"- {member['name']} ({member['role']}): {task_id}")
    lines.append("\n## Findings")
    for member, task_id, report in member_outputs:
        lines.extend([
            f"### {member['name']} ({member['role']}) — {task_id}",
            report.strip() or "No report.",
            "",
        ])
    return "\n".join(lines).strip()
```

它不会试图“智能裁判”谁对谁错，只负责把不同角色的观察组织起来。这样主 Agent 可以基于团队报告继续决策。

### team 工具

新增三个工具：

- `team_run`：运行一个团队。
- `team_list`：列出团队运行记录。
- `team_read`：读取一次团队运行的完整报告。

`team_run` 的 schema 支持用户自定义成员：

```python
{
  "objective": "审查这个模块的重构方案",
  "members": [
    {"name": "architect", "role": "Architect", "prompt": "关注架构边界"},
    {"name": "tester", "role": "Tester", "prompt": "关注测试策略"}
  ]
}
```

如果不传 `members`，就使用默认三人小队。

## 我踩的坑

### 坑 1：Team 不是 Task

一开始我想把团队也塞进 `TASKS`。但团队是多个 Task 的组合，不是一次模型调用。

如果强行把 Team 当 Task，会丢失成员和子任务之间的关系。所以我单独引入 `TEAM_RUNS`，再用 `task_ids` 关联具体子任务。

### 坑 2：角色必须明确

如果只给每个子 Agent 同一个 prompt，它们会产出非常相似的报告。

所以 `TeamMember` 必须包含 `role` 和 `prompt`。团队协作的价值不在“多跑几次”，而在“用不同视角看同一个问题”。

### 坑 3：先顺序执行，不急着并发

第十三章 已经有后台任务，但 第十五章 我没有直接做并发团队。

原因是并发会引入更多状态同步、错误合并和报告顺序问题。先顺序执行，可以把团队记录、成员提示词和聚合报告打稳。

## 小结

第十五章 的关键词是：**角色化协作**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类系统里，不一定直接叫 Agent Teams，但会有类似模式：

- 一个主 Agent 把调查拆给多个子 Agent。
- 不同子 Agent 使用不同系统提示、工具范围或角色。
- Harness 收集多个结果，压缩或汇总后交还主 Agent。
- 复杂任务中常见 research / review / test / implementation 角色分工。

我这个最小实现对应的是：

```text
team objective -> role-specific subagents -> tracked tasks -> aggregated report
```

和真实系统相比，它还很简单：

- 团队成员顺序执行，没有并发。
- 聚合只是格式化，不做二次模型总结。
- 没有角色专属工具权限。
- 没有成员之间互相通信。
- 没有投票、裁决或冲突解决。

但核心思想已经出现：

> 多 Agent 协作不是简单复制多个模型调用，而是 Harness 用角色、任务记录和聚合机制组织出来的协作流程。


Subagent 解决“派一个人调查”；Task System 解决“记录这个人做了什么”；Agent Teams 解决“派一组不同角色的人，从多个角度看同一个问题”。

有了 `team_run`、`team_list` 和 `team_read`，Agent Harness 开始从单一执行者走向小型团队协作。