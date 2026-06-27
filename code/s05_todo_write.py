#!/usr/bin/env python3
"""Day 05: TodoWrite.

Day 04 的 Agent 已经有了生命周期 Hooks。本章开始给 Harness 增加“可观察的
计划状态”：模型不再只把计划写在自然语言里，而是通过 todo_write 工具维护一份
结构化任务列表。

TodoWrite 在这里故意做成内存态：

    tool_use(todo_write) -> validate todos -> replace TODOS -> tool_result

它不负责真正执行任务，只负责让 Agent、Harness 和用户都能看见当前计划、当前焦点
以及已经完成的工作。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

try:
    import readline  # type: ignore

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MODEL = os.environ.get("MODEL_ID")
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

SYSTEM = (
    f"You are a coding agent working in {WORKDIR}. "
    "Use the available tools to solve the user's task. "
    "For multi-step work, maintain a visible task list with todo_write. "
    "The todo list must have at most one in_progress item."
)

PermissionDecision = Literal["allow", "deny", "ask"]
HookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
HookCallback = Callable[..., str | None]
TodoStatus = Literal["pending", "in_progress", "completed"]
TodoPriority = Literal["high", "medium", "low"]


class TodoItem(TypedDict):
    """TodoWrite 维护的最小任务结构。"""

    content: str
    status: TodoStatus
    priority: TodoPriority


# TodoWrite 在 Day 05 先只做内存态，后续 Memory 章节再考虑持久化。
TODOS: list[TodoItem] = []

# 永远拒绝：命中后不询问，直接返回 Permission denied。
DENIED_BASH_FRAGMENTS = [
    "rm -rf /",
    "sudo ",
    "shutdown",
    "reboot",
    "> /dev/",
    "format ",
    "mkfs",
    "dd if=",
    "del /f /s /q c:\\",
]

# 需要询问：不是必然危险，但会修改环境或依赖。
ASK_BASH_FRAGMENTS = [
    "rm ",
    "del ",
    "rmdir ",
    "move ",
    "copy ",
    "git reset",
    "git clean",
    "pip install",
    "npm install",
]

# Hook 注册表：事件名 -> 回调列表。
HOOKS: dict[HookEvent, list[HookCallback]] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


def register_hook(event: HookEvent, callback: HookCallback) -> None:
    """注册一个 hook 回调。"""
    HOOKS[event].append(callback)


def trigger_hooks(event: HookEvent, *args: Any) -> str | None:
    """触发生命周期事件；返回非 None 表示中断默认流程。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return str(result)
    return None


def safe_path(path: str) -> Path:
    """把模型传来的路径限制在当前工作目录内。"""
    resolved = (WORKDIR / path).resolve()
    if resolved != WORKDIR and WORKDIR not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


def run_bash(command: str) -> str:
    """执行 shell 命令。权限判断不写在这里，而是作为 PreToolUse hook。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds."
    except OSError as exc:
        return f"Error: {exc}"

    output = (result.stdout + result.stderr).strip()
    if not output:
        output = f"(no output, exit code {result.returncode})"
    elif result.returncode != 0:
        output = f"(exit code {result.returncode})\n{output}"

    return output[:50_000]


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件，并带上行号返回，方便模型后续精确编辑。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error: {exc}"

    lines = text.splitlines()
    if limit is not None and limit > 0 and len(lines) > limit:
        shown = lines[:limit]
        shown.append(f"... ({len(lines) - limit} more lines)")
    else:
        shown = lines

    numbered = [f"{index + 1:>4} | {line}" for index, line in enumerate(shown)]
    return "\n".join(numbered)[:50_000] if numbered else "(empty file)"


def run_write(path: str, content: str) -> str:
    """写入文件；父目录不存在时自动创建。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return f"Error: {exc}"

    return f"Wrote {len(content)} characters to {path}."


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """对文件做一次精确字符串替换。"""
    try:
        file_path = safe_path(path)
        content = file_path.read_text(encoding="utf-8", errors="replace")
        if old_text not in content:
            return f"Error: old_text not found in {path}."

        updated = content.replace(old_text, new_text, 1)
        file_path.write_text(updated, encoding="utf-8")
    except Exception as exc:
        return f"Error: {exc}"

    return f"Edited {path}."


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


def format_todos(todos: list[TodoItem]) -> str:
    """把当前 todo list 格式化成人和模型都容易读的摘要。"""
    if not todos:
        return "Todo list is empty."

    icons = {
        "pending": "□",
        "in_progress": "▶",
        "completed": "✓",
    }
    lines = ["Current todo list:"]
    for index, todo in enumerate(todos, start=1):
        icon = icons[todo["status"]]
        lines.append(
            f"{index}. {icon} [{todo['priority']}] {todo['content']} ({todo['status']})"
        )
    return "\n".join(lines)


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


