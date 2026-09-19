#!/usr/bin/env python3
"""把"自进化任务流水线"的真实运行过程录成可读文本。

用法::

    python3 tests/trace_tasks.py            # 打印到 stdout
    python3 tests/trace_tasks.py > out.txt  # 存文件

录的是 agent 自己那一侧看到的东西：每回合的 response（prompt / executeCmd /
acceptTask / submitAnswer）、判题器回来的 llmResp 与 lastCmdResult、以及状态机
当时处在哪个 phase。第一个任务会把两段 prompt 的**完整原文**打出来，其余任务只
列阶段与耗时，避免文件里堆 6 段几乎一样的 prompt。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import brain  # noqa: E402
from agent.roundlog import RECORDER  # noqa: E402
from mock_judge import World  # noqa: E402

ROUNDS = 3 * 130  # 只有第 1~3 天白天做任务，录前 3 天足够


def prompt_sections(text: str) -> list[str]:
    """把 prompt 按 ``## 小节`` 拆开，只列小节标题与长度。"""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            out.append(line.strip())
    return out


def main() -> int:
    RECORDER.configure(path=None, echo=False)  # 本脚本自己不写回合日志
    brain._STATE = brain.MatchState()
    world = World((10, 24))

    out: list[str] = []
    task_no = 0
    first_task_dumped = False

    for round_no in range(1, ROUNDS + 1):
        payload = world.payload(round_no)
        response = brain.decide(payload)
        commands = response["roleCommandMap"] or {}
        actions = {uid: cmd.get("action") for uid, cmd in commands.items()}
        runner = brain._STATE.task

        interesting = (
            bool(response["prompt"])
            or bool(response["executeCmd"])
            or any(a in ("acceptTask", "submitAnswer") for a in actions.values())
        )
        if interesting:
            new_task = any(a == "acceptTask" for a in actions.values())
            if new_task:
                task_no += 1
                out.append("")
                out.append("=" * 96)
                out.append(f"任务 #{task_no}   接取回合 {round_no}（第 {(round_no - 1) // 130 + 1} 天）")
                out.append("=" * 96)
            out.append("")
            out.append(f"[回合 {round_no}] phase={runner.phase}  任务点={runner.index}")
            out.append(f"    我方指令      : {actions or '（本回合无角色指令）'}")
            if payload.get("llmResp"):
                out.append(f"    判题器 llmResp : {payload['llmResp'][:120]!r}")
            if payload.get("lastCmdResult"):
                out.append(f"    沙盒回执       : {payload['lastCmdResult'][:120]!r}")
            if response["prompt"]:
                dump = not first_task_dumped
                out.append(f"    → 发 prompt（{len(response['prompt'])} 字符），小节:")
                out.extend(f"         {s}" for s in prompt_sections(response["prompt"]))
                if dump:
                    out.append("        ┌── prompt 完整原文 ──────────────")
                    out.extend(f"        │ {l}" for l in response["prompt"].splitlines())
                    out.append("        └────────────────────────────────")
                    first_task_dumped = True
            if response["executeCmd"]:
                out.append(f"    → 发 executeCmd: {response['executeCmd']}")

        world.apply(response, round_no)
        world.tick(round_no)

    out.append("")
    out.append(f"（前 3 天共接取 {task_no} 个任务，提交 {world.submitted} 次）")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
