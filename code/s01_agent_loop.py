#!/usr/bin/env python3
"""Day 01: Agent Loop.

这一章只实现 Claude Code 类 Agent 最核心的一件事：

    模型请求工具 -> Harness 执行工具 -> 结果塞回上下文 -> 再问模型

后续章节里的权限、Hooks、Todo、SubAgent、上下文压缩，都是围绕这个循环继续加工程层。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

try:
    import readline  # type: ignore

    # macOS libedit 下的 UTF-8 / backspace 兼容；Windows 没有 readline，忽略即可。
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

MODEL = os.environ.get("MODEL_ID")
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

SYSTEM = (
    f"You are a coding agent working in {os.getcwd()}. "
    "Use bash commands to solve the user's task. Act, don't explain."
)

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run.",
                }
            },
            "required": ["command"],
        },
    }
]


def run_bash(command: str) -> str:
    """执行一条 shell 命令，并把输出返回给模型。

    这里的危险命令拦截只是 Day 01 的演示保护，不是真正的权限系统，
    真正的 permission / sandbox 会在后续章节实现。
    """
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
        return "Error: dangerous command blocked by the Day 01 demo guard."

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
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


def print_assistant_text(content: Any) -> None:
    """打印 assistant 回复里的文本块。"""
    if isinstance(content, str):
        print(content)
        return

    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)


def agent_loop(messages: list[dict[str, Any]]) -> None:
    """最小 Agent Loop：持续执行 tool_use，直到模型停止请求工具。"""
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

            if block.name != "bash":
                output = f"Error: unknown tool {block.name!r}."
            else:
                command = block.input["command"]
                print(f"\033[33m$ {command}\033[0m")
                output = run_bash(command)
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
    """启动一个最小交互式 Agent。"""
    print("Day 01 Agent Loop. 输入任务开始，输入 q / exit / 空行退出。")
    history: list[dict[str, Any]] = []

    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
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
