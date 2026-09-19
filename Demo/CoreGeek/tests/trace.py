#!/usr/bin/env python3
"""Per-round trace of the mock judge, for debugging the schedule.

逐回合打印 mock 判题器的世界状态与 agent 的决策，用来肉眼核对"某一天某个回合
工人 A / 工人 B / 开拓者到底在干什么"。它不参与断言，纯调试工具。

用法:  python3 Demo/CoreGeek/tests/trace.py [基地x] [基地y] [起始回合] [结束回合]
默认:  基地 (10,24)（左侧脚本）、回合 1..140（第 1 天白天 + 第 1 天黑夜）
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/ 目录自身入 path，才能 import 同目录的 mock_judge
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_judge import World  # noqa: E402

# src/ 入 path，才能 import 被测 agent
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import brain, tactics  # noqa: E402
from agent.protocol import Pos, ROUNDS_PER_DAY  # noqa: E402


def main() -> None:
    # 命令行参数：基地左上角坐标（接口文档 1.3.1 注：基地 pos 传左上角），以及打印区间
    base = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (10, 24)
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    end = int(sys.argv[4]) if len(sys.argv) > 4 else 140
    # 每跑一个 case 之前重置 agent 的跨回合状态（生产环境里一个进程只服务一局比赛）
    brain._STATE = brain.MatchState()
    world = World(base)
    # 用 agent 自己的 tactics 布局，保证调试输出与 agent 的"16 面墙"认知一致
    layout = tactics.layout(Pos(*base))
    for round_no in range(1, end + 1):
        payload = world.payload(round_no)
        response = brain.decide(payload)
        if round_no >= start:
            # 1 天 = 130 回合 = 白天 70 + 黑夜 60（任务书 4.2）
            day = (round_no - 1) // ROUNDS_PER_DAY + 1
            # 一局内前 70 回合为白天（任务书 4.2）
            phase = "DAY" if ((round_no - 1) % ROUNDS_PER_DAY) < 70 else "NIGHT"
            # 只取动作名与 name/num，压成一行方便对比相邻回合
            commands = {
                key: (value.get("action"), value.get("name") or value.get("num") or "")
                for key, value in (response.get("roleCommandMap") or {}).items()
            }
            # 每个工人背包里的石头数：核对"挖满 N 个石头再建墙"的配额是否触发
            stone = {u.unit_id: u.backpack.count("stone") for u in world.units if u.kind == "worker"}
            print(
                f"r{round_no:4d} d{day} {phase:5s} gold={world.gold:5d} "
                f"walls={len(world.walls):2d} towers={len(world.towers)} "
                f"stone={stone} cmds={commands}"
                # 非空 prompt/executeCmd 是本回合占用了 LLM / 沙盒通道（接口文档 2.1）
                + (f" prompt={len(response.get('prompt') or '')}" if response.get("prompt") else "")
                + (f" exec" if response.get("executeCmd") else "")
            )
            if response.get("prompt"):
                # 只打前 120 字符，避免刷屏
                print(f"        PROMPT: {response['prompt'][:120]!r}")
        # 先让"判题器"应用本回合指令，再推进世界（机器人移动/刷新）
        world.apply(response, round_no)
        world.tick(round_no)
        # rejected = 指令合法但执行不生效（任务书第八章"指令执行失败"），最多打 2 条
        if round_no >= start and world.rejected:
            for line in world.rejected[-2:]:
                print(f"        REJECT {line}")
            world.rejected.clear()
    # 收尾打印里程碑事件（造塔、领任务、提交答案、围墙被拆、机器人浪潮）
    print("events:", *world.events, sep="\n  ")


if __name__ == "__main__":
    main()
