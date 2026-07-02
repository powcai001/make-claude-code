# 第 11 章：Error Recovery

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s11_error_recovery.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s11_error_recovery.py)


## 本章解决什么问题？

前 10 章的 Agent 已经有了不少能力：工具调用、权限、Hook、TodoWrite、Subagent、Skill Loading、Context Compact、Memory、System Prompt 组装。

但只要真正跑起来，就会遇到一个很现实的问题：**错误一定会发生**。

模型可能传错工具参数；文件路径可能不存在；bash 命令可能失败；权限系统可能拒绝某个操作；模型 API 也可能临时超时或限流。如果 Harness 只是把这些错误当成普通字符串丢回去，模型经常会做两件坏事：

1. 原样重试同一个失败工具调用。
2. 放弃当前任务，直接告诉用户“失败了”。

真实 Agent 不能这样。错误不是终点，而应该变成下一轮决策的输入。所以 第十一章 我实现一个最小版 Error Recovery：

> 把工具和模型调用中的错误分类、记录，并附带恢复建议返回给模型，让 Agent 能改变策略继续推进。

## 核心概念

第十一章 的流程是：

```text
model/tool error
  -> classify_error()
  -> record_error()
  -> attach recovery hint
  -> next model turn
```

这里有三个关键概念。

第一，错误分类。

同样是失败，处理方式不一样：

- 权限失败：不要原样重试，要解释或询问用户。
- 参数错误：修正工具输入再试。
- 文件不存在：先用只读工具确认路径。
- 超时：缩小命令范围或拆成多步。
- 临时错误：可以有限重试。

第二，恢复提示。

模型看到 `Error: old_text not found` 时，不一定知道下一步该做什么。Harness 可以把经验写成提示：先 read 文件，确认最新内容，再 edit。

第三，错误历史。

如果模型连续踩同一个坑，Harness 至少应该能让它查看最近错误，而不是只依赖上一条 tool result。

## 我的实现

完整实现见：`code/s11_error_recovery.py`

第十一章 继续基于 第十章，所以 System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在错误分类、恢复提示和模型调用重试。

### ErrorRecord

我先加了一个结构化错误记录：

```python
ErrorKind = Literal["permission", "validation", "not_found", "timeout", "transient", "unknown"]

class ErrorRecord(TypedDict):
    """最近一次工具或模型错误的结构化记录。"""

    kind: ErrorKind
    where: str
    message: str
    hint: str
```

它不是为了做复杂日志系统，而是为了让错误变成 Harness 能处理的数据。

全局只保留最近 20 条：

```python
ERROR_HISTORY: list[ErrorRecord] = []
MAX_ERROR_HISTORY = 20
```

### 错误分类

分类函数非常朴素，只按关键字判断：

```python
def classify_error(message: str) -> ErrorKind:
    """把非结构化错误文本粗略分类，供恢复策略使用。"""
    lowered = message.lower()
    if "permission denied" in lowered or "user rejected" in lowered:
        return "permission"
    if "must be" in lowered or "required" in lowered or "invalid" in lowered:
        return "validation"
    if "not found" in lowered or "no such file" in lowered:
        return "not_found"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "rate limit" in lowered or "overloaded" in lowered or "temporarily" in lowered:
        return "transient"
    return "unknown"
```

真实系统会更复杂，可能根据异常类型、HTTP 状态码、工具 schema、权限模式来判断。但最小实现里，关键字已经能覆盖很多常见失败。

### 恢复建议

分类之后，Harness 给出下一步建议：

```python
def recovery_hint(kind: ErrorKind, where: str, message: str) -> str:
    """根据错误类型生成给模型看的下一步建议。"""
    if kind == "permission":
        return "Do not retry the same action. Explain why it was blocked or ask the user for a safer alternative."
    if kind == "validation":
        return "Fix the tool input schema or missing arguments before retrying."
    if kind == "not_found":
        return "Check the path/name with a read-only tool before trying again."
    if kind == "timeout":
        return "Retry with a narrower command, smaller input, or split the work into steps."
    if kind == "transient":
        return "The operation may succeed later. Retry only if the action is safe and bounded."
    return "Inspect the error, change strategy, and avoid repeating the exact same failing call."
```

这其实是在把“资深工程师的默认反应”编码进 Harness。

### 给工具结果附加恢复提示

工具返回错误时，我不会只返回原始错误，而是追加一个 `[error_recovery]` 块：

```python
def attach_recovery_hint(where: str, output: str) -> str:
    """当工具结果看起来是错误时，追加恢复建议。"""
    stripped = output.strip()
    lowered = stripped.lower()
    is_error = (
        lowered.startswith("error:")
        or lowered.startswith("permission denied")
        or lowered.startswith("(exit code")
    )
    if not is_error:
        return output
    record = record_error(where, output)
    return f"{output}\n\n{format_error_record(record)}"
```

比如工具结果可能从：

```text
Error: old_text not found in README.md.
```

