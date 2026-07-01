# 第 19 章：MCP Plugin

## 本章解决什么问题？

前 18 天里，Agent 的工具能力都是写死在 Harness 里的。比如 `bash`、`read_file`、`task`、`team_run`、`worktree_create`，都是 Python 文件里的函数。

这对学习很方便，但真实 Agent 不可能把所有能力都内置进核心代码。不同项目会需要不同外部系统：数据库、浏览器、GitHub、Jira、监控、内部知识库、设计稿平台、云服务 API……如果每接一个系统都改 Harness 核心，系统会很快失控。

所以真实 Claude Code / Codex 类工具会把一部分能力抽象成外部插件或 MCP server：Harness 负责发现、展示和调用，具体能力由插件提供。

Day 19 我实现一个最小版 MCP Plugin：

> 用一个内存态 plugin registry 表示外部能力提供方，把插件工具统一暴露成 `mcp_list`、`mcp_read`、`mcp_call` 这组工具。

## 核心概念

Day 19 的流程是：

```text
mcp_register / builtin_mcp_plugins
  -> McpPlugin
  -> McpTool
  -> mcp_list / mcp_read
  -> mcp_call(plugin, tool, arguments)
```

这里有三个关键概念。

第一，McpPlugin。

Plugin 是能力提供方。它有名字、描述、是否启用，以及一组工具。

第二，McpTool。

Tool 是插件暴露出来的具体能力。它有工具名、描述和 input_schema。

第三，统一调用入口。

主 Agent 不直接调用某个外部系统，而是调用 `mcp_call(plugin, tool, arguments)`。这让 Harness 可以统一做发现、权限、启用/禁用、错误处理和审计。

## 我的实现

完整实现见：`code/s19_mcp_plugin.py`

Day 19 继续基于 Day 18，所以 Worktree Isolation、Autonomous Agents、Team Protocols、Agent Teams、Cron Scheduler、Background Tasks、Task System、Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在 MCP plugin registry 和 MCP 工具。

### McpTool 和 McpPlugin

先定义两个结构：

```python
class McpTool(TypedDict):
    """MCP 插件暴露的一个工具。"""

    name: str
    description: str
    input_schema: dict[str, Any]


class McpPlugin(TypedDict):
    """最小 MCP 插件记录。"""

    name: str
    description: str
    tools: dict[str, McpTool]
    enabled: bool
```

然后用一个全局 registry 保存插件：

```python
MCP_PLUGINS: dict[str, McpPlugin] = {}
MCP_LOCK = threading.RLock()
```

这里用锁是因为前面的后台任务、cron、subagent 都可能在不同线程里读取状态。

### 内置 workspace 插件

为了不依赖真实网络服务，我先做了一个内置演示插件：`workspace`。

```python
def builtin_mcp_plugins() -> dict[str, McpPlugin]:
    """返回内置演示 MCP 插件。"""
    workspace: McpPlugin = {
        "name": "workspace",
        "description": "Read-only workspace metadata provider.",
        "enabled": True,
        "tools": {
            "cwd": {
                "name": "cwd",
                "description": "Return current effective cwd for bash/tool execution.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            "stats": {
                "name": "stats",
                "description": "Return basic counts for tasks, teams, cron jobs, worktrees, and autonomous runs.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
        },
    }
    return {workspace["name"]: workspace}
```

它有两个工具：

- `workspace.cwd`：返回当前有效 cwd。
- `workspace.stats`：返回当前 Harness 状态计数。

这两个工具都是只读的，适合演示 MCP 的发现和调用流程。

### mcp_list 和 mcp_read

`mcp_list` 负责列出所有插件：

```python
def run_mcp_list() -> str:
    """工具：列出所有 MCP 插件和工具。"""
    ensure_mcp_plugins()
    with MCP_LOCK:
        plugins = list(MCP_PLUGINS.values())
    lines = ["MCP plugins:"]
    for plugin in plugins:
        tools = ", ".join(plugin["tools"].keys()) or "-"
        lines.append(f"- {plugin['name']} [{'enabled' if plugin['enabled'] else 'disabled'}]: {plugin['description']} (tools: {tools})")
    return "\n".join(lines)
```

`mcp_read` 读取单个插件详情：

