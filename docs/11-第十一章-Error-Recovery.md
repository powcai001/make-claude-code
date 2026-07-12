# 第 11 章：Error Recovery

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s11_error_recovery.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s11_error_recovery.py)

## 本章解决什么问题？

前 10 章的 Agent 已经有了工具、权限、Hooks、TodoWrite、Subagent、Skill Loading、Context Compact、Memory 和 System Prompt 组装。

但只要真正跑起来，错误一定会发生：模型可能传错参数，路径可能不存在，bash 命令可能失败，权限系统可能拒绝操作，模型 API 也可能临时超时、限流或服务过载。

如果 Harness 只是把错误当普通字符串丢回模型，模型很容易原样重试，或者直接放弃。

所以这一章实现一个最小版 Error Recovery：

> 把工具和模型调用中的错误分类、记录，并附带恢复建议返回给模型，让 Agent 能改变策略继续推进。

它不是“自动重试一切”，而是先区分失败性质：哪些应该让模型改策略，哪些能安全地有限重试，哪些必须停下来问用户。

## 核心概念

流程是：

```text
model/tool error
  -> classify_error()
  -> record_error()
  -> attach recovery hint
  -> next model turn
```

错误处理至少分两层：

```text
工具失败：返回给模型，让模型诊断并改策略
模型 API 短暂失败：Harness 做有限、有界重试
```

这里不要混淆。工具可能有副作用，不能因为失败就自动再执行一次；模型 API 的 429、5xx、网络断连通常没有改变工作区，才适合有限重试。

最小实现目前有六类：

- `permission`：权限拒绝或用户拒绝。
- `validation`：工具参数或 schema 不合法。
- `not_found`：路径、文件或资源不存在。
- `timeout`：命令或请求超时。
- `transient`：限流、服务过载、5xx、连接中断等短暂错误。
- `unknown`：其余无法确认的错误。

## 我的实现

完整实现见：`code/s11_error_recovery.py`。

### 1. 错误分类和恢复提示

```python
ErrorKind = Literal[
    "permission", "validation", "not_found",
    "timeout", "transient", "unknown",
]

class ErrorRecord(TypedDict):
    kind: ErrorKind
    where: str
    message: str
    hint: str
```

分类函数仍然是教学版的文本匹配，但覆盖了常见 API 形态：

```python
def classify_error(message: str) -> ErrorKind:
    lowered = message.lower()
    if "permission denied" in lowered or "user rejected" in lowered:
        return "permission"
    if "not found" in lowered or "no such file" in lowered:
        return "not_found"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "must be" in lowered or "required" in lowered or "invalid" in lowered:
        return "validation"
    if any(marker in lowered for marker in (
        "rate limit", "429", "500", "502", "503", "504", "529",
        "overloaded", "service unavailable", "connection reset", "econnreset",
    )):
        return "transient"
    return "unknown"
```

随后按错误类型附上不同恢复建议：

```text
permission  -> 不要重试同一动作；解释原因或问用户更安全的替代方案
validation  -> 修正参数或 schema 后再试
not_found   -> 先用只读工具确认路径或名称
timeout     -> 缩小命令范围、减少输入或拆分步骤
transient   -> 只在动作安全且有界时稍后重试
unknown     -> 先检查错误，改变策略，避免重复同一次失败调用
```

### 2. 所有工具失败统一进入恢复链路

工具结果不再只靠零散的前缀判断。我提取了统一入口：

```python
def is_error_output(output: str) -> bool:
    lowered = output.strip().lower()
    return (
        lowered.startswith("error:")
        or lowered.startswith("permission denied")
        or lowered.startswith("(exit code")
        or lowered.startswith("(no output, exit code")
    )


def attach_recovery_hint(where: str, output: str) -> str:
    if not is_error_output(output):
        return output
    record = record_error(where, output)
    return f"{output}\n\n{format_error_record(record)}"
```

例如编辑失败：

```text
Error: old_text not found in README.md.

[error_recovery]
kind: not_found
where: edit_file
hint: Check the path/name with a read-only tool before trying again.
```

模型下一轮更可能先读取文件，而不是继续盲目编辑。

### 3. Bash 的非零退出必须被视为错误