变成：

```text
Error: old_text not found in README.md.

[error_recovery]
kind: not_found
where: edit_file
hint: Check the path/name with a read-only tool before trying again.
```

这样模型下一轮更可能先 `read_file`，而不是继续盲目 `edit_file`。

主 Agent 和 Subagent 的工具循环都接入了这个函数：

```python
try:
    output = handler(**block.input)
except Exception as exc:
    output = f"Error: {exc}"
output = attach_recovery_hint(block.name, output)
trigger_hooks("PostToolUse", block.name, block.input, output)
```

权限拒绝、未知工具、工具异常、bash 非零退出，都可以被统一转成带恢复建议的 tool result。

### 模型调用重试

工具错误是一类，模型 API 错误是另一类。第十一章 我给模型调用包了一层：

```python
def call_model_with_retries(**kwargs: Any) -> Any:
    """调用模型 API；对短暂错误做有限重试，对最终失败给出结构化记录。"""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:
            last_error = exc
            message = f"{type(exc).__name__}: {exc}"
            kind = classify_error(message)
            if kind != "transient" and attempt == 1:
                break
            if attempt < 3:
                time.sleep(0.5 * attempt)

    message = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown model error"
    record = record_error("model_api", message)
    raise RuntimeError(format_error_record(record)) from last_error
```

策略很保守：

- 只有 `transient` 类错误才会重试。
- 最多尝试 3 次。
- 每次等待时间很短。
- 最终失败时抛出结构化错误，而不是吞掉异常。

这避免了两种极端：既不会一遇到限流就直接崩，也不会无限重试浪费时间。

### show_errors 工具

最后我加了一个只读调试工具：

```python
def run_show_errors() -> str:
    """工具：查看最近错误和恢复建议。"""
    if not ERROR_HISTORY:
        return "No errors recorded yet."
    lines: list[str] = ["Recent errors:"]
    for index, record in enumerate(ERROR_HISTORY, start=1):
        lines.append(
            f"{index}. [{record['kind']}] {record['where']}: "
            f"{record['message'][:160]}\n   hint: {record['hint']}"
        )
    return "\n".join(lines)
```

它的作用和 第十章 的 `show_system_prompt` 类似：让 Harness 的内部状态可观察。

## 我踩的坑

### 坑 1：一开始只想 try/except

最简单的错误处理是：

```python
try:
    output = handler(**block.input)
except Exception as exc:
    output = f"Error: {exc}"
```

这当然比直接崩溃好，但对 Agent 来说还不够。

因为模型拿到的仍然只是一段非结构化错误文本，它不知道应该换路径、改参数、询问用户，还是稍后重试。

所以 第十一章 的关键不是“捕获异常”，而是“把异常转成可行动反馈”。

### 坑 2：不能所有错误都重试

我给模型 API 做了 retry，但没有给所有工具都自动 retry。

原因是工具可能有副作用。比如 `write_file`、`edit_file`、`bash` 都可能改变工作区。如果 Harness 自动重试，可能造成重复写入、重复安装、重复提交。

所以我只对模型 API 的临时错误做有限重试；工具错误则返回给模型，让模型决定下一步。

### 坑 3：错误提示不能鼓励绕过权限

权限失败的恢复建议很敏感。

如果用户拒绝了某个操作，恢复建议不能说“换个命令绕过限制”。所以 `permission` 类型的 hint 是：

```text
Do not retry the same action. Explain why it was blocked or ask the user for a safer alternative.
```

这也是 Error Recovery 和安全边界的关系：恢复不是绕过，而是换成更安全、更明确的路径。

## 小结

第十一章 的关键词是：**可恢复失败**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类工具里，Error Recovery 会散落在很多地方：

- 工具 schema 校验失败后的错误消息。
- Bash 命令非零退出码和 stderr 截断。
- 权限拒绝后的 tool result。
- API 429/5xx 的有限重试。
- 上下文过长时触发 compact 或提示用户。
- 文件编辑失败时要求重新读取文件。
- Harness 日志和可观测性面板。

我这个最小实现对应的是其中三块：

```text
工具错误 -> 恢复提示 -> 下一轮模型决策
模型临时错误 -> 有限重试 -> 结构化失败
错误历史 -> show_errors 可观察
```

和真实系统相比，它还很简单：

- 错误分类只是关键字匹配。
- 没有按工具类型定制更细的恢复策略。
- 没有指数退避和 jitter。
- 没有持久化错误日志。
- 没有自动检测重复失败循环。
- 没有把错误统计接入遥测。

但核心思想已经有了：

> Harness 不应该只是把错误转成字符串，而应该帮助模型理解错误性质，并给出下一步可执行策略。


一个 Agent 真正能用，不是因为它永远不犯错，而是因为它犯错后能知道错在哪里、下一步该怎么改，并且不会盲目重复同一个失败动作。

Error Recovery 把失败从“流程终点”变成了“下一轮上下文”。
