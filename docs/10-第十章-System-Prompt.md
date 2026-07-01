# 第 10 章：System Prompt

## 本章解决什么问题？

前 9 天我一直在往 Agent 里加能力：工具调用、权限、Hook、TodoWrite、Subagent、Skill Loading、Context Compact、Memory。

这些能力加完之后，一个新的问题出现了：**谁来告诉模型这些能力怎么用？**

最开始我只有一段很短的 system prompt：

```text
You are a coding agent working in ...
Use the available tools to solve the user's task.
```

这在 Day 01 够用，但到了 Day 10 就不够了。因为模型需要知道：

- 它是谁。
- 当前工作目录在哪里。
- 什么时候应该写 todo。
- 什么时候应该启动 subagent。
- 什么时候应该加载 skill。
- 什么时候可以写 memory，什么时候不能写。
- 工具调用有哪些边界。
- 旧 Memory 和当前用户指令冲突时听谁的。

如果把这些内容都手写成一个越来越长的字符串，问题会很快失控：

1. 规则之间没有结构，后续很难维护。
2. Memory、Skill 这类动态内容很难插入到合适位置。
3. 上下文预算不清楚，prompt 可能无限膨胀。
4. 不同来源的指令没有优先级，冲突时模型容易乱。

所以 Day 10 我把 system prompt 从“一段字符串”升级成“可组装的控制面”。

> System Prompt 不只是提示词文本，而是 Harness 在每轮模型调用前，根据身份、规则、工具、Memory、Skill 等来源动态组装出来的运行时配置。

## 核心概念

Day 10 的流程是：

```text
identity / behavior / tool policy / memory / skills
  -> prompt sections
  -> sort by priority
  -> trim by budget
  -> render final system prompt
  -> model call
```

这里有三个关键概念。

第一，section。

system prompt 不再是一整段不可拆的字符串，而是多个片段：

- `identity`：Agent 身份和工作目录。
- `core_behavior`：基础行为规则。
- `tool_policy`：工具使用边界。
- `persistent_memory`：Day 09 的持久化记忆。
- `active_skills`：Day 07 的动态技能摘要。

第二，priority。

不同 section 有不同优先级。身份和核心规则应该先出现；Memory 和 Skill 是上下文增强，应该排在后面。

这不是说排在后面的内容“不重要”，而是当 prompt 预算紧张时，基础行为规则比动态背景更不能丢。

第三，budget。

每个 section 都有自己的字符预算，总 prompt 也有总预算。真实系统会按 token 估算，我这里仍然用字符数近似，和 Day 08 的 Context Compact 保持一致。

## 我的实现

完整实现见：`code/s10_system_prompt.py`

Day 10 的实现继续基于 Day 09，所以 Memory、Skill、Compact 等机制都还在。变化集中在 system prompt 的构造方式。

### 拆分静态规则

Day 09 之前，我用一个 `SYSTEM` 字符串承载所有静态规则。

Day 10 我把它拆成三块：

```python
BASE_IDENTITY = f"You are a coding agent working in {WORKDIR}."

CORE_BEHAVIOR = """Use the available tools to solve the user's task.
For multi-step work, maintain a visible task list with todo_write.
When you need focused research, use task to launch a subagent.
Prefer small, reversible edits and explain important trade-offs.
The todo list must have at most one in_progress item."""

TOOL_POLICY = """Tool rules:
- Use read_file before editing unfamiliar files.
- Use write_file or edit_file only for workspace files.
- Use list_skills and read_skill when a relevant skill may help.
- Use remember to persist stable user preferences, project conventions, or important facts.
- Do not store secrets in Memory."""
```

这一步看起来只是重构，但它很重要。

因为从这一刻开始，system prompt 不再是“写死的一段话”，而是由 Harness 维护的一组策略模块。

### PromptSection

我定义了一个最小结构：

```python
class PromptSection(TypedDict):
    """System Prompt 的一个可组装片段。"""

    name: str
    priority: int
    content: str
    budget: int
```

它有四个字段：

- `name`：方便调试，也会作为 Markdown 标题渲染。
- `priority`：决定拼接顺序。
- `content`：真正注入模型的内容。
- `budget`：该 section 最多占多少字符。

### 收集 section

然后我写了 `build_prompt_sections()`：

```python
def build_prompt_sections(active_skills: list[Skill]) -> list[PromptSection]:
    """收集本轮 system prompt 需要的全部 section。"""
    raw_sections = [
        make_section("identity", 10, BASE_IDENTITY, 1_000),
        make_section("core_behavior", 20, CORE_BEHAVIOR, 3_000),
        make_section("tool_policy", 30, TOOL_POLICY, 3_000),
        make_section("persistent_memory", 40, build_memory_block(), 5_000),
        make_section("active_skills", 50, format_skill_summaries(active_skills), 5_000),
    ]
    return [section for section in raw_sections if section is not None]
```

这里能看到 Day 07 和 Day 09 的机制都被纳入了 system prompt 组装流程：

