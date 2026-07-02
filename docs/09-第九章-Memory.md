# 第 09 章：Memory

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s09_memory.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s09_memory.py)


## 本章解决什么问题？

第八章 做的是 Context Compact。它解决的是“当前会话太长”的问题：把早期历史压缩成摘要，让 Agent 不至于因为上下文窗口爆掉而停下来。

但 Compact 不是长期记忆。它有两个明显边界：

1. 它通常只存在于当前 `messages` 里，进程重启后就没了。
2. 它保存的是“这次对话怎么走到这里”，不适合保存稳定偏好和项目约定。

真实使用 Agent 时，我会不断重复告诉它一些事实：

- 这个项目用中文写文档。
- 新章节要同时更新 `code/` 和 `docs/`。
- 提交前要跑语法检查。
- 不要把 `.env`、本地缓存、私有配置写进 Git。

这些信息不应该每轮都靠用户重新输入，也不应该混在不断被压缩的聊天历史里。所以 第九章 我实现一个最小版 Memory：

> 把稳定事实写进工作区的持久化文件，并在每轮构造 system prompt 时重新注入。

## 核心概念

Memory 的流程是：

```text
user preference / project fact
  -> remember tool
  -> .agent_memory/project.md
  -> build_system_prompt()
  -> next model call
```

这里有三个关键概念。

第一，Memory 不是 conversation history。

对话历史记录的是“刚才发生了什么”；Memory 记录的是“以后也大概率成立的事实”。比如“用户喜欢中文解释”适合进入 Memory；“刚才 bash 命令输出了 20 行日志”不适合。

第二，Memory 要可见、可编辑。

我没有把记忆塞进数据库，也没有做向量检索，而是写到 `.agent_memory/project.md`。这让用户和开发者可以直接打开文件检查：Agent 到底记住了什么？有没有记错？要不要删掉？

第三，Memory 要有边界。

记忆能力很诱人，但如果什么都记，就会污染未来上下文。所以我给这个最小实现加了几个限制：

- 只保存 500 字以内的短事实。
- `remember` 和写文件一样需要权限确认。
- system prompt 明确说明：新用户指令优先于旧 Memory。
- `.agent_memory/` 默认加入 `.gitignore`，避免把本地偏好和隐私提交出去。

## 我的实现

完整实现见：`code/s09_memory.py`

第九章 的实现是在 第八章 的 Agent 上继续叠加，所以前面的工具调用、权限、Hook、TodoWrite、Subagent、Skill Loading、Context Compact 都保留。

新增的核心代码其实不多。

### Memory 文件

我先定义 Memory 的保存位置：

```python
MEMORY_DIR = WORKDIR / ".agent_memory"
PROJECT_MEMORY_FILE = MEMORY_DIR / "project.md"
MAX_MEMORY_CHARS = 12_000
```

这个路径有两个考虑。

第一，它在当前工作区下面，和项目绑定，而不是全局共享。不同项目的约定通常不同，不应该混在一起。

第二，它是 Markdown 文件，方便人类查看和修改。

对应初始化函数：

```python
def ensure_memory_file() -> None:
    """确保 Memory 目录和项目记忆文件存在。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not PROJECT_MEMORY_FILE.exists():
        PROJECT_MEMORY_FILE.write_text(
            "# Project Memory\n\n"
            "Stable facts remembered by the local coding agent.\n\n",
            encoding="utf-8",
        )
```

Agent 启动时会调用它：

```python
def main() -> None:
    """启动一个带持久化 Memory 的最小 Agent。"""
    ensure_memory_file()
    print("第九章 Memory. 输入任务开始，输入 q / exit / 空行退出。")
```

### 读取并注入 Memory

Memory 真正影响模型，是在构造 system prompt 的时候：

```python
def build_memory_block() -> str:
    """把持久化 Memory 转成 system prompt 可注入的短块。"""
    memory = read_project_memory().strip()
    if not memory:
        return ""
    return (
        "Persistent project memory is available below. Treat it as user/project context, "
        "but prefer newer explicit user instructions if there is a conflict.\n"
        f"{memory}"
    )
```

然后和 Skill Loading 的结果一起拼进 system prompt：

```python
def build_system_prompt(active_skills: list[Skill]) -> str:
    """根据 Memory 和本轮选中的技能动态构造 system prompt。"""
    blocks = [SYSTEM]
    memory_block = build_memory_block()
    skill_block = format_skill_summaries(active_skills)
    if memory_block:
        blocks.append(memory_block)
    if skill_block:
        blocks.append(skill_block)
    return "\n\n".join(blocks)
```

这里我特意写了冲突处理规则：

> prefer newer explicit user instructions if there is a conflict

