"""八方向网格寻路。

地图是 41x32 的小网格，角色每回合走一格、可走 8 个方向，所以这里用
A*（启发函数取切比雪夫距离，与移动代价一致）算"下一步踩哪一格"。

外部只需要 ``next_step()``：给一个目标格，返回朝它走一步的坐标；
返回 ``None`` 表示这一回合到不了（被堵死/目标不可达），调用方应当放弃或换目标。
"""

from collections import deque
from heapq import heappop, heappush
from itertools import count

from .protocol import Pos, Turn, Unit, distance

#: 8 个方向的偏移量（含对角线）。对角线可以走，且"两个障碍物相邻的对角线"
#: 不构成阻挡（任务书 4.5.4 第 2 条），所以这里不做拐角切割判断。
_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def next_step(
    turn: Turn,
    moving: Unit,
    goal: Pos,
    extra_blocked: frozenset[Pos] = frozenset(),
) -> Pos | None:
    """返回 ``moving`` 朝 ``goal`` 前进一格后的坐标；不可达则返回 ``None``。

    障碍来源统一由 ``Turn.blocked()`` 给出：中立区域（矿区/小贩/武器商店/
    任务点）、场上所有单位、机器人、可见敌方单位。注意它会排除 ``moving``
    自己所在格，否则自己会被当成障碍。

    ``extra_blocked`` 是调用方额外指定的禁行格（例如夜间"机器人周围两格内
    不许站"的安全区），只影响寻路，不影响"能不能站"的其他判断。
    """
    # 一次性取出障碍集合，后面的循环里反复用，避免每步重算
    blocked = turn.blocked(moving) | extra_blocked
    # 堆的第三关键字用自增序号，保证同优先级(F值)时弹出顺序稳定、可复现
    order = count()
    # 优先队列元素：(F=已走步数+启发值, G=已走步数, 序号, 坐标)
    frontier: list[tuple[int, int, int, Pos]] = [
        (distance(moving.pos, goal), 0, next(order), moving.pos)
    ]
    came_from: dict[Pos, Pos] = {}   # 记录路径，用于回溯第一步
    best = {moving.pos: 0}           # 每个格子已知的最短代价，用于剪枝
    seen: set[Pos] = set()           # 已确定最优的格子（弹出即定型）

    while frontier:
        _, cost, _, current = heappop(frontier)
        if current in seen:
            continue
        if current == goal:
            # 到达目标：回溯出"从起点迈出的第一步"，而不是整条路径
            return _first_step(came_from, moving.pos, goal)
        seen.add(current)
        for dx, dy in _STEPS:
            step = Pos(current.x + dx, current.y + dy)
            # 越界由 land() 负责（它内部会查地图范围）；障碍直接跳过
            if step in blocked or not turn.land(step):
                continue
            new_cost = cost + 1
            # 已经用更小或相同的代价走到过这里，就没必要再入队
            if new_cost >= best.get(step, new_cost + 1):
                continue
            best[step] = new_cost
            came_from[step] = current
            heappush(
                frontier,
                (
                    new_cost + distance(step, goal),  # F = G + H
                    new_cost,
                    next(order),
                    step,
                ),
            )
    # 队列空了还没到目标：目标被围死或根本不可达
    return None


def path_length(
    turn: Turn,
    moving: Unit,
    goal: Pos,
    extra_blocked: frozenset[Pos] = frozenset(),
) -> int | None:
    """从 ``moving.pos`` 走到 ``goal`` 需要几步（8 邻域 BFS）；不可达返回 ``None``。

    与 ``next_step`` 共用同一套障碍判定。用途：判断"现在往回赶还来不来得及"——
    直线距离会被墙骗（围栏有开口，直线 2 格可能实际要绕 8 步）。
    """
    if moving.pos == goal:
        return 0
    return path_from(turn, moving.pos, goal, turn.blocked(moving) | extra_blocked)


def path_from(
    turn: Turn, start: Pos, goal: Pos, blocked: frozenset[Pos] = frozenset()
) -> int | None:
    """从任意起点 ``start`` 到 ``goal`` 的最短步数；不可达返回 None。

    用来估算"从商店回站位要几步"这类问题（起点不是角色当前位置）。
    """
    if start == goal:
        return 0
    queue = deque([(start, 0)])
    seen = {start}
    while queue:
        current, dist = queue.popleft()
        for dx, dy in _STEPS:
            step = Pos(current.x + dx, current.y + dy)
            if step in seen:
                continue
            if step == goal:            # 目标本身可能被占，仍然算"到得了"
                return dist + 1
            if step in blocked or not turn.land(step):
                continue
            seen.add(step)
            queue.append((step, dist + 1))
    return None


def _first_step(came_from: dict[Pos, Pos], start: Pos, goal: Pos) -> Pos:
    """从 ``came_from`` 回溯，返回 ``start`` 沿路径走出的第一步。

    调用前必须保证 ``goal`` 在 ``came_from`` 链上（即 A* 确实找到过路径），
    且 ``goal != start``。
    """
    current = goal
    while came_from[current] != start:
        current = came_from[current]
    return current
