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
#: 低于阈值就派离它最近的工人用围墙修补包补满血。天数固定为 1..10，所以直接写成
#: 查表，避免用公式把脚本里 500→600 那个跳变拟合错。
NIGHT_WALL_THRESHOLD = {4: 400, 5: 450, 6: 500, 7: 600, 8: 700, 9: 800, 10: 900}

#: 白天要买到手的围墙修补包数量（库存目标，不是"当天必须新买几个"）。
#: 选手要求"多买点"：在原表（5/6/10）基础上翻倍，并且第 4 天就直接拉到 10 个——
#: 夜间修墙是用量最大的时候，多囤点才扛得住多面墙同时被啃。
#: 两个工人各囤一份（第 4 天 100 金/天，第 6 天起 200 金/天），受金币与背包余量限制。
DAY_FIXER_PURCHASE = {
    4: 10,
    5: 12,
    6: 20,
    7: 20,
    8: 20,
    9: 20,
    10: 20,
}

#: 工人A的石头目标：第 1 天挖 10 块建"核心 10 面墙"，第 2 天挖 10 块建剩余 6 面
STONE_TARGET_DAY1 = 10
STONE_TARGET_DAY2 = 10
#: 第 3 天起补墙时，石头不够就一次挖 10 块
STONE_TARGET_REPAIR = 10

