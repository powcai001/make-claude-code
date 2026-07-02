# 第 08 章：Context Compact

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s08_context_compact.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s08_context_compact.py)


## 本章解决什么问题？

第 07 章 解决的是 system prompt 变大的问题：不要把所有 Skill 永久塞进上下文，而是按任务动态加载。

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

所以 第 08 章 我实现一个最小版 Context Compact。

它的目标不是长期记忆，而是当前会话续航：

> 当 messages 太长时，把较早历史压缩成摘要，保留最近窗口，然后继续对话。

知乎上关于 Claude Code 上下文压缩的深度文章里有一个核心观点：**压缩的目标不是省 token，而是保护模型的注意力。** 研究表明，上下文塞到 70% 以上，模型的中段失忆和指令漂移就会明显恶化——这叫 **Context Rot（上下文腐烂）**。模型不是真的"忘了"，是注意力被稀释、信号被噪声淹没。所以压缩系统本质上是一个信号工程师：把无关紧要的旧工具输出降为摘要，让最近的对话不被淹没。

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

第 08 章 最关键的插入点在模型调用之前：

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

运行方式：

```bash
python code/s08_context_compact.py
```

试一个会产生大量工具输出的任务，让对话变长：

```text
读取 README.md，然后读取 code/s01_agent_loop.py 的前 50 行，总结一下
```

多轮对话后，当 messages 总字符数超过 `COMPACT_TRIGGER_CHARS`（默认 30000）时，终端会显示：

```text
[compact] context pressure high, compacting...
[compact] done. messages reduced.
```

这表示 Harness 把旧消息压缩成了一条 `[compact summary]`，只保留最近 6 条消息原样不动。模型接下来看到的是：摘要 + 最近窗口，而不是全部历史。

你可以用 `compact` 工具手动触发压缩，也可以让它自动触发。这就是这一章想让用户"感受到"的东西：**长会话不会因为 messages 无限增长而崩掉，Harness 会在压力过大时自动瘦身。**

## 我踩的坑

这一章的坑，抓住几件事就够了：

- **Compact 不是 Memory。** 压缩摘要只存在当前 messages 里，程序退出就没了。它的目标是让当前会话继续跑下去，不是跨会话记忆。长期 Memory 留到下一章。
- **不能压缩全部消息。** 最近窗口包含正在进行的工具调用、刚读到的文件、用户最新要求，这些不能被摘要化。必须保留 `COMPACT_KEEP_RECENT` 条原样不动。
- **字符数≠token 数。** 中英文、代码、JSON 的 token 密度都不一样。但第 08 章的目标是讲清楚机制，字符数估算简单、无依赖、容易测试。真实系统可以换成 tokenizer 或 API 返回的 usage。
- **压缩输入本身也可能超长。** 如果历史已经非常长，直接拿去总结也可能超限。所以 `build_compact_prompt` 里还有一层 `MAX_COMPACT_INPUT_CHARS` 截断。

## 小结

本章实现了一个最小版 Context Compact。

它的核心不是压缩算法，而是 Harness 里多了一个阶段：**每次调用模型前，先检查上下文压力，压力过大就压缩旧历史。**

```text
旧消息 -> 压缩成摘要    最近窗口 -> 原样保留
```

真实 Claude Code 有五级压缩策略（Snip 剪裁 → Microcompact 微压缩 → Context Collapse 折叠 → Autocompact 自动压缩 → Reactive Compact 应急压缩），从轻到重逐级升级。核心原则是**渐进退化**：能裁掉的内容先裁掉，实在不够了再上更重的方案。但无论几级，本质都是同一个问题：**这一轮对话里，模型应该把注意力放在什么上面？**

现在我的 Agent Harness 变成了：

```text
Agent Loop + Tool System + Permission Gate + Lifecycle Hooks
+ TodoWrite + Subagent + Skill Loading + Context Compact
```

下一章要解决的问题是：压缩只是当前会话续命，真正跨会话的记忆怎么做？这就是 Memory。
