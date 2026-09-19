"""与基地坐标绑定的几何布局 + 逐日战术计划表。

本模块是"选手口述脚本"的直译，刻意保持无逻辑（只有坐标和计划表），
方便对着脚本逐条核对。所有坐标都写成"基地左上角 ``(x, y)`` 的相对形式"。

基地坐标约定（重要）：
    接口文档 1.3.1 注明"基地大小为 2*2，基地对应的 pos 传递的是左上角的坐标"，
    而地图原点在左下角、y 轴向上（任务书 4.1）。所以基地实际占据::

        (x,   y)   (x+1, y)      <- 上面一行（y 大）
        (x, y-1)   (x+1, y-1)    <- 下面一行（y 小）

    即占据范围 x∈[x, x+1]、y∈[y-1, y]。

两套镜像脚本（任务书 4.1：两个基地分别在左上/右下）：
* ``left``  —— 基地在左上（``x < 20``）
* ``right`` —— 基地在右下（``x > 20``）；边界 ``x == 20`` 归入 right
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import (
    WALL_FIXER,
    Pos,
)

LEFT = "left"
RIGHT = "right"

#: 夜间修补围墙的血量阈值，按天递增（脚本：第 4 天 400 → 第 10 天 900）。
#: 低于阈值就派最近的工人去用围墙修补包(回满血)。天数固定为 1..10，
#: 所以直接写成查表，避免用公式把脚本里那个 500→600 的跳变拟合错。
NIGHT_WALL_THRESHOLD = {4: 400, 5: 450, 6: 500, 7: 600, 8: 700, 9: 800, 10: 900}

#: 白天各角色要买到手的围墙修补包数量（第 4 天 5 个 → 第 6 天起 10 个）。
#: 语义是"库存目标"而不是"当天必须新买几个"：手里已经够了就不再买，
#: 夜里用掉之后第二天会自然补货。
DAY_FIXER_PURCHASE = {
    4: 5,
    5: 6,
    6: 10,
    7: 10,
    8: 10,
    9: 10,
    10: 10,
}

#: 第 1 天工人A挖石头的目标数量（挖满 20 再去建 16 面墙，留 4 块备用）
STONE_TARGET_FIRST_DAY = 20
#: 第 2/4 天工人A挖石头的目标数量（留着修补被打坏的墙）
STONE_TARGET_REPAIR_DAY = 10


def side_of(base: Pos) -> str:
    """按基地 x 坐标判断用哪一套脚本（``x < 20`` 走左，否则走右）。"""
    return LEFT if base.x < 20 else RIGHT


@dataclass(frozen=True, slots=True)
class Layout:
    """一套脚本用到的全部关键坐标（都由基地坐标推导，比赛期间不变）。"""

    base: Pos                          # 基地左上角
    side: str                          # "left" / "right"
    pioneer_stand: Pos                 # 开拓者白天/夜间的首选站位
    pioneer_fallbacks: tuple[Pos, ...]  # 首选站位被占时的备选，按优先级排列
    worker_a_stand: Pos                # 工人A第 1 天站这儿造 3 座火箭炮
    rockets: tuple[Pos, Pos, Pos]      # 3 座火箭炮的位置；顺序=建造顺序=夜间轮转开火顺序
    walls: tuple[Pos, ...]             # 围栏 16 格，按"顺时针绕一圈"排列
    upgrade_walls: tuple[Pos, ...]     # 重点升级的 10 格（朝向敌人的那一侧）
    night_worker_a: Pos                # 第 4 天起夜间工人A的站位
    night_worker_b: Pos                # 第 4 天起夜间工人B的站位


def layout(base: Pos, side: str | None = None) -> Layout:
    """由基地左上角坐标推出一整套关键坐标。

    ``side`` 一般不用传，留参数只是为了测试时强制指定某一套脚本。
    """
    side = side or side_of(base)
    x, y = base.x, base.y

    if side == LEFT:
        return Layout(
            base=base,
            side=side,
            # 站位选在基地左下偏左一格：它同时紧贴 3 座火箭炮（见下方 rockets），
            # 所以开拓者站这儿就能操控任意一座开火（火箭射程内的操控要求是距离≤1）
            pioneer_stand=Pos(x - 1, y - 1),
            # 首选站位被敌人/机器人占了时的退路，顺序即优先级：
            # (x-2,y-1) 还能管到 2 座火箭炮；(x-1,y+1) 只够管到 (x-1,y) 那一座
            pioneer_fallbacks=(Pos(x - 2, y - 1), Pos(x - 1, y + 1)),
            worker_a_stand=Pos(x - 1, y - 1),
            # 第 1 天要建的 3 座火箭炮。这个顺序有三重含义：
            #   1) 建造顺序；(2) 夜间"每回合换一座开火"的轮转顺序；
            #   3) 每个位置与 pioneer_stand 的切比雪夫距离都恰好是 1。
            rockets=(Pos(x, y - 2), Pos(x - 1, y), Pos(x - 1, y - 2)),
            # 16 格围栏，按顺时针（上边→右边→下边）列出。这个顺序同时也是
            # "绕圈补墙"的推荐行走顺序，虽然实际建造时按距离就近取。
            walls=(
                Pos(x - 2, y + 2), Pos(x - 1, y + 2), Pos(x, y + 2),
                Pos(x + 1, y + 2), Pos(x + 2, y + 2), Pos(x + 3, y + 2),
                Pos(x + 3, y + 1), Pos(x + 3, y), Pos(x + 3, y - 1),
                Pos(x + 3, y - 2),
                Pos(x + 3, y - 3), Pos(x + 2, y - 3), Pos(x + 1, y - 3),
                Pos(x, y - 3), Pos(x - 1, y - 3), Pos(x - 2, y - 3),
            ),
            # 只把这 10 格升到 3 级：右边一列 + 下边一排，即朝敌人（右下）的那一侧；
            # 剩下 6 格（上边左半 + 下边左半）保持 1 级。
            upgrade_walls=(
                Pos(x + 1, y + 2), Pos(x + 2, y + 2), Pos(x + 3, y + 2),
                Pos(x + 3, y + 1), Pos(x + 3, y), Pos(x + 3, y - 1),
                Pos(x + 3, y - 2), Pos(x + 3, y - 3), Pos(x + 2, y - 3),
                Pos(x + 1, y - 3),
            ),
            night_worker_a=Pos(x + 2, y + 1),
            night_worker_b=Pos(x + 2, y - 2),
        )

    # right：整体镜像。站位与火箭炮位置相对基地右侧，开火时的可达性同上。
    return Layout(
        base=base,
        side=side,
        pioneer_stand=Pos(x + 2, y - 1),
        pioneer_fallbacks=(Pos(x + 3, y - 1), Pos(x + 3, y + 1)),
        worker_a_stand=Pos(x + 2, y - 1),
        rockets=(Pos(x + 1, y - 2), Pos(x + 2, y), Pos(x + 2, y - 2)),
        walls=(
            Pos(x - 2, y + 2), Pos(x - 1, y + 2), Pos(x, y + 2),
            Pos(x + 1, y + 2), Pos(x + 2, y + 2), Pos(x + 3, y + 2),
            Pos(x - 2, y + 1), Pos(x - 2, y), Pos(x - 2, y - 1),
            Pos(x - 2, y - 2),
            Pos(x + 3, y - 3), Pos(x + 2, y - 3), Pos(x + 1, y - 3),
            Pos(x, y - 3), Pos(x - 1, y - 3), Pos(x - 2, y - 3),
        ),
        # 镜像版重点升级的是左边一列 + 下边一排（朝敌人左上/中部的那一侧）
        upgrade_walls=(
            Pos(x, y + 2), Pos(x - 1, y + 2), Pos(x - 2, y + 2),
            Pos(x - 2, y + 1), Pos(x - 2, y), Pos(x - 2, y - 1),
            Pos(x - 2, y - 2), Pos(x - 2, y - 3), Pos(x - 1, y - 3),
            Pos(x, y - 3),
        ),
        night_worker_a=Pos(x - 1, y + 1),
        night_worker_b=Pos(x - 1, y - 2),
    )


# --------------------------------------------------------------------------
# 计划表：一串有序步骤，每回合从头到尾求值，第一个"能动手"的步骤胜出
# --------------------------------------------------------------------------
#: 一个步骤就是一个元组，形如 ``(kind, 参数...)``。当前用到的 kind：
#:
#: * ``("rockets",)``                    第 1 天到 station 旁建 3 座火箭炮
#: * ``("stone", N)``                    挖石头到 N 块（当天配额，见 brain 里的闩）
#: * ``("walls",)``                      把 16 格围栏里缺的补上（没石头就先去挖）
#: * ``("upgrade",)``                    补墙 + 把 10 格重点墙升到 3 级（按等级自动选券）
#: * ``("ore",)``                        挖最近的铁/铜，满 20 就去小贩卖掉
#: * ``("buy", 物品, N)``                买到手 N 个（钱不够就少买）
#: * ``("stance",)``                     开拓者走到站位（被占就用备选）
#: * ``("tasks",)``                      开拓者做自进化任务（状态机在 taskflow.py）
#: * ``("station_voucher",)``            备一张基地升级券 1（基地还是 1 级且钱够时）
#: * ``("guard",)``                      夜间：基地危急就用券，否则操控火箭炮开火
#: * ``("night_repair",)``               第 4 天起夜间：按阈值修墙，否则回站位
#:
#: ⚠️ 顺序语义（改计划表时必须注意）：
#: ``("ore",)`` 是"永远有事做"的步骤，一旦走到它后面就再也轮不到别的步骤，
#: 所以凡是要主动触发的步骤（买/升级）都必须排在 ``("ore",)`` 之前。
#: 正因为每回合都会从表头重算，排在前面的步骤条件不满足时返回 None 让位，
#: 条件满足的那一回合自然就被执行——不需要记住"今天买过没有"。
Step = tuple[Any, ...]


def worker_a_plan(day: int, is_day: bool, side: str) -> tuple[Step, ...]:
    """工人A（编号较小的那个工人）当天的步骤表。"""
    if not is_day:
        if day <= 3:
            # 第 1 天白天往往来不及建完 16 面墙（挖 20 石头 + 绕圈建造约 75 回合），
            # 所以夜间先补墙再挖矿——围栏比多挖几块矿重要得多。
            return (("walls",), ("ore",))
        # 第 4 天起夜间改为站桩 + 按阈值修墙（站位由 brain 在 ctx 里给出）
        return (("night_repair",),)

    if day == 1:
        # 造炮（3*25=75 金，正好是初始金币）→ 挖 20 石头 → 建 16 面墙 → 挖矿卖钱
        return (
            ("rockets",),
            ("stone", STONE_TARGET_FIRST_DAY),
            ("walls",),
            ("ore",),
        )
    if day == 2:
        # 挖 10 石头备修 → 买券把 10 格重点墙升到 2 级 → 挖矿卖钱
        return (
            ("stone", STONE_TARGET_REPAIR_DAY),
            ("upgrade",),
            ("ore",),
        )
    if day == 3:
        # 只做升级：把 10 格重点墙从 2 级升到 3 级（有洞就先补）
        return (("upgrade",),)
    if day == 4:
        # 补洞 → 挖 10 石头 → 升级/修补 → 额外买 5 个围墙修补包
        return (
            ("stone", STONE_TARGET_REPAIR_DAY),
            ("upgrade",),
            ("buy", WALL_FIXER, DAY_FIXER_PURCHASE[4]),
        )
    # 第 5..10 天：补洞 → 备修补包 → 升级（若有 1/2 级墙）→ 剩下的时间挖矿卖钱
    return (
        ("walls",),
        ("buy", WALL_FIXER, DAY_FIXER_PURCHASE.get(day, 10)),
        ("upgrade",),
        ("ore",),
    )


def worker_b_plan(day: int, is_day: bool, side: str) -> tuple[Step, ...]:
    """工人B当天的步骤表：前 3 天纯挖矿，第 4 天起先备修补包再挖矿。"""
    if not is_day:
        if day <= 3:
            return (("ore",),)
        return (("night_repair",),)

    fixers = DAY_FIXER_PURCHASE.get(day, 0)
    if fixers:
        return (("buy", WALL_FIXER, fixers), ("ore",))
    return (("ore",),)


def pioneer_plan(day: int, is_day: bool, side: str) -> tuple[Step, ...]:
    """开拓者当天的步骤表：前 3 天做任务，第 4 天起原地待命（脚本要求"保持不动"）。"""
    if not is_day:
        if day <= 3:
            # 前 3 天夜里顺带把基地升级券买上（钱够的话），然后进入战位开火
            return (("station_voucher",), ("guard",))
        return (("guard",),)

    if day <= 3:
        return (("tasks",), ("station_voucher",), ("stance",))
    return (("stance",),)


def night_wall_threshold(day: int) -> int:
    """当天的夜间修墙阈值；表外天数（异常输入）一律用最大的 900。"""
    return NIGHT_WALL_THRESHOLD.get(day, 900)
