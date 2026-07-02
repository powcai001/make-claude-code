# 第 05 章：TodoWrite

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s05_todo_write.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s05_todo_write.py)


## 本章解决什么问题？

第 04 章 结束时，我的 Agent 已经有了一个比较像样的 Harness：模型负责决策，工具负责执行，权限和日志通过 Hooks 挂在生命周期上。

但它还缺一个很关键的东西：**可见的任务状态**。

长流程里，模型最容易遇到的问题不是不会做，而是做着做着忘了自己在哪：目标变模糊、已经完成的步骤被对话淹没、下一步焦点不清楚。知乎上讨论 Claude Code TodoWrite 时常提到三个词：**专注、灵活、透明**。专注，是让模型始终知道当前目标；灵活，是执行中可以动态调整计划；透明，是用户能看见进度，而不是只能相信模型说“我在做”。

如果用户说：

```text
帮我实现第 05 章，写代码、写文档、跑测试、提交并推送。
```

这不是一步任务。模型需要先拆解，再逐步执行。如果计划只写在模型的自然语言回复里，就会有几个问题：

1. Harness 看不见模型当前打算做什么。
2. 用户看不见任务是否跑偏。
3. 对话变长后，模型容易忘记前面已经完成了什么。
4. 多步任务中断后，很难恢复现场。

所以 第 05 章 我先实现一个最小版 `TodoWrite`。

它不负责执行任务，也不是一个复杂 Planner。它只做一件事：

> 把 Agent 的计划变成 Harness 可观察的结构化状态。

## 核心概念

TodoWrite 的抽象很简单：

```text
用户任务
  -> 模型拆成 todo list
  -> todo_write 写入 Harness 状态
  -> 每完成一步，再整体更新 todo list
```

每个 todo 有三个字段：

- `content`：任务内容。
- `status`：任务状态，只允许 `pending / in_progress / completed`。
- `priority`：优先级，只允许 `high / medium / low`。

我这里故意要求：

```text
每次写入完整 todos，而不是只追加一条 todo。
```

原因是 TodoWrite 表示的是“当前完整计划状态”，不是操作日志。

比如一开始：

```text
1. ▶ [high] 实现代码
2. □ [high] 撰写文档
3. □ [medium] 运行验证
```

完成第一步后，模型应该写入完整的新状态：

```text
1. ✓ [high] 实现代码
2. ▶ [high] 撰写文档
3. □ [medium] 运行验证
```

这样 Harness 永远只需要相信一份当前状态，而不用从一串增删改事件里推导结果。

我还加了一个限制：

```text
最多只能有一个 in_progress。
```

因为 `in_progress` 表示 Agent 当前焦点。如果同时有三个任务都是进行中，Harness 和用户都不知道模型此刻到底在做哪一步。

## 我的实现

完整实现见：`code/s05_todo_write.py`

最核心的是这几个部分。

第一，定义 todo 的结构：

```python
TodoStatus = Literal["pending", "in_progress", "completed"]
TodoPriority = Literal["high", "medium", "low"]


class TodoItem(TypedDict):
    """TodoWrite 维护的最小任务结构。"""

    content: str
    status: TodoStatus
    priority: TodoPriority


# TodoWrite 在 第 05 章 先只做内存态，后续 Memory 章节再考虑持久化。
TODOS: list[TodoItem] = []
```

第二，校验模型传进来的 todo：

```python
def normalize_todo(raw: Any, index: int) -> TodoItem:
    """校验模型传来的单个 todo，并转成内部结构。"""
    if not isinstance(raw, dict):
        raise ValueError(f"todos[{index}] must be an object")

    content = raw.get("content")
    status = raw.get("status")
    priority = raw.get("priority")

    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"todos[{index}].content must be a non-empty string")
    if status not in {"pending", "in_progress", "completed"}:
        raise ValueError(
            f"todos[{index}].status must be pending, in_progress, or completed"
        )
    if priority not in {"high", "medium", "low"}:
        raise ValueError(f"todos[{index}].priority must be high, medium, or low")

    return {
        "content": content.strip(),
        "status": status,
        "priority": priority,
    }
```

第三，真正的 `todo_write` handler：