这里修了一个容易漏掉的边界：`false` 这类命令没有 stdout 和 stderr，但退出码是 1。它不能被伪装成“无输出的正常命令”。

```python
output = (result.stdout + result.stderr).strip()
if result.returncode != 0:
    if output:
        output = f"(exit code {result.returncode})\n{output}"
    else:
        output = f"(exit code {result.returncode}; no output)"
elif not output:
    output = "(no output, exit code 0)"
```

这样所有非零退出都会进入 `attach_recovery_hint()`。

### 4. 只重试 transient API 错误

模型 API 调用包了一层有限重试：

```python
MODEL_RETRY_ATTEMPTS = 3
MODEL_RETRY_BASE_DELAY_SECONDS = 0.5
MODEL_RETRY_MAX_DELAY_SECONDS = 4.0

def call_model_with_retries(**kwargs: Any) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, MODEL_RETRY_ATTEMPTS + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:
            last_error = exc
            message = f"{type(exc).__name__}: {exc}"
            if classify_error(message) != "transient" or attempt == MODEL_RETRY_ATTEMPTS:
                break
            time.sleep(retry_delay_seconds(attempt))

    record = record_error("model_api", f"{type(last_error).__name__}: {last_error}")
    raise RuntimeError(format_error_record(record)) from last_error
```

退避时间是有上限的指数退避，并增加了轻量 jitter：

```text
delay = min(base × 2^(attempt - 1), max_delay) + jitter
```

关键边界是：**只要遇到非 transient 错误，立刻停止重试。**

比如第一次 429，第二次变成参数错误，不会再发起第三次请求。

### 5. 错误历史可观察，但不泄漏给子 Agent

主 Agent 可以使用 `show_errors` 查看最近错误和恢复建议。

但子 Agent 不再拥有 `show_errors`。子 Agent 的作用是隔离调查上下文，不应该读取主 Agent 的错误历史，也不应把自己的诊断需求扩展成跨上下文观察。

```text
主 Agent：可查看本轮运行中的错误历史
子 Agent：只使用自己的工具结果继续调查
```

## 验证

新增了标准库 `unittest` 测试：`tests/test_s11_error_recovery.py`。

覆盖内容：

- 错误分类优先级；
- 错误历史只保留最近 20 条；
- 静默 Bash 非零退出会产生恢复提示；
- transient 错误最多重试 3 次；
- 非 transient 错误不重试；
- “先 transient、后永久错误”会在第二次停止；
- 子 Agent 不暴露 `show_errors`。

运行：

```bash
python -m py_compile code/s11_error_recovery.py tests/test_s11_error_recovery.py
python -m unittest discover -s tests -v
```

本次验证结果：8 个测试全部通过。

## 我踩的坑

- **不能只写 `try/except`。** 捕获异常只是防崩；把错误转成可行动反馈，模型才知道下一步怎么改。
- **不能所有错误都重试。** 工具可能有副作用，`write_file`、`edit_file`、`bash` 不能盲目自动重试。
- **重试也不能只看第一次。** 任意一次出现永久错误，都应该立即停止，不能因为前一次短暂失败就继续重试。
- **上下文超限不是普通 API 重试。** 它应该优先触发 Context Compact 或更激进的历史收缩；重发同一个超长请求没有意义。
- **恢复不是绕过权限。** 用户拒绝后，不能换一个近似命令继续做。恢复应该换成更安全、更明确的路径。

## 小结

这一章的关键词是：**可恢复失败**。

```text
工具错误 -> 恢复提示 -> 下一轮模型决策
模型短暂错误 -> 有界退避 -> 重新调用
上下文压力 -> Compact / Collapse 类恢复
```

真实 Claude Code / Codex 类系统会比这里复杂得多：类型化 HTTP 错误、认证刷新、模型 fallback、流式中断恢复、上下文溢出的多级恢复、错误遥测和重复失败检测。

但最小版本先抓住一个核心：

> Harness 不应该只是把错误转成字符串，而应该帮助模型理解错误性质，并给出下一步可执行策略。

一个 Agent 真正能用，不是因为它永远不犯错，而是因为它犯错后能知道错在哪里、下一步该怎么改，并且不会盲目重复同一个失败动作。
