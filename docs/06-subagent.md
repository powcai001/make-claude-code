# 第 06 章：Subagent

## 本章解决什么问题？

Day 05 加上 `TodoWrite` 以后，Agent 已经能维护一份可见计划了。

但计划可见之后，还会遇到另一个问题：**主上下文太容易被调查细节塞满**。

比如用户说：

```text
帮我理解这个仓库的 Day05 是怎么实现的，然后基于它继续做 Day06。
```

这类任务通常要先读 README、读上一章代码、读上一章文档、对比当前骨架文件。真正有价值的是最后的结论：Day06 应该做什么、怎么做、哪些文件要改。

如果所有搜索过程、文件片段、临时推理都塞进主 Agent 的上下文，主 Agent 很快就会被噪音淹没。

所以 Day 06 我实现一个最小版 Subagent：

> 主 Agent 可以把一个聚焦的调查任务交给子 Agent，子 Agent 独立阅读和分析，最后只把报告返回给主 Agent。

它解决的不是“并发执行”问题，而是“上下文隔离”和“任务委派”问题。

## 核心概念

Subagent 在这一章里的抽象是：

```text
主 Agent
  -> 调用 task 工具
  -> Harness 创建一个新的子 Agent Loop
  -> 子 Agent 使用自己的 messages 和受限工具调查
  -> 子 Agent 返回最终报告
  -> 主 Agent 继续决策
```

这里有三个关键点。

第一，子 Agent 不是一个神秘的新模型。

它本质上还是同一个 Agent Loop，只是换了一份系统提示词、换了一份消息上下文、换了一组工具边界。

第二，子 Agent 必须有独立上下文。

主 Agent 不需要看到子 Agent 中间读了多少文件、跑了多少命令、犯了多少次错。主 Agent 只需要最后的汇报。

第三，子 Agent 必须有工具边界。

我这一章只给子 Agent 两个工具：

```text
read_file / bash
```

不给它：

```text
write_file / edit_file / todo_write / task
```

这样可以避免三个问题：

1. 子 Agent 在“调查任务”里偷偷改文件。
2. 子 Agent 污染主 Agent 的 todo list。
3. 子 Agent 继续创建子 Agent，递归委派到失控。

## 我的实现

完整实现见：`code/s06_subagent.py`

主 Agent 新增了一个工具：`task`。

```python
{
    "name": "task",
    "description": (
        "Launch a focused subagent for research or analysis. "
        "The subagent has its own short conversation and returns a concise report."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short description shown in logs.",
            },
            "prompt": {
                "type": "string",
                "description": "Detailed instructions for the subagent.",
            },
        },
        "required": ["description", "prompt"],
    },
}
```

主 Agent 调用它时，走的仍然是普通工具分发：

```python
TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": lambda **kwargs: run_bash(kwargs["command"]),
    "read_file": lambda **kwargs: run_read(kwargs["path"], kwargs.get("limit")),
    "write_file": lambda **kwargs: run_write(kwargs["path"], kwargs["content"]),
    "edit_file": lambda **kwargs: run_edit(
        kwargs["path"],
        kwargs["old_text"],
        kwargs["new_text"],
    ),
    "todo_write": lambda **kwargs: run_todo_write(kwargs["todos"]),
    "task": lambda **kwargs: run_task(kwargs["description"], kwargs["prompt"]),
}
```

也就是说，从主 Agent Loop 看，Subagent 没有特殊地位。

它只是一次工具调用：

```text
tool_use(task) -> tool_result
```

真正启动子 Agent 的函数是 `run_subagent`：

```python
def run_subagent(description: str, prompt: str, max_turns: int = 6) -> str:
    """启动一个独立上下文的子 Agent，并返回它的最终调查报告。"""
    if not isinstance(description, str) or not description.strip():
        return "Error: description must be a non-empty string."
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt must be a non-empty string."
    if not MODEL:
        return "Error: MODEL_ID is not set."

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Task description: {description.strip()}\n\n"
                f"Detailed prompt:\n{prompt.strip()}\n\n"
                "When you are done, return only the final report."
            ),
        }
    ]

    last_text = ""
    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=messages,
            tools=SUBAGENT_TOOLS,
            max_tokens=4000,
        )
        messages.append({"role": "assistant", "content": response.content})
        last_text = extract_text(response.content)

        if response.stop_reason != "tool_use":
            return (last_text or "Subagent finished without text output.")[:50_000]

        # 后面和主 Agent Loop 类似：执行子 Agent 的工具，再把 tool_result 回填。
```

这里最重要的一行是：

