"""判题器接口的数据模型（请求解析 + 指令构造）。

本模块只做"翻译"，不含任何决策逻辑：
* 把判题器发来的 JSON（接口文档第 1 章 Request）解析成不可变的小对象；
* 提供 ``Turn`` 上的若干地图/单位查询方法，供决策层使用；
* 把动作组装成判题器要的 JSON（接口文档第 2 章 Response 里的 RoleCommand）。

坐标系（任务书 4.1）：
    原点 ``(0,0)`` 在地图**左下角**，x 向右增大，y 向上增大；地图 41x32。
    两点距离一律用切比雪夫距离 ``max(|dx|, |dy|)``（任务书 4.5.4）。

基地坐标（接口文档 1.3.1）：
    ``station`` 的 ``pos`` 是 2x2 基地的**左上角**，所以它占据
    ``(x,y) (x+1,y) (x,y-1) (x+1,y-1)`` 四格。这一点极易搞错，
    凡是"基地占哪几格""离基地一格"的判断都要用 ``station_footprint()``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
DAY_ROUNDS = 70          # 白天 70 回合（任务书 4.2）
NIGHT_ROUNDS = 60        # 黑夜 60 回合
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS   # 一天 130 回合
MAX_DAYS = 10            # 一场最多 10 天
MAX_ROUNDS = ROUNDS_PER_DAY * MAX_DAYS       # 1300 回合，到达即结束

# --------------------------------------------------------------------------
# 经济与名称常量
# --------------------------------------------------------------------------
WEAPON_BUILD_COST = 25   # 建造一座武器工事 25 金币（任务书 4.5.1）
WALL_STONE_COST = 1      # 建造/修补一格围墙 1 块石头

#: 中立区域类型里的矿石名，同时也是背包里的物品名（小写）
LAND = "land"            # 非中立区域（可通行/可建造）——查询时用 get 的默认值
STONE = "stone"
IRON = "iron"
COPPER = "copper"
ORES = (STONE, IRON, COPPER)
MINE_KINDS = (STONE, IRON, COPPER)
ORE_SELL_THRESHOLD = 10  # 背包里**任一种**矿（铜或铁）到 10 个就去小贩处
                         # 触发后把铜和铁一起卖光（sell 一次只能卖一种，见接口文档 2.2）

#: roleType 取值（接口文档 1.3.1）
STATION = "station"
WALL = "wall"
WORKER = "worker"
PIONEER = "pioneer"
TOWER_TYPES = ("gatling", "railgun", "rocket")   # 三种武器工事
CONTROLLABLE_TYPES = (WORKER, PIONEER)           # 能主动行动的角色
#: neutralType 取值里我们需要单独找的两个（接口文档 1.2.1）
VENDOR = "vendor"                # 小贩：卖矿
WEAPON_SHOP = "weaponShop"       # 武器商店：买券/消耗品

#: 商店物品名（必须与 weaponShopList 里的名字逐字一致，接口文档 4.6.3）
WALL_FIXER = "WallFixer"                 # 围墙修补包：目标围墙回满血
MEDICINE = "Medicine"                    # 生命药剂：使用者回满血
DIZZY_WEAPON = "DizzyWeapon"             # 眩晕法宝：3x3 内机器人眩晕 5 回合
BOMB = "Bomb"                            # 范围炸弹：3x3 内机器人 100 伤害
STATION_VOUCHER1 = "StationUpgradeVoucher1"   # 基地 1→2 级，并回满血
STATION_VOUCHER2 = "StationUpgradeVoucher2"   # 基地 2→3 级
#: 升级券按"当前等级"选：1 级用券 1，2 级用券 2
WEAPON_VOUCHER = {1: "WeaponUpgradeVoucher1", 2: "WeaponUpgradeVoucher2"}
WALL_VOUCHER = {1: "WallUpgradeVoucher1", 2: "WallUpgradeVoucher2"}

#: 建筑各等级的血量上限（任务书 4.5.1）。用来判断"这面墙掉血了没有"。
WALL_MAX_HEALTH = {1: 1000, 2: 1500, 3: 2000}
STATION_MAX_HEALTH = {1: 1500, 2: 3000, 3: 4500}

#: 阵营 -> {任务点序号: neutralType}。双方各有 2 个专属任务点（任务书 4.6.2）
TASK_POINT_NEUTRAL = {
    "challenger": {1: "challengerTaskPoint1", 2: "challengerTaskPoint2"},
    "defender": {1: "defenderTaskPoint1", 2: "defenderTaskPoint2"},
}

#: 攻击目标优先级：中型 > 小型 > 大型 > BOSS（脚本给定）。
#: 依据："每发火箭换多少分"——中型 2 分/3 发 = 0.67，小型 1 分/2 发 = 0.5，
#: BOSS 10 分/40 发 = 0.25，大型 4 分/25 发 = 0.16。
ROBOT_PRIORITY = {
    "middleRobot": 0,
    "smallRobot": 1,
    "largeRobot": 2,
    "bossRobot": 3,
}

#: 武器射程随等级变化（任务书 4.5.1）。火箭 3 级是全图，用一个大数表示。
#: 夜间挖矿的安全距离：工人不得进入机器人周围 N-1 格内（不得接近"两格内"）。
#: 注意机器人攻击距离是 3 格（任务书 4.7.2），所以这个值只满足选手的字面要求，
#: 并不保证"绝对打不到"——真要绝对安全需要 >=4。
NIGHT_SAFE_DISTANCE = 3

TOWER_RANGE_BY_LEVEL = {
    "gatling": (3, 5, 7),
    "railgun": (6, 8, 10),
    "rocket": (10, 15, 10**9),
}

#: 8 个相邻方向（含对角线）
_NEIGHBOUR_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


# --------------------------------------------------------------------------
# 坐标
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Pos:
    """地图坐标。frozen 让它可哈希，能直接做 dict/set 的 key。"""

    x: int
    y: int

    @classmethod
    def load(cls, raw: Any) -> "Pos":
        """从 ``{"x":..,"y":..}`` 解析。"""
        return cls(int(raw["x"]), int(raw["y"]))

    def dump(self) -> dict[str, int]:
        """转回接口要的 ``{"x":..,"y":..}`` 形式。"""
        return {"x": self.x, "y": self.y}

    def __add__(self, other: "Pos") -> "Pos":
        return Pos(self.x + other.x, self.y + other.y)


def distance(first: Pos, second: Pos) -> int:
    """切比雪夫距离（任务书 4.5.4：攻击距离/视野/移动判定都用它）。"""
    return max(abs(first.x - second.x), abs(first.y - second.y))


def step(first: Pos, second: Pos) -> Pos:
    """从 first 指向 second 的位移向量（当前只在测试/调试里用得到）。"""
    return Pos(second.x - first.x, second.y - first.y)


def neighbours(pos: Pos) -> tuple[Pos, ...]:
    """周围 8 格。"""
    return tuple(Pos(pos.x + dx, pos.y + dy) for dx, dy in _NEIGHBOUR_STEPS)


def cells_within(pos: Pos, radius: int) -> tuple[Pos, ...]:
    """以 ``pos`` 为中心、切比雪夫距离 ≤radius 的所有格子（不含中心自己）。

    移动/建造/采集/使用/买卖都要求"距离一格内"，所以 radius 基本恒为 1。
    """
    out = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            out.append(Pos(pos.x + dx, pos.y + dy))
    return tuple(out)


def station_footprint(pos: Pos) -> tuple[Pos, ...]:
    """基地占据的 4 格。

    接口文档 1.3.1 明确 ``pos`` 是**左上角**，所以另外三格是
    右、下、右下（在 y 轴向上的坐标系里即 ``y-1`` 那一行）。
    """
    return (
        pos,
        Pos(pos.x + 1, pos.y),
        Pos(pos.x, pos.y - 1),
        Pos(pos.x + 1, pos.y - 1),
    )


def footprint_distance(pos: Pos, footprint: Sequence[Pos]) -> int:
    """点到一组格子（如基地 4 格）的最近距离。"""
    if not footprint:
        return 0
    return min(distance(pos, cell) for cell in footprint)


def cells_at_footprint_distance(footprint: Sequence[Pos], radius: int) -> tuple[Pos, ...]:
    """到基地边界恰好 ``radius`` 格的所有格子（排除基地自身）。

    用于"离基地一格"的建造位（3 个炮位就是从这里挑的）。
    """
    xs = [cell.x for cell in footprint]
    ys = [cell.y for cell in footprint]
    out = []
    for x in range(min(xs) - radius, max(xs) + radius + 1):
        for y in range(min(ys) - radius, max(ys) + radius + 1):
            pos = Pos(x, y)
            if pos in footprint:
                continue
            if footprint_distance(pos, footprint) == radius:
                out.append(pos)
    return tuple(out)


# --------------------------------------------------------------------------
# 单位
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Unit:
    """我方/敌方单位（接口文档 1.3.1 的 Role）。"""

    unit_id: int
    pos: Pos
    kind: str                 # roleType：station/wall/gatling/railgun/rocket/worker/pioneer
    health: int
    level: int                # 仅建筑有；角色没有该字段，解析成 0
    cooldown: int             # 武器冷却剩余回合数（目前只有火箭发射台会用）
    attack_range: int
    capacity: int | None      # backPackCapability；侦察/异常数据可能缺，所以允许 None
    backpack: tuple[str, ...]  # 背包物品名列表

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Unit":
        """解析一个单位。所有可选字段都用 ``or`` 兜底，避免判题器缺字段就崩。"""
        raw_capacity = raw.get("backPackCapability")
        return cls(
            int(raw.get("id") or 0),
            Pos.load(raw["pos"]),
            str(raw["roleType"]),
            int(raw.get("health") or 0),
            int(raw.get("level") or 0),
            int(raw.get("cooldown") or 0),
            int(raw.get("attackRange") or 0),
            int(raw_capacity) if raw_capacity is not None else None,
            tuple(str(item) for item in raw.get("backpack") or ()),
        )

    # -- 背包 --------------------------------------------------------------
    def count(self, name: str) -> int:
        """背包里某物品的数量（严格区分大小写，矿石名都是小写）。"""
        return self.backpack.count(name)

    def count_ci(self, name: str) -> int:
        """忽略大小写的数量查询，用于商店物品（判题器可能大小写不一致）。"""
        wanted = name.lower()
        return sum(1 for item in self.backpack if item.lower() == wanted)

    @property
    def backpack_full(self) -> bool:
        """背包是否已满（满了就买不进也采不进）。"""
        if self.capacity is None:
            return False
        return len(self.backpack) >= self.capacity

    def range_of_attack(self) -> int:
        """实际攻击距离。

        判题器会在 ``attackRange`` 里给出当前值，优先用它；缺失时按
        ``roleType + level`` 查表（任务书 4.5.1），等级越界时夹到合法范围。
        """
        if self.attack_range > 0:
            return self.attack_range
        table = TOWER_RANGE_BY_LEVEL.get(self.kind)
        if table is None:
            return 0
        level = min(max(self.level or 1, 1), len(table))
        return table[level - 1]

    @property
    def alive(self) -> bool:
        return self.health > 0


@dataclass(frozen=True, slots=True)
class Robot:
    """机器人（接口文档 1.5.1）。机器人全图可见，不需要考虑视野。"""

    robot_id: int
    pos: Pos
    kind: str          # smallRobot/middleRobot/largeRobot/bossRobot
    health: int
    dizzy: bool        # abnormalState == "dizzy"
    target_team: str   # 它盯着哪个阵营打

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Robot":
        return cls(
            int(raw.get("id") or 0),
            Pos.load(raw["pos"]),
            str(raw.get("roleType") or ""),
            int(raw.get("health") or 0),
            str(raw.get("abnormalState") or "") == "dizzy",
            str(raw.get("targetTeam") or ""),
        )

    @property
    def priority(self) -> int:
        """攻击优先级，越小越优先；未知类型排最后（99）。"""
        return ROBOT_PRIORITY.get(self.kind, 99)


@dataclass(frozen=True, slots=True)
class TaskPoint:
    """一个任务点（接口文档 1.3.2 的 PlayerTask + 地图上的实际占格）。"""

    index: int              # 1 或 2
    task_type: str          # "自进化类1" / "自进化类2"
    pos: Pos                # 判题器给的坐标
    cells: tuple[Pos, ...]  # 实际占格（任务点 2 占两格）
    cooldown: int           # 刷新冷却剩余回合数，0 表示可接
    score_reward: int
    gold_reward: int
    valid: bool
    timeout_rounds: int     # 任务超时回合数

    @property
    def usable(self) -> bool:
        """现在能不能领任务：既没做完也没在冷却。"""
        return self.valid and self.cooldown <= 0


# --------------------------------------------------------------------------
# 一回合的请求
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Turn:
    """一回合的完整局面（接口文档 1.1 的顶层 Request）。"""

    round_no: int
    day: int                 # 第几天（1..10）
    is_day: bool             # True=白天，False=黑夜
    gold: int
    width: int
    height: int
    camp: str                # "challenger" / "defender"
    zones: dict[Pos, str]    # 中立区域：坐标 -> neutralType（矿/小贩/商店/任务点）
    ours: tuple[Unit, ...]
    enemy: tuple[Unit, ...]  # 只包含可见的敌方单位（基地/围墙永远可见）
    robots: tuple[Robot, ...]
    task_points: tuple[TaskPoint, ...]
    phase_task: str          # 当前已领取任务的原文，没有任务时为空串
    llm_resp: str            # 上一回合 LLM 的回复
    last_cmd_result: str     # 上一回合 executeCmd 在沙盒里的执行结果
    action_results: dict[int, bool]  # 上回合各角色动作是否合法
    shop_prices: dict[str, int]      # 武器商店售价表
    vendor_prices: dict[str, int]    # 小贩收购价表

    # -- 解析 --------------------------------------------------------------
    @classmethod
    def load(cls, payload: dict[str, Any]) -> "Turn":
        """把判题器的 JSON 全量解析成 ``Turn``。

        解析原则：能兜底就兜底。判题器少发某个可选字段时，我们宁可当它是空，
        也不能因为 KeyError 让这一回合没有响应（那会被判成异常响应）。
        """
        round_no = int(payload["roundNo"])
        info = payload.get("mapInfo") or {}
        team = payload.get("teamOur") or {}
        camp = str(team.get("type") or "challenger")
        zones = {
            Pos.load(zone["pos"]): str(zone.get("neutralType") or LAND)
            for zone in info.get("zones") or ()
            if zone.get("pos")
        }
        return cls(
            round_no=round_no,
            # 回合号从 1 开始：第 1..70 回合是第 1 天白天（任务书 4.2）
            day=(round_no - 1) // ROUNDS_PER_DAY + 1,
            is_day=((round_no - 1) % ROUNDS_PER_DAY) < DAY_ROUNDS,
            gold=int(team.get("goldNum") or 0),
            width=int(info.get("width") or 41),
            height=int(info.get("height") or 32),
            camp=camp,
            zones=zones,
            ours=tuple(
                Unit.load(role) for role in team.get("roles") or ()
            ),
            enemy=tuple(
                Unit.load(role)
                for role in (payload.get("teamEnemy") or {}).get("roles") or ()
            ),
            robots=tuple(
                Robot.load(robot)
                for robot in (payload.get("robot") or {}).get("roles") or ()
            ),
            task_points=_load_task_points(team, camp, zones),
            phase_task=str(payload.get("phaseTask") or ""),
            llm_resp=str(payload.get("llmResp") or ""),
            last_cmd_result=str(payload.get("lastCmdResult") or ""),
            action_results={
                int(key): bool(value)
                for key, value in (payload.get("lastRoundRoleActionResults") or {}).items()
            },
            shop_prices={
                str(item["name"]): int(item["price"])
                for item in payload.get("weaponShopList") or ()
                if item.get("name")
            },
            vendor_prices={
                str(item["name"]): int(item["price"])
                for item in payload.get("vendorShopList") or ()
                if item.get("name")
            },
        )

    @property
    def round_in_day(self) -> int:
        """当天的第几回合（0..129）。白天 0..69，黑夜 70..129（任务书 4.2）。"""
        return (self.round_no - 1) % ROUNDS_PER_DAY

    # -- 单位查询 ----------------------------------------------------------
    def alive(self, kinds: Sequence[str]) -> tuple[Unit, ...]:
        """按 roleType 过滤出存活单位。"""
        return tuple(unit for unit in self.ours if unit.kind in kinds and unit.alive)

    def station(self) -> Unit | None:
        """我方基地；被摧毁后判题器可能不再下发，所以返回 Optional。"""
        for unit in self.ours:
            if unit.kind == STATION:
                return unit
        return None

    def workers(self) -> tuple[Unit, ...]:
        """存活的工人，按 ID 升序 —— 第一个就是脚本里的"工人A"。"""
        return tuple(sorted(self.alive((WORKER,)), key=lambda unit: unit.unit_id))

    def pioneer(self) -> Unit | None:
        """存活的开拓者（只有一个）。"""
        pioneers = self.alive((PIONEER,))
        return pioneers[0] if pioneers else None

    def controllable(self) -> tuple[Unit, ...]:
        """所有可以主动行动的角色（工人 + 开拓者），按 ID 排序。"""
        return tuple(sorted(self.alive(CONTROLLABLE_TYPES), key=lambda unit: unit.unit_id))

    def weapons(self) -> tuple[Unit, ...]:
        """我方武器工事，按坐标排序（稳定顺序，便于与角色配对）。"""
        return tuple(
            sorted(
                self.alive(TOWER_TYPES),
                key=lambda unit: (unit.pos.x, unit.pos.y),
            )
        )

    def walls(self) -> tuple[Unit, ...]:
        return self.alive((WALL,))

    def wall_at(self, pos: Pos) -> Unit | None:
        """某个坐标上有没有我方围墙。"""
        for unit in self.ours:
            if unit.kind == WALL and unit.pos == pos:
                return unit
        return None

    def tower_at(self, pos: Pos) -> Unit | None:
        """某个坐标上有没有我方武器工事。"""
        for unit in self.ours:
            if unit.kind in TOWER_TYPES and unit.pos == pos:
                return unit
        return None

    def unit_at(self, pos: Pos) -> Unit | None:
        """某个坐标上的我方或可见敌方单位。"""
        for unit in self.ours:
            if unit.pos == pos:
                return unit
        for unit in self.enemy:
            if unit.pos == pos:
                return unit
        return None

    def footprint(self, unit: Unit) -> tuple[Pos, ...]:
        """单位占据的格子：基地 4 格，其余 1 格。"""
        if unit.kind == STATION:
            return station_footprint(unit.pos)
        return (unit.pos,)

    def station_footprint(self) -> tuple[Pos, ...]:
        """我方基地占据的 4 格；基地没了就返回空。"""
        station = self.station()
        return station_footprint(station.pos) if station else ()

    # -- 网格查询 ----------------------------------------------------------
    def in_bounds(self, pos: Pos) -> bool:
        return 0 <= pos.x < self.width and 0 <= pos.y < self.height

    def land(self, pos: Pos) -> bool:
        """是不是"空地"：在图内且不属于任何中立区域。

        中立区域（矿区/小贩/武器商店/任务点）不可通行也不可建造
        （任务书 4.1：这些元素都会阻挡角色移动）。
        """
        if not self.in_bounds(pos):
            return False
        return self.zones.get(pos, LAND) == LAND

    def occupied_cells(self) -> frozenset[Pos]:
        """我方所有单位占据的格子（基地算 4 格）。"""
        cells: set[Pos] = set()
        for unit in self.ours:
            if unit.alive:
                cells.update(self.footprint(unit))
        return frozenset(cells)

    def blocked(self, moving: Unit | None = None) -> frozenset[Pos]:
        """不可进入的格子集合。

        来源：中立区域、我方单位、可见敌方单位、机器人（任务书 4.1）。
        ``moving`` 是"正在移动的那个单位"，它自己所在格会被排除，
        否则寻路时会把起点当成障碍。
        """
        cells = {pos for pos, kind in self.zones.items() if kind != LAND}
        cells.update(self.occupied_cells())
        for unit in self.enemy:
            if unit.alive:
                cells.add(unit.pos)
        for robot in self.robots:
            if robot.health > 0:
                cells.add(robot.pos)
        if moving is not None:
            cells.discard(moving.pos)
        return frozenset(cells)

    def free(self, pos: Pos, moving: Unit | None = None) -> bool:
        """该格是否"可站"：是空地且没有别的单位/机器人占着。"""
        return self.land(pos) and pos not in self.blocked(moving)

    def mine_positions(self, kinds: Sequence[str] = MINE_KINDS) -> tuple[Pos, ...]:
        """所有矿的位置（默认三种矿全要），按坐标稳定排序。"""
        return tuple(
            sorted(
                (pos for pos, kind in self.zones.items() if kind in kinds),
                key=lambda pos: (pos.x, pos.y),
            )
        )

    def zone_positions(self, kind: str) -> tuple[Pos, ...]:
        """某类中立区域的全部坐标（例如所有小贩）。"""
        return tuple(
            sorted(
                (pos for pos, value in self.zones.items() if value == kind),
                key=lambda pos: (pos.x, pos.y),
            )
        )

    def nearest_zone(self, origin: Pos, kind: str) -> Pos | None:
        """离 ``origin`` 最近的一处该类中立区域；没有就返回 None。"""
        cells = self.zone_positions(kind)
        if not cells:
            return None
        return min(cells, key=lambda pos: (distance(origin, pos), pos.x, pos.y))

    def task_point(self, index: int) -> TaskPoint | None:
        """按序号取任务点（1 或 2）。"""
        for point in self.task_points:
            if point.index == index:
                return point
        return None

    def task_point_cells(self, index: int) -> tuple[Pos, ...]:
        """任务点实际占据的所有格子。

        ``playerTasks`` 只给一个坐标，但任务点 2 占两格，所以优先用
        ``mapInfo.zones`` 里同类型的中立区域坐标；查不到才退回单格。
        """
        point = self.task_point(index)
        if point is not None and point.cells:
            return point.cells
        neutral = TASK_POINT_NEUTRAL.get(self.camp, {}).get(index)
        return self.zone_positions(neutral) if neutral else ()

    def price(self, name: str) -> int:
        """商店里某物品的价格；大小写不匹配时退化为忽略大小写查找。"""
        if name in self.shop_prices:
            return self.shop_prices[name]
        wanted = name.lower()
        for key, value in self.shop_prices.items():
            if key.lower() == wanted:
                return value
        return 0

    def robots_by_priority(self) -> tuple[Robot, ...]:
        """按攻击优先级排好序的机器人（调试/分析用）。"""
        return tuple(
            sorted(
                (robot for robot in self.robots if robot.health > 0),
                key=lambda robot: (robot.priority, robot.robot_id),
            )
        )


def _load_task_points(
    team: dict[str, Any], camp: str, zones: dict[Pos, str]
) -> tuple[TaskPoint, ...]:
    """解析 ``teamOur.playerTasks``，并把地图上的实际占格补上。

    任务点 2 占两格（任务书 4.6.2），两格都算"周围一格内可领任务"，
    所以这里按 neutralType 把同名区域的坐标全部收集起来。
    """
    neutral = TASK_POINT_NEUTRAL.get(camp, {})
    by_neutral: dict[str, list[Pos]] = {}
    for pos, kind in zones.items():
        by_neutral.setdefault(kind, []).append(pos)

    points = []
    for entry in team.get("playerTasks") or ():
        pos = Pos.load(entry["taskPosition"])
        task_type = str(entry.get("taskType") or "")
        # "自进化类1" -> 1，"自进化类2" -> 2
        index = 1 if task_type.endswith("1") else 2
        cells = tuple(sorted(by_neutral.get(neutral.get(index, ""), []), key=lambda p: (p.x, p.y)))
        points.append(
            TaskPoint(
                index=index,
                task_type=task_type,
                pos=pos,
                cells=cells or (pos,),
                cooldown=int(entry.get("coldDownRounds") or 0),
                score_reward=int(entry.get("scoreReward") or 0),
                gold_reward=int(entry.get("goldReward") or 0),
                valid=bool(entry.get("isValid")),
                timeout_rounds=int(entry.get("timeoutRounds") or 0),
            )
        )
    points.sort(key=lambda point: point.index)
    return tuple(points)


# --------------------------------------------------------------------------
# 指令构造（接口文档 2.2 RoleCommand）
# --------------------------------------------------------------------------
def move_command(pos: Pos) -> dict[str, Any]:
    """移动一格（全部角色可用，每回合一次）。"""
    return {"action": "move", "targetPos": [pos.dump()]}


def collect_command(pos: Pos) -> dict[str, Any]:
    """采集：需站在矿区周围一格内，每回合获得对应矿石 1 个。"""
    return {"action": "collect", "targetPos": [pos.dump()]}


def build_command(pos: Pos, name: str) -> dict[str, Any]:
    """建造：``name`` 为 "wall" 或武器名（"rocket"/"gatling"/"railgun"）。

    目标必须是自身周围一格内的空地（任务书 4.4）。
    """
    return {"action": "build", "targetPos": [pos.dump()], "name": name}


def remove_command(pos: Pos) -> dict[str, Any]:
    """拆除围墙（当前战术没用到，保留以备扩展）。"""
    return {"action": "remove", "targetPos": [pos.dump()]}


def attack_command(controller_id: int, targets: Iterable[Pos]) -> dict[str, Any]:
    """操控武器攻击。

    * 这条指令的 key（roleCommandMap 里的键）是**武器工事**的 ID；
    * ``controllerId`` 是正在操控它的**角色** ID，且该角色必须站在武器周围一格内；
    * ``targets`` 的个数必须等于武器当前等级数（加特林/火箭；电磁狙击炮恒为 1），
      所以现在只传 1 个落点等价于"武器等级 1"。
    """
    return {
        "action": "attack",
        "controllerId": str(controller_id),
        "targetPos": [pos.dump() for pos in targets],
    }


def sell_command(name: str, num: int) -> dict[str, Any]:
    """在小贩周围一格内卖出 ``num`` 个矿石换金币。"""
    return {"action": "sell", "name": name, "num": int(num)}


def buy_command(name: str, num: int) -> dict[str, Any]:
    """在武器商店周围一格内买入 ``num`` 个商品。"""
    return {"action": "buy", "name": name, "num": int(num)}


def use_command(name: str, pos: Pos | None = None) -> dict[str, Any]:
    """使用背包里的物品。

    升级券（武器/围墙/基地）、围墙修补包、眩晕法宝、范围炸弹都需要
    ``targetPos``；生命药剂、机器人召唤令不需要，所以 ``pos`` 可为 None。
    """
    command: dict[str, Any] = {"action": "use", "name": name}
    if pos is not None:
        command["targetPos"] = [pos.dump()]
    return command


def accept_task_command() -> dict[str, Any]:
    """领取任务：开拓者需站在己方任务点周围一格内（任务书 4.4）。"""
    return {"action": "acceptTask"}


def submit_answer_command(answer: str) -> dict[str, Any]:
    """提交任务答案。"""
    return {"action": "submitAnswer", "taskAnswer": answer}


def drop_command(name: str) -> dict[str, Any]:
    """丢弃背包物品（当前战术没用到，保留以备扩展）。"""
    return {"action": "drop", "name": name}