```python
def run_mcp_read(plugin: str) -> str:
    """工具：读取一个 MCP 插件详情。"""
    ensure_mcp_plugins()
    with MCP_LOCK:
        record = MCP_PLUGINS.get(plugin)
    if record is None:
        return f"Error: MCP plugin {plugin!r} not found."
    ...
```

这对应真实系统里的“发现外部能力”。模型不能盲目调用它不知道的插件，应该先 list/read。

### mcp_call

统一调用入口是：

```python
def run_mcp_call(plugin: str, tool: str, arguments: dict[str, Any] | None = None) -> str:
    """工具：调用一个 MCP 插件工具。"""
    ensure_mcp_plugins()
    args = arguments or {}
    with MCP_LOCK:
        record = MCP_PLUGINS.get(plugin)
    if record is None:
        return f"Error: MCP plugin {plugin!r} not found."
    if not record["enabled"]:
        return f"Error: MCP plugin {plugin!r} is disabled."
    if tool not in record["tools"]:
        return f"Error: MCP tool {plugin}.{tool} not found."

    if plugin == "workspace" and tool == "cwd":
        return str(_CWD_STACK.current())
    if plugin == "workspace" and tool == "stats":
        return "\n".join([...])
```

真实 MCP 会通过 JSON-RPC 或进程通信把调用发给外部 server。本章没有接外部进程，只保留调用形状：`plugin + tool + arguments`。

### 启用 / 禁用 / 注册

我还加了三个管理工具：

- `mcp_register`：注册一个内存态占位插件。
- `mcp_enable`：启用或禁用插件。
- `mcp_list` / `mcp_read`：查看状态。

占位插件没有真实 handler，调用时会返回：

```text
MCP plugin xxx.yyy is registered but has no local handler in this minimal demo.
```

这正好说明了 Day 19 的边界：我实现的是 Harness 侧 registry，不是真实外部 MCP transport。

## 我踩的坑

### 坑 1：一开始想直接做真实 MCP server

MCP 的完整协议涉及 transport、JSON-RPC、server lifecycle、tool schema、resource、prompt、鉴权等内容。

如果 Day 19 直接实现完整 MCP，会偏离这个项目的节奏。

所以我先做 registry 和统一调用入口，让代码具备“插件化工具”的形状。未来要接真实 MCP server，可以把 `run_mcp_call()` 的内部实现换成进程通信或网络通信。

### 坑 2：插件工具和内置工具不能混在一起

最简单的做法是把 MCP 工具直接塞进 `TOOLS`，变成 `workspace_cwd`、`workspace_stats`。

但这样会污染工具命名空间，也看不出这些工具来自哪个 provider。

所以我选择统一入口：`mcp_call(plugin, tool, arguments)`。这样调用路径更明确，也方便权限和审计。

### 坑 3：插件状态需要 enabled 开关

外部插件可能失败、过期或被用户临时禁用。如果没有 enabled 状态，模型只要看到插件就会尝试调用。

所以 `McpPlugin` 有 `enabled` 字段，`mcp_call` 会先检查启用状态。

## 对应真实 Claude Code 的哪里

真实 Claude Code / Codex 类工具里，MCP 或插件系统通常对应这些能力：

- 发现外部 server 暴露的工具。
- 把外部工具 schema 注入模型可用工具列表。
- 调用外部工具并把结果转成 tool result。
- 管理 server 的启用、禁用、错误和权限。
- 将插件能力和内置工具统一纳入 Harness。

我这个最小实现对应的是：

```text
plugin registry -> tool discovery -> mcp_call -> normalized result
```

和真实系统相比，它还很简单：

- 没有真实 MCP transport。
- 没有外部进程生命周期管理。
- 没有鉴权和密钥管理。
- 没有资源、prompt、sampling 等高级 MCP 能力。
- 没有把插件工具动态展开成独立 model tools。

但核心思想已经出现：

> Harness 不应该只会调用自己内置的工具，还应该能发现、管理和调用外部能力提供方。

## 小结

Day 19 的关键词是：**外部能力插件化**。

内置工具让 Agent 能完成基本工作；MCP Plugin 让 Agent 能接入外部系统。

本章用一个内存态 registry 搭出了最小形状：插件可注册、可发现、可启用禁用、可统一调用。下一步如果要接真实 MCP server，只需要把 registry 背后的 transport 从本地函数换成协议调用。