```python
messages: list[dict[str, Any]] = [...]
```

它创建的是一份新的消息列表，不是复用主 Agent 的 `messages`。

子 Agent 可用的工具也单独定义：

```python
SUBAGENT_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": lambda **kwargs: run_bash(kwargs["command"]),
    "read_file": lambda **kwargs: run_read(kwargs["path"], kwargs.get("limit")),
}

SUBAGENT_TOOLS = [tool for tool in TOOLS if tool["name"] in {"bash", "read_file"}]
```

所以即使主 Agent 有 `write_file / edit_file / todo_write / task`，子 Agent 也看不到这些工具。

这就是 Day06 的核心：

```text
同一个 Harness，不同的上下文，不同的工具边界。
```

## 我踩的坑

### 坑 1：让子 Agent 继续拥有 `task`

如果子 Agent 也能调用 `task`，它就可以继续创建子 Agent。

这听起来像“更强的能力”，但在最小实现里很危险：

```text
主 Agent -> 子 Agent -> 孙 Agent -> 曾孙 Agent -> ...
```

一旦模型开始递归委派，很容易耗尽 token、超出轮数，甚至让用户完全看不懂当前任务到底是谁在执行。

所以我在 Day06 明确禁止子 Agent 使用 `task`。

### 坑 2：子 Agent 和主 Agent 共用 messages

最简单的写法是直接把主 Agent 的 `messages` 传给子 Agent。

但这样就失去了 Subagent 的意义。

子 Agent 的中间搜索过程会全部进入主上下文：读文件、工具结果、临时推理、错误重试……主 Agent 不仅没有减负，反而更乱。

所以 `run_subagent` 里必须创建新的 messages：

```python
messages: list[dict[str, Any]] = [
    {
        "role": "user",
        "content": (...),
    }
]
```

最后只把子 Agent 的最终报告作为 `tool_result` 返回给主 Agent。

### 坑 3：给子 Agent 写权限

如果子 Agent 能写文件，就会出现一个很难解释的问题：

用户以为主 Agent 只是“派人调查一下”，结果子 Agent 已经修改了项目。

这会破坏权限模型。

Day03 做 Permission 时已经有一个隐含原则：所有会改变环境的行为都应该清楚地暴露给用户。

所以 Day06 里我让子 Agent 只做研究：可以读文件，可以运行命令，但不直接写文件。

真正的修改仍然由主 Agent 决策和执行。

### 坑 4：不限制轮数

子 Agent 也是 Agent Loop，所以它也可能一直调用工具。

如果没有 `max_turns`，一个调查任务可能永远不返回。

所以我在 `run_subagent` 里加了：

```python
for turn in range(1, max_turns + 1):
```

超过轮数就返回错误和最后的 partial output。

这个限制让 Subagent 成为一个可控工具，而不是失控的内循环。

## 对应真实 Claude Code 的哪里

真实 Claude Code 里也有类似的 Task/Subagent 能力。

用户看起来是在和一个 Claude 交互，但 Claude 可以把某些工作委派给专门的子 Agent，例如搜索、分析、定位代码、总结结果。

它的关键价值是：

1. 主 Agent 不需要背负所有搜索细节。
2. 子 Agent 可以围绕一个清晰目标独立工作。
3. 最终只把压缩过的结论返回主 Agent。
4. Harness 可以控制子 Agent 的工具、权限和生命周期。

我这一章的实现和真实 Claude Code 的相同点是：

- 都通过一个工具触发子 Agent。
- 子 Agent 都有独立上下文。
- 子 Agent 最终返回报告给主 Agent。
- 子 Agent 的能力由 Harness 控制，而不是无限开放。

不同点是：

- 真实 Claude Code 有更多 agent type 和调度策略。
- 真实系统可能支持更复杂的并行、取消、展示和权限冒泡。
- 我这里没有后台任务，也没有多 Agent 团队。
- 我这里的子 Agent 只做研究，不做写操作。

这些简化是为了让 Day06 聚焦一个核心机制：

```text
Subagent = 独立上下文 + 受限工具 + 最终报告
```

## 小结

本章实现了一个最小版 Subagent。

现在我的 Agent Harness 变成了：

```text
Agent Loop
+ Tool System
+ Permission Gate
+ Lifecycle Hooks
+ TodoWrite
+ Subagent / Task
```

从这一章开始，Agent 不再只能自己读完所有上下文，而是可以把一部分调查工作委派出去。

Subagent 的核心思想是：

> 复杂任务不要把所有细节都塞进主上下文；把可隔离的调查交给子 Agent，主 Agent 只接收结论。
