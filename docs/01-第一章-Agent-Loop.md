# 第 01 章：Agent Loop

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s01_agent_loop.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s01_agent_loop.py)


## 本章解决什么问题？

如果只有一个大模型，它本质上只能做一件事：根据上下文预测下一段文本。

它可以告诉你“应该运行测试”，但它自己不会真的打开终端；它可以猜测某个文件哪里有 bug，但它自己不会真的读取文件；它可以写出一段修复方案，但它自己不会真的把代码改到仓库里。

所以，Claude Code 这类工具的第一层关键能力，不是“模型更聪明”，而是给模型外面套了一层 Harness：

```text
模型负责判断下一步做什么
Harness 负责把这一步真的执行掉
执行结果再回到模型上下文里
```

这就是 Agent Loop。

没有 Agent Loop，人类需要在模型和真实世界之间来回搬运信息：复制命令、运行命令、复制报错、再问模型。Agent Loop 把这件事自动化了：模型可以请求工具，Harness 执行工具，然后把工具结果作为新的上下文喂回模型。

本章我只做一件事：实现一个最小 Agent，它只有一个 `bash` 工具，但已经具备 Agent 的核心形态。

## 核心概念

一句话概括：

```text
一个工具 + 一个循环 = 一个 Agent
```

这不是完整的 Claude Code，但它已经包含了 Claude Code 类 Agent 的最小闭环：

```text
User Task
   |
   v
Messages + Tools  --->  Model
                           |
                           | tool_use
                           v
Harness executes tool
                           |
                           | tool_result
                           v
Messages append result ---> Model again
                           |
                           | end_turn
                           v
Done
```

几个核心概念：

- `messages`：对话历史，也是 Agent 的短期状态。模型每次判断下一步，都依赖这份上下文。
- `tools`：Harness 暴露给模型的外部能力。本章只有一个工具：`bash`。
- `tool_use`：模型不是直接执行命令，而是向 Harness 发出“我要调用某个工具”的请求。
- `tool_result`：Harness 执行工具后，把结果包装成消息，再交还给模型。
- `stop_reason`：循环是否结束的信号。如果模型还在请求工具，就继续；如果模型停止请求工具，就结束。

从 Harness Engineering 的角度看，Agent Loop 是最底层的运行时结构。后面的权限、Hooks、Todo、上下文压缩、多 Agent 协作，本质上都不是替代这个循环，而是围绕这个循环继续加控制层。

## messages 是怎么长出来的

理解 Agent Loop 最大的卡点，不是循环本身，而是 `messages` 这个数据结构到底长什么样、每一轮它是怎么变长的。这一节把它彻底讲清楚，因为后面所有章节（权限、压缩、子 Agent）都在往这份 `messages` 里塞东西。

### 一条消息 = 一个角色 + 一组内容块

`messages` 是一个数组，里面每条消息只有两个字段：`role`（谁说的）和 `content`（说了什么）。但 `content` 不一定是一段字符串——它常常是一个**内容块数组**，数组里可以混放不同类型的块：

```text
text 块   —— 普通文字
tool_use 块 —— 模型发起的工具调用请求（在 assistant 消息里）
tool_result 块 —— 工具执行后的结果（在 user 消息里）
```

这就是为什么 assistant 的回复经常长这样（一段文字 + 一个工具请求，放在同一条消息里）：

```python
{
    "role": "assistant",
    "content": [
        {"type": "text", "text": "好的，我先创建文件。"},
        {"type": "tool_use", "id": "call_00_abc", "name": "bash",
         "input": {"command": "echo 'print(\"hello agent\")' > hello.py"}}
    ]
}
```

### 一次完整任务，messages 长这样

下面是一个真实的例子：用户要求"创建 hello.py 并运行它"。整个过程中 `messages` 一共长了 6 条消息，我按发生顺序标号：

```python
messages = [
    # ① 用户最初的任务
    {"role": "user", "content": "在当前目录创建 hello.py，内容是打印 hello agent，然后运行它"},

    # ② 模型第一轮回复：说话 + 请求工具（content 是数组，可同时含文本块和工具块）
    {"role": "assistant", "content": [
        {"type": "text", "text": "好的，我先创建文件。"},
        {"type": "tool_use", "id": "call_00_abc", "name": "bash",
         "input": {"command": "echo 'print(\"hello agent\")' > hello.py"}}
    ]},

    # ③ 工具结果：role 是 "user"（不是 "tool"！），用 tool_result 块回传
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_00_abc",
         "content": "(no output, exit code 0)"}
    ]},

    # ④ 模型第二轮回复：看到结果后继续请求下一个工具
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_01_def", "name": "bash",
         "input": {"command": "python hello.py"}}
    ]},

    # ⑤ 第二个工具结果
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_01_def",
         "content": "hello agent"}
    ]},

    # ⑥ 模型最后一轮：不再请求工具，循环结束
    {"role": "assistant", "content": [
        {"type": "text", "text": "已完成：创建了 hello.py 并运行，输出 hello agent。"}
    ]},
]
```

### 三条铁律

