# 我从零造 ClaudeCode

> 一个面向 Harness 工程师的实战项目：用 20 天从零实现一个 Claude Code 风格的 Agent Harness，并把过程写成一本中文电子书。

## 简介

我花 20 天，从零实现一个 Claude Code harness，并写成这本电子书《我从零造ClaudeCode》。这不是“教程翻译”，而是“我亲手造了一遍的过程记录”。

## 核心内容

- ✅ 21 章：从 agent loop 到多 agent 协作，完整复刻 Claude Code 的核心机制
- ✅ 20 个可运行/可阅读实现，前 10 章目标为可独立运行
- ✅ 每章记录“我踩的坑”，展示真实工程决策过程
- ✅ 彩蛋章节对照 claude-code-system-prompts，分析官方实现思路

## 目标读者

- 想冲 DeepSeek Harness 团队的工程师
- 已经会写代码，但想理解 agent harness 设计思想的人
- 想理解 `Model + Harness = Agent` 的实践者

## 仓库结构

```text
make-claude-code/
├── README.md
├── docs/                  # 电子书正文
├── code/                  # 每章对应实现
├── notes/                 # 每日学习日志草稿
├── assets/images/         # 配图、流程图、截图
└── .gitignore
```

## 目录

### Part 1：地基 —— 让 Agent 动起来

- [第 00 章：前言](docs/00-第零章-前言.md)
- [第 01 章：Agent Loop](docs/01-第一章-Agent-Loop.md)
- [第 02 章：Tool Use](docs/02-第二章-Tool-Use.md)
- [第 03 章：Permission](docs/03-第三章-Permission.md)
- [第 04 章：Hooks](docs/04-第四章-Hooks.md)

### Part 2：记忆与规划 —— 让 Agent 不失忆、不跑偏

- [第 05 章：TodoWrite](docs/05-第五章-TodoWrite.md)
- [第 06 章：Subagent](docs/06-第六章-Subagent.md)
- [第 07 章：Skill Loading](docs/07-第七章-Skill-Loading.md)
- [第 08 章：Context Compact](docs/08-第八章-Context-Compact.md)
- [第 09 章：Memory](docs/09-第九章-Memory.md)
- [第 10 章：System Prompt](docs/10-第十章-System-Prompt.md)
- [第 11 章：Error Recovery](docs/11-第十一章-Error-Recovery.md)

### Part 3：长期运行 —— 让 Agent 能自己跑很久

- [第 12 章：Task System](docs/12-第十二章-Task-System.md)
- [第 13 章：Background Tasks](docs/13-第十三章-Background-Tasks.md)
- [第 14 章：Cron Scheduler](docs/14-第十四章-Cron-Scheduler.md)

### Part 4：多 Agent 协作 —— 从单兵到团队

- [第 15 章：Agent Teams](docs/15-第十五章-Agent-Teams.md)
- [第 16 章：Team Protocols](docs/16-第十六章-Team-Protocols.md)
- [第 17 章：Autonomous Agents](docs/17-第十七章-Autonomous-Agents.md)
- [第 18 章：Worktree Isolation](docs/18-第十八章-Worktree-Isolation.md)

### Part 5：扩展与集成 —— 终点

- [第 19 章：MCP Plugin](docs/19-第十九章-MCP-Plugin.md)
- [第 20 章：Comprehensive](docs/20-第二十章-Comprehensive.md)
- [彩蛋章节：对照 ClaudeCode 真实系统提示词](docs/99-彩蛋章节-对照ClaudeCode真实系统提示词.md)

## 如何运行代码

前 10 章的代码文件目标是可独立运行：

```bash
python code/s01_agent_loop.py
python code/s02_tool_use.py
```

每章完整代码放在 `code/`，正文只保留关键片段和解释。

## 20 天路线

- 第 1-4 天：Agent Loop / Tool Use / Permission / Hooks
- 第 5-9 天：TodoWrite / Subagent / Skill Loading / Context Compact / Memory / System Prompt / Error Recovery
- 第 10-12 天：Task System / Background Tasks / Cron Scheduler
- 第 13-16 天：Agent Teams / Team Protocols / Autonomous Agents / Worktree Isolation
- 第 17-18 天：MCP Plugin / Comprehensive
- 第 19-20 天：彩蛋章节 + 全书打磨

## License

MIT
