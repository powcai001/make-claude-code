# 第 07 章：Skill Loading

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s07_skill_loading.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s07_skill_loading.py)


## 本章解决什么问题？

第 06 章 实现 Subagent 后，Agent 已经可以把一部分调查工作委派出去，避免主上下文被搜索细节塞满。

但还有一个更常见的上下文问题：**系统提示词会越长越大**。

如果我想让 Agent 同时擅长 Python、文档写作、前端、数据库、测试、部署，最粗暴的做法是把所有领域规则都写进一个巨大 system prompt。

这样很快会出现三个问题：

1. 每次请求都浪费上下文，即使当前任务根本不需要这些规则。
2. 不同领域的指令可能互相干扰。
3. 新增能力必须改核心 system prompt，扩展性很差。

所以 第 07 章 我实现一个最小版 Skill Loading。

它的核心思想是：

> 技能不是一直塞在 system prompt 里的长文本，而是按任务动态加载的指令包。

知乎上讨论 Claude Code Skill 时常用一个词：**progressive disclosure（渐进式披露）**。意思是不要一开始就把所有知识全量塞进上下文，而是先给模型一份"菜单"（技能名 + 描述），等真正需要时再读取完整内容。这和"超长 system prompt 一把梭"相比，前期稍重，但更利于长期维护和上下文预算控制。

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

这和第 06 章的 Subagent 有点像：核心都是控制上下文。

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

第 07 章 不引入 embeddings，也不做复杂召回，只用简单关键词打分：

```python
def select_skills(query: str, skills: dict[str, Skill], limit: int = 3) -> list[Skill]:
    """从已发现技能中选出和当前用户输入最相关的几个。"""
    scored = [(score_skill(query, skill), skill["name"], skill) for skill in skills.values()]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [skill for score, _name, skill in scored if score > 0][:limit]
```

这个实现很朴素，但它有一个好处：行为确定、容易理解、容易调试。

第 07 章 的重点不是检索算法，而是 Harness 里多了一个新的阶段：

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

这延续了第 06 章的边界：子 Agent 可以调查和读取技能，但不能写文件，也不能递归创建更多子 Agent。

### Agent Loop 的变化

第 07 章 最关键的变化发生在用户输入之后：

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

运行方式：

```bash
python code/s07_skill_loading.py
```

第 07 章的 `skills/` 目录下有两个示例技能：`python` 和 `docs`。试一个和 Python 相关的任务：

```text
帮我写一个 Python 函数，计算斐波那契数列第 n 项
```

你会看到 Harness 在用户输入进入模型前，先选出相关技能：

```text
[skills] active: python
```

这表示本轮 system prompt 里被注入了 Python 技能的摘要（名字 + 描述 + 路径），但完整内容没有塞进去。如果模型需要完整指令，它会调用 `read_skill("python")` 按需读取。

如果换一个文档类任务：

```text
帮我写一段项目介绍
```

你会看到：

```text
[skills] active: docs
```

这就是这一章想让用户"感受到"的东西：**Harness 每一轮根据任务动态装配上下文，而不是把所有规则一次性全量塞进 system prompt。**

## 我踩的坑

这一章的坑，抓住几件事就够了：

- **不要把所有 Skill 全塞进 system prompt。** 扫描到几个 SKILL.md 就全量拼接，很快会把上下文撑爆，而且当前任务可能只需要一个技能。正确做法是只注入摘要，完整内容按需读取。
- **Skill 是指令，不是可执行插件。** Harness 只读取 Markdown，不执行里面的命令，也不 import Python 文件。`Skill = instructions, not executable code` 是一个重要的安全边界。
- **不要扫描全局技能目录。** 如果默认扫描机器上的全局 skills，项目就不可复现——别人 clone 后运行结果取决于他本机装了什么。第 07 章只扫描工作区内的 `skills/`。
- **没有 skills 目录时不能报错。** `discover_skills` 在目录不存在时返回空字典，让 Skill Loading 是可选增强，而不是启动前置条件。

## 小结

本章实现了一个最小版 Skill Loading。

它的核心不是检索算法，而是 Harness 里多了一个阶段：**构造上下文之前，先决定本轮需要哪些技能。**

```text
用户输入 -> 发现技能 -> 选择相关技能 -> 注入摘要 -> 按需读取完整内容
```

真实 Claude Code / Codex 都有类似的按需加载机制。真实系统会更完整：有多层 metadata、版本管理、用户级/项目级/插件级多层技能来源、更严格的信任边界和权限模型。但核心都是 progressive disclosure：**先给菜单，再按需上菜，而不是一开始把厨房搬上桌。**

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

下一章要解决的问题是：对话越来越长之后，messages 会撞上上下文窗口上限。怎么在不丢失关键信息的前提下压缩历史？这就是 Context Compact。