整个 Agent Loop 里关于 messages 最容易踩错的，就是下面三条。记住它们，后面所有的坑都能自己推导出来。

**铁律一：assistant 只追加，不原地改。**
模型每轮返回的 `response.content` 必须整体 append 成一条新的 `assistant` 消息。哪怕它这一轮只说了句话、没调工具，也要追加。模型下一轮要读懂自己上一轮说了什么，靠的就是这条消息还在 `messages` 里。

**铁律二：tool_result 的 role 是 `user`，不是 `tool`。**
这是 Anthropic API 和 OpenAI API 最显眼的区别。OpenAI 用 `role: "tool"` 单独装结果；Anthropic 把工具结果塞进 `role: "user"` 的消息里，用 `type: "tool_result"` 区分。原因是：**从模型视角，工具结果是"环境（user 侧）提供的新信息"**，所以归到 user 角色。

**铁律三：tool_use_id 必须严格配对。**
②里的 `id` 和 ③里的 `tool_use_id` 必须一字不差。模型一轮里可能请求多个工具（content 数组里有多个 `tool_use` 块），那下一条 `user` 消息里就要返回**多个** `tool_result`，每个对上各自的 id。配错任意一个，API 直接报错。

### 一图看清 messages 如何随循环增长

把上面的 6 条消息画成时间线，就能看出 Agent Loop 到底在干什么——它其实就是在不停地往 `messages` 这个数组里**交替追加** assistant 消息和 user 消息：

```text
循环轮次        messages 新增的条目            说话方
─────────────────────────────────────────────────
（初始）   ①  user 任务                       user
 第1轮    ②  assistant 回复(含 tool_use)      assistant
          ③  user 工具结果(tool_result)        user(环境)
 第2轮    ④  assistant 回复(含 tool_use)      assistant
          ⑤  user 工具结果(tool_result)        user(环境)
 第3轮    ⑥  assistant 最终文字(无 tool_use) → 循环结束
```

每一轮循环做的事，就是往这个数组末尾加 2 条（一条 assistant + 一条 user 工具结果），然后把**整个数组**重新发给模型。模型是无状态的——它每次都从头读完整个 `messages`，假装自己记得前面发生过什么。所以 `messages` 不只是对话记录，它是**模型唯一的记忆载体**。

## 我的实现

完整实现见：`code/s01_agent_loop.py`

核心代码可以压缩成三部分。

第一部分：定义一个工具。

```python
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
            "required": ["command"],
        },
    }
]
```

这里要注意：模型看到的不是 Python 函数，而是一个工具描述。模型会根据 `name`、`description` 和 `input_schema` 决定什么时候调用它，以及给它传什么参数。

第二部分：Harness 真正执行命令。

```python
def run_bash(command: str) -> str:
    result = subprocess.run(
        command,
        shell=True,
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = (result.stdout + result.stderr).strip()
    if not output:
        output = f"(no output, exit code {result.returncode})"
    elif result.returncode != 0:
        output = f"(exit code {result.returncode})\n{output}"

    return output[:50_000]
```

模型只负责提出 `tool_use`，真正运行命令的是 Harness。

第三部分：最核心的循环。

```python
def agent_loop(messages: list[dict[str, Any]]) -> None:
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            print_assistant_text(response.content)
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            command = block.input["command"]
            output = run_bash(command)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": tool_results})
```

这段代码里最重要的是顺序：

1. 把当前 `messages` 和 `tools` 发给模型。
2. 把模型回复追加到 `messages`。
3. 如果模型没有请求工具，结束。
4. 如果模型请求工具，Harness 执行工具。
5. 把工具结果作为 `tool_result` 追加回 `messages`。
6. 回到第 1 步。

这就是一个最小 Agent 的心跳。

运行方式：

```bash
python code/s01_agent_loop.py
```

然后输入一个任务，例如：

```text
在当前目录创建 hello.py，内容是打印 hello agent，然后运行它
```

如果环境变量、依赖和模型配置正确，模型会先请求执行 shell 命令，Harness 执行后把结果返回给模型，模型再决定下一步。

## 我踩的坑

回头看，这一章真正卡住我的，全都集中在 `messages` 这个数据结构上。下面五个坑，前三个其实都是同一种错的变种——没真正理解 messages 是怎么长的。

### 1. 漏掉 assistant 那一步，messages 对不上（违反铁律一）

最容易写错的第一版循环长这样：

```python
# ❌ 错误：只把工具结果塞回去，没把 assistant 回复塞回去
response = client.messages.create(...)
tool_results = [...执行工具...]
messages.append({"role": "user", "content": tool_results})   # 直接跳过 assistant
messages_again = client.messages.create(..., messages=messages)
```

运行直接报错。报错信息会提示 `tool_result` 找不到对应的 `tool_use`。

为什么？因为 Anthropic API 要求每一条 `tool_result` 必须能向前找到一条同会话里的 `tool_use`。我跳过了 assistant 那条消息，等于让模型"凭空收到一个工具结果，但不知道是谁请求的"。这就是违反了**铁律一（assistant 只追加不原地改）**。

正确顺序必须严格交替：

```text
assistant(tool_use)  →  user(tool_result)
```

