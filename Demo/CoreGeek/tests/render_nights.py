#!/usr/bin/env python3
"""把每天夜晚开始时（机器人已刷出）的地图渲染成 ASCII，用于人工核对战术。

用法::

    python3 tests/render_nights.py            # 打印到 stdout
    python3 tests/render_nights.py > out.txt  # 存文件

渲染的是 **agent 自己收到的那一份 payload**（在第 N 天夜晚的第二个回合，
此时机器人已经刷出），所以图上看到的就是决策当时的真实局面，不含任何
"上帝视角"的补全。

两套基地各跑一整场（1300 回合）：左上角基地（x<20，left 脚本）和
右下角基地（x>20，right 脚本）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import brain, tactics  # noqa: E402
from agent.protocol import (  # noqa: E402
    COPPER,
    DAY_ROUNDS,
    IRON,
    ROUNDS_PER_DAY,
    STONE,
    Pos,
    Turn,
)
from agent.roundlog import RECORDER  # noqa: E402
from mock_judge import World  # noqa: E402

WIDTH, HEIGHT = 41, 32

#: 中立区域的显示代号（两个字符一格）
ZONE_MARKS = {
    "stone": "st", "iron": "fe", "copper": "cu",
    "vendor": "ve", "weaponShop": "sh",
    "challengerTaskPoint1": "t1", "challengerTaskPoint2": "t2",
    "defenderTaskPoint1": "t1", "defenderTaskPoint2": "t2",
}
#: 机器人的显示代号（小/中/大/BOSS）
ROBOT_MARKS = {
    "smallRobot": "sm", "middleRobot": "md",
    "largeRobot": "lg", "bossRobot": "bs",
}

LEGEND = """  图例:  .  空地          #1/#2/#3 我方围墙(数字=等级)   SB 我方基地(2x2)
         RK 我方火箭炮     wA/wB 工人A/工人B              pi 开拓者
         st/fe/cu 石/铁/铜矿   ve 小贩   sh 武器商店       t1/t2 任务点1/2
         sm/md/lg/bs 小/中/大/BOSS 机器人                  （敌方单位本 mock 未模拟）"""


def canvas(turn: Turn, worker_ids: dict[str, int]) -> list[list[str]]:
    """把一帧局面画成 32 行 x 41 列、每格两个字符的画布。"""
    grid = [[" ." for _ in range(WIDTH)] for _ in range(HEIGHT)]

    # 1) 中立区域（矿/小贩/商店/任务点）
    for pos, kind in turn.zones.items():
        grid[pos.y][pos.x] = ZONE_MARKS.get(kind, "??")

    # 2) 机器人（全图可见）
    for robot in turn.robots:
        grid[robot.pos.y][robot.pos.x] = ROBOT_MARKS.get(robot.kind, "rb")

    # 3) 我方单位（后画，优先级最高）
    for unit in turn.ours:
        if unit.kind == "wall":
            code = f"#{min(max(unit.level, 1), 3)}"      # 墙等级一眼可见
        elif unit.kind == "station":
            code = "SB"
        elif unit.kind == "rocket":
            code = "RK"
        elif unit.kind == "worker":
            code = "wA" if unit.unit_id == worker_ids.get("A") else "wB"
        elif unit.kind == "pioneer":
            code = "pi"
        else:
            continue
        for cell in turn.footprint(unit):                # 基地占 4 格
            grid[cell.y][cell.x] = code
    return grid


def draw(grid: list[list[str]]) -> str:
    """画布转文本。y 轴向上，所以从 y=31 打到 y=0。"""
    ruler = "   " + "".join(f"{x:2d}" for x in range(WIDTH))
    lines = [ruler]
    for y in range(HEIGHT - 1, -1, -1):
        lines.append(f"{y:2d} " + "".join(grid[y]))
    return "\n".join(lines)


def _bag(unit) -> str:
    """背包摘要：矿石按数量，道具按"名字x数量"。"""
    parts = []
    for ore, label in ((STONE, "石"), (IRON, "铁"), (COPPER, "铜")):
        count = unit.backpack.count(ore)
        if count:
            parts.append(f"{label}{count}")
    items: dict[str, int] = {}
    for item in unit.backpack:
        if item in (STONE, IRON, COPPER):
            continue
        items[item] = items.get(item, 0) + 1
    parts += [f"{name}x{n}" if n > 1 else name for name, n in sorted(items.items())]
    return "[" + ",".join(parts) + "]" if parts else "[]"


def summarize(turn: Turn, layout: tactics.Layout, worker_ids: dict[str, int]) -> str:
    """地图下面那股"数值现状"，用来核对战术是否按预期推进。"""
    out: list[str] = []
    station = turn.station()
    if station is not None:
        out.append(f"  基地 level{station.level} HP {station.health}   金币 {turn.gold}")
    else:
        out.append(f"  基地已被摧毁！   金币 {turn.gold}")

    standing = turn.walls()
    levels = [
        (turn.wall_at(pos).level if turn.wall_at(pos) else 0) for pos in layout.upgrade_walls
    ]
    threshold = tactics.night_wall_threshold(turn.day)
    damaged = [
        pos
        for pos in layout.walls
        if turn.wall_at(pos) is not None and turn.wall_at(pos).health < threshold
    ]
    out.append(
        f"  围墙 {len(standing)}/16   重点墙等级 {levels}   夜间阈值 {threshold}"
        f"（掉血低于阈值: {len(damaged)} 面）"
    )

    towers = "  ".join(
        f"RK({unit.pos.x},{unit.pos.y})L{unit.level}cd{unit.cooldown}"
        for unit in sorted(turn.weapons(), key=lambda u: (u.pos.x, u.pos.y))
    )
    out.append(f"  武器 {towers or '（无）'}")

    roles = []
    for unit in turn.ours:
        if unit.kind == "worker":
            tag = "wA" if unit.unit_id == worker_ids.get("A") else "wB"
        elif unit.kind == "pioneer":
            tag = "pi"
        else:
            continue
        roles.append(f"{tag}@({unit.pos.x},{unit.pos.y}) HP{unit.health} {_bag(unit)}")
    out.append("  角色 " + "   ".join(roles))

    tasks = "  ".join(
        f"t{point.index} {'可接' if point.usable else ('冷却' + str(point.cooldown) if point.cooldown else '已用完')}"
        for point in turn.task_points
    )
    out.append(f"  任务点 {tasks or '（无）'}")

    robots = turn.robots_by_priority()
    detail = "  ".join(f"{ROBOT_MARKS.get(r.kind, '??')}({r.health})" for r in robots)
    out.append(f"  机器人 {len(robots)} 只: {detail or '（无）'}")
    return "\n".join(out)


def render(turn: Turn, base: tuple[int, int], round_no: int, worker_ids: dict[str, int]) -> str:
    layout = tactics.layout(Pos(*base))
    parts = [
        "",
        "=" * 100,
        f"第 {turn.day} 天 · 夜晚 —— 回合 {round_no}（机器人已刷出）  "
        f"| 我方基地左上角 {base} | 脚本 {layout.side}",
        "=" * 100,
        LEGEND,
        draw(canvas(turn, worker_ids)),
        summarize(turn, layout, worker_ids),
    ]
    return "\n".join(parts)


def run_case(base: tuple[int, int], title: str, wave_factor: int = 1) -> list[str]:
    """跑一整场，在每个夜晚帧渲染一次。返回渲染好的文本行。

    ``wave_factor`` 把 mock 的夜袭规模放大（用"第 day*factor 天"的波次），
    默认 1 表示用原样波次；调大是为了让"夜间修墙"分支真的被触发，
    否则默认波次太弱，10 个夜晚都看不到一面墙掉到阈值以下。
    """
    brain._STATE = brain.MatchState()
    world = World(base)
    if wave_factor != 1:
        original = world._spawn_wave

        def scaled(day: int, round_no: int) -> None:      # type: ignore[no-untyped-def]
            original(day * wave_factor, round_no)

        world._spawn_wave = scaled
    blocks = [
        "",
        "#" * 100,
        f"# {title}   基地左上角 = {base}   夜袭倍率 x{wave_factor}",
        "#" * 100,
    ]
    for round_no in range(1, 10 * ROUNDS_PER_DAY + 1):
        payload = world.payload(round_no)          # 只取一次：多取会打乱任务流水线的回执
        response = brain.decide(payload)
        round_in_day = (round_no - 1) % ROUNDS_PER_DAY
        if round_in_day == DAY_ROUNDS + 1:         # 夜晚第 2 回合：机器人已出现在 payload 里
            turn = Turn.load(payload)
            blocks.append(render(turn, base, round_no, brain._STATE.worker_ids))
        world.apply(response, round_no)
        world.tick(round_no)
    return blocks


def main() -> int:
    RECORDER.configure(path=None, echo=False)      # 渲染时不需要回合日志
    wave_factor = 1
    if "--wave" in sys.argv:
        wave_factor = int(sys.argv[sys.argv.index("--wave") + 1])
    cases = (
        ((10, 24), "【A】我方基地在左上（x<20，left 脚本）"),
        ((30, 10), "【B】我方基地在右下（x>20，right 脚本）"),
    )
    out: list[str] = []
    for base, title in cases:
        out += run_case(base, title, wave_factor)
    text = "\n".join(out)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
