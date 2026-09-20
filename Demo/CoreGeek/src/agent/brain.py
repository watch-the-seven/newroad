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
from .grid import next_step, path_from, path_length
from .roundlog import RECORDER as ROUND_LOG
from .protocol import (
    COPPER,
    DAY_ROUNDS,
    PIONEER,
    Robot,
    IRON,
    NIGHT_SAFE_DISTANCE,
    ORE_SELL_THRESHOLD,
    STATION_VOUCHER1,
    STATION_VOUCHER2,
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
    WEAPON_VOUCHER,
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

#: 火箭单发中心伤害（任务书 4.5.1：火箭攻击力 20*等级，即每枚导弹 20）
#: 去商店买围墙券的往返安全余量（回合）
VOUCHER_TRIP_SLACK = 2
#: 升级链保留金：升级链（墙券 + 基地券）没走完之前，别把金币全砸在围墙修补包上，
#: 否则墙与基地会停在低等级——1 级墙 1000 血 vs 3 级 2000 血，对"活着"是反向的。
#: 开拓者的回位余量：它离开火位只有几步、路线也短（不像工人要绕围栏开口），
#: 用工人那套 8 回合余量会白扔一大截白天（实测会把墙/基地升级链挤垮）。
PIONEER_RECALL_MARGIN = 3
#: 设为 0 表示不保留（修补包优先到底）。
#: 取 400 是实测出来的甜点：修补包能囤到 20~30 个（原表只有 12~20），而且升级链
#: 反而更早完成（墙全 3 级 @682 回合 vs 原表 @930）——因为不再把金币全压在包里。
FIXER_CHAIN_RESERVE = 400
#: 火箭单发中心伤害（任务书 4.5.1：火箭攻击力 20*等级，即每枚导弹 20）
DAMAGE_PER_MISSILE = 20
#: 非火箭武器在 mock/正式接口里的单发伤害（加特林 10、电磁按能量，这里保守取 10）
DAMAGE_OTHER_WEAPON = 10
#: 开拓者只打"自己这一边"的机器人：|基地y - 机器人y| <= 这个值（选手要求 9）
OUR_SIDE_Y_SPAN = 9

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
        # 正在清仓的工人ID：任一种矿到 10 后要把背包里的铜和铁全部卖光（一回合一种）。
        # 需要它是因为卖掉第一种后"任一种到 10"可能不再成立，光看阈值会漏掉第二种。
        self.sell_pending: set[int] = set()
        # 已经"下过单/放弃下单"的 (天, 单位ID)：开拓者每天最多买一次
        self.daily_purchase: set[tuple[int, int]] = set()

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
        return _step_walls(
            turn, state, role, layout, claimed, str(step[1]) if len(step) > 1 else "all"
        )
    if kind == "upgrade":
        return _step_upgrade(turn, state, role, layout, claimed)
    if kind == "wall_voucher":
        return _step_wall_voucher(turn, state, role, layout, claimed)
    if kind == "stock_fixers":
        return _step_stock_fixers(turn, state, role, layout, claimed)
    if kind == "recall":
        return _step_recall(turn, state, role, layout, claimed)
    if kind == "ore":
        return _step_ore(turn, state, role, claimed)
    if kind == "buy":
        return _step_buy(turn, role, str(step[1]), int(step[2]), claimed)
    if kind == "stance":
        return _step_stance(turn, role, layout, claimed)
    if kind == "pioneer_buy":
        return _step_pioneer_buy(turn, state, role, layout, claimed)
    if kind == "use_vouchers":
        return _step_use_vouchers(turn, state, role, layout, claimed)
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
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    extra: frozenset[Pos] = frozenset(),
) -> dict[str, Any] | None:
    """朝"精确站到 target 格"走一步。

    返回 ``None`` 有三种含义，调用方都无法区分（也不需要区分）：
    已经站在目标上了 / 目标当前不可站 / 这一回合到不了。
    成功时会把落脚点写进 ``claimed``，避免同回合其他角色也走向同一格。
    """
    if role.pos == target:
        return None
    if not turn.free(target, role) or target in extra:
        return None
    step = next_step(turn, role, target, extra)
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
    extra: frozenset[Pos] = frozenset(),
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
            if not turn.land(stand) or stand in blocked or stand in claimed or stand in extra:
                continue
            stands.append((distance(role.pos, stand), stand.x, stand.y, stand))
    stands.sort(key=lambda item: item[:3])  # 就近优先，同距离按坐标稳定排序
    for _, _, _, stand in stands:
        step = next_step(turn, role, stand, extra)
        if step is not None and step not in claimed:
            claimed.add(step)
            return move_command(step)
    return None