def deny_reason(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """返回必须直接拒绝的原因；没有命中则返回 None。"""
    if tool_name != "bash":
        return None

    command = str(tool_input.get("command", "")).lower()
    for fragment in DENIED_BASH_FRAGMENTS:
        if fragment in command:
            return f"bash command contains denied fragment: {fragment!r}"
    return None


def permission_decision(
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[PermissionDecision, str]:
    """把一次工具调用分类成 allow / ask / deny。"""
    reason = deny_reason(tool_name, tool_input)
    if reason:
        return "deny", reason

    if tool_name in {"write_file", "edit_file"}:
        return "ask", f"{tool_name} modifies files in the workspace."

    if tool_name == "bash":
        command = str(tool_input.get("command", "")).lower()
        for fragment in ASK_BASH_FRAGMENTS:
            if fragment in command:
                return "ask", f"bash command may change files or environment: {fragment!r}"

    return "allow", "safe by current Day 05 policy"


def ask_user_permission(tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
    """把 ask 决策交给终端用户；默认拒绝。"""
    print(f"\n\033[33m⚠ Permission required: {tool_name}\033[0m")
    print(f"Reason: {reason}")
    print(f"Input: {tool_input}")
    choice = input("Allow? [y/N] ").strip().lower()
    return choice in {"y", "yes"}


def check_permission(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """工具执行前的统一权限入口。"""
    decision, reason = permission_decision(tool_name, tool_input)

    if decision == "allow":
        return True, reason

    if decision == "deny":
        print(f"\n\033[31m⛔ Permission denied: {reason}\033[0m")
        return False, reason

    if ask_user_permission(tool_name, tool_input, reason):
        return True, "approved by user"

    return False, f"user rejected permission request: {reason}"


ToolHandler = Callable[..., str]

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

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace. Returns content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "limit": {"type": "integer", "description": "Optional maximum number of lines."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write UTF-8 text content to a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace the first exact occurrence of old_text in a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "old_text": {"type": "string", "description": "Exact text to replace."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "todo_write",
        "description": (
            "Replace the visible task list for multi-step work. "
            "Use statuses pending, in_progress, completed and priorities high, medium, low. "
            "There must be at most one in_progress item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The full replacement todo list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Task description."},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": ["content", "status", "priority"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
]


def user_prompt_log_hook(query: str) -> str | None:
    """用户输入进入模型前，记录一次入口事件。"""
    print(f"\033[90m[hook:UserPromptSubmit] {query[:80]}\033[0m")
    return None


def log_hook(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """工具执行前记录日志。"""
    print(f"\033[90m[hook:PreToolUse] {tool_name}: {tool_input}\033[0m")
    return None


def permission_hook(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """把权限检查挂到 PreToolUse 生命周期。"""
    allowed, reason = check_permission(tool_name, tool_input)
    if allowed:
        return None
    return f"Permission denied: {reason}"


def large_output_hook(tool_name: str, tool_input: dict[str, Any], output: str) -> str | None:
    """工具执行后观察输出；这里先只做日志，不修改结果。"""
    if len(output) > 20_000:
        print(
            f"\033[90m[hook:PostToolUse] {tool_name} returned "
            f"{len(output)} characters; handler already truncated if needed.\033[0m"
        )
    return None


def todo_summary_hook(messages: list[dict[str, Any]]) -> str | None:
    """Agent 准备停止前打印当前 todo 状态，让计划对 Harness 可见。"""
    print(f"\033[90m[hook:Stop] conversation has {len(messages)} messages\033[0m")
    if TODOS:
        print("\033[90m[hook:Stop] todo state:\033[0m")
        print(format_todos(TODOS))
    return None


register_hook("UserPromptSubmit", user_prompt_log_hook)
register_hook("PreToolUse", log_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", todo_summary_hook)


def print_assistant_text(content: Any) -> None:
    """打印 assistant 回复里的文本块。"""
    if isinstance(content, str):
        print(content)
        return

    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)


def agent_loop(messages: list[dict[str, Any]]) -> None:
    """Agent Loop 只触发生命周期事件，不关心每个扩展逻辑的细节。"""
    if not MODEL:
        raise RuntimeError("请先在环境变量或 .env 中设置 MODEL_ID。")

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
            forced = trigger_hooks("Stop", messages)
            if forced:
                messages.append({"role": "user", "content": forced})
                continue

            print_assistant_text(response.content)
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            handler = TOOL_HANDLERS.get(block.name)
            if handler is None:
                output = f"Error: unknown tool {block.name!r}."
            else:
                print(f"\033[33m> {block.name}: {block.input}\033[0m")
                blocked = trigger_hooks("PreToolUse", block.name, block.input)
                if blocked is not None:
                    output = blocked
                else:
                    try:
                        output = handler(**block.input)
                    except Exception as exc:
                        output = f"Error: {exc}"
                    trigger_hooks("PostToolUse", block.name, block.input, output)
                print(output[:500])

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": tool_results})


def main() -> None:
    """启动一个带 TodoWrite 的最小 Agent。"""
    print("Day 05 TodoWrite. 输入任务开始，输入 q / exit / 空行退出。")
    history: list[dict[str, Any]] = []

    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if query.strip().lower() in {"", "q", "exit"}:
            break

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)


if __name__ == "__main__":
    main()
