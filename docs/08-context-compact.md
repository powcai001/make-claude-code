# 第 08 章：Context Compact

## 本章解决什么问题？

Day 07 解决的是 system prompt 变大的问题：不要把所有 Skill 永久塞进上下文，而是按任务动态加载。

但还有另一种上下文膨胀更难避免：**对话历史本身会越来越长**。

一个 Agent 跑复杂任务时，会不断积累：

- 用户需求
- 模型回复
- 工具调用
- 文件内容
- 命令输出
- todo list 更新
- subagent 报告

这些内容全都放在 `messages` 里。时间一长，就会出现几个问题：

1. 模型调用越来越贵。
2. 旧工具结果占据大量上下文。
3. 噪音影响当前决策。
4. 最终触发模型 context limit，导致会话无法继续。

所以 Day 08 我实现一个最小版 Context Compact。

它的目标不是长期记忆，而是当前会话续航：

> 当 messages 太长时，把较早历史压缩成摘要，保留最近窗口，然后继续对话。

## 核心概念

Context Compact 的流程是：

```text
messages
  -> estimate context size
  -> split old / recent
  -> summarize old messages
  -> replace old messages with compact summary
  -> continue with recent messages
```

这里有三个关键概念。

第一，context pressure。

也就是当前 `messages` 对上下文窗口造成的压力。真实系统通常会按 token 估算，我这里为了教学简单，用字符数近似。

第二，compact summary。

它不是长期记忆文件，也不会跨会话保存。它只是把当前会话前半段压缩成一段摘要，放回 `messages` 里，帮助模型继续当前任务。

第三，recent window。

最近几条消息必须原样保留。

因为最近窗口里常常包含正在进行的工具调用、刚刚读到的文件片段、用户最新要求。如果把这些也压缩掉，模型很容易丢失当前操作状态。

所以我的策略是：

```text
旧消息 -> 压缩
最近 COMPACT_KEEP_RECENT 条 -> 原样保留
```

## 我的实现

完整实现见：`code/s08_context_compact.py`

我先定义几个常量：

```python
COMPACT_TRIGGER_CHARS = 30_000
COMPACT_KEEP_RECENT = 6
MAX_COMPACT_INPUT_CHARS = 40_000
MAX_COMPACT_SUMMARY_CHARS = 8_000
COMPACT_SUMMARY_PREFIX = "[compact summary]"
```

这里用字符数，不用 tokenizer。

这不精确，但足够说明 Harness 里应该有一个“上下文压力估算”的阶段。

### 估算上下文大小

`messages` 的内容不一定都是字符串。它可能是：

- 普通文本
- tool_use block
- tool_result list
- dict
- list

所以我先写了一个递归转换函数：

```python
def stringify_for_context(value: Any) -> str:
    """递归把 message/content/tool_result 转成可估算的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(f"{key}: {stringify_for_context(item)}")
        return "\n".join(parts)
    if isinstance(value, list):
        return "\n".join(stringify_for_context(item) for item in value)

    block_type = getattr(value, "type", None)
    if block_type == "text":
        return str(getattr(value, "text", ""))
    if block_type == "tool_use":
        return f"tool_use {getattr(value, 'name', '')}: {getattr(value, 'input', '')}"
    return str(value)
```

然后估算总大小：

```python
def estimate_context_chars(messages: list[dict[str, Any]]) -> int:
    """用字符数近似估算当前 conversation 的上下文压力。"""
    return sum(len(stringify_for_context(message)) for message in messages)
```

### 构造压缩提示

压缩不是简单裁剪。

如果直接删除旧消息，模型会忘掉用户目标、已经做过的决定、读过哪些文件、还有什么没完成。

所以我构造了一个 summary prompt，要求模型输出结构化摘要：

```python
def build_compact_prompt(old_messages: list[dict[str, Any]]) -> str:
    """构造压缩提示，要求模型输出当前会话的结构化摘要。"""
    chunks: list[str] = []
    used = 0
    for message in old_messages:
        text = extract_message_text(message)
        remaining = MAX_COMPACT_INPUT_CHARS - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining] + "\n... (truncated for compact input)"
        chunks.append(text)
        used += len(text)

    transcript = "\n\n".join(chunks)
    return (
        "Summarize the following earlier conversation for a coding agent that will continue "
        "the same session. Keep facts that affect future actions.\n\n"
        "Include these sections:\n"
        "- User goal\n"
        "- Current plan / todo state\n"
        "- Important facts discovered\n"
        "- Files touched or read\n"
        "- Decisions made\n"
        "- Open questions / next steps\n\n"
        "Earlier conversation:\n"
        f"{transcript}"
    )
```

注意这里还有一个输入截断：`MAX_COMPACT_INPUT_CHARS`。

否则“拿超长历史去总结”本身也可能再次超出上下文。