而且两者都要 append 进 messages，缺一不可。原文「messages 是怎么长出来的」那张时间线里，每一轮都是成对出现的两条——②③是一对，④⑤是一对，就是这个原因。

### 2. 把 tool_result 的 role 写成了 "tool"（违反铁律二）

我一开始是照着 OpenAI 的习惯写的：

```python
# ❌ OpenAI 风格，Anthropic 不认
{"role": "tool", "content": output}
```

Anthropic 的 API 会直接拒绝这个结构。正确写法是把结果装进一条 `role: "user"` 的消息，内容块类型声明为 `tool_result`：

```python
# ✅ Anthropic 风格
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        }
    ],
}
```

这就是**铁律二**。理解了原因就不容易再写错：从模型视角，工具结果是"环境（user 侧）递进来的新信息"，所以角色归 user。OpenAI 把它单独拎出来叫 `role: "tool"`，Anthropic 选择复用 user 角色、用 `type` 区分——两种设计都讲得通，但**不能混用**。

### 3. 一个 tool_use_id 配错，整轮崩（违反铁律三）

模型在一轮回复里可能同时请求多个工具，`content` 数组里塞了多个 `tool_use` 块：

```python
{"role": "assistant", "content": [
    {"type": "tool_use", "id": "call_A", "name": "bash", "input": {"command": "ls"}},
    {"type": "tool_use", "id": "call_B", "name": "bash", "input": {"command": "pwd"}},
]}
```

那下一条 user 消息里就必须返回**两个** `tool_result`，并且 `tool_use_id` 严格对应：

```python
{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "call_A", "content": "hello.py\n"},
    {"type": "tool_result", "tool_use_id": "call_B", "content": "/home/me"},
]}
```

我最早图省事，只处理了 `response.content[0]`，把后面那些 tool_use 块全丢了。结果是 `call_B` 永远等不到结果，API 报"有 tool_use 没有对应的 tool_result"。

这就是**铁律三**。正确做法是遍历整条回复的所有块：

```python
for block in response.content:
    if block.type == "tool_use":
        ...执行并收集结果，每个 block.id 都要配一个 tool_result
```

一个都不能漏，id 一个都不能错。

### 4. 把字符串当成了完整的 assistant 消息

还有一个隐晦的坑。模型某轮可能只回了文字、没调工具（比如最后那轮的收尾）。我一开始以为"没调工具就不用 append"，结果下一轮模型把前面自己说过的话全"忘"了，开始重复说一遍。

根因还是铁律一：**assistant 只追加，不原地改，也不省略**。不管这一轮有没有 tool_use，`response.content` 都要整体 append 进去。模型是无状态的，它每一轮都从头重读整个 messages，少一条它就真的不知道那条存在过。

判断循环是否结束，看的是 `stop_reason`，而不是"这一轮有没有 tool_use 块"。这两件事要分开：append 是无条件的，退出循环才看 stop_reason。

### 5. 以为危险命令黑名单就是权限系统

本章代码里有一个非常轻量的危险命令拦截，比如拦 `rm -rf /`、`shutdown`。但这不是安全沙箱，也不是完整权限系统，只是为了让示例别太容易翻车。

真正的权限边界至少要考虑：用户确认、命令分类、工作目录限制、文件读写范围、网络访问、超时和资源限制、审计日志。这些放到后面的 Permission 章节再展开。

我提这个坑，是因为它和前四个性质不同——前四个是"不理解 messages 结构"的错，这个是"把临时补丁当成了基础设施"的错。用前言里的话说：危险命令黑名单是会随模型变强而过时的**补丁**，而真正的权限系统是承担"行动边界"职责的**结构性分工**，两者不能混为一谈。

## 对应真实 Claude Code 的哪里

本章对应的是 Claude Code 类系统最核心的运行时循环：

```text
model call -> tool use -> tool execution -> tool result -> model call
```

真实 Claude Code 肯定比本章复杂得多，它不只有一个 `bash` 工具，还会有文件读写、搜索、任务列表、子 Agent、MCP、Hooks、权限系统、上下文压缩、后台任务等机制。

但这些机制的底层位置都可以放回这个循环里理解：

- 工具系统：决定模型能请求哪些外部动作。
- 权限系统：决定某个工具请求能不能执行。
- Hooks：在工具执行前后插入额外逻辑。
- Todo：把长任务状态显式化。
- Context Compact：当 `messages` 太长时压缩上下文。
- SubAgent：把一部分任务交给另一个循环。

所以第一章的目标不是复刻完整 Claude Code，而是先抓住它最小的骨架。

## 小结

本章我实现了一个只有 `bash` 工具的最小 Agent Loop。

它证明了一件事：Agent 并不神秘。模型本身不是 Agent，模型外面的 Harness 循环，才让模型拥有了“观察世界、采取行动、根据结果继续行动”的能力。

可以把本章记成一句话：

```text
Model 负责决定下一步，Harness 负责执行下一步，Agent Loop 负责让它们不断闭环。
```

从下一章开始，我会继续在这个循环上加能力：先把“工具调用”这件事讲得更细。
