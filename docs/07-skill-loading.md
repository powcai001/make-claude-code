# 第 07 章：Skill Loading

## 本章解决什么问题？

Day 06 实现 Subagent 后，Agent 已经可以把一部分调查工作委派出去，避免主上下文被搜索细节塞满。

但还有一个更常见的上下文问题：**系统提示词会越长越大**。

如果我想让 Agent 同时擅长 Python、文档写作、前端、数据库、测试、部署，最粗暴的做法是把所有领域规则都写进一个巨大 system prompt。

这样很快会出现三个问题：

1. 每次请求都浪费上下文，即使当前任务根本不需要这些规则。
2. 不同领域的指令可能互相干扰。
3. 新增能力必须改核心 system prompt，扩展性很差。

所以 Day 07 我实现一个最小版 Skill Loading。

它的核心思想是：

> 技能不是一直塞在 system prompt 里的长文本，而是按任务动态加载的指令包。

## 核心概念

这一章的流程是：

```text
user prompt
  -> discover workspace skills
  -> select relevant skills
  -> inject compact skill summaries into system prompt
  -> read full skill only when needed
```

Skill 在这里不是插件，也不是 Python 代码。

它只是一个 Markdown 指令文件：

```text
skills/
  python/
    SKILL.md
  docs/
    SKILL.md
```

一个最简单的 `SKILL.md` 可以长这样：

```markdown
---
name: python
description: Help with Python code editing and testing
---

# Python Skill

When editing Python code, prefer small functions, run py_compile, and keep errors explicit.
```

Harness 会做三件事：

1. 扫描工作区里的 `skills/**/SKILL.md`。
2. 根据用户输入选出几个相关 skill。
3. 只把 skill 的名字、描述和路径注入 system prompt。

完整 skill 内容不直接塞进 system prompt，而是通过 `read_skill(name)` 按需读取。

这和 Day06 的 Subagent 有点像：核心都是控制上下文。

- Subagent 控制“调查过程”不要污染主上下文。
- Skill Loading 控制“领域指令”不要污染每一次请求。

## 我的实现

完整实现见：`code/s07_skill_loading.py`

我先定义了最小的 Skill 结构：

```python
class Skill(TypedDict):
    """从 SKILL.md 解析出来的最小技能元数据。"""

    name: str
    description: str
    path: str
    content: str
    keywords: list[str]


SKILLS_DIR = WORKDIR / "skills"
LOADED_SKILLS: dict[str, Skill] = {}
```

这里我故意只扫描工作区内的 `skills/` 目录。

虽然我的开发环境里也有全局 ZCode skills，但这个项目的示例代码不应该依赖我的机器状态。否则换一台电脑运行，结果就不一样了。

### 发现和解析 Skill

`discover_skills` 只做只读扫描：

```python
def discover_skills(root: Path = SKILLS_DIR) -> dict[str, Skill]:
    """发现工作区内的 skills/**/SKILL.md；目录不存在时返回空字典。"""
    if not root.exists() or not root.is_dir():
        return {}

    discovered: dict[str, Skill] = {}
    for path in sorted(root.glob("**/SKILL.md")):
        try:
            resolved = path.resolve()
            if resolved != WORKDIR and WORKDIR not in resolved.parents:
                continue
            skill = parse_skill_file(resolved)
        except Exception:
            continue
        discovered[skill["name"]] = skill
    return discovered
```

`parse_skill_file` 支持两种格式。

第一种是带 frontmatter：

```markdown
---
name: docs
description: Help write clear project documentation
---
```

第二种是普通 Markdown：没有 metadata 时，就从目录名、标题或第一段正文推导。

### 选择 Skill

Day07 不引入 embeddings，也不做复杂召回，只用简单关键词打分：

```python
def select_skills(query: str, skills: dict[str, Skill], limit: int = 3) -> list[Skill]:
    """从已发现技能中选出和当前用户输入最相关的几个。"""
    scored = [(score_skill(query, skill), skill["name"], skill) for skill in skills.values()]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [skill for score, _name, skill in scored if score > 0][:limit]
```

这个实现很朴素，但它有一个好处：行为确定、容易理解、容易调试。

Day07 的重点不是检索算法，而是 Harness 里多了一个新的阶段：

```text
构造上下文之前，先决定本轮需要哪些技能。
```

### 注入 system prompt

我没有把完整 `SKILL.md` 全部注入 system prompt，而是只注入摘要：

```python
def build_system_prompt(active_skills: list[Skill]) -> str:
    """根据本轮选中的技能动态构造 system prompt。"""
    skill_block = format_skill_summaries(active_skills)
    if not skill_block:
        return SYSTEM
    return f"{SYSTEM}\n\n{skill_block}"
```

