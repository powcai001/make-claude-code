# 第 16 章：Team Protocols

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s16_team_protocols.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s16_team_protocols.py)


## 本章解决什么问题？

第 15 章 让 Agent 能组成角色化小队。`team_run` 可以派出 researcher、reviewer、tester 三个子 Agent，再汇总成团队报告。

但用着用着会发现一个问题：**团队流程是临时的**。

每次调用 `team_run`，到底先做什么后做什么，全靠主 Agent 现场发挥。如果是“规划一次重构”，它可能这次先 research 后 plan；下次又先 plan 后 research。同一个问题，每次跑出来的团队流程都不一样。

真实工程里，很多协作是有固定套路的：

- 规划类任务：research → plan → review。
- 决策类任务：propose → challenge → synthesize。
- 安全类任务：threat model → attack → mitigate。

这些套路不应该每次重新发明。所以 第 16 章 我在 Agent Teams 上再加一层 Team Protocols。

> Team Protocols 把团队协作流程沉淀成可复用的协议模板，让 `team_run` 可以按 `plan`、`debate`、`redteam` 等协议走固定的多阶段流程。

## 核心概念

第 16 章 的流程是：

```text
team_run(protocol="debate")
  -> resolve_protocol("debate")
  -> [propose, challenge, synthesize] stages
  -> tracked tasks
  -> protocol report
```

这里有三个关键概念。

第一，ProtocolStage。

协议由多个阶段组成，每个阶段有名字、角色和提示词。阶段不只是“换个角色”，它代表协作流程中的一步。

第二，TeamProtocol。

协议模板把多个阶段串成一个有名字、有描述的流程。内置三个：`plan`、`debate`、`redteam`。

第三，协议优先级。

`team_run` 现在多了一个 `protocol` 参数。如果传了协议名，就走协议定义的阶段；如果传了 `members`，就走自定义团队；都不传就用默认小队。

## 我的实现

完整实现见：`code/s16_team_protocols.py`

第 16 章 继续基于 第 15 章，所以 Agent Teams、Cron Scheduler、Background Tasks、Task System、Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在协议模板、协议解析和协议化团队运行。

### ProtocolStage 和 TeamProtocol

先定义协议结构：

```python
ProtocolName = Literal["plan", "debate", "redteam", "custom"]

class ProtocolStage(TypedDict):
    """Team Protocol 中的一个阶段。"""

    name: str
    role: str
    prompt: str


class TeamProtocol(TypedDict):
    """可复用的团队协作协议模板。"""

    name: ProtocolName
    description: str
    stages: list[ProtocolStage]
```

`TeamRun` 也加了 `protocol` 字段，记录这次运行走的是哪个协议。

### 内置协议

`builtin_team_protocols()` 返回三个模板：

```python
plan_protocol: TeamProtocol = {
    "name": "plan",
    "description": "Plan a change: research, design a plan, then review the plan for risks.",
    "stages": [
        {"name": "research", "role": "Researcher", "prompt": "Map the relevant files..."},
        {"name": "plan", "role": "Planner", "prompt": "Propose a concrete, step-by-step plan..."},
        {"name": "review", "role": "Reviewer", "prompt": "Stress-test the plan..."},
    ],
}
debate_protocol: TeamProtocol = {
    "name": "debate",
    "description": "Debate an approach: propose, challenge, then synthesize.",
    "stages": [
        {"name": "propose", "role": "Proposer", "prompt": "Argue for a specific approach..."},
        {"name": "challenge", "role": "Challenger", "prompt": "Attack the proposal..."},
        {"name": "synthesize", "role": "Synthesizer", "prompt": "Merge the strongest points..."},
    ],
}
redteam_protocol: TeamProtocol = {
    "name": "redteam",
    "description": "Red-team a design: build a threat model, find failures, then mitigate.",
    "stages": [
        {"name": "threat_model", "role": "Threat Modeler", "prompt": "List abuse cases..."},
        {"name": "attack", "role": "Attacker", "prompt": "Describe concrete ways the target could fail..."},
        {"name": "mitigate", "role": "Defender", "prompt": "Propose bounded mitigations..."},
    ],
}
```

Agent 启动时会注册它们：

```python
def main() -> None:
    """启动一个支持 Team Protocols 的最小 Agent。"""
    ensure_memory_file()
    ensure_cron_scheduler_started()
    TEAM_PROTOCOLS.update(builtin_team_protocols())
    print("第 16 章 Team Protocols. ...")
```

### 协议解析

`resolve_protocol()` 负责把协议名翻译成阶段成员：