- Skill Loading 产生 `active_skills`。
- Memory 产生 `persistent_memory`。

这就是 Harness 的核心工作：把分散在系统各处的状态和规则，整理成模型当下能理解的上下文。

### 按预算渲染

渲染逻辑分两步。

先裁剪单个 section：

```python
def trim_to_budget(content: str, budget: int) -> str:
    """按字符预算裁剪 section，保留开头并明确标注截断。"""
    if budget <= 0 or len(content) <= budget:
        return content
    marker = "\n... (section truncated by prompt budget)"
    return content[: max(0, budget - len(marker))].rstrip() + marker
```

再组装最终 prompt：

```python
def render_prompt_sections(sections: list[PromptSection]) -> str:
    """按优先级和预算把 sections 渲染成最终 system prompt。"""
    rendered: list[str] = []
    used = 0
    for section in sorted(sections, key=lambda item: item["priority"]):
        remaining = PROMPT_BUDGET_CHARS - used
        if remaining <= 0:
            break
        budget = min(section["budget"], remaining)
        content = trim_to_budget(section["content"], budget)
        rendered.append(f"## {section['name']}\n{content}")
        used += len(content)
    return "\n\n".join(rendered)
```

最后，外部仍然只调用一个函数：

```python
def build_system_prompt(active_skills: list[Skill]) -> str:
    """把身份、规则、Memory、Skill 组装成最终 system prompt。"""
    return render_prompt_sections(build_prompt_sections(active_skills))
```

这个接口保持简单，Agent Loop 不需要知道里面有多少 section。

### show_system_prompt 工具

为了调试，我还加了一个只读工具：

```python
def run_show_system_prompt() -> str:
    """工具：查看当前不含本轮 Skill 匹配的 system prompt。"""
    return build_system_prompt([])[:50_000]
```

它的作用不是让模型天天调用，而是在开发 Harness 时检查：最终注入模型的 system prompt 到底长什么样。

这类可观测性很重要。因为 system prompt 一旦动态组装，如果没有查看入口，调试会非常痛苦。

## 我踩的坑

### 坑 1：差点继续堆一个巨大的 SYSTEM 字符串

最省事的做法是继续往 `SYSTEM` 后面拼：

```python
SYSTEM = "..." + memory + skills + more_rules
```

但这样很快会变成“字符串意大利面”。

当规则越来越多时，我会搞不清楚：

- 哪些是身份规则？
- 哪些是工具规则？
- 哪些是动态上下文？
- 哪些可以裁剪？
- 哪些永远不能丢？

所以我强行把它拆成 section。拆完之后，后续要加规则，只需要新增一个 section，而不是修改一大段字符串。

### 坑 2：预算不能只看总长度

一开始我只想做一个总预算：超过 `PROMPT_BUDGET_CHARS` 就整体裁剪。

但整体裁剪会有一个问题：如果 Memory 很长，可能把 Skill 或工具规则挤掉；如果 Skill 很长，可能把核心行为规则挤掉。

所以我给每个 section 都加了自己的 `budget`。

这样即使 Memory 变大，它最多只占 `persistent_memory` 的预算，不会吞掉整个 system prompt。

### 坑 3：动态 prompt 必须可观测

System prompt 动态组装之后，模型行为变差时很难判断原因：

- 是 Memory 没注入？
- 是 Skill 没选中？
- 是规则被截断？
- 是 section 顺序不对？

所以我加了 `show_system_prompt` 工具。

这让我可以直接看到最终 prompt，而不是靠猜。

## 对应真实 Claude Code 的哪里

真实 Claude Code / Codex 类系统里，system prompt 往往不是单个手写字符串，而是由多种上下文拼出来的：

- 基础身份和安全边界。
- 工具使用说明。
- 当前工作目录和环境信息。
- 用户/项目级记忆文件。
- 可用 Skill 或 slash command 说明。
- 当前会话状态、todo 状态、权限模式。
- 平台和 shell 差异。
- 只在特定场景出现的开发者指令。

这些内容来自不同地方，但最终都会汇入模型调用的上下文。

我这个 Day 10 最小实现对应的是这条链路：

```text
Harness state + rules + dynamic context
  -> prompt sections
  -> final system prompt
  -> model call
```

和真实系统相比，我的实现很简化：

- 用字符数而不是 token 预算。
- 没有复杂的 instruction hierarchy。
- 没有多级 system/developer/user 消息合并策略。
- 没有按模型类型生成不同 prompt。
- 没有 prompt A/B、版本号、遥测和灰度。

但核心思想已经出现：

> System prompt 是 Harness 的控制面。它把工程系统的规则、状态和能力翻译成模型可执行的上下文。

## 小结

Day 10 的关键词是：**结构化组装**。

前几章我一直在加能力；这一章我开始整理“如何把这些能力告诉模型”。

一个 Agent 强不强，不只取决于有哪些工具，也取决于 Harness 能不能在每轮调用前，把身份、规则、状态、记忆、技能，以清晰、有优先级、受预算控制的方式放进 system prompt。