摘要大概长这样：

```text
Relevant skills are available for this turn:
Use read_skill(name) if you need the full instructions before applying a skill.
- python: Help with Python code editing and testing (skills/python/SKILL.md)
```

这样模型知道“有这个技能”，但完整内容要在需要时通过工具读取。

### 新增工具

我新增了两个只读工具：

```python
"list_skills": lambda **kwargs: run_list_skills(),
"read_skill": lambda **kwargs: run_read_skill(kwargs["name"]),
```

主 Agent 可以使用它们，子 Agent 也可以使用它们。

但子 Agent 仍然不能使用：

```text
write_file / edit_file / todo_write / task
```

这延续了 Day06 的边界：子 Agent 可以调查和读取技能，但不能写文件，也不能递归创建更多子 Agent。

### Agent Loop 的变化

Day07 最关键的变化发生在用户输入之后：

```python
trigger_hooks("UserPromptSubmit", query)
skills = refresh_skills()
active_skills = select_skills(query, skills)
if active_skills:
    names = ", ".join(skill["name"] for skill in active_skills)
    print(f"\033[90m[skills] active: {names}\033[0m")
history.append({"role": "user", "content": query})
agent_loop(history, build_system_prompt(active_skills))
```

也就是说，system prompt 不再是启动程序时固定死的字符串，而是每一轮根据用户输入动态构造。

这就是 Skill Loading 的核心位置：

```text
用户输入进入模型之前，Harness 先做上下文装配。
```

## 我踩的坑

### 坑 1：把所有 Skill 全塞进 system prompt

最简单的实现是：扫描到几个 `SKILL.md`，就把它们完整拼到 system prompt 后面。

但这很快会把上下文撑爆。

更糟的是，当前任务可能只需要一个 Python skill，却被迫带上文档、前端、部署、数据库等所有说明。

所以我改成只注入 compact summary，完整内容按需读取。

### 坑 2：把 Skill 当成插件执行

Skill Loading 很容易被误解成“加载插件”。

但这一章里，Skill 只是指令，不是代码。

Harness 只读取 Markdown，不执行里面的任何命令，也不会 import 任何 Python 文件。

这是一个重要的安全边界：

```text
Skill = instructions, not executable code.
```

### 坑 3：扫描全局技能目录

我的机器上有全局 ZCode skills。如果示例代码默认扫描这些目录，看起来会更“强”。

但这会让项目不可复现。

别人 clone 这个仓库以后，运行结果取决于他本机装了哪些全局技能，这不是一个好的教学实现。

所以 Day07 默认只扫描：

```text
workspace/skills/**/SKILL.md
```

### 坑 4：没有 skills 目录时报错

最小实现必须能在空仓库里运行。

如果没有 `skills/` 目录，`discover_skills` 不能报错，而应该返回空字典：

```python
if not root.exists() or not root.is_dir():
    return {}
```

这样 Skill Loading 是一个可选增强，而不是启动前置条件。

## 对应真实 Claude Code 的哪里

真实 Claude Code / Codex 这类 Agent Harness 里，都会有类似“按需加载能力说明”的机制。

用户看到的是一个 Agent，但 Harness 背后会根据任务类型加载不同的说明、工具描述、技能文档或项目规则。

这类机制的价值是：

1. 不让 system prompt 无限膨胀。
2. 不同任务只加载相关指令。
3. 技能可以独立维护和安装。
4. Harness 可以控制哪些技能可信、哪些技能可见、哪些技能需要按需读取。

我这一章的实现和真实系统的相同点是：

- 都把技能当成外部指令包。
- 都在模型调用前参与 context construction。
- 都倾向于先注入摘要，再按需读取完整内容。
- 都需要处理技能发现、选择和读取。

不同点是：

- 真实系统有更完整的 metadata、版本、安装和冲突处理。
- 真实系统可能支持用户级、项目级、插件级多层技能来源。
- 真实系统有更强的信任边界和权限模型。
- 我这里只做工作区本地 Markdown 文件，不执行插件代码。

这些简化是故意的。

Day07 的目标不是做一个完整插件市场，而是让 Harness 多一个关键能力：

```text
根据任务动态装配上下文。
```

## 小结

本章实现了一个最小版 Skill Loading。

现在我的 Agent Harness 变成了：

```text
Agent Loop
+ Tool System
+ Permission Gate
+ Lifecycle Hooks
+ TodoWrite
+ Subagent / Task
+ Skill Loading
```

Skill Loading 的核心思想是：

> 不要把所有能力都永久塞进 system prompt；让 Harness 在每一轮根据任务选择需要的指令包。