```python
def run_todo_write(todos: list[Any]) -> str:
    """整体替换当前任务列表，并返回新的可见状态。"""
    if not isinstance(todos, list):
        return "Error: todos must be a list."

    try:
        normalized = [normalize_todo(raw, index) for index, raw in enumerate(todos)]
    except ValueError as exc:
        return f"Error: {exc}"

    in_progress_count = sum(1 for todo in normalized if todo["status"] == "in_progress")
    if in_progress_count > 1:
        return "Error: todo list can have at most one in_progress item."

    TODOS.clear()
    TODOS.extend(normalized)
    return format_todos(TODOS)
```

注意这里不是：

```python
TODOS.append(new_item)
```

而是：

```python
TODOS.clear()
TODOS.extend(normalized)
```

这表达了 TodoWrite 的语义：**替换当前计划状态**。

最后，把它注册成一个普通工具：

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
}
```

从 Agent Loop 的角度看，`todo_write` 和 `bash / read_file / edit_file` 没有本质区别。

它也是一个工具调用：

```text
tool_use(todo_write) -> tool_result
```

区别只在于它改变的是 Harness 的内存状态，而不是文件系统或 shell 环境。

运行方式：

```bash
python code/s05_todo_write.py
```

试一个多步任务：

```text
先用 todo_write 列出计划：创建 hello.py，写 greet(name) 函数，运行验证，最后总结结果。
```

你会看到模型先调用 `todo_write`，把计划写成结构化状态：

```text
□ [high] 创建 hello.py
□ [high] 实现 greet(name)
□ [medium] 运行验证
□ [low] 总结结果
```

当它开始执行某一步时，会把那一项改成 `in_progress`：

```text
▶ [high] 创建 hello.py
□ [high] 实现 greet(name)
□ [medium] 运行验证
□ [low] 总结结果
```

完成后再整体写回新状态：

```text
✓ [high] 创建 hello.py
▶ [high] 实现 greet(name)
□ [medium] 运行验证
□ [low] 总结结果
```

结束时，`Stop` hook 还会打印当前 todo 状态。用户能直观看到：Agent 现在做到哪一步、下一步是什么、有没有跑偏。

这就是这一章想让用户"感受到"的东西：**计划不再只藏在模型的自然语言里，而是变成 Harness 和用户都看得见的结构化状态。**

## 我踩的坑

这一章的坑，抓住几件事就够了：

- **TodoWrite 不是追加日志，而是当前计划快照。** 如果只提供 `add_todo()`，后面更新状态会很麻烦：到底是追加一条，还是修改旧任务？本章选择每次提交完整列表，让 Harness 永远相信一份最新状态。
- **只能有一个 `in_progress`。** `in_progress` 表示当前焦点。多个任务同时进行中，用户和 Harness 都不知道 Agent 此刻到底在做哪一步。
- **TodoWrite 不是执行器。** 它不会写文件、跑命令、提交代码；真正执行任务的还是 `bash/read_file/write_file/edit_file`。TodoWrite 只是把这些动作放进一条清晰的计划线里。
- **TodoWrite 现在还是内存态。** 程序一退出就丢失，不能跨会话恢复。真正持久化要等后面的 Memory / Task System。

## 小结

本章实现了一个最小版 `TodoWrite`。

它给 Agent 补上的不是新执行能力，而是**可观察的工作状态**：

```text
用户任务 -> 模型拆计划 -> todo_write 写入状态 -> 执行中不断更新
```

真实 Claude Code 里也有 TodoWrite。系统会鼓励模型在复杂任务前创建 todo list，开始某项任务时标记 `in_progress`，完成后及时标记 `completed`，并且不要同时有多个 `in_progress`。这背后的目标不是形式主义，而是解决长流程里的上下文腐烂：让模型专注，让用户透明地看到进度，也让 Harness 能在 Stop hook、压缩、恢复等位置拿到结构化任务状态。

这章之后，我的 Agent 不再只是“看到一步做一步”，而是开始拥有显式的工作状态：

```text
Agent Loop + Tool System + Permission Gate + Lifecycle Hooks + TodoWrite
```

下一章要解决的问题是：当任务变复杂时，能不能把一部分工作交给一个独立上下文里的小 Agent 去做？这就是 Subagent。