# --------------------------------------------------------------------------
# 夜间安全（选手要求：夜里挖矿不得接近机器人两格内，绝不能被机器人打死）
# --------------------------------------------------------------------------
def _danger_cells(turn: Turn) -> frozenset[Pos]:
    """夜间禁区：机器人周围 ``NIGHT_SAFE_DISTANCE - 1`` 格内的所有格子。

    注意机器人攻击距离是 3 格（任务书 4.7.2），所以按选手字面要求的"两格"建禁区
    并不能保证绝对打不到；这是**遵从选手口径**的实现，真要绝对安全应改成 >=4。
    """
    cells: set[Pos] = set()
    for robot in turn.robots:
        if robot.health <= 0:
            continue
        cells.add(robot.pos)
        cells.update(cells_within(robot.pos, NIGHT_SAFE_DISTANCE - 1))
    return frozenset(cells)


def _mine_is_safe(turn: Turn, mine: Pos, danger: frozenset[Pos]) -> bool:
    """这个矿值不值得挖：至少要有一个相邻格不在禁区里（否则站过去就挨打）。"""
    return any(
        turn.land(cell) and cell not in danger for cell in cells_within(mine, 1)
    )


def _retreat(
    turn: Turn, role: Unit, claimed: set[Pos], danger: frozenset[Pos], radius: int = 4
) -> dict[str, Any] | None:
    """从禁区里撤出来（已经被机器人逼近时的保命动作）。"""
    stands = [
        cell
        for cell in cells_within(role.pos, radius)
        if turn.land(cell)
        and cell not in turn.blocked(role)
        and cell not in danger
        and cell not in claimed
    ]
    stands.sort(key=lambda cell: (distance(role.pos, cell), cell.x, cell.y))
    for stand in stands:
        move = _goto_exact(turn, role, stand, claimed)
        if move is not None:
            return move
    return None


# --------------------------------------------------------------------------
# 满级判定（决定"只买修补包"和"开始买围墙券"的时机）
# --------------------------------------------------------------------------
def _weapons_maxed(turn: Turn) -> bool:
    """3 座武器是否都已到 3 级。"""
    weapons = turn.weapons()
    return len(weapons) >= 3 and all(tower.level >= 3 for tower in weapons)


def _core_walls_maxed(turn: Turn, layout: tactics.Layout) -> bool:
    """核心 10 面墙是否都已到 3 级。"""
    for pos in layout.core_walls:
        wall = turn.wall_at(pos)
        if wall is None or wall.level < 3:
            return False
    return True