#: 天黑前回位的余量（回合）：剩余白天回合 <= 到站位距离 + 该余量 时就开始回位。
#: 取 6 而不是 3：围栏内侧走廊很窄，两个工人同时回位时会互相堵住（实测有 3 个回合
#: 完全走不通），余量太小就会迟到、赶不上"黑夜开始前必须到位"。
RECALL_MARGIN = 8
#: 围墙升级券最早在白天第几个回合开始买（脚本口径：每个白天的第 50 回合）
WALL_VOUCHER_FROM_ROUND = 50
#: 开拓者只在白天前 50 个回合内下单（超过就当天不买，直接回攻击位）
PIONEER_BUY_UNTIL_ROUND = 50


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
    core_walls: tuple[Pos, ...]        # 核心 10 面（第 1 天先建这批）
    rest_walls: tuple[Pos, ...]        # 剩余 6 面（第 2 天补齐）
    upgrade_walls: tuple[Pos, ...]     # 重点升级的 10 格，**已按升级顺序排好**：
                                       # 先"同一条纵向线上的 6 面"，再剩下 4 面
    night_worker_a: Pos                # 第 4 天起夜间工人A的待命站位
    night_worker_b: Pos                # 第 4 天起夜间工人B的待命站位


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
            # 升级顺序 = 先 x=x+3 这一整条纵向线上的 6 面（含上下两个角），
            # 再剩下 4 面（上排左 2 + 下排左 2）
            upgrade_walls=(
                Pos(x + 3, y + 2), Pos(x + 3, y + 1), Pos(x + 3, y),
                Pos(x + 3, y - 1), Pos(x + 3, y - 2), Pos(x + 3, y - 3),
                Pos(x + 1, y + 2), Pos(x + 2, y + 2),
                Pos(x + 2, y - 3), Pos(x + 1, y - 3),
            ),
            core_walls=(
                Pos(x + 1, y + 2), Pos(x + 2, y + 2), Pos(x + 3, y + 2),
                Pos(x + 3, y + 1), Pos(x + 3, y), Pos(x + 3, y - 1),
                Pos(x + 3, y - 2), Pos(x + 3, y - 3), Pos(x + 2, y - 3),
                Pos(x + 1, y - 3),
            ),
            rest_walls=(
                Pos(x - 2, y + 2), Pos(x - 1, y + 2), Pos(x, y + 2),
                Pos(x, y - 3), Pos(x - 1, y - 3), Pos(x - 2, y - 3),
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
        # 镜像版：先 x=x-2 这条纵向线上的 6 面，再剩下 4 面
        upgrade_walls=(
            Pos(x - 2, y + 2), Pos(x - 2, y + 1), Pos(x - 2, y),
            Pos(x - 2, y - 1), Pos(x - 2, y - 2), Pos(x - 2, y - 3),
            Pos(x, y + 2), Pos(x - 1, y + 2),
            Pos(x - 1, y - 3), Pos(x, y - 3),
        ),
        core_walls=(
            Pos(x, y + 2), Pos(x - 1, y + 2), Pos(x - 2, y + 2),
            Pos(x - 2, y + 1), Pos(x - 2, y), Pos(x - 2, y - 1),
            Pos(x - 2, y - 2), Pos(x - 2, y - 3), Pos(x - 1, y - 3),
            Pos(x, y - 3),
        ),
        rest_walls=(
            Pos(x + 1, y + 2), Pos(x + 2, y + 2), Pos(x + 3, y + 2),
            Pos(x + 3, y - 3), Pos(x + 2, y - 3), Pos(x + 1, y - 3),
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
#: * ``("walls", which)``                补墙；which = "core"(核心10) / "rest"(剩余6) / "all"(16)
#: * ``("stock_fixers",)``               把围墙修补包补到当天目标；全满级后改为买到没钱
#: * ``("wall_voucher",)``               买围墙升级券（有门限：武器全3级 + 第50回合起）
#: * ``("upgrade",)``                    用手里的券升级 10 面核心墙（先6同列再4，不买）
#: * ``("recall",)``                     第4天起：天黑前回夜间站位（优先级高于挖矿/购买）
#: * ``("ore",)``                        挖最近的铁/铜；白天任一种到 10 就去清仓，夜里只挖不卖
#: * ``("buy", 物品, N)``                买到手 N 个（钱不够就少买）
#: * ``("stance",)``                     开拓者走到攻击位（被占就用备选）
#: * ``("tasks",)``                      开拓者做自进化任务（状态机在 taskflow.py）
#: * ``("pioneer_buy",)``                开拓者每天最多买一次（按优先级挑物品）
#: * ``("use_vouchers",)``               开拓者用手里的券升级武器/基地
#: * ``("guard",)``                      夜间：基地危急就用券，否则操控火箭炮开火
#:
#: * ``("night_repair",)``               第 4 天起夜间：用围墙修补包修墙，否则回站位
#:
#: ⚠️ 夜里**没有** `("walls",)`：任务书 4.4 规定 `build` 仅白天可用，所以夜里不
#: 新建围墙（洞口等第二天白天补）。但用修补包修墙是 `use`，夜里照常做。
#:
#: ⚠️ 顺序语义（改计划表时必须注意）：
#: ``("ore",)`` 是"永远有事做"的步骤，一旦走到它后面就再也轮不到别的步骤，
#: 所以凡是要主动触发的步骤（买/升级）都必须排在 ``("ore",)`` 之前。
#: 正因为每回合都会从表头重算，排在前面的步骤条件不满足时返回 None 让位，
#: 条件满足的那一回合自然就被执行——不需要记住"今天买过没有"。
Step = tuple[Any, ...]


def worker_a_plan(day: int, is_day: bool, side: str) -> tuple[Step, ...]:
    """工人A（编号较小的那个工人）当天的步骤表。

    第 1 天：挖 10 石 → 建**核心 10 面**墙 → 挖矿
    第 2 天：挖 10 石 → 建**剩余 6 面**墙 → 挖矿
    第 3 天起：回位（第4天起）→ 备修补包 → 补被打空的墙 → 买围墙券（有门限）
              → 升级 10 面（6 同列 + 4）→ 挖矿
    """
    if not is_day:
        # 夜里不能建造围墙（任务书 4.4），但可以用围墙修补包修墙（use 无昼夜限制）。
        # 第 1~3 天夜里只挖矿；第 4 天起夜里改为"修墙或回站位待命"。
        if day <= 3:
            return (("ore",),)
        return (("night_repair",),)

    if day == 1:
        # 先造 3 座火箭炮（3*25=75 金，正好是初始金币），再挖 10 石建核心 10 面墙
        return (
            ("rockets",),
            ("stone", STONE_TARGET_DAY1),
            ("walls", "core"),
            ("ore",),
        )
    if day == 2:
        return (
            ("stone", STONE_TARGET_DAY2),
            ("walls", "rest"),
            ("ore",),
        )
    # 第 3 天起
    return (
        ("recall",),          # 第 4 天起生效：天黑前必须回到夜间站位（优先级最高）
        # 用手里已有的券升级：不占购物往返、就在基地旁，早上先干完最划算
        # （放后面会被"跑商店买修补包"整个吃掉白天，实测墙升级会拖到第 10 天）
        ("upgrade",),
        ("stock_fixers",),    # 购买优先级：修补包 > 围墙升级券
        ("walls", "all"),     # 16 面里被打空的补上
        ("wall_voucher",),    # 再按"能不能在天黑前赶回来"去商店补券
        ("ore",),
    )


def worker_b_plan(day: int, is_day: bool, side: str) -> tuple[Step, ...]:
    """工人B：白天"回位 → 备修补包 → 挖矿"，夜里同工人A（前3天挖矿、之后修墙）。"""
    if not is_day:
        if day <= 3:
            return (("ore",),)
        return (("night_repair",),)

    return (
        ("recall",),
        ("stock_fixers",),
        ("ore",),
    )


def pioneer_plan(day: int, is_day: bool, side: str) -> tuple[Step, ...]:
    """开拓者：白天做任务（仅前3天）→ 每天最多买一次 → 去攻击位升级武器 → 待命。

    * 夜里只负责"基地危急时用基地升级券 + 操控火箭炮开火"；
    * 白天前 3 天先做两个自进化任务，再做购买；
    * 购买由 ``_step_pioneer_buy`` 按优先级决定：武器升级券 > 基地升级券1（第6天起）
      > 基地升级券2（10 面墙全 3 级后）；超过第 50 回合就当天不买。
    """
    if not is_day:
        return (("guard",),)

    # ``("recall",)`` 放最前：只要发现"再不往回赶就赶不上了"，就立刻中止任务/购物回开火位。
    # 开拓者**每一天**都需要回位（工人只从第 4 天起需要）——"黑夜开始必须在炮位上"。
    if day <= 3:
        return (
            ("recall",),
            ("tasks",),
            ("pioneer_buy",),
            ("use_vouchers",),
            ("stance",),
        )
    return (("recall",), ("pioneer_buy",), ("use_vouchers",), ("stance",))


def night_wall_threshold(day: int) -> int:
    """当天的夜间修墙阈值；表外天数（异常输入）一律用最大的 900。"""
    return NIGHT_WALL_THRESHOLD.get(day, 900)
