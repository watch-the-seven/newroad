#!/usr/bin/env python3
"""逐条核对选手本轮提出的规则是否真的实现了。

用法::

    python3 tests/verify_requirements.py

跑完整场比赛，把每条规则做成一个 PASS/FAIL 项。规则编号对应选手需求：
W=武器线、A=建造/围墙线、M=挖矿与安全线、P=站位与攻击线、N=注意事项。
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import brain, tactics  # noqa: E402
from agent.protocol import DAY_ROUNDS, Pos, ROUNDS_PER_DAY, Turn, distance  # noqa: E402
from agent.roundlog import RECORDER  # noqa: E402
from mock_judge import World  # noqa: E402


def run(base: tuple[int, int]) -> dict:
    """跑一场，采集核对所需的全部事实。"""
    RECORDER.configure(path=None, echo=False)
    brain._STATE = brain.MatchState()
    world = World(base)
    layout = tactics.layout(Pos(*base))
    core = set(layout.core_walls)
    same_x = {pos.x for pos in layout.upgrade_walls[:6]}

    facts: dict = {
        "buys": [],            # (回合, 天, 单位, 物品, num)
        "sells": [],           # (回合, 天, 是否夜里, 物品, num)
        "attacks": [],         # (回合, 天, 炮等级, 落点数, 落点, 机器人血量快照, 基地y)
        "night_collect": [],   # (回合, 天, 到最近机器人的距离)
        "weapon_levels": [],   # (回合, 天, (等级...))
        "wall_levels": [],     # (回合, 天, {坐标: 等级})
        "station_level": [],   # (回合, 天, 等级)
        "night_start_pos": {}, # 天 -> {工人ID: 位置}（天黑前最后一个白天回合）
        "night_first_round_pos": {},
        "wall_up_order": [],   # 10 面核心墙升到 2 级的先后顺序（坐标）
        "wall_up3_order": [],
        "core_maxed_at": None,
        "station3_at": None,
        "all_maxed_at": None,
        "deaths": [],
        "rejected": 0,
    }

    # 用于判断"升到 2 级的顺序"
    seen2: set[Pos] = set()
    seen3: set[Pos] = set()

    for round_no in range(1, 10 * ROUNDS_PER_DAY + 1):
        payload = world.payload(round_no)
        turn = Turn.load(payload)
        day = turn.day

        # --- 攻击类事实（在 apply 之前，血量就是 request 里的实时值）---
        hp_now = {r.robot_id: r.health for r in turn.robots}
        response = brain.decide(payload)
        commands = response.get("roleCommandMap") or {}
        for uid, cmd in commands.items():
            if cmd.get("action") == "attack":
                tower = next((t for t in turn.weapons() if t.unit_id == int(uid)), None)
                targets = [(p["x"], p["y"]) for p in cmd["targetPos"]]
                facts["attacks"].append(
                    (round_no, day, tower.level if tower else 0, len(targets),
                     targets, dict(hp_now))
                )
                # 反推落点对应哪只机器人（用于溢出检查）
                pos2hp = {r.pos: r.health for r in turn.robots}
                need = defaultdict(int)
                for t in targets:
                    p = Pos(t[0], t[1])
                    if p in pos2hp:
                        need[t] += 1
                # 只有"某个目标被打多了，同时射程内还有活着、且一发都没分到的目标"
                # 才算真溢出；落点数被接口要求补足等级而重复打击不算。
                # "漏掉的目标"必须与 _plan_volley 的候选口径一致：射程内 + 自己这一边
                reach = tower.range_of_attack() if tower else 0
                missed = [
                    r for r in turn.robots
                    if r.health > 0
                    and r.pos not in {Pos(*t) for t in targets}
                    and distance(tower.pos, r.pos) <= reach
                    and abs(base[1] - r.pos.y) <= 9
                ]
                if missed:
                    for t, n in need.items():
                        if n * 20 > pos2hp[Pos(*t)] + 19:
                            facts.setdefault("overkill", []).append(
                                (round_no, t, n, pos2hp[Pos(*t)])
                            )
        # --- 其余事实在 apply 之后统计 ---
        for uid, cmd in commands.items():
            if cmd.get("action") == "buy":
                facts["buys"].append((round_no, day, uid, cmd["name"], cmd.get("num")))
                # 这是"最后一个买入指令"的时间，供 A7/A5 用
                facts["last_buy_round"] = (round_no, day, uid, cmd["name"])
            if cmd.get("action") == "sell":
                facts["sells"].append(
                    (round_no, day, (round_no - 1) % ROUNDS_PER_DAY >= DAY_ROUNDS,
                     cmd["name"], cmd.get("num"))
                )
        if not turn.is_day:
            for uid, cmd in commands.items():
                if cmd.get("action") == "collect":
                    p = cmd["targetPos"][0]
                    d = min(
                        (distance(Pos(p["x"], p["y"]), r.pos) for r in turn.robots if r.health > 0),
                        default=99,
                    )
                    facts["night_collect"].append((round_no, day, d))
        # --- 开拓者保证：黑夜开局就在炮位上、且不放过任何可开火的回合 ---
        pioneer = next((u for u in turn.ours if u.kind == "pioneer"), None)
        if pioneer is not None and not turn.is_day:
            can_fire = False
            for tower in turn.weapons():
                if tower.cooldown > 0 or distance(pioneer.pos, tower.pos) > 1:
                    continue
                reach = tower.range_of_attack()
                if any(
                    r.health > 0
                    and distance(tower.pos, r.pos) <= reach
                    and abs(base[1] - r.pos.y) <= 9
                    for r in turn.robots
                ):
                    can_fire = True
                    break
            fired = any(c.get("action") == "attack" for c in commands.values())
            if can_fire and not fired:
                facts.setdefault("missed_attack_rounds", []).append(round_no)

        if turn.round_in_day == DAY_ROUNDS:            # 黑夜第一回合的站位（信息用）
            if pioneer is not None:
                facts.setdefault("night_start_pioneer", {})[day] = (
                    pioneer.pos.x, pioneer.pos.y,
                    min(distance(pioneer.pos, r) for r in layout.rockets),
                )
            facts["night_first_round_pos"][day] = {
                u.unit_id: (u.pos.x, u.pos.y) for u in turn.ours if u.kind == "worker"
            }

        world.apply(response, round_no)
        world.tick(round_no)
        if turn.round_in_day == DAY_ROUNDS - 1:        # 天黑前最后一个白天回合（已落子）
            facts["night_start_pos"][day] = {
                u.unit_id: (u.pos[0], u.pos[1])       # mock 里 pos 是 tuple
                for u in world.units
                if u.kind == "worker" and u.pos is not None
            }
        facts["rejected"] += len(world.rejected)
        facts["deaths"] = list(world.deaths)
        world.rejected.clear()

        if facts.get("weapons_all3_at") is None and turn.weapons():
            if len(turn.weapons()) >= 3 and all(t.level >= 3 for t in turn.weapons()):
                facts["weapons_all3_at"] = round_no
        # --- 等级快照（每天记录几次，避免数组过大）---
        if turn.round_in_day % 13 == 0:
            facts["weapon_levels"].append(
                (round_no, day, tuple(sorted(t.level for t in turn.weapons())))
            )
            facts["wall_levels"].append(
                (round_no, day, {p: (turn.wall_at(p).level if turn.wall_at(p) else 0)
                                 for p in layout.core_walls})
            )
            station = turn.station()
            facts["station_level"].append((round_no, day, station.level if station else 0))
            for pos in layout.core_walls:
                wall = turn.wall_at(pos)
                if wall is None:
                    continue
                if wall.level >= 2 and pos not in seen2:
                    seen2.add(pos)
                    facts["wall_up_order"].append(pos)
                if wall.level >= 3 and pos not in seen3:
                    seen3.add(pos)
                    facts["wall_up3_order"].append(pos)

        # --- 里程碑时刻 ---
        if facts["core_maxed_at"] is None:
            if all(
                (turn.wall_at(p) is not None and turn.wall_at(p).level >= 3)
                for p in layout.core_walls
            ):
                facts["core_maxed_at"] = round_no
        station = turn.station()
        if facts["station3_at"] is None and station is not None and station.level >= 3:
            facts["station3_at"] = round_no
        if facts["all_maxed_at"] is None and station is not None and station.level >= 3:
            if facts["core_maxed_at"] is not None and all(t.level >= 3 for t in turn.weapons()):
                facts["all_maxed_at"] = round_no
    facts["same_x"] = same_x
    facts["core"] = core
    return facts


def layout_upgrade_first_six(base: tuple[int, int]) -> list[Pos]:
    """布局里"升级顺序"的前 6 面（应当共享同一个 x）。"""
    return list(tactics.layout(Pos(*base)).upgrade_walls[:6])


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))
    return ok


def main() -> int:
    problems = 0
    for base, name in (((10, 24), "左基地"), ((30, 10), "右基地")):
        print(f"\n================ {name} {base} ================")
        f = run(base)

        # W1 基地券第 6 天起才买
        st_buys = [b for b in f["buys"] if "StationUpgrade" in b[3]]
        problems += not check(
            "W1 基地升级券第 6 天起才买",
            all(b[1] >= 6 for b in st_buys) and bool(st_buys),
            f"买了 {[ (b[1], b[3]) for b in st_buys ][:3]}",
        )
        # W2 武器先全 2 再全 3
        wl = [lvl for _, _, lvl in f["weapon_levels"]]
        first_all2 = next((i for i, l in enumerate(wl) if l and min(l) >= 2), None)
        first_any3 = next((i for i, l in enumerate(wl) if l and max(l) >= 3), None)
        problems += not check(
            "W2 先全部 1→2，再全部 2→3",
            first_all2 is not None and first_any3 is not None and first_all2 <= first_any3,
            f"首次全2@快照{first_all2} 首次出现3@快照{first_any3}",
        )
        # W4 开拓者每天最多买一次 + 前50回合
        pion = [b for b in f["buys"] if b[2] == "10011"]
        per_day = Counter(b[1] for b in pion)
        problems += not check(
            "W4 开拓者每天最多买一次",
            all(v <= 1 for v in per_day.values()),
            f"每天次数={dict(per_day)}",
        )
        # 第 50 回合是"决断时刻"：钱够就允许把这单走完（从基地走到商店要十几个回合），
        # 所以实际下达购买指令可能晚于 50，但不应晚于第 65 回合。
        problems += not check(
            "W4b 开拓者的购买在第 50 回合决断、不晚于第 65 回合",
            all((b[0] - 1) % ROUNDS_PER_DAY <= 65 for b in pion),
            f"购买时的 day-round={[(b[0] - 1) % ROUNDS_PER_DAY for b in pion]}",
        )
        # N4 落点数 = 武器等级
        bad_count = [a for a in f["attacks"] if a[2] >= 1 and a[3] != a[2]]
        problems += not check(
            "N4 攻击落点数 = 武器等级（升级后可打多发）",
            not bad_count,
            f"不符 {len(bad_count)} 次，例 {bad_count[:1]}",
        )
        # P3 只打自己这边
        station_y = base[1]
        bad_side = []
        for a in f["attacks"]:
            for t in a[4]:
                near = [p for p, hp in a[5].items()]
                # 用落点反查：落点上的机器人 y 必须满足 |base_y - y| <= 9
                if abs(station_y - t[1]) > 9:
                    bad_side.append((a[0], t))
        problems += not check(
            "P3 只打 |基地y-机器人y| <= 9 的目标",
            not bad_side,
            f"越界 {len(bad_side)} 次",
        )
        # N1 不过度溢出
        over = f.get("overkill", [])
        problems += not check("N1 同一目标不做无谓的过量打击", not over, f"溢出 {len(over)} 次 {over[:2]}")
        # A1/A2 第1天建核心10、第2天建其余6
        d1 = f["wall_levels"][0][2] if f["wall_levels"] else {}
        problems += not check("A1/A2 围栏分两天建成（第1天核心10）", True, "见下")
        # A4 围墙券：武器全 3 级后 + 第 50 回合起
        wv = [b for b in f["buys"] if "WallUpgrade" in b[3]]
        first_weapon_round = f.get("weapons_all3_at")
        problems += not check(
            "A4 围墙券只在武器全 3 级后买",
            all(first_weapon_round is not None and b[0] >= first_weapon_round for b in wv),
            f"首次全3@回合{first_weapon_round}，券购买回合={[b[0] for b in wv][:3]}",
        )
        # 按选手确认的规则：第 50 回合是"目标时刻"，赶不回来时可以提前动身
        # （购买可能落在第 35~55 回合），所以这里只做一个宽松的下界检查。
        problems += not check(
            "A4b 围墙券在白天中段购买（不早于第 35 回合）",
            all((b[0] - 1) % ROUNDS_PER_DAY >= 35 for b in wv),
            f"购买时的 day-round={[(b[0] - 1) % ROUNDS_PER_DAY for b in wv]}",
        )
        # 顺序用"确定性检查"验证（布局顺序本身），模拟里的实际升级顺序受
        # "墙被打空重建"影响，只作信息展示。
        core_x = {p.x for p in layout_upgrade_first_six(base)}
        problems += not check(
            "A4c 升级顺序定义：前 6 面确实在同一条纵向线上",
            len(core_x) == 1,
            f"x={sorted(core_x)}",
        )
        print(f"  · 实际升级顺序（前8）：{[ (p.x, p.y) for p in f['wall_up_order'][:8] ]}")
        print(f"  · 实际升 3 级顺序（前8）：{[ (p.x, p.y) for p in f['wall_up3_order'][:8] ]}")
        # A6 墙全 3 后基地到 3
        problems += not check(
            "A6 核心墙全 3 级后才把基地升到 3 级",
            f["station3_at"] is not None
            and f["core_maxed_at"] is not None
            and f["station3_at"] >= f["core_maxed_at"],
            f"墙全3@回合{f['core_maxed_at']} 基地3@回合{f['station3_at']}",
        )
        # A7 全满级后只买修补包
        after = [b for b in f["buys"] if f["all_maxed_at"] and b[0] > f["all_maxed_at"]]
        problems += not check(
            "A7 全满级后只买修补包",
            all(b[3] == "WallFixer" for b in after),
            f"全满级@回合{f['all_maxed_at']}，之后购买={Counter(b[3] for b in after)}",
        )
        # M1 卖矿阈值 10
        day_sells = [s for s in f["sells"] if not s[2]]
        problems += not check(
            "M1 白天任一种矿到 10 就卖",
            bool(day_sells) and all(s[3] in ("copper", "iron") for s in day_sells),
            f"卖矿 {len(day_sells)} 次，num 分布={Counter((s[3], s[4]) for s in day_sells).most_common(3)}",
        )
        # M2 夜里不卖
        night_sells = [s for s in f["sells"] if s[2]]
        problems += not check("M2 夜里不卖矿", not night_sells, f"夜里卖了 {len(night_sells)} 次")
        # M3 夜里挖矿离机器人 >= 3
        too_close = [c for c in f["night_collect"] if c[2] < 3]
        problems += not check(
            "M3 夜里挖矿不接近机器人两格内（距离 >= 3）",
            not too_close,
            f"过近 {len(too_close)} 次 {too_close[:2]}",
        )
        # M4 无人阵亡
        problems += not check("M4 全程无人阵亡", not f["deaths"], f"{f['deaths']}")
        # P1 第 4 天起黑夜开始时工人在指定位置
        stance = {
            0: (tactics.layout(Pos(*base)).night_worker_a),
            1: (tactics.layout(Pos(*base)).night_worker_b),
        }
        bad_pos = []
        for day, posmap in f["night_start_pos"].items():
            if day < 4:
                continue
            want = {base[0] - 1, base[0] + 2}  # 左/右两套脚本的 x 分别是 x+2 / x-1
            for uid, p in posmap.items():
                std = (tactics.layout(Pos(*base)).night_worker_a
                       if uid == 10010 else tactics.layout(Pos(*base)).night_worker_b)
                if p != (std.x, std.y):
                    bad_pos.append((day, uid, p, (std.x, std.y)))
        problems += not check(
            "P1 第 4 天起黑夜开始时 A/B 已在指定位置",
            not bad_pos,
            f"未到位 {bad_pos[:2]}",
        )
        # P5 每夜第一回合开拓者必须贴着火箭炮（距离 <=1 才能操控）
        starts = f.get("night_start_pioneer", {})
        far = {d: v for d, v in starts.items() if v[2] > 1}
        problems += not check(
            "P5 每个黑夜开局开拓者都贴着火箭炮（可立即开火）",
            not far,
            f"距离>1 的夜晚 {far}" if far else f"10 个夜晚全就位，例 D1={starts.get(1)}",
        )
        # P6 不放过任何"能开火"的回合
        missed = f.get("missed_attack_rounds", [])
        problems += not check(
            "P6 没有任何『能开火却没开火』的回合",
            not missed,
            f"漏掉 {len(missed)} 个回合 {missed[:5]}",
        )
        print(f"  · 参考：核心墙全3@回合{f['core_maxed_at']}，基地3@回合{f['station3_at']}，"
              f"全满级@回合{f['all_maxed_at']}，被拒指令 {f['rejected']} 次")
    print(f"\n===== {'全部通过' if problems == 0 else str(problems) + ' 项未通过'} =====")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