### 替换旧消息

真正压缩发生在 `compact_messages`：

```python
def compact_messages(messages: list[dict[str, Any]], system_prompt: str) -> bool:
    """把旧消息压缩成一条摘要消息，并保留最近窗口。"""
    if len(messages) <= COMPACT_KEEP_RECENT + 1:
        return False

    old_messages = messages[:-COMPACT_KEEP_RECENT]
    recent_messages = messages[-COMPACT_KEEP_RECENT:]
    prompt = build_compact_prompt(old_messages)

    response = client.messages.create(
        model=MODEL,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    summary = extract_text(response.content)[:MAX_COMPACT_SUMMARY_CHARS]

    messages[:] = [
        {
            "role": "user",
            "content": f"{COMPACT_SUMMARY_PREFIX}\n{summary}",
        },
        *recent_messages,
    ]
    return True
```

这里有一个小技巧：

```python
messages[:]
```

我不是返回一个新列表，而是原地替换。这样 `history` 这个对象本身还保持不变，只是里面的内容被 compact 了。

### 插入 Agent Loop

Day08 最关键的插入点在模型调用之前：

```python
while True:
    maybe_compact_messages(messages, system_prompt)
    response = client.messages.create(
        model=MODEL,
        system=system_prompt,
        messages=messages,
        tools=TOOLS,
        max_tokens=8000,
    )
```

也就是说，每次调用模型前，Harness 都先检查当前上下文压力。

如果没超过阈值，什么都不做。

如果超过阈值，就把旧消息压缩成摘要，再继续调用模型。

## 我踩的坑

### 坑 1：把 Compact 当成 Memory

Context Compact 很像“记忆”，但它不是 Memory。

Compact 的摘要只存在当前 `messages` 里，程序退出就没了。它的目标是让当前会话继续跑下去。

真正跨会话保存、长期可检索的 Memory，留到 Day09 再做。

### 坑 2：压缩所有消息

如果把全部 messages 都压缩成一段摘要，模型会丢掉最近状态。

尤其是工具调用场景里，最近几轮可能包含：

- 刚刚读到的文件内容
- 刚刚失败的命令输出
- 用户最新追加的约束
- 当前正在执行的 todo

这些内容不能轻易摘要化。

所以我保留 recent window：

```python
old_messages = messages[:-COMPACT_KEEP_RECENT]
recent_messages = messages[-COMPACT_KEEP_RECENT:]
```

### 坑 3：用字符数冒充 token 数

字符数不等于 token 数。

中文、英文、代码、JSON 的 token 密度都不一样。

但 Day08 的目标是讲清楚机制，而不是实现精确计费器。字符数估算的优点是简单、无依赖、容易测试。

真实系统里可以替换成 tokenizer 或模型 API 返回的 usage。

### 坑 4：压缩输入本身也会超长

如果历史已经非常长，直接把所有旧消息发给总结模型，也可能超过上下文限制。

所以 `build_compact_prompt` 里还有一层：

```python
MAX_COMPACT_INPUT_CHARS = 40_000
```

这不是完美方案，但能保证最小实现不会因为“为了压缩而再次超长”。

## 对应真实 Claude Code 的哪里

真实 Claude Code / Codex 这类 Agent Harness 都需要 context management。

用户感觉像是在一段长会话里连续工作，但背后 Harness 必须持续管理：

- 哪些上下文仍然重要
- 哪些工具结果可以压缩
- 哪些最近消息必须保留
- 什么时候触发自动 compact
- compact 后如何让模型继续同一个任务

Day07 和 Day08 的区别是：

```text
Day07 Skill Loading：进入上下文前，选择需要加载什么。
Day08 Context Compact：上下文变长后，选择如何压缩历史。
```

我这一章的实现和真实系统的相同点是：

- 都会估算上下文压力。
- 都会把旧消息总结成 compact summary。
- 都会保留最近窗口。
- 都把 compact 放在 Harness 层，而不是让用户手动管理所有历史。

不同点是：

- 真实系统会使用更准确的 token 估算。
- 真实系统会更小心处理 tool_use / tool_result 配对。
- 真实系统可能有手动 compact、自动 compact、恢复机制等多种策略。
- 我这里不持久化摘要，不做长期 Memory。

这些简化是为了让 Day08 聚焦一个核心机制：

```text
长会话需要压缩历史，而不是无限追加 messages。
```

## 小结

本章实现了一个最小版 Context Compact。

现在我的 Agent Harness 变成了：

```text
Agent Loop
+ Tool System
+ Permission Gate
+ Lifecycle Hooks
+ TodoWrite
+ Subagent / Task
+ Skill Loading
+ Context Compact
```

Context Compact 的核心思想是：

> 当前会话可以变长，但 messages 不能无限增长；旧上下文要压缩成摘要，最近上下文要原样保留。
