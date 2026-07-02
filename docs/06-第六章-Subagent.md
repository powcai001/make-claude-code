# 第 06 章：Subagent

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s06_subagent.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s06_subagent.py)


## 本章解决什么问题？

第五章 加上 `TodoWrite` 以后，Agent 已经能维护一份可见计划了。

但计划可见之后，还会遇到另一个问题：**主上下文太容易被调查细节塞满**。

比如用户说：

```text
帮我理解这个仓库的 Day05 是怎么实现的，然后基于它继续做 第六章。
```

这类任务通常要先读 README、读上一章代码、读上一章文档、对比当前骨架文件。真正有价值的是最后的结论：第六章 应该做什么、怎么做、哪些文件要改。

如果所有搜索过程、文件片段、临时推理都塞进主 Agent 的上下文，主 Agent 很快就会被噪音淹没。

所以 第六章 我实现一个最小版 Subagent：

> 主 Agent 可以把一个聚焦的调查任务交给子 Agent，子 Agent 独立阅读和分析，最后只把报告返回给主 Agent。

它解决的不是“并发执行”问题，而是“上下文隔离”和“任务委派”问题。

知乎上关于 Claude Code Subagent 的讨论里，经常把它的价值拆成四点：节省主上下文、限制工具权限、让子 Agent 专门化、必要时用更便宜的模型跑探索任务。第六章先不做模型路由和并发，只抓最核心的两个：**独立上下文 + 受限工具**。

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

这就是 第六章 的核心：

```text
同一个 Harness，不同的上下文，不同的工具边界。
```

运行方式：

```bash
python code/s06_subagent.py
```

试一个适合委派的任务：

```text
请用 task 调查第三章 Permission 是怎么实现的，只返回结论报告，不要修改文件。
```

你会看到主 Agent 调用 `task` 工具，类似：

```text
> task: {
  'description': '调查 Permission 实现',
  'prompt': '阅读第三章文档和 code/s03_permission.py，总结 Permission 的核心机制...'
}
```

子 Agent 会在自己的 messages 里读文件、分析代码，最后只把报告作为 `tool_result` 返回给主 Agent。主 Agent 不需要背负子 Agent 中间读了哪些文件、尝试了哪些命令、产生了多少临时推理。

这就是这一章想让用户"感受到"的东西：**把调查细节隔离出去，只把结论带回主上下文。**

## 我踩的坑

这一章的坑，抓住几件事就够了：

- **子 Agent 不能继续拥有 `task`。** 否则就可能出现主 Agent -> 子 Agent -> 孙 Agent 的无限套娃。工具层直接不给它 `task`，比依赖模型自觉更可靠。
- **子 Agent 不能复用主 Agent 的 messages。** 否则调查过程、工具结果、错误重试都会污染主上下文。Subagent 的意义就是独立上下文，最后只返回报告。
- **子 Agent 的工具要受限。** 本章让它只读和执行命令，不给 `write_file/edit_file/todo_write`。用户以为只是“调查一下”，子 Agent 就不应该偷偷改项目。
- **子 Agent 也要有预算。** 它本质上还是一个 Agent Loop，所以必须有 `max_turns`。没有轮数上限，调查任务也可能永远不返回。

## 小结

本章实现了一个最小版 Subagent。

它的核心不是“多开一个模型”，而是：

```text
Subagent = 独立上下文 + 受限工具 + 最终报告
```

从这一章开始，Agent 不再只能自己读完所有上下文，而是可以把可隔离的调查工作委派出去。主 Agent 负责目标和决策，子 Agent 负责局部探索，最后只把压缩后的结论带回主上下文。

真实 Claude Code 的 Subagent / AgentTool 更复杂：它有不同类型的子 Agent（Explore、Plan、General-Purpose 等）、可选的同步/异步执行、独立 abort controller、上下文 fork、权限冒泡、worktree 文件系统隔离，甚至后面会扩展到多 Agent 团队。但这些能力都建立在同一个基础上：**子 Agent 有自己的上下文和工具边界，父 Agent 不应该被所有细节污染。**

现在我的 Agent Harness 变成了：

```text
Agent Loop
+ Tool System
+ Permission Gate
+ Lifecycle Hooks
+ TodoWrite
+ Subagent / Task
```

下一章要解决的问题是：当能力越来越多时，如何按需加载专门说明，而不是把所有规则都塞进 system prompt？这就是 Skill Loading。
