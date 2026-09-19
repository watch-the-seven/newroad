"""逐回合决策：把战术计划表翻译成"这一回合谁做什么"。

整体结构（三层）：

1. ``tactics.py`` 给出**计划表**——每天每个角色一串有序步骤（Step）。
2. ``decide()`` 每回合拿判题器的 payload 重建局面，把每个角色的计划表
   **从头求值一遍**，第一个"能动手"的步骤产出指令，该角色这回合就执行它。
3. 各 ``_step_*`` 函数负责一个步骤的具体行为（造炮/挖矿/建墙/升级/买/卖/开火）。

为什么每回合都从头算，而不是记住"今天做到第几步了"：
几乎没有需要跨回合记忆的东西——钱够不够、墙建好没、包够不够，全部能从
payload 里读出来。这样任何一次指令失败（被挡、被抢、钱不够）都会在下一回合
自动重试，不需要维护一份容易和现实脱节的长计划。真正需要跨回合记忆的只有
``MatchState`` 里那几个字段（基地布局、工人 A/B 的编号、火箭炮轮转下标、
采石配额闩、任务流水线）。

一回合内的协作约定：

* ``claimed``：本回合**已被占用/被预定**的格子集合。三个角色同时行动，
  靠它避免两个人抢同一格或同一个矿；每个 ``_goto_*`` 会把落脚点加进去。
* ``commands``：``{单位ID: 指令}``，最终变成响应里的 ``roleCommandMap``。
  key 可以是角色 ID（move/build/collect…），也可以是武器工事的 ID（attack）。
* ``top``：响应顶层的 ``prompt`` / ``executeCmd``，只有开拓者做任务时会写。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any, Sequence

from . import tactics
from .grid import next_step
from .roundlog import RECORDER as ROUND_LOG
from .protocol import (
    COPPER,
    IRON,
    ORE_SELL_THRESHOLD,
    STATION_VOUCHER1,
    STONE,
    Pos,
    Turn,
    Unit,
    VENDOR,
    WALL,
    WALL_FIXER,
    WALL_MAX_HEALTH,
    WALL_VOUCHER,
    WEAPON_BUILD_COST,
    WEAPON_SHOP,
    attack_command,
    build_command,
    buy_command,
    cells_within,
    collect_command,
    distance,
    footprint_distance,
    move_command,
    sell_command,
    station_footprint,
    use_command,
)
from .taskflow import WAIT, TaskRunner

LOGGER = logging.getLogger(__name__)

#: 基地血量低于这个值就动用基地升级券 1（升到 2 级并回满血，任务书 4.6.3）。
#: 阈值来自脚本："当基地血量小于 500 时使用基地升级卷1"。
STATION_EMERGENCY_HEALTH = 500

#: ``decide()`` 全程持锁：HTTP 服务是多线程的，而 MatchState 是全局可变状态。
_LOCK = threading.Lock()


class MatchState:
    """一场比赛里需要跨回合记住的少量状态。

    其余信息（金币、背包、墙的血量……）每回合都从 payload 重新读，不入这里。
    """

    def __init__(self) -> None:
        self.round_no = 0                                  # 上一回合的回合号（用于识别新一局）
        self.attack_index = 0                              # 火箭炮轮转开火下标（0..2）
        self.task = TaskRunner()                           # 开拓者任务流水线
        self.layout: tactics.Layout | None = None          # 由基地坐标推出的布局，整局不变
        self.worker_ids: dict[str, int] = {}               # {"A": 工人A的ID, "B": 工人B的ID}
        # 已挖够当日石头配额的 (天, 单位ID)，避免"建一面墙花掉石头后又跑回去挖"
        self.stone_quota: set[tuple[int, int]] = set()

    def sync(self, turn: Turn) -> None:
        """每回合开头调用：识别新一局、锁定布局与工人编号。"""
        if self.round_no and turn.round_no < self.round_no:
            # 回合号回退说明判题器开始了新的一局（同一个进程可能被复用）
            ROUND_LOG.marker("match-reset", previousRound=self.round_no)
            self.__init__()
        self.round_no = turn.round_no

        # 基地坐标决定了用 left 还是 right 那套脚本；基地不会移动，只在首次/变化时重算
        station = turn.station()
        if station is not None and (self.layout is None or self.layout.base != station.pos):
            self.layout = tactics.layout(station.pos)
            LOGGER.info(
                "base at (%s,%s) -> %s script", station.pos.x, station.pos.y, self.layout.side
            )

        # 工人 A/B 按接口文档的固定编号区分：编号小的叫 A。这里在第一回合记下来，
        # 之后即使其中一个阵亡，剩下的那个也不会"身份错乱"（不会把 B 当成 A）。
        workers = turn.workers()
        if len(workers) >= 2 and len(self.worker_ids) < 2:
            self.worker_ids = {"A": workers[0].unit_id, "B": workers[-1].unit_id}
            LOGGER.info("worker A=%s worker B=%s", workers[0].unit_id, workers[-1].unit_id)

        self.task.on_round(turn)


#: 全局比赛状态单例。判题器一个进程只跑一场比赛，所以用模块级单例最简单。
_STATE = MatchState()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """一回合的全部决策，返回判题器要的响应体。

    响应必须是 ``roleCommandMap`` + ``prompt`` + ``executeCmd`` 三个字段，
    其中后两个平时是空串，只有开拓者做任务那几回合才会有值。
    """
    started = time.perf_counter()
    turn = Turn.load(payload)
    with _LOCK:
        state = _STATE
        state.sync(turn)
        commands: dict[int, dict[str, Any]] = {}                 # 单位ID -> 指令
        top: dict[str, str] = {"prompt": "", "executeCmd": ""}   # 响应顶层字段
        claimed: set[Pos] = set()                                # 本回合已被预定的格子
        if state.layout is not None:                             # 还没见过基地就先不动
            if turn.is_day:
                _day(turn, state, commands, top, claimed)
            else:
                _night(turn, state, commands, top, claimed)
        _log(turn, commands, top)
        response = {
            # key 必须是字符串（接口文档 2.1 的例子），这里统一转换
            "roleCommandMap": {str(key): value for key, value in commands.items()},
            "prompt": top["prompt"],
            "executeCmd": top["executeCmd"],
        }
    # 完整记录放在锁外做，减少持锁时间；耗时包含决策本身
    ROUND_LOG.record(payload, response, (time.perf_counter() - started) * 1000)
    return response


def _log(turn: Turn, commands: dict[int, dict[str, Any]], top: dict[str, str]) -> None:
    """打一行人类可读的摘要（完整 request/response 由 roundlog 负责）。"""
    if not LOGGER.isEnabledFor(logging.INFO):
        return
    summary = " ".join(
        f"{unit_id}:{command.get('action')}" for unit_id, command in commands.items()
    )
    LOGGER.info(
        "r%s d%s %s gold=%s [%s] prompt=%s exec=%s",
        turn.round_no,
        turn.day,
        "DAY" if turn.is_day else "NIGHT",
        turn.gold,
        summary or "-",
        len(top["prompt"]),
        (top["executeCmd"][:80].replace("\n", " ") if top["executeCmd"] else "-"),
    )


# --------------------------------------------------------------------------
# 白天 / 夜晚 编排
# --------------------------------------------------------------------------
def _day(
    turn: Turn,
    state: MatchState,
    commands: dict[int, dict[str, Any]],
    top: dict[str, str],
    claimed: set[Pos],
) -> None:
    """白天：两个工人各跑自己的计划表，然后开拓者跑自己的。

    注意 ``claimed`` 是所有角色共用的——工人先决策会先占格，
    开拓者随后决策时会避开这些格子，避免两人撞在一起。
    """
    layout = state.layout
    assert layout is not None

    for role in turn.workers():
        plan = _worker_plan(turn, state, role, layout, is_day=True)
        _run_plan(turn, state, role, plan, layout, commands, top, claimed, {})

    pioneer = turn.pioneer()
    if pioneer is not None:  # 开拓者可能已阵亡
        plan = tactics.pioneer_plan(turn.day, True, layout.side)
        _run_plan(turn, state, pioneer, plan, layout, commands, top, claimed, {})


def _night(
    turn: Turn,
    state: MatchState,
    commands: dict[int, dict[str, Any]],
    top: dict[str, str],
    claimed: set[Pos],
) -> None:
    """夜晚：先算好"谁去修哪面墙"，再把 ctx 传给工人的计划表。

    ⚠️ 夜里**不能建造**围墙（任务书 4.4：``build`` 仅工人在白天可用），所以夜间
    不存在任何 ``build`` 动作；但**用围墙修补包修墙在夜里是允许的**（``use`` 没有
    昼夜限制），这正是下面这套"离哪面墙最近的工人去修"协调逻辑存在的理由。
    夜里被打坏的墙（洞口）只能等第二天白天重建——"白天没修完就没修完吧"。
    """
    layout = state.layout
    assert layout is not None

    workers = turn.workers()
    # 修墙是跨工人协调的（"离这面墙最近的工人去修"），所以放在这里统一分配，
    # 每个工人只拿到"分给我的那面墙"（可能为 None）。
    repairs = _assign_repairs(turn, layout, workers)
    for role in workers:
        plan = _worker_plan(turn, state, role, layout, is_day=False)
        # 第 4 天起夜里工人要站到固定位置待命，站位也通过 ctx 传下去
        stance = (
            layout.night_worker_a
            if _worker_label(state, role, workers) == "A"
            else layout.night_worker_b
        )
        ctx = {"repair": repairs.get(role.unit_id), "stance": stance}
        _run_plan(turn, state, role, plan, layout, commands, top, claimed, ctx)

    pioneer = turn.pioneer()
    if pioneer is not None:
        plan = tactics.pioneer_plan(turn.day, False, layout.side)
        _run_plan(turn, state, pioneer, plan, layout, commands, top, claimed, {})


def _worker_plan(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, *, is_day: bool
) -> tactics.Step:
    """按"这是工人A还是工人B"取对应的计划表。"""
    label = _worker_label(state, role, turn.workers())
    if label == "A":
        return tactics.worker_a_plan(turn.day, is_day, layout.side)
    return tactics.worker_b_plan(turn.day, is_day, layout.side)


def _worker_label(state: MatchState, role: Unit, workers: Sequence[Unit]) -> str:
    """判断角色是工人 A 还是 B。

    优先用开局记下的 ID（即使另一个工人已阵亡也能正确区分）；
    没有记录时退化成"当前活着的工人里编号较小的算 A"。
    """
    if state.worker_ids:
        if role.unit_id == state.worker_ids.get("A"):
            return "A"
        if role.unit_id == state.worker_ids.get("B"):
            return "B"
    return "A" if workers and role.unit_id == workers[0].unit_id else "B"


# --------------------------------------------------------------------------
# 计划表解释器
# --------------------------------------------------------------------------
def _run_plan(
    turn: Turn,
    state: MatchState,
    role: Unit,
    plan: Sequence[tactics.Step],
    layout: tactics.Layout,
    commands: dict[int, dict[str, Any]],
    top: dict[str, str],
    claimed: set[Pos],
    ctx: dict[str, Any],
) -> None:
    """按顺序求值计划表，直到某个步骤产出"东西"为止。

    ``_apply_step`` 的返回值是一个小协议（这是本模块最容易读错的地方）：

    * ``None``              → 这个步骤已完成/当前无需动作，继续看下一个步骤；
    * ``WAIT``              → 正在等外部（LLM/沙盒），**必须停下**，不许再往后走；
    * ``("prompt", 文本)``  → 写进响应顶层的 ``prompt``；
    * ``("exec", 命令)``    → 写进响应顶层的 ``executeCmd``；
    * ``(单位ID, 指令)``    → 写进 ``roleCommandMap``。

    以字符串开头的是前两种（顶层字段），以整数开头的是最后一种（单位指令），
    靠 ``isinstance(outcome[0], str)`` 区分。每个角色每回合最多产出一条指令。
    """
    for step in plan:
        outcome = _apply_step(turn, state, role, step, layout, claimed, ctx)
        if outcome is None:
            continue
        if outcome is WAIT:
            return
        if isinstance(outcome[0], str):
            kind, value = outcome
            if kind == "prompt":
                top["prompt"] = value
            elif kind == "exec":
                top["executeCmd"] = value
            return
        commands[outcome[0]] = outcome[1]
        return


def _apply_step(
    turn: Turn,
    state: MatchState,
    role: Unit,
    step: tactics.Step,
    layout: tactics.Layout,
    claimed: set[Pos],
    ctx: dict[str, Any],
) -> Any:
    """把 ``("kind", 参数...)`` 形式的步骤分派到具体实现。

    各 kind 的含义见 ``tactics.py`` 里 ``Step`` 的说明。
    """
    kind = step[0]
    if kind == "rockets":
        return _step_rockets(turn, role, layout, claimed)
    if kind == "stone":
        return _step_stone(turn, state, role, int(step[1]), claimed, latch=True)
    if kind == "walls":
        return _step_walls(turn, state, role, layout, claimed)
    if kind == "upgrade":
        return _step_upgrade(turn, state, role, layout, claimed)
    if kind == "ore":
        return _step_ore(turn, role, claimed)
    if kind == "buy":
        return _step_buy(turn, role, str(step[1]), int(step[2]), claimed)
    if kind == "stance":
        return _step_stance(turn, role, layout, claimed)
    if kind == "station_voucher":
        return _step_station_voucher(turn, role, claimed)
    if kind == "tasks":
        # 任务流水线自带状态，直接把移动能力（_goto_adjacent）借给它用
        return state.task.drive(turn, role, claimed, _goto_adjacent)
    if kind == "night_repair":
        return _step_night_repair(turn, role, layout, claimed, ctx)
    if kind == "guard":
        return _step_guard(turn, state, role, layout, claimed)
    return None


# --------------------------------------------------------------------------
# 移动helper
# --------------------------------------------------------------------------
def _adjacent(origin: Pos, target: Pos) -> bool:
    """切比雪夫距离 ≤1（任务书 4.5.4：距离一律用切比雪夫）。

    注意"距离 0"也算相邻：调用方一般还要额外判断 ``role.pos != target``，
    因为角色不该站到要建造/拆除/使用的那一格上。
    """
    return distance(origin, target) <= 1


def _goto_exact(
    turn: Turn, role: Unit, target: Pos, claimed: set[Pos]
) -> dict[str, Any] | None:
    """朝"精确站到 target 格"走一步。

    返回 ``None`` 有三种含义，调用方都无法区分（也不需要区分）：
    已经站在目标上了 / 目标当前不可站 / 这一回合到不了。
    成功时会把落脚点写进 ``claimed``，避免同回合其他角色也走向同一格。
    """
    if role.pos == target:
        return None
    if not turn.free(target, role):
        return None
    step = next_step(turn, role, target)
    if step is None or step in claimed:
        return None
    claimed.add(step)
    return move_command(step)


def _goto_adjacent(
    turn: Turn,
    role: Unit,
    targets: Sequence[Pos],
    claimed: set[Pos],
    radius: int = 1,
) -> dict[str, Any] | None:
    """朝"站到任意 target 周围 ``radius`` 格内"走一步。

    用于所有需要"贴近才能操作"的动作：建造/采集/使用/买卖（半径 1）。
    已经在范围内直接返回 ``None``（调用方据此认为本步已就绪）。

    实现要点：先把所有可达的候选落脚点按"离自己多近"排序，再逐个试 A*，
    第一个能走出一步的胜出——这样最近的矿/商店被堵住时会自动换下一个。
    """
    targets = tuple(targets)
    if not targets:
        return None
    for target in targets:
        if distance(role.pos, target) <= radius:
            return None
    blocked = turn.blocked(role)
    stands: list[tuple[int, int, int, Pos]] = []
    for target in targets:
        for stand in cells_within(target, radius):
            if stand == role.pos:
                return None
            # 不可站：不是空地、被单位/机器人占着、或同回合已被别人预定
            if not turn.land(stand) or stand in blocked or stand in claimed:
                continue
            stands.append((distance(role.pos, stand), stand.x, stand.y, stand))
    stands.sort(key=lambda item: item[:3])  # 就近优先，同距离按坐标稳定排序
    for _, _, _, stand in stands:
        step = next_step(turn, role, stand)
        if step is not None and step not in claimed:
            claimed.add(step)
            return move_command(step)
    return None


# --------------------------------------------------------------------------
# 工人相关步骤
# --------------------------------------------------------------------------
def _step_rockets(
    turn: Turn, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """第 1 天：走到基地旁，依次建起 3 座火箭炮（3×25=75 金，正好是初始金币）。

    建造顺序即 ``layout.rockets`` 的顺序；只有某个位置已经存在武器工事时才跳过，
    所以中途被打断（被挡/钱不够）时下回合会接着建剩下的。
    """
    for pos in layout.rockets:
        if turn.tower_at(pos) is not None:   # 这个位置已经有武器了（重复建造会覆盖，不必）
            continue
        if turn.gold < WEAPON_BUILD_COST:    # 钱不够就先去做别的（下一回合再回来）
            return None
        if _adjacent(role.pos, pos) and role.pos != pos:
            claimed.add(pos)
            return (role.unit_id, build_command(pos, "rocket"))
        # 不在旁边 → 先去站位（它同时贴着 3 个炮位），实在过不去再逐格贴近
        move = _goto_exact(turn, role, layout.worker_a_stand, claimed)
        if move is None:
            move = _goto_adjacent(turn, role, (pos,), claimed)
        if move is not None:
            return (role.unit_id, move)
    return None


def _step_stone(
    turn: Turn,
    state: MatchState,
    role: Unit,
    target: int,
    claimed: set[Pos],
    *,
    latch: bool = False,
) -> Any:
    """挖石头直到背包里有 ``target`` 块。

    ``latch=True`` 时会把"当天配额已完成"记进 ``state.stone_quota``：
    否则建墙花掉石头后数量又低于 target，工人会反复跑回矿区，
    实测能把 16 面墙拖到白天结束都建不完（这是踩过的坑）。

    ``latch=False`` 是"应急挖几块"（建墙时发现一块石头都没有），不受配额闩限制。
    """
    key = (turn.day, role.unit_id)
    if latch and key in state.stone_quota:
        return None
    if role.count(STONE) >= target:
        if latch:
            state.stone_quota.add(key)
        return None
    if role.backpack_full:  # 背包满了：交给 ore 步骤去卖东西，这里不硬挖
        return None
    mines = [pos for pos in turn.mine_positions((STONE,)) if pos not in claimed]
    if not mines:
        return None
    mines.sort(key=lambda pos: (distance(role.pos, pos), pos.x, pos.y))
    for mine in mines:
        if _adjacent(role.pos, mine) and role.pos != mine:
            claimed.add(mine)
            return (role.unit_id, collect_command(mine))
    move = _goto_adjacent(turn, role, mines, claimed)
    if move is not None:
        return (role.unit_id, move)
    return None


def _buildable(turn: Turn, pos: Pos, claimed: set[Pos]) -> bool:
    """这一格现在能不能建东西。

    ``turn.blocked()`` 不传 moving 参数，表示"场上任何单位占着都算被占"——
    建造目标格必须完全空着；再叠加本回合已被别人预定的格子。
    """
    return turn.land(pos) and pos not in turn.blocked() and pos not in claimed


def _step_walls(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """把 16 格围栏里缺的格子补上（被拆掉的墙也算"缺"）。

    石头用光时会先回去挖（一次挖够"剩余缺墙数"，上限 20），
    形成"挖一批 → 建一批"的循环，而不是建一面挖一块。
    """
    missing = [pos for pos in layout.walls if turn.wall_at(pos) is None]
    if not missing:
        return None
    if role.count(STONE) <= 0:
        # latch=False：这是应急挖矿，不受"当天已挖够 20 块"的闩限制
        return _step_stone(
            turn, state, role, min(20, len(missing)), claimed, latch=False
        )
    missing.sort(key=lambda pos: (distance(role.pos, pos), pos.x, pos.y))
    for pos in missing:
        if not _buildable(turn, pos, claimed):
            continue  # 这一格暂时建不了（被占），试下一个
        if _adjacent(role.pos, pos) and role.pos != pos:
            claimed.add(pos)
            return (role.unit_id, build_command(pos, WALL))
        move = _goto_adjacent(turn, role, (pos,), claimed)
        if move is not None:
            return (role.unit_id, move)
    return None


def _pending_wall_work(turn: Turn, layout: tactics.Layout) -> list[tuple[Pos, str]]:
    """算出 10 格重点墙上还差哪些作业，返回 ``[(位置, 要用的物品)]``。

    规则（任务书 4.6.3）：
    * 1 级墙 → 围墙升级券 1（升到 2 级，且升级会回满血）；
    * 2 级墙 → 围墙升级券 2（升到 3 级）；
    * 3 级墙但掉血了 → 围墙修补包。

    注意：规范（接口文档 1.3.1）说建筑一定带 ``level``，但这里对脏数据做了加固——
    若 ``level`` 缺失或被解析成 0，就按 1 级墙处理（用券 1），而不是让
    ``WALL_VOUCHER[0]`` 抛 KeyError。抛异常的后果很严重：该回合响应会退化成
    空指令，而且条件不会自愈，之后每回合都会失败，等于整局瘫痪。
    """
    pending: list[tuple[Pos, str]] = []
    for pos in layout.upgrade_walls:
        wall = turn.wall_at(pos)
        if wall is None:
            continue  # 缺墙的情况交给 _step_walls 重建
        if wall.level < 3:
            pending.append((pos, WALL_VOUCHER.get(wall.level, WALL_VOUCHER[1])))
        elif wall.health < WALL_MAX_HEALTH[3]:
            pending.append((pos, WALL_FIXER))
    return pending


def _step_upgrade(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """围栏作业总入口：先把 16 格补全，再把 10 格重点墙推到 3 级。

    三步优先级：① 补缺墙 → ② 用手里的券/修补包干活 → ③ 没钱没货就去买。
    每回合都会重来一遍，所以"钱不够先少买、攒够了再买"是自然发生的。
    """
    # ① 围栏有洞就先补（脚本：16 个地方都有城墙后才谈升级）
    if any(turn.wall_at(pos) is None for pos in layout.walls):
        outcome = _step_walls(turn, state, role, layout, claimed)
        if outcome is not None:
            return outcome

    pending = _pending_wall_work(turn, layout)
    if not pending:
        return None

    # ② 手里已经有的物品，按"离自己最近"的顺序用掉
    ordered = sorted(
        pending, key=lambda item: (distance(role.pos, item[0]), item[0].x, item[0].y)
    )
    for pos, item in ordered:
        if role.count_ci(item) <= 0:
            continue
        if _adjacent(role.pos, pos) and role.pos != pos:
            claimed.add(pos)
            return (role.unit_id, use_command(item, pos))
        move = _goto_adjacent(turn, role, (pos,), claimed)
        if move is not None:
            return (role.unit_id, move)

    # ③ 没有现货：统计还缺哪种券/包，去买能买得起的那一批（优先缺得多的物品）
    need = Counter(item for _, item in pending)
    for item, count in sorted(need.items(), key=lambda kv: (-kv[1], kv[0])):
        price = turn.price(item)
        if price <= 0:          # 商店清单里没有这个物品，跳过
            continue
        affordable = min(count, turn.gold // price)   # 钱不够就少买
        if affordable <= 0:
            continue
        outcome = _step_buy(turn, role, item, affordable, claimed, absolute=True)
        if outcome is not None:
            return outcome
    return None


def _step_ore(turn: Turn, role: Unit, claimed: set[Pos]) -> Any:
    """挖矿主线：卖满 20 的矿 → （背包满时清仓）→ 挖最近的铁/铜。

    脚本口径："挖最近的铜矿或铁矿，挖够 20 个就去卖给小贩"，
    且石头单独管理（不参与这里的卖矿），所以只看 IRON/COPPER 两种。
    """
    # 1) 某种矿攒够 20 就先去卖掉（铜价 5 > 铁价 3，所以先看铜）
    for ore in (COPPER, IRON):
        amount = role.count(ore)
        if amount >= ORE_SELL_THRESHOLD:
            outcome = _step_sell(turn, role, ore, amount, claimed)
            if outcome is not None:
                return outcome
    # 2) 背包满了（100 格）就清仓，石头放最后——它是建墙/修墙的材料，尽量留着
    if role.backpack_full:
        for ore in (COPPER, IRON, STONE):
            amount = role.count(ore)
            if amount:
                outcome = _step_sell(turn, role, ore, amount, claimed)
                if outcome is not None:
                    return outcome

    # 3) 挖最近的铁/铜；距离相同时优先铜（单价高）
    mines = [pos for pos in turn.mine_positions((IRON, COPPER)) if pos not in claimed]
    if not mines:
        return None
    mines.sort(
        key=lambda pos: (
            distance(role.pos, pos),
            0 if turn.zones.get(pos) == COPPER else 1,
            pos.x,
            pos.y,
        )
    )
    for mine in mines:
        if _adjacent(role.pos, mine) and role.pos != mine:
            claimed.add(mine)
            return (role.unit_id, collect_command(mine))
    move = _goto_adjacent(turn, role, mines, claimed)
    if move is not None:
        return (role.unit_id, move)
    return None


def _step_sell(
    turn: Turn, role: Unit, ore: str, amount: int, claimed: set[Pos]
) -> Any:
    """走到小贩身边把某种矿全卖掉（一次卖光，减少往返）。"""
    vendor = turn.nearest_zone(role.pos, VENDOR)
    if vendor is None:
        return None
    if _adjacent(role.pos, vendor) and role.pos != vendor:
        return (role.unit_id, sell_command(ore, amount))
    move = _goto_adjacent(turn, role, (vendor,), claimed)
    if move is not None:
        return (role.unit_id, move)
    return None


def _step_buy(
    turn: Turn,
    role: Unit,
    item: str,
    count: int,
    claimed: set[Pos],
    *,
    absolute: bool = False,
) -> Any:
    """到武器商店买 ``count`` 个 ``item``；钱不够就买得起几个买几个。

    ``absolute`` 决定 ``count`` 的含义：
    * ``False``（默认）：库存目标——手里已有 ``count`` 个就什么都不做；
    * ``True``：还要新买 ``count`` 个（调用方已经算过差量）。
    """
    if absolute:
        need = count
    else:
        have = role.count_ci(item)
        if have >= count:
            return None
        need = count - have
    price = turn.price(item)
    if price <= 0:                      # 不在商店清单里
        return None
    affordable = min(need, turn.gold // price)
    if affordable <= 0:                 # 一分钱都买不起 → 让位给后面的步骤
        return None
    shop = turn.nearest_zone(role.pos, WEAPON_SHOP)
    if shop is None:
        return None
    if _adjacent(role.pos, shop) and role.pos != shop:
        return (role.unit_id, buy_command(item, affordable))
    move = _goto_adjacent(turn, role, (shop,), claimed)
    if move is not None:
        return (role.unit_id, move)
    return None


# --------------------------------------------------------------------------
# 开拓者相关步骤
# --------------------------------------------------------------------------
def _step_stance(turn: Turn, role: Unit, layout: tactics.Layout, claimed: set[Pos]) -> Any:
    """走到站位（首选位置被占就用备选），站好了就返回 None。

    返回 WAIT 而不是 None 是刻意的：走不过去（被堵）时要停在这里，
    不能让上层继续执行后面的步骤。
    """
    for pos in (layout.pioneer_stand, *layout.pioneer_fallbacks):
        if role.pos == pos:
            return None                      # 已经站好了
        if not turn.free(pos, role):
            continue                         # 这格被占/不可站，试下一个
        move = _goto_exact(turn, role, pos, claimed)
        if move is not None:
            return (role.unit_id, move)
    return WAIT


def _step_station_voucher(turn: Turn, role: Unit, claimed: set[Pos]) -> Any:
    """备一张基地升级券 1（只在基地还是 1 级、手里没有、钱够时去买）。

    脚本原话是第 1 天白天买，但第 1 天往往钱不够（初始 75 金全用来造炮了），
    所以这里做成"第 1~3 天持续尝试"的目标，买到就不再跑动。
    """
    station = turn.station()
    if station is None or station.level >= 2:
        return None                          # 基地已 2 级，券 1 没用了
    if role.count_ci(STATION_VOUCHER1) > 0:
        return None                          # 手里已有
    return _step_buy(turn, role, STATION_VOUCHER1, 1, claimed, absolute=True)


def _pioneer_stand(turn: Turn, role: Unit, layout: tactics.Layout) -> Pos:
    """挑一个当前可站的站位：优先首选，其次备选；都被占就仍返回首选。"""
    for pos in (layout.pioneer_stand, *layout.pioneer_fallbacks):
        if pos == role.pos or turn.free(pos, role):
            return pos
    return layout.pioneer_stand


def _step_guard(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """夜间职责：基地危急时救基地，否则站好位操控火箭炮开火。

    顺序：① 基地血量 <500 且有券 → 用券（升到 2 级并回满血）；
         ② 走到站位；
         ③ 轮转开火。
    """
    station = turn.station()
    if (
        station is not None
        and station.health < STATION_EMERGENCY_HEALTH
        and role.count_ci(STATION_VOUCHER1) > 0
    ):
        footprint = station_footprint(station.pos)
        if footprint_distance(role.pos, footprint) <= 1:
            LOGGER.info("station hp=%s -> using %s", station.health, STATION_VOUCHER1)
            return (role.unit_id, use_command(STATION_VOUCHER1, station.pos))
        # 有券但离基地太远：先往基地靠（升级券要求站在目标建筑周围一格内）
        move = _goto_adjacent(turn, role, footprint, claimed)
        if move is not None:
            return (role.unit_id, move)

    stand = _pioneer_stand(turn, role, layout)
    if role.pos != stand:
        move = _goto_exact(turn, role, stand, claimed)
        if move is not None:
            return (role.unit_id, move)
        move = _goto_adjacent(turn, role, (stand,), claimed)
        if move is not None:
            return (role.unit_id, move)
        return WAIT   # 站位被占/到不了：这回合先不动（也别去干别的）
    return _fire(turn, state, role, layout)


def _fire(turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout) -> Any:
    """轮转操控 3 座火箭炮开火。

    火箭发射台每次发射后有 3 回合冷却（任务书 4.5.1），而我们有 3 座，
    所以"每回合换一座打"正好让每回合都有且只有一发，火力利用率最高——
    这就是脚本里那个开火顺序的由来。

    从 ``state.attack_index`` 开始依次找"没在冷却 + 自己够得着 + 有目标"的炮；
    找到就开火并把下标推进一位（形成轮转）；一圈都没有就这回合不开火。
    操控距离要求：角色必须站在武器周围一格内（任务书 4.4）。
    """
    total = len(layout.rockets)
    if not total:
        return None
    start = state.attack_index % total
    for offset in range(total):
        index = (start + offset) % total
        position = layout.rockets[index]
        tower = turn.tower_at(position)
        if tower is None or tower.cooldown > 0:
            continue                       # 这座还没造出来 / 还在冷却
        if distance(role.pos, position) > 1:
            continue                       # 站远了操控不了（比如退到备选站位时）
        target = _pick_target(turn, tower)
        if target is None:
            continue                       # 射程内没有机器人
        state.attack_index = index + 1     # 下回合从下一座开始
        # 命令的 key 是武器工事 ID，controllerId 才是开拓者 ID（接口文档 2.2）
        return (tower.unit_id, attack_command(role.unit_id, (target,)))
    return None


def _pick_target(turn: Turn, tower: Unit) -> Pos | None:
    """挑一个攻击落点。

    优先级：中型 > 小型 > 大型 > BOSS（脚本给定的顺序，也是"每发火箭换多少分"
    最优的顺序：中型 2 分/3 发 = 0.67，小型 1 分/2 发 = 0.5，BOSS 10 分/40 发 = 0.25，
    大型 4 分/25 发 = 0.16）。同类型里选离炮最近的，再按 ID 稳定排序。

    已知可改进点：不考虑"补刀"（优先打死血少的）与"溅射覆盖"
    （火箭 8 格溅射、多枚落点重叠时伤害叠加），也永远只传 1 个 targetPos，
    所以武器升到 2/3 级后必须同时改成传对应数量的落点，否则攻击非法。
    """
    reach = tower.range_of_attack()
    candidates = [
        robot
        for robot in turn.robots
        if robot.health > 0 and distance(tower.pos, robot.pos) <= reach
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda robot: (robot.priority, distance(tower.pos, robot.pos), robot.robot_id)
    )
    return candidates[0].pos


# --------------------------------------------------------------------------
# 夜间修墙调度
# --------------------------------------------------------------------------
def _assign_repairs(
    turn: Turn, layout: tactics.Layout, workers: Sequence[Unit]
) -> dict[int, Pos]:
    """把"掉血低于阈值"的墙分配给最近的工人，返回 ``{工人ID: 墙位置}``。

    脚本口径："如果有围墙的血量小于阈值，离这个围墙最近的工人就到这个围墙
    1 格距离使用围墙修理包"。多面墙同时告急时按血量从低到高贪心分配，
    一个工人一回合只负责一面墙。
    """
    threshold = tactics.night_wall_threshold(turn.day)
    damaged: list[tuple[int, Pos]] = []
    for pos in layout.walls:
        wall = turn.wall_at(pos)
        if wall is not None and wall.health < threshold:
            damaged.append((wall.health, pos))
    if not damaged:
        return {}
    damaged.sort(key=lambda item: (item[0], item[1].x, item[1].y))  # 最危险的先分配

    # 只有手里有围墙修补包的工人才参与分配
    carriers = [worker for worker in workers if worker.count_ci(WALL_FIXER) > 0]
    if not carriers:
        return {}
    assignment: dict[int, Pos] = {}
    taken: set[int] = set()           # 已接活的工人，一回合只修一面
    for _, pos in damaged:
        best: tuple[int, int] | None = None   # (距离, 工人ID)
        for worker in carriers:
            if worker.unit_id in taken:
                continue
            dist = distance(worker.pos, pos)
            if best is None or dist < best[0]:
                best = (dist, worker.unit_id)
        if best is None:
            break                     # 工人都派完了，剩下的墙这回合没人修
        assignment[best[1]] = pos
        taken.add(best[1])
    return assignment


def _step_night_repair(
    turn: Turn,
    role: Unit,
    layout: tactics.Layout,
    claimed: set[Pos],
    ctx: dict[str, Any],
) -> Any:
    """夜间：去修分配给我的那面墙（没有/没包就回站位待命）。

    ``ctx["repair"]`` 是 ``_assign_repairs`` 分给本工人的墙，
    ``ctx["stance"]`` 是本工人的夜间站位。
    """
    target = ctx.get("repair")
    stance = ctx.get("stance") or layout.night_worker_a

    if target is not None and role.count_ci(WALL_FIXER) > 0:
        wall = turn.wall_at(target)
        threshold = tactics.night_wall_threshold(turn.day)
        if wall is not None and wall.health < threshold:
            if _adjacent(role.pos, target) and role.pos != target:
                claimed.add(target)
                return (role.unit_id, use_command(WALL_FIXER, target))
            move = _goto_adjacent(turn, role, (target,), claimed)
            if move is not None:
                return (role.unit_id, move)

    # 没有修墙任务（或修不了）：回到自己的夜间站位待命，等下回合重新分配
    if role.pos == stance:
        return None
    move = _goto_exact(turn, role, stance, claimed)
    if move is not None:
        return (role.unit_id, move)
    move = _goto_adjacent(turn, role, (stance,), claimed)
    if move is not None:
        return (role.unit_id, move)
    return WAIT