```python
def resolve_protocol(name: str | None) -> tuple[ProtocolName, list[TeamMember]]:
    """根据协议名解析出协议标签和阶段成员。"""
    if name and name in TEAM_PROTOCOLS:
        protocol = TEAM_PROTOCOLS[name]
        members = [
            {"name": stage["name"], "role": stage["role"], "prompt": stage["prompt"]}
            for stage in protocol["stages"]
        ]
        label: ProtocolName = "custom" if name not in {"plan", "debate", "redteam"} else name
        return label, members
    return "custom", default_team_members()
```

这样 `team_run` 内部不需要关心协议细节，只需要拿到一组带角色的成员。

### 协议化运行

`run_team()` 现在多了一个 `protocol` 参数：

```python
def run_team(objective: str, members: list[dict[str, Any]] | None = None, protocol: str | None = None) -> str:
    """工具：按协议顺序运行多个带角色的子 Agent，并汇总团队报告。"""
    if not isinstance(objective, str) or not objective.strip():
        return "Error: objective must be a non-empty string."
    protocol_label, team_members = resolve_protocol(protocol if not members else None)
    if members:
        team_members = normalize_team_members(members)
        protocol_label = "custom"
    run = new_team_run(objective, team_members, protocol=protocol_label)
```

优先级很明确：传 `members` 就走自定义团队；否则按 `protocol` 走协议；都没有就用默认小队。

每个阶段的提示词现在会带上阶段编号：

```python
member_prompt = (
    f"Team objective:\n{objective.strip()}\n\n"
    f"Protocol: {run['protocol']}\n"
    f"Stage {index + 1}/{len(team_members)} — {member['name']} ({member['role']})\n"
    f"Stage instructions:\n{member['prompt']}\n\n"
    "Return a concise stage-specific report."
)
```

这让子 Agent 知道自己处于协作流程的哪一步，而不是孤立地完成任务。

### protocol_list 工具

新增一个只读工具，列出可用协议：

```python
def run_protocol_list() -> str:
    """工具：列出可用的团队协议模板。"""
    protocols = list(TEAM_PROTOCOLS.values())
    if not protocols:
        return "No team protocols registered."
    lines = ["Available team protocols:"]
    for protocol in protocols:
        roles = ", ".join(stage["role"] for stage in protocol["stages"])
        lines.append(f"- {protocol['name']}: {protocol['description']} (stages: {roles})")
    return "\n".join(lines)
```

主 Agent 可以先查协议，再决定用哪个。

## 我踩的坑

### 坑 1：协议和自定义成员会冲突

`team_run` 同时支持 `protocol` 和 `members`。如果两个都传，到底听谁的？

我定的规则是：`members` 优先。只要传了 `members`，就忽略 `protocol`，标记为 `custom`。这样语义清晰：协议是模板，`members` 是覆盖模板的精确控制。

### 坑 2：协议不能在模块顶层注册

一开始我想在定义 `TEAM_PROTOCOLS` 时直接 `.update(builtin_team_protocols())`。

结果导入就崩了，因为 `builtin_team_protocols()` 定义在文件后面，模块顶层执行时它还不存在。

所以我把注册放到 `main()` 里。这样既保证运行时协议可用，又不会破坏模块导入（测试时可以单独 import 函数）。

### 坑 3：阶段要有顺序感

如果只是给每个子 Agent 一个角色，它们仍可能各做各的。

所以协议阶段的提示词里我加了 `Stage {index + 1}/{total}`。这让子 Agent 知道自己是流程的第几步，更容易产出互补而不是重复的报告。

## 小结

第 16 章 的关键词是：**协议化协作**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类系统里，Team Protocols 对应的是：

- 预设的多 Agent 工作流模板。
- 不同任务类型用不同协作套路（规划、评审、红队、辩论）。
- Harness 把工作流沉淀成可复用配置，而不是每次靠提示词临时编排。
- 阶段化执行：每个阶段有明确角色、输入和输出。

我这个最小实现对应的是：

```text
protocol template -> stages -> tracked tasks -> protocol report
```

和真实系统相比，它还很简单：

- 阶段之间只靠提示词串联，没有显式的阶段输出依赖。
- 没有阶段间结果传递（下一阶段看不到上一阶段的结构化输出，只能靠汇总）。
- 协议只保存在内存，没有持久化或外部配置文件。
- 没有协议级权限或工具范围。

但核心思想已经出现：

> 协作流程本身应该是一等公民。把“怎么做”沉淀成协议，团队才能稳定、可复现地协作。


第 15 章 让 Agent 能组队；第 16 章 让团队知道“按什么套路组队”。

有了 `plan`、`debate`、`redteam` 三个内置协议和 `protocol_list`，同一类问题每次都能走相同的协作流程，团队输出从“随机多视角”变成“稳定可复现的多阶段工作流”。
