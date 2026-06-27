# 第 05 章：TodoWrite

## 本章解决什么问题？

Day 04 结束时，我的 Agent 已经有了一个比较像样的 Harness：模型负责决策，工具负责执行，权限和日志通过 Hooks 挂在生命周期上。

但它还缺一个很关键的东西：**可见的任务状态**。

如果用户说：

```text
帮我实现 Day05，写代码、写文档、跑测试、提交并推送。
```

这不是一步任务。模型需要先拆解，再逐步执行。如果计划只写在模型的自然语言回复里，就会有几个问题：

1. Harness 看不见模型当前打算做什么。
2. 用户看不见任务是否跑偏。
3. 对话变长后，模型容易忘记前面已经完成了什么。
4. 多步任务中断后，很难恢复现场。

所以 Day 05 我先实现一个最小版 `TodoWrite`。

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


# TodoWrite 在 Day 05 先只做内存态，后续 Memory 章节再考虑持久化。
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

## 我踩的坑

### 坑 1：把 TodoWrite 设计成“追加一条任务”

一开始很容易把它想成：

```python
add_todo(content, status, priority)
```

这样写确实简单，但很快会遇到问题。

比如任务状态从 `in_progress` 变成 `completed`，到底是追加一条新记录，还是修改旧记录？如果追加，就会出现两条同名任务。如果修改，又要引入 id、查找、更新、删除等一套操作。

后来我改成“每次传完整列表”。

这样 Harness 不需要理解模型的局部操作，只需要接收最新状态：

```text
旧状态 -> 新状态
```

这和真实 Claude Code 的 `TodoWrite` 更接近。

### 坑 2：允许多个 `in_progress`

如果不限制 `in_progress` 数量，模型很容易写出：

```text
1. ▶ 实现代码
2. ▶ 写文档
3. ▶ 跑测试
```

这看起来都在做，但实际上等于没有当前焦点。

Agent 的多步执行需要一个明确的“现在正在做什么”。所以我在 handler 里加了检查：

```python
in_progress_count = sum(1 for todo in normalized if todo["status"] == "in_progress")
if in_progress_count > 1:
    return "Error: todo list can have at most one in_progress item."
```

这个约束很小，但会显著提高任务状态的可读性。

### 坑 3：把 TodoWrite 当成执行器

TodoWrite 不是执行器。

它不会帮 Agent 写文件、跑命令、提交代码。它只是记录计划。

真正执行任务的还是之前几章实现的工具：

```text
bash / read_file / write_file / edit_file
```

TodoWrite 的价值是让这些动作被一个清晰的任务状态串起来。

## 对应真实 Claude Code 的哪里

真实 Claude Code 里有一个非常重要的工具就叫 `TodoWrite`。

它的作用不是写项目文件，而是维护当前会话的任务列表。Claude Code 还会在系统提示里要求模型：

- 复杂任务要主动创建 todo list。
- 开始某项任务时把它标成 `in_progress`。
- 完成后及时标成 `completed`。
- 不要同时有多个 `in_progress`。

我这章实现的版本和真实系统的相同点是：

1. 都把计划变成结构化状态。
2. 都使用 `pending / in_progress / completed` 这类状态机。
3. 都要求一次提交完整任务列表。
4. 都把 TodoWrite 做成普通工具，由模型主动调用。

不同点是：

1. 我这里只存在内存里，程序退出就丢失。
2. 我没有做 UI 展示，只是在终端和 Stop hook 里打印。
3. 我没有实现跨会话恢复。
4. 我没有把 TodoWrite 和更复杂的 Planner / Task System 绑定。

这些简化是故意的。

Day 05 的目标不是实现完整任务系统，而是先补上一个关键 Harness 能力：

```text
Agent 的计划必须可见、可校验、可更新。
```

## 小结

本章实现了一个最小版 `TodoWrite`。

现在我的 Agent 不只是能调用工具，还能维护一份可见的任务计划：

```text
Agent Loop + Tool System + Permission Gate + Lifecycle Hooks + TodoWrite
```

这一步很重要，因为从这里开始，Agent 不再只是“看到一步做一步”，而是开始拥有显式的工作状态。

TodoWrite 的核心思想是：

> 计划不要只藏在模型上下文里，要变成 Harness 可以观察和约束的结构化状态。
