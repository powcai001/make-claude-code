#!/usr/bin/env python3
"""Day 02: Tool Use.

Day 01 的 Agent 已经有了循环。本章不改循环，只扩展工具层：

    tool schema -> tool handler -> dispatch map -> tool_result

模型看到的是结构化工具说明，真正执行动作的是 Harness。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

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
    "Use the available tools to solve the user's task. Act, don't explain."
)


def safe_path(path: str) -> Path:
    """把模型传来的路径限制在当前工作目录内。"""
    resolved = (WORKDIR / path).resolve()
    if resolved != WORKDIR and WORKDIR not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


def run_bash(command: str) -> str:
    """执行 shell 命令。这里仍然只是演示保护，不是完整权限系统。"""
    dangerous_fragments = [
        "rm -rf /",
        "sudo ",
        "shutdown",
        "reboot",
        "> /dev/",
        "format ",
        "del /f /s /q c:\\",
    ]
    lowered = command.lower()
    if any(fragment in lowered for fragment in dangerous_fragments):
        return "Error: dangerous command blocked by the Day 02 demo guard."

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
]


def print_assistant_text(content: Any) -> None:
    """打印 assistant 回复里的文本块。"""
    if isinstance(content, str):
        print(content)
        return

    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)


def agent_loop(messages: list[dict[str, Any]]) -> None:
    """Agent Loop 不关心具体工具，只负责分发和回填结果。"""
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
                try:
                    output = handler(**block.input)
                except Exception as exc:
                    output = f"Error: {exc}"
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
    """启动一个带多工具分发的最小 Agent。"""
    print("Day 02 Tool Use. 输入任务开始，输入 q / exit / 空行退出。")
    history: list[dict[str, Any]] = []

    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if query.strip().lower() in {"", "q", "exit"}:
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()


if __name__ == "__main__":
    main()
