#!/usr/bin/env python3
"""Offline mock judge used to sanity check the agent end to end.

It implements just enough of the real rules to replay a full 10-day match in
process: movement, mining, selling, buying, building, vouchers, the task
pipeline (with a stubbed LLM and a real shell sandbox) and a simple robot
night wave that damages the fence.

Run:  python3 Demo/CoreGeek/tests/mock_judge.py

（中文说明）
这是《未来战争》竞赛 agent 的**离线 mock 判题器**：按 docs/任务书.md 与
docs/接口文档.md，实现"够用就好"的一份规则复刻，用来在本地把 1300 回合
（10 天 × 130 回合）整局跑完并做断言。它模拟：

  * 接口层：每回合下发 request（接口文档 1.x 的结构）、接收 response（2.x）；
  * 指令层：12 种动作码的合法性检查（接口文档 2.3）；
  * 规则层：移动/采集/建造/买卖/升级券/围墙修补/任务领取提交（任务书 4.x、5.x）；
  * 任务链路：桩 LLM（按 prompt 内容返回固定答案）+ 真 shell 沙盒（executeCmd）；
  * 夜袭：按天数生成机器人浪潮，让它们拆墙、啃基地（任务书 4.7）。

注意：它是"第二实现"，规则理解若与真判题器不一致，mock 会跟着一起错；
所以它证明的是**自洽性与不崩**，不等于"符合真判题器"。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any

# tests/ 的上一级 = CoreGeek 包根；agent 代码在其 src/ 下
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent import brain, tactics  # noqa: E402
# round log 会写文件；测试里显式关掉，避免每跑一次留下几十 MB 日志
from agent.roundlog import RECORDER as ROUND_LOG  # noqa: E402
from agent.protocol import (  # noqa: E402
    DAY_ROUNDS,
    ROUNDS_PER_DAY,
    STATION_MAX_HEALTH,
    WALL_MAX_HEALTH,
    WEAPON_BUILD_COST,
    Pos,
    station_footprint,
)

# 地图 41x32（任务书 4.1）
WIDTH, HEIGHT = 41, 32
# 初始金币 75（任务书 4.5.3）
START_GOLD = 75

# 武器商店售价，逐条对应任务书 4.6.3 的三张价目表；
# 判题器通过 request.weaponShopList 下发（接口文档 1.1）
SHOP_PRICES = {
    "WeaponUpgradeVoucher1": 100,
    "WeaponUpgradeVoucher2": 150,
    "WallUpgradeVoucher1": 20,
    "WallUpgradeVoucher2": 30,
    "StationUpgradeVoucher1": 100,
    "StationUpgradeVoucher2": 150,
    "WallFixer": 10,
    "Medicine": 10,
    "DizzyWeapon": 100,
    "Bomb": 100,
    "SmallRobotSummonOrder": 20,
    "MiddleRobotSummonOrder": 30,
    "LargeRobotSummonOrder": 100,
    "BossRobotSummonOrder": 200,
    "AcientTablet": 15,
    "StarSand": 15,
    "FlameBreath": 15,
    "FrostPotion": 15,
    "ThornAmulet": 15,
    "IronWhistle": 15,
}
# 小贩收购价（接口文档 vendorShopList）。真实规则里价格会随世界新闻波动
# （任务书 4.6.1 / 5.1），mock 里固定不变，够用来验证"卖矿换金币"这条链路。
VENDOR_PRICES = {"stone": 1, "iron": 3, "copper": 5}

# 机器人（HP, 攻击力），对应任务书 4.7.2 的表格；
# 攻击距离统一为 3，mock 里简化成"必须先走到目标旁边才打"。
ROBOT_STATS = {
    "smallRobot": (40, 5),
    "middleRobot": (60, 10),
    "largeRobot": (500, 20),
    "bossRobot": (800, 40),
}

# 造一个真实存在的 .md 参考文件，让任务描述里能写出"程序本机可读"的路径，
# 从而走 taskflow 的"本机直读"分支（而不是沙盒 cat 兜底）。
TASK_FILE = Path(tempfile.mkdtemp(prefix="mock-task-")) / "weather_api.md"
TASK_FILE.write_text(
    "# 天气查询接口\n\nGET /weather?city=beijing  ->  {\"temp\": 21}\n",
    encoding="utf-8",
)
# 模拟任务书 5.3 的自进化类任务描述：给出参考资料路径 + 要求的答案结构
TASK_TEXT = (
    "任务1：请查询北京天气，参考资料在 " + str(TASK_FILE) + " 。"
    "请按 {\"answer\": <温度>} 的结构作答。"
)

# 中立单位坐标：小贩、武器商店；任务点 1 占 1 格、任务点 2 占 2 格（任务书 4.6.2）
VENDOR_POS = (20, 16)
SHOP_POS = (20, 20)
TASK_POINTS = {1: (14, 14), 2: (17, 17)}
TASK_POINT_CELLS = {1: [(14, 14)], 2: [(17, 17), (16, 17)]}


@dataclass
class MockUnit:
    """我方单位（基地/工人/开拓者/围墙/武器）的可序列化模型。

    dump() 产出的字典就是接口文档 1.3.1 的 Role 结构；判题器只会把
    "己方全部单位"放进 request.teamOur.roles，所以围墙和武器也必须 dump 出去，
    否则 agent 根本看不见自己已经建好的东西。
    """

    unit_id: int
    # mock 内部统一用 (x, y) 元组表示坐标，dump 时再转成 {"x":..,"y":..}
    pos: tuple[int, int]
    kind: str
    health: int
    # 等级/冷却只有建筑有值，角色恒为默认 0（接口文档 1.3.1）
    level: int = 0
    cooldown: int = 0
    attack_range: int = 0
    # 背包格子数：工人 100、开拓者 40（任务书 4.5.2）
    capacity: int = 0
    backpack: list[str] = field(default_factory=list)

    def dump(self) -> dict[str, Any]:
        """按接口文档 1.3.1 的字段名输出一个单位。"""
        data: dict[str, Any] = {
            "id": self.unit_id,
            "pos": {"x": self.pos[0], "y": self.pos[1]},
            "roleType": self.kind,
            "health": self.health,
            # 攻击力在 mock 里不用（伤害表写死在 ROBOT_STATS/武器逻辑里），固定 0
            "attackPower": 0,
            "attackRange": self.attack_range,
            "backPackCapability": self.capacity,
            "backpack": list(self.backpack),
        }
        # level 仅建筑持有；cooldown 目前只有火箭发射台非 0（接口文档 1.3.1）
        if self.kind in ("station", "wall", "gatling", "railgun", "rocket"):
            data["level"] = self.level
            data["cooldown"] = self.cooldown
        return data


@dataclass
class MockRobot:
    """场上机器人；dump() 对应接口文档 1.5.1 的 RobotRole。"""

    robot_id: int
    pos: tuple[int, int]
    kind: str
    health: int
    # 被眩晕时为 "dizzy"，否则空串（接口文档 1.5.1）；
    # mock 不实现眩晕法宝，字段仅用于结构完整性
    dizzy: bool = False

    def dump(self) -> dict[str, Any]:
        return {
            "id": self.robot_id,
            "pos": {"x": self.pos[0], "y": self.pos[1]},
            "roleType": self.kind,
            "health": self.health,
            "abnormalState": "dizzy" if self.dizzy else "",
            # 机器人只会打我们这一侧
            "targetTeam": "challenger",
        }


class World:
    """一局比赛的伪判题器世界：维护资源、单位、中立元素与机器人。"""

    def __init__(self, base: tuple[int, int], seed: int = 7) -> None:
        # base 是基地 2x2 的左上角（接口文档 1.3.1 注）
        self.base = base
        # 固定种子：让"矿区随机生成/机器人随机落点"可复现，便于回归对比
        self.rng = Random(seed)
        self.gold = START_GOLD
        # 当前已领取任务的原文描述（接口文档 1.1 phaseTask）
        self.phase_task = ""
        # 本回合 payload 里要带的"上回合回执"，由 next_* 在 payload() 里搬运
        self.llm_resp = ""
        self.next_llm_resp = ""
        self.last_cmd_result = ""
        self.next_cmd_result = ""
        self.robots: list[MockRobot] = []
        # 围墙/武器按坐标索引，便于 O(1) 查"这格有没有墙"
        self.walls: dict[tuple[int, int], MockUnit] = {}
        self.towers: dict[tuple[int, int], MockUnit] = {}
        # 矿区：坐标 -> 矿种；mine_uses 记录剩余可采次数（任务书 4.1：采集 10 次后消失）
        self.mines: dict[tuple[int, int], str] = {}
        self.mine_uses: dict[tuple[int, int], int] = {}
        # 本回合采空、等待下回合刷新的矿种（任务书 4.1："下回合会随机刷新"）
        self._pending_respawn: list[str] = []
        # 阵亡登记：[(第几天, 单位ID)]，以及等待复活的单位（任务书 4.5.2）
        self.deaths: list[tuple[int, int]] = []
        self._dead_units: list[list] = []
        # 围墙 ID 从 40000 起（接口文档 1.3.1 的 ID 分配规则）
        self.next_id = 40000
        # 两类问题分开记：
        #   rejected  = 指令本身合法但执行不生效（任务书第八章"指令执行失败"，不计异常）
        #   malformed = 响应格式错误 / 指令无法识别（会计入队伍异常次数）
        self.rejected: list[str] = []
        self.malformed: list[str] = []
        # 里程碑事件流，供 trace/report 打印
        self.events: list[str] = []
        self.accepted = 0
        self.submitted = 0
        self.prompts = 0
        self.execs = 0
        self.buys: list[tuple[int, str, int]] = []
        # 每回合记录"缺几面墙"，用于事后判断围栏何时补齐、夜里有没有破口
        self.wall_holes_seen: list[tuple[int, int]] = []
        # 任务点刷新冷却（接口文档 1.3.2 coldDownRounds）
        self.task_cooldown = {1: 0, 2: 0}
        # 最近一次实际领取的任务点，提交答案时只冷却它
        self.active_point = 0
        # 武器工事全局同时最多 3 座（任务书 4.5.1）
        self.max_weapons = 3

        x, y = base
        # 开局阵容：基地 x1、工人 x2、开拓者 x1（任务书 4.5.3）；
        # ID 必须与接口文档 1.3.1 的分配规则一致，agent 靠 ID 区分工人 A/B
        self.units: list[MockUnit] = [
            MockUnit(10013, base, "station", STATION_MAX_HEALTH[1], level=1),
            MockUnit(10010, (x - 4, y + 4), "worker", 220, capacity=100),
            MockUnit(10012, (x + 4, y + 4), "worker", 220, capacity=100),
            MockUnit(10011, (x - 4, y - 4), "pioneer", 200, capacity=40),
        ]
        self._seed_mines()

    # -- setup -------------------------------------------------------------
    def _fence_rect(self) -> tuple[int, int, int, int]:
        """返回 16 面墙围成的矩形（含外扩），用来把刷矿/刷机器人挡在墙外。"""
        x, y = self.base
        return (x - 2, y - 3, x + 3, y + 2)

    def _clear_of_fence(self, x: int, y: int) -> bool:
        """该格是否在我们围栏区之外（含外扩 1 格）。

        任务书 4.1：矿区"不会生成在可建造区域内"，采空后的刷新同样如此。
        mock 里用围栏矩形+外扩 1 格来近似"可建造区域"，否则矿会落在我们要砌墙的
        格子上，把围栏永久卡成 15/16（真机不会出现这种情况）。
        """
        x0, y0, x1, y1 = self._fence_rect()
        return not (x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1)

    def _seed_mines(self) -> None:
        """随机撒矿：矿区不会生成在可建造区域内（任务书 4.1）。"""
        kinds = ["stone", "iron", "copper"]
        # 这些格子留给基地/商店/小贩/任务点，不刷矿
        reserved = {
            self.base,
            Pos(self.base[0], self.base[1]),
            VENDOR_POS,
            SHOP_POS,
            *TASK_POINTS.values(),
            *(c for cells in TASK_POINT_CELLS.values() for c in cells),
        }
        candidates = []
        # 留出边界一圈（range(1, W-1)），避免矿贴在地图边缘影响走位测试
        for y in range(1, HEIGHT - 1):
            for x in range(1, WIDTH - 1):
                # 围栏及其外扩 1 格内不刷矿，保证建墙流程不被矿区挡住
                if not self._clear_of_fence(x, y):
                    continue
                if (x, y) in reserved:
                    continue
                candidates.append((x, y))
        self.rng.shuffle(candidates)
        # 固定撒 36 个矿，三种矿轮流分配，保证铁/铜/石都够用
        for index, pos in enumerate(candidates[:36]):
            kind = kinds[index % 3]
            self.mines[pos] = kind
            # 每个矿采集 10 次后消失（任务书 4.1）
            self.mine_uses[pos] = 10

    def all_wall_cells(self) -> tuple[Pos, ...]:
        """16 面外围墙的坐标：直接复用 agent 的 tactics 布局，保证双方认知一致。"""
        return tactics.layout(Pos(*self.base)).walls

    def upgrade_cells(self) -> tuple[Pos, ...]:
        """10 面重点升级墙的坐标（同样是 tactics 的布局）。"""
        return tactics.layout(Pos(*self.base)).upgrade_walls

    # -- payload -----------------------------------------------------------
    def zones(self) -> dict[tuple[int, int], str]:
        """中立元素表：矿区 + 小贩 + 武器商店 + 己方两个任务点。

        neutralType 取值见接口文档 1.2.1；任务点 2 占两格（任务书 4.6.2）。
        """
        zones: dict[tuple[int, int], str] = dict(self.mines)
        zones[VENDOR_POS] = "vendor"
        zones[SHOP_POS] = "weaponShop"
        zones[TASK_POINTS[1]] = "challengerTaskPoint1"
        for cell in TASK_POINT_CELLS[2]:
            zones[cell] = "challengerTaskPoint2"
        return zones

    def payload(self, round_no: int) -> dict[str, Any]:
        """组装本回合的 request（顶层结构见接口文档 1.1）。"""
        # llmResp / lastCmdResult 都是"上一回合"的回执：把上回合暂存的搬运过来，
        # 并清空 next_*，保证一个 prompt/executeCmd 只被消费一次。
        self.llm_resp = self.next_llm_resp
        self.next_llm_resp = ""
        self.last_cmd_result = self.next_cmd_result
        self.next_cmd_result = ""
        zones = self.zones()
        return {
            "roundNo": round_no,
            "mapInfo": {
                "width": WIDTH,
                "height": HEIGHT,
                "zones": [
                    {"neutralType": kind, "pos": {"x": pos[0], "y": pos[1]}}
                    for pos, kind in zones.items()
                ],
            },
            "teamOur": {
                # 我们固定扮演挑战者（基地在左上）
                "type": "challenger",
                "teamId": "6324",
                "teamName": "Mock",
                "goldNum": self.gold,
                "totalScore": 0,
                "playerTasks": [self._task_dump(1), self._task_dump(2)],
                # 己方全部单位都要下发：角色 + 已建成的武器 + 已建成的围墙
                # （漏掉武器/围墙会让 agent 以为没建过而反复重建）
                "roles": [
                    unit.dump()
                    for unit in (
                        *self.units,
                        *self.towers.values(),
                        *self.walls.values(),
                    )
                ],
            },
            # mock 不放敌方单位；agent 当前也不依赖敌方信息
            "teamEnemy": {"roles": []},
            # 机器人全图可见（接口文档 1.5）
            "robot": {"roles": [robot.dump() for robot in self.robots]},
            "phaseTask": self.phase_task,
            # mock 不校验上回合动作结果，传空 dict（接口文档 1.1）
            "lastRoundRoleActionResults": {},
            # 0 = 上回合没探测宝藏（接口文档 1.1）
            "lastSummonTreasureResult": 0,
            "llmResp": self.llm_resp,
            # 世界新闻（任务书 4.8）；mock 不实现价格波动与传闻推理，留空
            "worldNews": {"officialNews": "", "folkLegends": ""},
            "lastCmdResult": self.last_cmd_result,
            "vendorShopList": [
                {"name": name, "price": price} for name, price in VENDOR_PRICES.items()
            ],
            "weaponShopList": [
                {"name": name, "price": price} for name, price in SHOP_PRICES.items()
            ],
            "errors": [],
        }

    def _task_dump(self, index: int) -> dict[str, Any]:
        """输出一个 PlayerTask（接口文档 1.3.2）。"""
        return {
            "taskType": f"自进化类{index}",
            "taskPosition": {
                "x": TASK_POINTS[index][0],
                "y": TASK_POINTS[index][1],
            },
            "coldDownRounds": self.task_cooldown[index],
            "scoreReward": 50,
            "goldReward": 30,
            # mock 里任务点永远可领（除非冷却中），不模拟"任务做完"的耗尽态
            "isValid": True,
            "timeoutRounds": 30,
        }

    # -- helpers -----------------------------------------------------------
    def unit(self, unit_id: int) -> MockUnit | None:
        """按 ID 找己方角色（不含围墙/武器）。"""
        for unit in self.units:
            if unit.unit_id == unit_id:
                return unit
        return None

    def occupant(self, pos: tuple[int, int]) -> str | None:
        """这格被什么占了；None 表示可进入。

        阻挡规则见任务书 4.1：建筑（含基地 2x2）、角色、机器人、中立单位、
        任务点、矿区都会阻挡移动。
        """
        if pos in self.walls or pos in self.towers or pos in self.zones():
            return "blocked"
        for unit in self.units:
            # 基地占 2x2：pos 是左上角，需要展开成 4 格判断（接口文档 1.3.1）
            cells = station_footprint(Pos(*unit.pos)) if unit.kind == "station" else (Pos(*unit.pos),)
            if Pos(*pos) in cells:
                return f"unit:{unit.unit_id}"
        for robot in self.robots:
            if robot.pos == pos:
                return f"robot:{robot.robot_id}"
        return None

    def land(self, pos: tuple[int, int]) -> bool:
        """是否在界内且不是中立元素格（= 可以站立/建造的空地）。"""
        if not (0 <= pos[0] < WIDTH and 0 <= pos[1] < HEIGHT):
            return False
        return pos not in self.zones()

    def adjacent(self, first: tuple[int, int], second: tuple[int, int]) -> bool:
        """切比雪夫距离 <= 1，即"周围一格内"（任务书 4.5.4）。"""
        return max(abs(first[0] - second[0]), abs(first[1] - second[1])) <= 1

    # -- apply -------------------------------------------------------------
    def apply(self, response: dict[str, Any], round_no: int) -> None:
        """应用 agent 本回合的 response，并检查响应/指令的合法性。"""
        # 顶层必须恰好是这三个字段（接口文档 2.1）
        if set(response) != {"roleCommandMap", "prompt", "executeCmd"}:
            self.malformed.append(
                f"r{round_no}: unexpected response keys {sorted(response)}"
            )
        # 响应必须能被 JSON 序列化，否则真判题器会判"响应格式错误"
        try:
            json.dumps(response)
        except (TypeError, ValueError) as error:
            self.malformed.append(f"r{round_no}: response not serializable: {error}")
        commands = response.get("roleCommandMap") or {}
        if not isinstance(commands, dict):
            self.malformed.append(f"r{round_no}: roleCommandMap is not a dict")
            commands = {}
        # 统计"自己动过手的角色"和"操控武器的角色"：
        # 一个角色只能同时操控一座武器工事（接口文档 2.2 controllerId 注），
        # 所以同回合里自己发指令 + 又去 controllerId 操控武器是非法的。
        controllable = {unit.unit_id for unit in self.units}
        controllers: set[int] = set()
        acting: set[int] = set()
        for key, command in commands.items():
            # roleCommandMap 的 key 是角色 ID（接口文档 2.1）
            try:
                unit_id = int(key)
            except (TypeError, ValueError):
                self.malformed.append(f"r{round_no}: non numeric role key {key!r}")
                continue
            if isinstance(command, dict) and command.get("controllerId"):
                try:
                    controllers.add(int(command["controllerId"]))
                except (TypeError, ValueError):
                    self.malformed.append(f"r{round_no}: bad controllerId {command!r}")
            # 围墙/武器也在 roleCommandMap 里作为 key（攻击指令挂在武器 ID 上），
            # 但只有"角色"才谈得上自己动手，所以只把 units 计入 acting
            if unit_id in controllable:
                acting.add(unit_id)
            self._apply_command(unit_id, command, round_no)
        double = controllers & acting
        if double:
            self.malformed.append(
                f"r{round_no}: role(s) {sorted(double)} both act and control a weapon"
            )
        # 顶层 prompt 非空 = 本回合调用一次 LLM（接口文档 2.1）；
        # 这里用桩函数生成"下一回合的 llmResp"。
        prompt = str(response.get("prompt") or "")
        command = str(response.get("executeCmd") or "")
        if prompt:
            self.prompts += 1
            self.next_llm_resp = self._stub_llm(prompt)
        # 顶层 executeCmd 非空 = 本回合在沙盒里执行一条命令（接口文档 2.1）
        if command:
            self.execs += 1
            self.next_cmd_result = self._run_shell(command)

    def _stub_llm(self, prompt: str) -> str:
        """桩 LLM：用 prompt 里的固定指令句区分"取数阶段/整理阶段"。

        这两句话来自 taskflow 的 EXTRACT_INSTRUCTION / FORMAT_INSTRUCTION，
        和真实流程一致：第一次问"给我 executeCmd"，第二次问"整理成结构"。
        """
        if "组织成任务要求的结构" in prompt:
            return '{"answer": 21}'
        return 'python3 -c "print(21)"'

    def _run_shell(self, command: str) -> str:
        """真跑一遍 executeCmd，并伪装成判题器的回执格式。

        返回 "[exitCode:N]\\n<输出>"，与接口文档 1.1 对 lastCmdResult 的约定一致；
        超时/异常返回 "[TIMEOUT]..."，用来验证 agent 的失败识别分支。
        15 秒上限对应接口文档 2.1 的"执行时长不得超过 15 秒"。
        """
        try:
            done = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=15,
                cwd=tempfile.gettempdir(),
            )
        except Exception as error:  # noqa: BLE001 - report like the judge would
            return f"[TIMEOUT]\n{error}"
        return f"[exitCode:{done.returncode}]\n{done.stdout}"

    def _apply_command(
        self, unit_id: int, command: dict[str, Any], round_no: int
    ) -> None:
        """按 action 分发到 _do_<action>；无法识别的动作记 malformed。

        "指令标识无法识别"属于任务书第八章的三类异常之一（指令错误）。
        """
        if not isinstance(command, dict) or "action" not in command:
            self.malformed.append(f"r{round_no}: u{unit_id} bad command {command!r}")
            return
        action = str(command.get("action"))
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            self.malformed.append(f"r{round_no}: u{unit_id} unknown action {action}")
            return
        handler(unit_id, command, round_no)

    def _targets(self, command: dict[str, Any]) -> list[tuple[int, int]]:
        """把 targetPos 数组转成内部坐标元组列表（接口文档 2.2）。"""
        return [
            (int(item["x"]), int(item["y"]))
            for item in command.get("targetPos") or ()
        ]

    def _actor(self, unit_id: int, round_no: int, action: str) -> MockUnit | None:
        """取执行者；角色不存在（例如已阵亡）记 rejected 并返回 None。"""
        unit = self.unit(unit_id)
        if unit is None:
            self.rejected.append(f"r{round_no}: {action} by unknown unit {unit_id}")
        return unit

    # -- actions -----------------------------------------------------------
    def _do_move(self, unit_id, command, round_no):
        """移动：每回合每角色一次、每次一格（任务书 4.4）。"""
        unit = self._actor(unit_id, round_no, "move")
        targets = self._targets(command)
        if unit is None:
            return
        # 移动只传 1 个目标位置（接口文档 2.2 targetPos 说明）
        if len(targets) != 1:
            self.malformed.append(f"r{round_no}: move needs exactly one target")
            return
        target = targets[0]
        # 只能走到当前坐标周围 8 格之一，且目标不能是原地（任务书 4.5.4 第 2 条）
        if not self.adjacent(unit.pos, target) or target == unit.pos:
            self.rejected.append(f"r{round_no}: u{unit_id} move not adjacent {target}")
            return
        # 阻挡格不可进入（任务书 4.1 的阻挡清单）
        if not self.land(target):
            self.rejected.append(f"r{round_no}: u{unit_id} move into blocked {target}")
            return
        # 被其他单位/机器人/建筑占据的格子也不可进入
        blocker = self.occupant(target)
        if blocker:
            self.rejected.append(f"r{round_no}: u{unit_id} move onto {blocker} {target}")
            return
        unit.pos = target

    def _do_collect(self, unit_id, command, round_no):
        """采集：工人站在矿周围一格内，每回合获得对应矿石 x1（任务书 4.4）。"""
        unit = self._actor(unit_id, round_no, "collect")
        targets = self._targets(command)
        if unit is None:
            return
        if len(targets) != 1:
            self.malformed.append(f"r{round_no}: collect needs exactly one target")
            return
        target = targets[0]
        kind = self.mines.get(target)
        # 目标必须真的是一座矿，且角色在它周围一格内
        if kind is None or not self.adjacent(unit.pos, target):
            self.rejected.append(f"r{round_no}: u{unit_id} collect {target} invalid")
            return
        # 背包满则采集失败（接口文档 1.3.1 backPackCapability）
        if len(unit.backpack) >= unit.capacity:
            self.rejected.append(f"r{round_no}: u{unit_id} collect backpack full")
            return
        unit.backpack.append(kind)
        self.mine_uses[target] -= 1
        # 每个矿采集 10 次后消失（任务书 4.1）
        if self.mine_uses[target] <= 0:
            self.mines.pop(target, None)
            self.mine_uses.pop(target, None)
            # 注意：任务书说的是"**下回合**会随机刷新在地图的其他区域"，
            # 所以这里只登记待刷新的矿种，真正的重生放到本回合结尾的 tick() 里做。
            # 若当回合立刻重生，同回合稍后行动的另一个工人可能正好要走进这格，
            # 在 mock 里会表现为"move into blocked"的假失败（真机不会）。
            self._pending_respawn.append(kind)

    def _respawn_mine(self, kind: str) -> None:
        """矿采空后在随机空地重生（任务书 4.1：下回合随机刷新）。"""
        # 最多试 500 次，避免地图被占满时死循环
        for _ in range(500):
            pos = (self.rng.randrange(1, WIDTH - 1), self.rng.randrange(1, HEIGHT - 1))
            # 同样排除围栏区：可建造区域内不刷矿（任务书 4.1）
            if self.land(pos) and not self.occupant(pos) and self._clear_of_fence(*pos):
                self.mines[pos] = kind
                self.mine_uses[pos] = 10
                return

    def _do_build(self, unit_id, command, round_no):
        """建造：仅工人在白天可用，目标须在自身周围一格内（任务书 4.4）。"""
        unit = self._actor(unit_id, round_no, "build")
        targets = self._targets(command)
        name = str(command.get("name") or "")
        if unit is None:
            return
        # 任务书 4.4：建造(build) 仅工人在白天可用，黑夜不可用。
        # mock 早期版本漏了这条校验，导致 agent"夜里补墙"这种非法行为也能通过测试，
        # 所以这里必须拦（夜里建墙 = 指令执行失败，浪费一个回合）。
        if (round_no - 1) % ROUNDS_PER_DAY >= DAY_ROUNDS:
            self.rejected.append(
                f"r{round_no}: u{unit_id} 夜里不能建造 {name}（任务书 4.4：建造仅白天）"
            )
            return
        # build 需要同时给 name 与 1 个 targetPos（接口文档 2.3）
        if len(targets) != 1 or not name:
            self.malformed.append(f"r{round_no}: build needs name and one target")
            return
        target = targets[0]
        if not self.adjacent(unit.pos, target) or not self.land(target):
            self.rejected.append(f"r{round_no}: u{unit_id} build {name} at {target} invalid")
            return
        if self.occupant(target):
            self.rejected.append(f"r{round_no}: u{unit_id} build {name} onto occupied {target}")
            return
        if name == "wall":
            # 围墙消耗石头 x1（任务书 4.5.1 建造代价）
            if "stone" not in unit.backpack:
                self.rejected.append(f"r{round_no}: u{unit_id} build wall without stone")
                return
            unit.backpack.remove("stone")
            # 新建围墙是 level1、满血 1000（任务书 4.5.1）
            self.walls[target] = MockUnit(self.next_id, target, "wall", WALL_MAX_HEALTH[1], level=1)
            self.next_id += 1
            return
        if name in ("rocket", "gatling", "railgun"):
            # 武器 25 金币（任务书 4.5.1）
            if self.gold < WEAPON_BUILD_COST:
                self.rejected.append(f"r{round_no}: u{unit_id} build {name} without gold")
                return
            # 武器工事全局同时最多 3 座（任务书 4.5.1）
            if len(self.towers) >= self.max_weapons:
                self.rejected.append(f"r{round_no}: u{unit_id} build {name} over weapon cap")
                return
            self.gold -= WEAPON_BUILD_COST
            # 塔 ID 按接口文档 1.3.1：加特林 10020+、电磁 10030+、火箭 10040+
            tower_id = {"gatling": 10020, "railgun": 10030, "rocket": 10040}[name] + len(self.towers)
            self.towers[target] = MockUnit(tower_id, target, name, 1000, level=1)
            # 攻击距离取 level1 的值（任务书 4.5.1）：加特林 3 / 电磁 6 / 火箭 10
            self.towers[target].attack_range = {"gatling": 3, "railgun": 6, "rocket": 10}[name]
            self.events.append(f"r{round_no} built {name} at {target}")
            return
        self.malformed.append(f"r{round_no}: unknown build name {name}")

    def _do_sell(self, unit_id, command, round_no):
        """贩卖：在小贩周围一格内，把矿石换成金币（任务书 4.4 / 4.6.1）。"""
        unit = self._actor(unit_id, round_no, "sell")
        name = str(command.get("name") or "")
        # num 不填默认为 1（接口文档 2.2）
        num = int(command.get("num") or 1)
        if unit is None:
            return
        if not self.adjacent(unit.pos, VENDOR_POS):
            self.rejected.append(f"r{round_no}: u{unit_id} sell away from vendor")
            return
        # 只有小贩收购清单里的矿种能卖，且背包里数量要够
        if name not in VENDOR_PRICES or unit.backpack.count(name) < num or num <= 0:
            self.rejected.append(f"r{round_no}: u{unit_id} sell {name} x{num} invalid")
            return
        for _ in range(num):
            unit.backpack.remove(name)
        self.gold += VENDOR_PRICES[name] * num

    def _do_buy(self, unit_id, command, round_no):
        """购买：在武器商店周围一格内买商品（任务书 4.4 / 4.6.3）。"""
        unit = self._actor(unit_id, round_no, "buy")
        name = str(command.get("name") or "")
        num = int(command.get("num") or 1)
        if unit is None:
            return
        price = SHOP_PRICES.get(name, 0)
        if not self.adjacent(unit.pos, SHOP_POS):
            self.rejected.append(f"r{round_no}: u{unit_id} buy away from shop")
            return
        # 金币不足或商品不存在则购买失败（任务书 4.6.3 注：背包空间不足或金币不足则失败）
        if price <= 0 or num <= 0 or price * num > self.gold:
            self.rejected.append(f"r{round_no}: u{unit_id} buy {name} x{num} unaffordable")
            return
        # 背包空间不足同样购买失败
        if len(unit.backpack) + num > unit.capacity:
            self.rejected.append(f"r{round_no}: u{unit_id} buy {name} x{num} over capacity")
            return
        self.gold -= price * num
        unit.backpack.extend([name] * num)
        self.buys.append((round_no, name, num))

    def _do_use(self, unit_id, command, round_no):
        """使用：消耗品与升级券（任务书 4.4 / 4.6.3）。

        mock 只实现 agent 真正用到的几种：
        围墙升级券、基地升级券、围墙修复包、生命药剂。
        """
        unit = self._actor(unit_id, round_no, "use")
        name = str(command.get("name") or "")
        targets = self._targets(command)
        # 注意：`use`（含围墙修补包、基地升级券）**没有**昼夜限制，夜里可以正常使用。
        # 夜里唯一不能做的是 `build`（见 _do_build），所以"夜里不能修围墙"指的是
        # 不能用建造动作补墙，而不是不能用围墙修补包。
        if unit is None:
            return
        # 物品必须先在背包里（购买后才可用）
        if name not in unit.backpack:
            self.rejected.append(f"r{round_no}: u{unit_id} use {name} not in backpack")
            return
        # 券名末位是目标等级：Voucher1 = level1->2，Voucher2 = level2->3
        variant = name[-1] if name[-1] in "12" else ""
        if name.startswith("WallUpgradeVoucher"):
            target = targets[0] if targets else None
            wall = self.walls.get(target) if target else None
            # 升级券需站在目标建筑周围一格内并指定目标位置（任务书 4.6.3 注）
            if wall is None or not self.adjacent(unit.pos, target):
                self.rejected.append(f"r{round_no}: u{unit_id} {name} on {target} invalid")
                return
            want = 1 if variant == "1" else 2
            # 券必须与当前等级匹配，否则不生效（任务书 4.6.3 注）
            if wall.level != want:
                self.rejected.append(f"r{round_no}: u{unit_id} {name} on level {wall.level}")
                return
            wall.level += 1
            # 升级后建筑恢复到满血（任务书 4.6.3 注）
            wall.health = WALL_MAX_HEALTH[wall.level]
        elif name.startswith("StationUpgradeVoucher"):
            station = self.unit(10013)
            assert station is not None
            target = targets[0] if targets else None
            # 基地是 2x2，targetPos 落在它的任意一格上都算指向基地
            if target is None or target not in station_footprint(Pos(*station.pos)):
                self.rejected.append(f"r{round_no}: u{unit_id} {name} bad target {target}")
                return
            # 需要在基地周围一格内使用
            if not self.adjacent(unit.pos, station.pos):
                self.rejected.append(f"r{round_no}: u{unit_id} {name} too far from station")
                return
            want = 1 if variant == "1" else 2
            if station.level != want:
                self.rejected.append(f"r{round_no}: u{unit_id} {name} station level {station.level}")
                return
            station.level += 1
            # 升级后回满血：level2=3000、level3=4500（任务书 4.5.1）
            station.health = STATION_MAX_HEALTH[station.level]
            self.events.append(f"r{round_no} station upgraded to level {station.level}")
        elif name == "WallFixer":
            # 围墙修复包：站在待修复围墙一格范围内使用，回满血（任务书 4.6.3 注）
            target = targets[0] if targets else None
            wall = self.walls.get(target) if target else None
            if wall is None or not self.adjacent(unit.pos, target):
                self.rejected.append(f"r{round_no}: u{unit_id} WallFixer on {target} invalid")
                return
            wall.health = WALL_MAX_HEALTH[wall.level]
        elif name == "Medicine":
            # 生命药剂：使用者回满血（任务书 4.6.3），工人 220、开拓者 200
            unit.health = 220 if unit.kind == "worker" else 200
        else:
            # 眩晕法宝/范围炸弹/召唤令等 mock 未实现，记为不支持
            self.rejected.append(f"r{round_no}: u{unit_id} use {name} unsupported")
            return
        unit.backpack.remove(name)

    def _do_acceptTask(self, unit_id, command, round_no):  # noqa: N802
        """领取任务：开拓者需站在己方任务点周围一格内（任务书 4.4）。"""
        unit = self._actor(unit_id, round_no, "acceptTask")
        if unit is None:
            return
        for index, cells in TASK_POINT_CELLS.items():
            # 任务点 2 占两格，站到任意一格旁边都能领（任务书 4.6.2）
            if any(self.adjacent(unit.pos, cell) for cell in cells):
                # 冷却中不可领取（接口文档 1.3.2 coldDownRounds）
                if self.task_cooldown[index] > 0:
                    self.rejected.append(f"r{round_no}: u{unit_id} accept on cooldown {index}")
                    return
                self.phase_task = TASK_TEXT
                self.accepted += 1
                self.active_point = index
                self.events.append(f"r{round_no} acceptTask point{index}")
                return
        self.rejected.append(f"r{round_no}: u{unit_id} acceptTask away from point")

    def _do_submitAnswer(self, unit_id, command, round_no):  # noqa: N802
        """提交答案：需带 taskAnswer 字符串（接口文档 2.3）。"""
        unit = self._actor(unit_id, round_no, "submitAnswer")
        if unit is None:
            return
        answer = command.get("taskAnswer")
        # 缺 taskAnswer 属于"指令字段缺失"，计入响应/指令错误
        if not isinstance(answer, str) or not answer.strip():
            self.malformed.append(f"r{round_no}: submitAnswer without taskAnswer")
            return
        self.submitted += 1
        # 提交后任务结束：phaseTask 清空（接口文档 1.1）
        self.phase_task = ""
        # 任务执行结束后，该任务点需等待 30 回合刷新（任务书 第五章）
        if self.active_point:
            self.task_cooldown[self.active_point] = 30
        self.events.append(f"r{round_no} submitAnswer {answer[:40]!r}")

    def tower_by_id(self, unit_id: int) -> MockUnit | None:
        """按 ID 找武器。

        roleCommandMap 的 key 是武器 ID，controllerId 才是操控它的角色 ID
        （接口文档 2.2），所以攻击指令要用 ID 反查武器。
        """
        for tower in self.towers.values():
            if tower.unit_id == unit_id:
                return tower
        return None

    def _do_attack(self, unit_id, command, round_no):
        """操控武器攻击：需指定 controllerId，角色要在武器周围一格内。

        规则要点：
          * 仅黑夜可用（任务书 4.4）—— mock 不校验昼夜，只校验几何与冷却；
          * 一个角色只能同时操控一座武器工事（接口文档 2.2）；
          * 火箭 20 中心伤害 + 周围 8 格一半伤害（任务书 4.5.4）；
            这里把加特林/电磁简化成单点 10 伤害。
        """
        tower = self.tower_by_id(unit_id)
        controller_raw = command.get("controllerId")
        if tower is None:
            self.rejected.append(f"r{round_no}: attack by non tower {unit_id}")
            return
        try:
            controller_id = int(controller_raw)
        except (TypeError, ValueError):
            # 攻击必须带 controllerId，否则属于字段缺失
            self.malformed.append(f"r{round_no}: attack without controllerId")
            return
        controller = self.unit(controller_id)
        # 操控者必须活着且紧贴武器（任务书 4.4：站在武器周围一格内）
        if controller is None or not self.adjacent(controller.pos, tower.pos):
            self.rejected.append(f"r{round_no}: u{controller_id} cannot control tower")
            return
        # 冷却未结束时不能开火（接口文档 1.3.1 cooldown）
        if tower.cooldown > 0:
            self.rejected.append(f"r{round_no}: tower {unit_id} still cooling")
            return
        targets = self._targets(command)
        if not targets:
            self.malformed.append(f"r{round_no}: attack without targetPos")
            return
        for target in targets:
            # 落点必须在武器攻击距离内（切比雪夫距离，任务书 4.5.4）
            if max(abs(target[0] - tower.pos[0]), abs(target[1] - tower.pos[1])) > tower.attack_range:
                self.rejected.append(f"r{round_no}: tower {unit_id} target {target} out of range")
                continue
            if tower.kind == "rocket":
                # 火箭：落点中心 20 伤害，周围 8 格溅射减半（任务书 4.5.4）
                for robot in self.robots:
                    if robot.pos == target:
                        robot.health -= 20
                    elif self.adjacent(robot.pos, target):
                        robot.health -= 10
            else:
                # 加特林/电磁：mock 简化成对落点上的机器人 10 伤害
                for robot in self.robots:
                    if robot.pos == target:
                        robot.health -= 10
        # 只要这次发射合法（有 controllerId、操控者贴身、未冷却、有落点），
        # 武器就要进入冷却，与是否打中无关（任务书 4.5.1："发射后进入 3 回合冷却空窗"）。
        # 火箭 3 回合，加特林/电磁没有冷却，恒为 0。
        tower.cooldown = 0 if tower.kind != "rocket" else 3
        # 结算伤害后清掉死亡机器人（被攻击伤害在本回合结束后统一结算，任务书 4.4）
        self.robots = [robot for robot in self.robots if robot.health > 0]

    # -- world tick --------------------------------------------------------
    def tick(self, round_no: int) -> None:
        """推进一个回合的"判题器侧"结算。

        调用时机是 agent 出手之后，因此这里的顺序等价于任务书 4.4 的
        "结算顺序：武器攻击 > 机器人移动"。
        """
        round_in_day = (round_no - 1) % ROUNDS_PER_DAY
        day = (round_no - 1) // ROUNDS_PER_DAY + 1
        # 本回合采空的矿，到下回合才刷新（任务书 4.1），所以在这里统一重生
        for kind in self._pending_respawn:
            self._respawn_mine(kind)
        self._pending_respawn.clear()
        # 阵亡角色的复活（任务书 4.5.2：第二天白天开始后 20 回合，背包保留）
        for entry in list(self._dead_units):
            respawn_round, unit = entry
            if round_no < respawn_round:
                continue
            unit.health = 220 if unit.kind == "worker" else 200
            unit.pos = self._free_spot_near_base()
            self.units.append(unit)
            self._dead_units.remove(entry)
            self.events.append(f"r{round_no} {unit.kind} {unit.unit_id} 在基地复活")
        # 武器冷却递减（接口文档 1.3.1 cooldown：剩余回合数）
        for tower in self.towers.values():
            if tower.cooldown > 0:
                tower.cooldown -= 1
        for index in list(self.task_cooldown):
            if self.task_cooldown[index] > 0:
                self.task_cooldown[index] -= 1
        # 白天第一个回合：残余机器人自动清除（任务书 4.7.3）
        if round_in_day == 0:
            self.robots = []
        # 黑夜第一个回合：机器人浪潮统一出现（任务书 4.7.3）
        if round_in_day == DAY_ROUNDS:
            self._spawn_wave(day, round_no)
        # 整个黑夜都让机器人行动
        if round_in_day >= DAY_ROUNDS:
            self._move_robots(round_no)
        self._check_fence(round_no, day)

    def _spawn_wave(self, day: int, round_no: int) -> None:
        """生成当晚的机器人浪潮：数量随天数增加（任务书 4.7.3）。

        mock 只用小型 + 中型，规模刻意压小；想测更重的波次可以调 count/种类。
        """
        count = min(day, 6)
        kinds = ["smallRobot"] * count + ["middleRobot"] * max(0, (day - 1) // 2)
        for index, kind in enumerate(kinds):
            health, _damage = ROBOT_STATS[kind]
            # 落点随机取在基地周围 9~13 格处；最多试 200 次避免死循环
            for _ in range(200):
                pos = (
                    self.base[0] + self.rng.choice([-1, 1]) * self.rng.randrange(9, 14),
                    self.base[1] + self.rng.choice([-1, 1]) * self.rng.randrange(9, 14),
                )
                if self.land(pos) and not self.occupant(pos):
                    self.robots.append(
                        MockRobot(30000 + day * 100 + index, pos, kind, health)
                    )
                    break
        if kinds:
            self.events.append(f"r{round_no} wave of {len(kinds)} robots")

    def _base_targets(self) -> list[tuple[int, int]]:
        """基地 2x2 的 4 个格子，机器人以"靠近任意一格"为行进目标。"""
        return [(cell.x, cell.y) for cell in station_footprint(Pos(*self.base))]

    def _move_robots(self, round_no: int) -> None:
        """机器人行为：攻击阻挡其移动的单位，否则朝基地走（任务书 4.7.3）。"""
        goals = self._base_targets()
        for robot in self.robots:
            _health, damage = ROBOT_STATS[robot.kind]
            # 任务书 4.7.3：机器人会攻击阻挡其移动的单位（包括角色与建筑）。
            # 角色远比墙脆（工人 220 血 vs 3 级墙 2000 血），所以"夜里在外面挖矿
            # 安不安全"完全取决于这一条——之前 mock 漏了它，导致夜间外出零风险。
            blockers = [
                unit
                for unit in self.units
                if unit.kind in ("worker", "pioneer") and self.adjacent(robot.pos, unit.pos)
            ]
            if blockers:
                victim_unit = min(blockers, key=lambda u: (u.health, u.unit_id))
                victim_unit.health -= damage
                self.events.append(
                    f"r{round_no} {victim_unit.kind} {victim_unit.unit_id} "
                    f"被 {robot.kind} 打到 {victim_unit.health}"
                )
                if victim_unit.health <= 0:
                    self._kill_unit(victim_unit, round_no)
                continue
            # 其次拆挡路的围墙：打相邻的墙里血量最低的那面
            adjacent_wall = [
                pos
                for pos in self.walls
                if max(abs(pos[0] - robot.pos[0]), abs(pos[1] - robot.pos[1])) <= 1
            ]
            if adjacent_wall:
                victim = min(
                    adjacent_wall,
                    key=lambda pos: (self.walls[pos].health, pos),
                )
                self.walls[victim].health -= damage
                # 墙血归零即拆除，对应区域变回可穿越空地（任务书 4.1）
                if self.walls[victim].health <= 0:
                    self.events.append(f"r{round_no} wall destroyed {victim}")
                    del self.walls[victim]
                continue
            # 没有相邻墙但贴着基地：直接啃基地
            hit_base = [
                cell for cell in goals if self.adjacent(robot.pos, cell)
            ]
            if hit_base:
                station = self.unit(10013)
                assert station is not None
                station.health -= damage
                continue
            # 否则朝基地走一格：8 邻格里选一个"离基地最近且可进入"的格子
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    candidate = (robot.pos[0] + dx, robot.pos[1] + dy)
                    if not self.land(candidate) or self.occupant(candidate):
                        continue
                    dist = min(
                        max(abs(candidate[0] - cell[0]), abs(candidate[1] - cell[1]))
                        for cell in goals
                    )
                    if best is None or dist < best[0]:
                        best = (dist, candidate)
            if best is not None:
                robot.pos = best[1]

    def _kill_unit(self, unit, round_no: int) -> None:
        """角色阵亡：移出战场并登记复活。

        任务书 4.5.2："角色阵亡后，第二天白天开始后 20 回合可以在基地复活，
        背包物品保留。" 所以复活回合 = 第 day+1 天的第 20 个白天回合。
        """
        day = (round_no - 1) // ROUNDS_PER_DAY + 1
        self.units = [u for u in self.units if u.unit_id != unit.unit_id]
        self.deaths.append((day, unit.unit_id))
        self.events.append(f"r{round_no} {unit.kind} {unit.unit_id} 阵亡（第 {day} 天）")
        self._dead_units.append([day * ROUNDS_PER_DAY + 20, unit])

    def _free_spot_near_base(self) -> tuple[int, int]:
        """在基地附近找一个空格（角色复活用）。"""
        for radius in range(1, 5):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    pos = (self.base[0] + dx, self.base[1] + dy)
                    if self.land(pos) and not self.occupant(pos):
                        return pos
        return self.base

    def _check_fence(self, round_no: int, day: int) -> None:
        """每回合记录"缺几面墙"，供 report 判断围栏何时补齐、夜里是否留破口。"""
        missing = [pos for pos in self.all_wall_cells() if (pos.x, pos.y) not in self.walls]
        self.wall_holes_seen.append((round_no, len(missing)))


def run_case(base: tuple[int, int], label: str, rounds: int = 10 * ROUNDS_PER_DAY) -> World:
    """跑满一整局（默认 10 天 = 1300 回合），返回跑完后的世界。"""
    # 关掉文件日志：跑 1300 回合会把整局 request/response 写进磁盘，测试不需要
    ROUND_LOG.configure(path=None, echo=False)  # keep the test run quiet
    # 重置 agent 的跨回合状态，避免上一个 case 的状态串味
    brain._STATE = brain.MatchState()
    world = World(base)
    for round_no in range(1, rounds + 1):
        # 标准的一回合：下发 request -> 取回 response -> 判题器应用 -> 推进世界
        payload = world.payload(round_no)
        response = brain.decide(payload)
        world.apply(response, round_no)
        world.tick(round_no)
    return world


def report(world: World, label: str) -> list[str]:
    """打印跑分摘要并做断言，返回问题列表（空列表 = 通过）。"""
    problems: list[str] = []
    layout = tactics.layout(Pos(*world.base))
    print(f"\n=== {label} base={world.base} side={layout.side} ===")
    print(f"  gold                : {world.gold}")
    print(f"  rockets built       : {sorted(world.towers)}")
    # 战术脚本要求 16 面外围墙（用户口述规则）
    print(f"  walls standing      : {len(world.walls)}/16")
    # 10 面重点墙的等级：目标是全部升到 level3（任务书 4.5.1 最高等级）
    levels = [world.walls[(pos.x, pos.y)].level if (pos.x, pos.y) in world.walls else 0
              for pos in layout.upgrade_walls]
    print(f"  10 key wall levels  : {levels}")
    print(f"  accepted / submitted: {world.accepted} / {world.submitted}")
    print(f"  prompts / sandbox   : {world.prompts} / {world.execs}")
    print(f"  purchases           : {world.buys}")
    # rejected = 指令合法但没生效（真判题器只跳过该指令，不计异常）
    alive = [f"{u.kind}{u.unit_id}" for u in world.units if u.kind in ("worker", "pioneer")]
    hp = {u.unit_id: u.health for u in world.units if u.kind in ("worker", "pioneer")}
    print(f"  角色阵亡            : {world.deaths or '无'}")
    # 默认波次（现实强度）下不应该有人阵亡：这是"夜里外出/站位是否安全"的回归闸门
    if world.deaths:
        problems.append(f"{label}: 默认波次下出现阵亡 {world.deaths}")
    print(f"  角色存活/血量       : {alive} {hp}")
    print(f"  rejected (rule)     : {len(world.rejected)}")
    for line in world.rejected[:8]:
        print(f"      - {line}")
    # malformed = 格式/指令错误：会真的累积成队伍异常，必须为 0
    print(f"  malformed           : {len(world.malformed)}")
    for line in world.malformed[:8]:
        print(f"      - {line}")

    # ---- 断言区 ----------------------------------------------------------
    if world.malformed:
        problems.append(f"{label}: malformed commands {world.malformed[:3]}")
    # 第一天要造满 3 座火箭（3 x 25 = 75 金 = 初始资金，任务书 4.5.3）
    if len(world.towers) != 3:
        problems.append(f"{label}: expected 3 rockets, got {len(world.towers)}")
    for pos in layout.rockets:
        if (pos.x, pos.y) not in world.towers:
            problems.append(f"{label}: missing rocket at {pos}")
    # 第 1~3 天每天 2 个任务点各做 1 次 -> 共 6 次领取、6 次提交
    if world.accepted < 6:
        problems.append(f"{label}: expected 6 accepted tasks, got {world.accepted}")
    if world.submitted < 6:
        problems.append(f"{label}: expected 6 submitted answers, got {world.submitted}")
    # 每个任务 2 次 LLM + 1 次沙盒 -> 至少 12 / 6（接口文档 2.1）
    if world.prompts < 12:
        problems.append(f"{label}: expected >=12 LLM prompts, got {world.prompts}")
    # 终局至少要有 10 面墙还在（说明夜间修补循环在起作用）
    if len(world.walls) < 10:
        problems.append(f"{label}: fence not maintained, only {len(world.walls)} walls left")
    # 围栏补齐时刻：16 面墙全在的第一个回合
    first_full = next(
        (round_no for round_no, missing in world.wall_holes_seen if missing == 0), None
    )
    print(f"  fence completed at  : round {first_full}")
    # 夜里不能建墙（任务书 4.4），所以第 1 天白天没建完的围栏只能等第 2 天白天补，
    # 这里允许拖到第 2 天白天结束前补齐（选手口述："白天没修完就没修完吧"）。
    if first_full is None or first_full > 2 * ROUNDS_PER_DAY:
        problems.append(
            f"{label}: fence not completed by the end of day 2 (round {first_full})"
        )
    # 基地升级券必须买到（基地血量 <500 时的救命手段，用户口述规则）
    if not any(name == "StationUpgradeVoucher1" for _, name, _ in world.buys):
        problems.append(f"{label}: station upgrade voucher was never bought")
    # 白天收工（每天第 70 回合）时围栏不该有缺口：夜里不能修墙，被打坏只能等
    # 第二天白天补，所以这是"白天修补职责是否生效"的检查。第 1 天允许没建完。
    day_ends = [
        (round_no, missing)
        for round_no, missing in world.wall_holes_seen
        if (round_no - 1) % ROUNDS_PER_DAY == DAY_ROUNDS - 1
    ]
    late_holes = [
        (round_no, missing)
        for round_no, missing in day_ends
        if missing and (round_no - 1) // ROUNDS_PER_DAY + 1 >= 2
    ]
    print(f"  每天收工缺口        : {day_ends}")
    if late_holes:
        problems.append(f"{label}: 白天收工仍有围墙缺口 {late_holes[:3]}")
    return problems


def main() -> int:
    """左（基地 x<20）、右（基地 x>20）两套脚本各跑一整局。"""
    problems: list[str] = []
    # 两个 case 分别验证镜像的两套坐标脚本
    for base, label in (((10, 24), "left"), ((30, 10), "right")):
        world = run_case(base, label)
        problems += report(world, label)
    print("\n================ RESULT ================")
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