也就是说，Memory 是背景上下文，不是最高优先级指令。用户当前明确说“这次不要提交”，就不能因为旧 Memory 里写着“完成后提交”而继续提交。

### remember 工具

为了让模型能主动保存稳定事实，我加了一个 `remember` 工具：

```python
def run_remember(text: str) -> str:
    """工具：把稳定事实追加到项目级 Memory 文件。"""
    if not isinstance(text, str) or not text.strip():
        return "Error: text must be a non-empty string."

    cleaned = " ".join(text.strip().split())
    if len(cleaned) > 500:
        return "Error: memory text should be concise and under 500 characters."

    ensure_memory_file()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with PROJECT_MEMORY_FILE.open("a", encoding="utf-8") as file:
        file.write(f"- {timestamp}: {cleaned}\n")
    return f"Remembered in {PROJECT_MEMORY_FILE.relative_to(WORKDIR)}: {cleaned}"
```

它做了三件事：

1. 校验输入必须是非空字符串。
2. 把多余空白压成一行，避免模型写入大段日志。
3. 追加时间戳，方便后续审计。

工具注册也很直接：

```python
TOOL_HANDLERS: dict[str, ToolHandler] = {
    ...
    "read_memory": lambda **kwargs: run_read_memory(),
    "remember": lambda **kwargs: run_remember(kwargs["text"]),
}
```

同时，`remember` 会被权限系统拦住：

```python
if tool_name in {"write_file", "edit_file", "remember"}:
    return "ask", f"{tool_name} modifies files in the workspace."
```

这点很重要。Memory 看起来只是“记一下”，本质上却是在写本地文件，而且会影响未来模型行为。它应该和普通写文件一样，需要用户确认。

### read_memory 工具

我还加了一个只读工具：

```python
def run_read_memory() -> str:
    """工具：读取当前持久化 Memory。"""
    memory = read_project_memory().strip()
    if not memory:
        return "No project memory has been saved yet."
    return memory
```

这样 Agent 或 Subagent 可以显式检查当前记忆，而不是只能被动依赖 system prompt。

## 我踩的坑

### 坑 1：一开始想把 todo list 也持久化

最开始我差点把 `TODOS` 也写进 Memory 文件。

后来发现这是两个完全不同的东西：

- TodoWrite 是当前任务计划，生命周期短。
- Memory 是稳定事实，生命周期长。

如果把 todo 也持久化，第二天打开项目时，Agent 可能还以为昨天那个 `in_progress` 任务没有完成，反而制造噪音。

所以 第九章 我只持久化“长期事实”，不持久化“当前计划”。

### 坑 2：Memory 不能无脑覆盖当前指令

如果 system prompt 里只写“以下是记忆”，模型可能会把旧记忆当成强约束。

比如 Memory 里有“完成章节后提交并推送”，但用户当前说“只修改文件，不要提交”。这时必须听当前用户。

所以我在 Memory block 里明确加了一句：

```text
prefer newer explicit user instructions if there is a conflict
```

这是一个很小的句子，但对 Harness 很关键：持久化上下文要有优先级边界。

### 坑 3：本地 Memory 不应该进 Git

Memory 里可能有用户偏好、机器路径、项目私有约定，甚至用户不小心让 Agent 记住的敏感内容。

所以我把 `.agent_memory/` 加进 `.gitignore`。

这也解释了为什么 `remember` 仍然需要权限确认：写 Memory 不是无害操作，它会改变未来行为。

## 小结

第九章 的关键词是：**稳定事实持久化**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类工具里，Memory 通常会体现为几类机制：

- 项目级说明文件：例如 `CLAUDE.md`、`AGENTS.md`、仓库里的开发约定文档。
- 用户级偏好：例如用户希望回答语言、代码风格、常用流程。
- 会话恢复摘要：和 第八章 的 Compact 接近，但可能被更持久地保存。
- Harness 注入逻辑：启动或每轮调用模型前，把相关记忆拼进 system/developer context。

我这个最小实现对应的是“项目级持久化记忆”：

```text
.agent_memory/project.md -> build_system_prompt() -> model call
```

和真实系统相比，它少了很多高级能力：

- 没有全局用户 Memory。
- 没有按相关性检索 Memory。
- 没有自动去重和冲突检测。
- 没有敏感信息扫描。
- 没有 Memory 编辑 UI。

但核心思想是一样的：

> 不是把所有历史都塞给模型，而是把少量稳定、有用、可复用的信息持久化，并在合适的时候重新注入上下文。


Context Compact 解决“当前会话如何续航”；Memory 解决“下次启动还要不要重新教一遍”。

我实现的版本很小：一个 Markdown 文件、两个工具、一次 system prompt 注入。但它已经具备真实 Agent Harness 里 Memory 的基本形状：可保存、可读取、可审计、可限制、可被当前指令覆盖。