def _everything_maxed(turn: Turn, layout: tactics.Layout) -> bool:
    """武器 + 核心墙 + 基地 是否全部到 3 级（之后每天只买修补包）。"""
    station = turn.station()
    if station is None or station.level < 3:
        return False
    return _weapons_maxed(turn) and _core_walls_maxed(turn, layout)


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
    turn: Turn,
    state: MatchState,
    role: Unit,
    layout: tactics.Layout,
    claimed: set[Pos],
    which: str = "all",
) -> Any:
    """把围栏里缺的格子补上（被拆掉的墙也算"缺"）。

    ``which`` 指定补哪一批：``"core"`` 核心 10 面（第 1 天）、``"rest"`` 剩余 6 面
    （第 2 天）、``"all"`` 全部 16 面（第 3 天起补被打空的）。

    石头用光时会先回去挖（一次挖够"剩余缺墙数"，上限 10），
    形成"挖一批 → 建一批"的循环，而不是建一面挖一块。
    """
    positions = {
        "core": layout.core_walls,
        "rest": layout.rest_walls,
        "all": layout.walls,
    }.get(which, layout.walls)
    missing = [pos for pos in positions if turn.wall_at(pos) is None]
    if not missing:
        return None
    if role.count(STONE) <= 0:
        # latch=False：这是应急挖矿，不受"当天已挖够"的闩限制
        return _step_stone(
            turn, state, role, min(tactics.STONE_TARGET_REPAIR, len(missing)), claimed,
            latch=False,
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
    """用手里的券升级核心 10 面墙（**不负责购买**，购买在 _step_wall_voucher）。

    顺序严格按 ``layout.upgrade_walls``：先"同一条纵向线上的 6 面"，再剩下 4 面。
    1 级墙用券 1、2 级墙用券 2、3 级但掉血用修补包；升级会回满血（任务书 4.6.3）。
    """
    pending = _pending_wall_work(turn, layout)
    if not pending:
        return None
    ordered = sorted(
        pending,
        key=lambda item: (
            layout.upgrade_walls.index(item[0]) if item[0] in layout.upgrade_walls else 99,
            distance(role.pos, item[0]),
        ),
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
    return None


def _trip_time_ok(
    turn: Turn, role: Unit, layout: tactics.Layout, state: MatchState, shop: Pos
) -> bool:
    """现在去商店买东西，还来不来得及在天黑前回到夜间站位？

    第 4 天起"黑夜开始前必须在指定位置"优先级高于购买，所以来不及就不去——
    否则会在半路被召回接管，停在离站位好几格的地方（实测就是这样）。
    """
    if turn.day < 4:
        return True
    stance = (
        layout.night_worker_a
        if _worker_label(state, role, turn.workers()) == "A"
        else layout.night_worker_b
    )
    to_shop = path_length(turn, role, shop)
    back = path_from(turn, shop, stance, turn.blocked())
    if to_shop is None or back is None:
        return False
    remaining = DAY_ROUNDS - turn.round_in_day
    return remaining >= to_shop + 1 + back + tactics.RECALL_MARGIN


def _step_stock_fixers(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """备围墙修补包（优先级高于围墙升级券）。

    * 平时：补到当天的库存目标（第4天5个、第5天6个、第6天起10个）；
    * 武器/围墙/基地全满级后：每天只买修补包，**买到没钱**（受金币与背包余量限制）。
    """
    shop = turn.nearest_zone(role.pos, WEAPON_SHOP)
    if shop is not None and not _trip_time_ok(turn, role, layout, state, shop):
        return None                       # 来不及往返：位置优先，今天不补货
    if _everything_maxed(turn, layout):
        # 全满级后没有别的开销了：修补包买到没钱为止
        return _step_buy(turn, role, WALL_FIXER, 99, claimed, absolute=True)
    target = tactics.DAY_FIXER_PURCHASE.get(turn.day, 0)
    if target <= 0:
        return None
    # 链没走完前留出保留金，避免墙/基地停在低等级（见 FIXER_CHAIN_RESERVE 注释）
    return _step_buy(
        turn,
        role,
        WALL_FIXER,
        target,
        claimed,
        gold_limit=turn.gold - FIXER_CHAIN_RESERVE,
    )


def _step_wall_voucher(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """买围墙升级券。两个门限（选手要求）：

    1. **武器全部升到 3 级之后**才开始；
    2. 只在**白天第 50 回合起**下单。

    买券 1 还是券 2 由"还剩哪些墙没升"决定，一次买够差量（钱不够就少买）。
    """
    if not _weapons_maxed(turn):
        return None
    pending = _pending_wall_work(turn, layout)
    need = Counter(
        item
        for _, item in pending
        if item in (WALL_VOUCHER[1], WALL_VOUCHER[2])
    )
    if not need:
        return None

    stance = (
        layout.night_worker_a
        if _worker_label(state, role, turn.workers()) == "A"
        else layout.night_worker_b
    )
    shop = turn.nearest_zone(role.pos, WEAPON_SHOP)
    if shop is None:
        return None
    at_shop = _adjacent(role.pos, shop) and role.pos != shop

    # 动身时刻 = "第 50 回合" 与 "最晚还能赶回来的时刻" 取较早的那个。
    # 原因：第 50 回合后只剩 20 回合，而"站位→商店→买→回站位"实测要 26+ 回合，
    # 死守第 50 回合会导致一整张券都买不到（墙与基地永远停在 1 级）。
    to_shop = 0 if at_shop else path_length(turn, role, shop)
    back = path_from(turn, shop, stance, turn.blocked())
    if to_shop is None or back is None:
        return None
    remaining = DAY_ROUNDS - turn.round_in_day
    trip = to_shop + 1 + back             # 去商店 + 买一次 + 回站位
    if (
        turn.round_in_day < tactics.WALL_VOUCHER_FROM_ROUND
        # 关键：动身门限必须把"回位余量"一起算进去，否则会在召回接管的同一回合
        # 才动身，买不到（实测 r48 动身、r48 就被召回接管）。
        and remaining > trip + tactics.RECALL_MARGIN + VOUCHER_TRIP_SLACK
    ):
        return None                       # 还早：继续挖矿，等最晚动身时刻
    if remaining < trip:
        return None                       # 连往返都来不及：老老实实回位（站位优先）

    for item, count in sorted(need.items(), key=lambda kv: (kv[0], -kv[1])):
        short = count - role.count_ci(item)
        if short <= 0:
            continue
        outcome = _step_buy(turn, role, item, short, claimed, absolute=True)
        if outcome is not None:
            return outcome
    return None


def _step_ore(turn: Turn, state: MatchState, role: Unit, claimed: set[Pos]) -> Any:
    """挖矿主线。

    **白天**：背包里任一种矿（铜或铁）到 10 个，就去小贩处把铜与铁**全部卖光**
    （sell 一次只能卖一种，所以两种都有时要两回合；``sell_pending`` 记住清仓状态）。
    石头完全不参与卖矿。

    **夜里**：只挖不卖（选手要求，且优先于上面的阈值），并且只在"安全矿"上挖——
    工人不得进入机器人周围 ``NIGHT_SAFE_DISTANCE-1`` 格内，寻路也会绕开禁区；
    如果已经被逼近，先撤到安全格；没有安全矿就停手不挖（绝不冒险被打死）。
    """
    danger = frozenset() if turn.is_day else _danger_cells(turn)

    # ---- 卖矿（只发生在白天）----
    if turn.is_day:
        copper, iron = role.count(COPPER), role.count(IRON)
        total = copper + iron
        clearing = role.unit_id in state.sell_pending
        if clearing and total == 0:            # 清仓完成
            state.sell_pending.discard(role.unit_id)
            clearing = False
        if total > 0 and (
            clearing
            or copper >= ORE_SELL_THRESHOLD
            or iron >= ORE_SELL_THRESHOLD
        ):
            if not clearing:
                state.sell_pending.add(role.unit_id)   # 进入清仓：铜铁都要卖掉
            ore, amount = (COPPER, copper) if copper else (IRON, iron)   # 铜先（单价高）
            outcome = _step_sell(turn, role, ore, amount, claimed)
            if outcome is not None:
                return outcome

    # ---- 背包满 ----
    if role.backpack_full:
        if turn.is_day:
            for ore in (COPPER, IRON, STONE):   # 石头放最后（砌墙材料）
                amount = role.count(ore)
                if amount:
                    outcome = _step_sell(turn, role, ore, amount, claimed)
                    if outcome is not None:
                        return outcome
        return WAIT      # 夜里没得卖：停手，别硬挖

    # ---- 夜间保命：已经在禁区里就先撤 ----
    if danger and role.pos in danger:
        move = _retreat(turn, role, claimed, danger)
        if move is not None:
            return (role.unit_id, move)
        return WAIT

    # ---- 选矿 ----
    mines = [pos for pos in turn.mine_positions((IRON, COPPER)) if pos not in claimed]
    if danger:
        mines = [pos for pos in mines if _mine_is_safe(turn, pos, danger)]
    if not mines:
        return WAIT if danger else None
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
    move = _goto_adjacent(turn, role, mines, claimed, extra=danger)
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
    gold_limit: int | None = None,
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
    # 背包空间不足会导致购买失败（接口/任务书都没说会部分成交），所以按剩余格数截断
    free = (role.capacity - len(role.backpack)) if role.capacity is not None else need
    gold = turn.gold if gold_limit is None else min(turn.gold, max(0, gold_limit))
    affordable = min(need, gold // price, max(0, free))
    if affordable <= 0:                 # 一分钱都买不起 / 背包没地方 → 让位给后面的步骤
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


def _step_recall(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """第 4 天起：天黑前必须回到夜间站位（优先级高于挖矿与购买）。

    判据：``剩余白天回合 <= 到站位的距离 + RECALL_MARGIN`` 就立刻回位。
    到位后返回 WAIT（不许再跑去挖矿，免得最后几回合又离位）。
    """
    if role.kind == PIONEER:
        # 开拓者每一天都要回开火位：黑夜第一回合就必须能开炮（不能把回合花在路上）
        stance = layout.pioneer_stand
    elif turn.day < 4:                  # 工人前 3 天夜里是挖矿，不需要回位
        return None
    else:
        stance = (
            layout.night_worker_a
            if _worker_label(state, role, turn.workers()) == "A"
            else layout.night_worker_b
        )
    remaining = DAY_ROUNDS - turn.round_in_day
    # 必须用**真实路径长度**：围栏有开口，直线 2 格可能要绕 8 步，
    # 用切比雪夫距离会算得太乐观、回位起步太晚（实测第 4 天就迟到）。
    steps = path_length(turn, role, stance)
    if steps is None:                   # 站位暂时到不了：先原地待命，别再跑远
        return WAIT
    margin = (
        PIONEER_RECALL_MARGIN if role.kind == PIONEER else tactics.RECALL_MARGIN
    )
    if remaining > steps + margin:
        return None                     # 时间还够，继续干活
    if role.pos == stance:
        return WAIT                     # 已到位：最后几回合原地待命
    move = _goto_exact(turn, role, stance, claimed)
    if move is not None:
        return (role.unit_id, move)
    return WAIT


def _step_pioneer_buy(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """开拓者每天最多买一次，且只在白天前 50 回合内下单。

    优先级（选手要求）：武器升级券（先券1、3 把都 2 级后改券2）
    > 基地升级券1（第 6 天起、基地还是 1 级）
    > 基地升级券2（核心 10 面墙全 3 级、基地 2 级）。
    超过第 50 回合就当天不买（回攻击位）。
    """
    key = (turn.day, role.unit_id)
    if key in state.daily_purchase:
        return None                     # 今天已经买过 / 已放弃

    weapons = turn.weapons()
    if len(weapons) < 3:
        return None                     # 3 座武器还没造齐，先不买
    station = turn.station()
    item: str | None = None
    if any(tower.level < 2 for tower in weapons):
        item = WEAPON_VOUCHER[1]
    elif any(tower.level < 3 for tower in weapons):
        item = WEAPON_VOUCHER[2]
    elif station is not None and station.level == 1 and turn.day >= 6:
        item = STATION_VOUCHER1
    elif station is not None and station.level == 2:
        # 券 2 提前买好放着，"使用"仍严格门限在核心墙全 3 级之后（见 _step_use_vouchers）。
        # 之前不提前买是因为它会挤掉墙券的钱；现在有 FIXER_CHAIN_RESERVE=400 保底，
        # 提前买不会影响墙升级，反而能让"墙满级 → 基地 3 级"这条链当天就走完。
        item = STATION_VOUCHER2
    if item is None:
        return None

    # 第 50 回合是**决断时刻**（选手口径："前 50 回合没攒够钱就不买"）：
    # 钱够就允许把这单走完（从基地走到商店还要十几个回合），钱不够才当天放弃。
    if turn.round_in_day > tactics.PIONEER_BUY_UNTIL_ROUND:
        if turn.gold < turn.price(item) or turn.round_in_day > 65:
            state.daily_purchase.add(key)
            return None

    need = 1
    if item == WEAPON_VOUCHER[1]:
        need = sum(1 for tower in weapons if tower.level < 2)
    elif item == WEAPON_VOUCHER[2]:
        need = sum(1 for tower in weapons if tower.level < 3)
    outcome = _step_buy(turn, role, item, need, claimed, absolute=True)
    if outcome is not None and outcome[1].get("action") == "buy":
        state.daily_purchase.add(key)   # 今天这一单已经下了
    return outcome


def _step_use_vouchers(
    turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout, claimed: set[Pos]
) -> Any:
    """用手里的券升级武器与基地（买完就去攻击位升级）。

    升级券要求站在目标建筑周围一格内（任务书 4.6.3）；攻击位同时贴着 3 座火箭炮，
    所以"买完立刻去攻击位"天然满足这个距离要求。
    """
    for tower in sorted(turn.weapons(), key=lambda t: (t.level, t.pos.x, t.pos.y)):
        if tower.level < 2:
            item = WEAPON_VOUCHER[1]
        elif tower.level < 3:
            item = WEAPON_VOUCHER[2]
        else:
            continue
        if role.count_ci(item) <= 0:
            continue
        if distance(role.pos, tower.pos) <= 1 and role.pos != tower.pos:
            return (role.unit_id, use_command(item, tower.pos))
        move = _goto_adjacent(turn, role, (tower.pos,), claimed)
        if move is not None:
            return (role.unit_id, move)

    station = turn.station()
    if station is not None:
        item = None
        # 券 1（基地 1→2 级）拿到就用：+1500 血本身就是收益，且这样才有时间走完
        # "墙全 3 级 → 基地 3 级"这条链（实测攥着不用会导致基地到不了 3 级）。
        # 券 2（2→3 级）按选手要求门限在"核心 10 面墙全 3 级"之后。
        if station.level == 1 and role.count_ci(STATION_VOUCHER1) > 0:
            item = STATION_VOUCHER1
        elif (
            station.level == 2
            and role.count_ci(STATION_VOUCHER2) > 0
            and _core_walls_maxed(turn, layout)
        ):
            item = STATION_VOUCHER2
        if item is not None:
            # 基地占 2x2：把 targetPos 指定为离自己最近的那一格，
            # 这样"站在目标周围一格内"的判定在任何实现下都成立
            target_cell = min(
                station_footprint(station.pos),
                key=lambda cell: (distance(role.pos, cell), cell.x, cell.y),
            )
            if distance(role.pos, target_cell) <= 1:
                return (role.unit_id, use_command(item, target_cell))
            move = _goto_adjacent(turn, role, station_footprint(station.pos), claimed)
            if move is not None:
                return (role.unit_id, move)
    return None


def _pioneer_stands(turn: Turn, role: Unit, layout: tactics.Layout) -> list[Pos]:
    """候选开火位，按优先级排序。

    1. 脚本规定的站位与其备选；
    2. **兜底：任何"贴着某座火箭炮"的空格**——三个站位都被机器人/敌人占了时，
       开拓者也要能贴上任意一座炮开火，绝不整夜干看着（选手要求：不能放过任何一个
       可以攻击的回合）。
    """
    stands: list[Pos] = []
    for pos in (layout.pioneer_stand, *layout.pioneer_fallbacks):
        if pos == role.pos or turn.free(pos, role):
            stands.append(pos)
    for rocket in layout.rockets:
        if turn.tower_at(rocket) is None:
            continue
        for cell in cells_within(rocket, 1):
            if cell in stands or not turn.land(cell):
                continue
            if cell not in turn.blocked(role):
                stands.append(cell)
    return stands


def _pioneer_stand(turn: Turn, role: Unit, layout: tactics.Layout) -> Pos:
    """首选开火位（给"回到哪"一个确定的答案）。"""
    stands = _pioneer_stands(turn, role, layout)
    return stands[0] if stands else layout.pioneer_stand


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
        target_cell = min(
            footprint, key=lambda cell: (distance(role.pos, cell), cell.x, cell.y)
        )
        if distance(role.pos, target_cell) <= 1:
            LOGGER.info("station hp=%s -> using %s", station.health, STATION_VOUCHER1)
            return (role.unit_id, use_command(STATION_VOUCHER1, target_cell))
        # 有券但离基地太远：先往基地靠（升级券要求站在目标建筑周围一格内）
        move = _goto_adjacent(turn, role, footprint, claimed)
        if move is not None:
            return (role.unit_id, move)

    # 就位开火：优先站位，其次备选，最后"随便贴一座炮"。
    # 只要有一线可能就开火——不能因为站位被占而整夜不放炮。
    for stand in _pioneer_stands(turn, role, layout):
        if role.pos == stand:
            return _fire(turn, state, role, layout)
        move = _goto_exact(turn, role, stand, claimed)
        if move is None:
            move = _goto_adjacent(turn, role, (stand,), claimed)
        if move is not None:
            return (role.unit_id, move)
    # 一个候选位都到不了：原地试一下（万一正好贴着某座炮），不行就等下一回合
    return _fire(turn, state, role, layout)


def _fire(turn: Turn, state: MatchState, role: Unit, layout: tactics.Layout) -> Any:
    """轮转操控 3 座火箭炮开火。

    每座火箭炮发射后有 3 回合冷却（任务书 4.5.1），3 座轮着来正好每回合一发。
    落点交给 ``_plan_volley``：按**当回合 request 里的实时血量**分配，
    武器几级就打几发（升级后可打多发），并且只打自己这一边的机器人。
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
            continue                       # 站远了操控不了
        targets = _plan_volley(turn, tower)
        if not targets:
            continue                       # 射程内（且在自己这边）没有机器人
        state.attack_index = index + 1     # 下回合从下一座开始
        return (tower.unit_id, attack_command(role.unit_id, targets))
    return None


def _plan_volley(turn: Turn, tower: Unit) -> list[Pos]:
    """给这一炮分配落点，目标是"伤害不过度溢出"。

    * 落点个数 = 武器等级（加特林/火箭可打多发；电磁狙击炮只能传 1 个，接口文档 2.2）；
    * **只打自己这一边**：``|基地y - 机器人y| <= OUR_SIDE_Y_SPAN``（选手要求）；
    * 同一目标重复落点会叠加伤害（任务书 4.5.4"多枚导弹落点重叠时伤害叠加"），
      所以按"还差几发才能打死"排序：先打快死的，发数够了就换下一个目标，
      不把多余的导弹浪费在已经必死的目标上。
    """
    station = turn.station()
    if station is None:
        return []
    level = max(1, int(tower.level or 1))
    if tower.kind not in ("rocket", "gatling"):
        level = 1                          # 电磁狙击炮只有 1 个落点
    reach = tower.range_of_attack()
    candidates = [
        robot
        for robot in turn.robots
        if robot.health > 0
        and distance(tower.pos, robot.pos) <= reach
        and abs(station.pos.y - robot.pos.y) <= OUR_SIDE_Y_SPAN
    ]
    if not candidates:
        return []
    ammo = DAMAGE_PER_MISSILE if tower.kind == "rocket" else DAMAGE_OTHER_WEAPON
    remaining = {robot.robot_id: robot.health for robot in candidates}
    shots: list[Pos] = []
    for _ in range(level):
        live = [robot for robot in candidates if remaining[robot.robot_id] > 0]
        if not live:
            # 目标已被前面的落点覆盖完了，但接口要求"落点数 = 武器等级"，
            # 所以还必须补足发数：挑一个"溅射收益最大"的已打落点重复一次
            # （同一落点重叠只叠加中心伤害，溅射仍能照顾到旁边还活着的机器人）。
            shots.append(_best_pad(shots, candidates, remaining))
            continue
        live.sort(
            key=lambda robot: (
                robot.priority,                             # 脚本优先级：中>小>大>BOSS
                -(-remaining[robot.robot_id] // ammo),      # 再挑"快死的"
                remaining[robot.robot_id],
                robot.robot_id,
            )
        )
        target = live[0]
        shots.append(target.pos)
        remaining[target.robot_id] -= ammo
    return shots


def _best_pad(
    shots: list[Pos], candidates: Sequence[Robot], remaining: dict[int, int]
) -> Pos:
    """挑一个补位落点：优先"周围还活着的机器人最多"的那个已打落点。"""
    live = [robot.pos for robot in candidates if remaining[robot.robot_id] > 0]
    if not shots:                          # 理论上不会发生（调用前已确认有候选）
        return candidates[0].pos
    return max(
        shots,
        key=lambda shot: (
            sum(1 for pos in live if pos != shot and distance(shot, pos) <= 1),
            -shot.x,
            -shot.y,
        ),
    )


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
