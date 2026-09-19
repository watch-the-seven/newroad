#!/usr/bin/env python3
"""Edge-case smoke tests: odd payloads must never crash the agent.

Run:  python3 Demo/CoreGeek/tests/test_edge.py

（中文说明）边界与故障路径单测：故意喂畸形/极端 payload，确认 agent 不抛异常、
响应结构始终合法；另外单独验证任务链路的两条兜底路径，以及每回合日志是否落盘。
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/ 的上一级是 CoreGeek 包根，src/ 才是 agent 代码所在
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent import brain, tactics  # noqa: E402
from agent.protocol import Pos  # noqa: E402

# 响应顶层必须是且仅是这三个字段（接口文档 2.1）
RESPONSE_KEYS = {"roleCommandMap", "prompt", "executeCmd"}


def payload(
    round_no: int,
    base: tuple[int, int] | None,
    *,
    station_health: int = 1500,
    zones: list[dict] | None = None,
) -> dict:
    """造一个"最小但结构完整"的 request。

    base=None 用来模拟"基地已不存在/单位全灭"的极端 payload；
    station_health=0 用来模拟基地被摧毁（任务书 4.5.1 基地初始 1500 血）。
    """
    roles = []
    if base is not None:
        roles.append(
            {
                # 基地 ID 固定 10013（接口文档 1.3.1 角色 ID 分配规则）
                "id": 10013,
                # 基地 pos 传 2x2 的左上角坐标（接口文档 1.3.1 注）
                "pos": {"x": base[0], "y": base[1]},
                "roleType": "station",
                "health": station_health,
                # level 只有建筑才有，角色没有（接口文档 1.3.1）
                "level": 1,
                "attackPower": 0,
                "attackRange": 0,
                "backPackCapability": 0,
                "backpack": [],
            }
        )
    # (角色 ID, 相对基地左上角的 dx, dy)：工人1=10010、工人2=10012、开拓者=10011
    # （接口文档 1.3.1；开局各 1 名开拓者 + 2 名工人，见任务书 4.5.3）
    workers = [(10010, -4, 4), (10012, 4, 4), (10011, -4, -4)]
    for unit_id, dx, dy in workers:
        if base is None:
            continue
        kind = "pioneer" if unit_id == 10011 else "worker"
        roles.append(
            {
                "id": unit_id,
                "pos": {"x": base[0] + dx, "y": base[1] + dy},
                "roleType": kind,
                # HP / 背包：开拓者 200/40 格，工人 220/100 格（任务书 4.5.2）
                "health": 200 if kind == "pioneer" else 220,
                "attackPower": 0,
                "attackRange": 0,
                "backPackCapability": 40 if kind == "pioneer" else 100,
                "backpack": [],
            }
        )
    return {
        "roundNo": round_no,
        # 地图固定 41x32（任务书 4.1）；zones 是中立元素列表（接口文档 1.2.1）
        "mapInfo": {"width": 41, "height": 32, "zones": zones or []},
        "teamOur": {
            # challenger 的基地在左上，defender 在右下（任务书 三）
            "type": "challenger",
            "teamId": "1",
            "teamName": "edge",
            # 初始金币 75（任务书 4.5.3）
            "goldNum": 75,
            "totalScore": 0,
            # 空任务列表：本文件大部分 case 不涉及任务，只测"没有任务时不崩"
            "playerTasks": [],
            "roles": roles,
        },
        "teamEnemy": {"roles": []},
        "robot": {"roles": []},
        # 未领取任务时 phaseTask 为空串（接口文档 1.1）
        "phaseTask": "",
        "lastRoundRoleActionResults": {},
        # 0 = 上回合未探测 summonTreasure（接口文档 1.1）
        "lastSummonTreasureResult": 0,
        "llmResp": "",
        "worldNews": {"officialNews": "", "folkLegends": ""},
        "lastCmdResult": "",
        "vendorShopList": [],
        "weaponShopList": [],
        "errors": [],
    }


def drive(label: str, make_payload, rounds: int = 300) -> list[str]:
    """连喂 rounds 个回合，返回发现的问题列表（空列表 = 通过）。

    这是本文件的核心守卫：**任何一回合抛异常、响应结构不对，都算失败**。
    默认 300 回合足以覆盖白天→黑夜→第 2 天白天的分支切换。
    """
    # 每个 case 重置跨回合状态，避免上一个 case 的布局/状态污染
    brain._STATE = brain.MatchState()
    problems: list[str] = []
    for round_no in range(1, rounds + 1):
        try:
            response = brain.decide(make_payload(round_no))
        # 这里就是断言本身：agent 无论如何都不该抛出去
        except Exception as error:  # noqa: BLE001 - this is the assertion
            problems.append(f"{label}: round {round_no} raised {error!r}")
            break
        if set(response) != RESPONSE_KEYS:
            problems.append(f"{label}: round {round_no} bad keys {sorted(response)}")
            break
        # 每个角色每回合至多 1 条指令（接口文档 2.3 注）——
        # roleCommandMap 是 dict，天然保证 key 唯一；这里只额外校验指令的形状。
        for key, command in response["roleCommandMap"].items():
            if not isinstance(command, dict) or not isinstance(command.get("action"), str):
                problems.append(f"{label}: round {round_no} bad command {key}={command!r}")
                break
        # prompt / executeCmd 必须是字符串（接口文档 2.1）
        if not isinstance(response["prompt"], str) or not isinstance(
            response["executeCmd"], str
        ):
            problems.append(f"{label}: round {round_no} prompt/executeCmd not str")
            break
    return problems


def task_pipeline_problem() -> list[str]:
    """The .md fallback path: file not readable locally -> cat in the sandbox.

    这里覆盖的是任务流水线的"happy path + 本地读不到 .md 时改用沙盒 cat"：
        accepted -> (本机读失败) cat -> prompt -> executeCmd -> prompt -> submitAnswer
    与 taskflow 的 5 个阶段一一对应，断言每一步交给判题器的字段是否正确。
    """
    from agent.protocol import Turn
    from agent.taskflow import TaskRunner, extract_md_paths

    problems: list[str] = []
    # 故意指向一个不存在的相对路径：本机读不到，必须落到沙盒 cat
    task_text = "任务1：请阅读 ./does-not-exist/notes.md 并回答 answer 是多少。"
    # 路径提取不能把开头的 "./" 吃掉（曾因 strip 字符集含 "." 而踩过坑）
    if extract_md_paths(task_text) != ("./does-not-exist/notes.md",):
        problems.append(f"md path extraction failed: {extract_md_paths(task_text)}")

    base = (10, 24)
    # 任务点 1 占 1 格、任务点 2 占 2 格（任务书 4.6.2）
    zones = [
        {"neutralType": "challengerTaskPoint1", "pos": {"x": 14, "y": 14}},
        {"neutralType": "challengerTaskPoint2", "pos": {"x": 17, "y": 17}},
    ]
    runner = TaskRunner()
    # 直接把状态机摆到"已领取任务"阶段，跳过走路/领任务，专注测流水线本身
    runner.phase = "accepted"
    runner.role_id = 10011
    # 替身移动函数：返回 None = 原地不动（本测试不关心走位）
    approach = lambda *args, **kwargs: None  # noqa: E731

    def turn_with(**extra) -> object:
        """构造一个带任务点1的 Turn，extra 用来塞上一回合的回执字段。"""
        data = payload(extra.pop("round_no", 40), base, zones=zones)
        # 只登记任务点1（自进化类1），对应 playerTasks 结构见接口文档 1.3.2
        data["teamOur"]["playerTasks"] = [
            {
                "taskType": "自进化类1",
                "taskPosition": {"x": 14, "y": 14},
                "coldDownRounds": 0,
                "scoreReward": 50,
                "goldReward": 30,
                "isValid": True,
                "timeoutRounds": 30,
            }
        ]
        data.update(extra)
        return Turn.load(data)

    # 第 1 步：本机读不到 .md -> 必须发沙盒 cat 命令
    turn = turn_with(round_no=40, phaseTask=task_text)
    role = next(unit for unit in turn.ours if unit.unit_id == 10011)
    outcome = runner.drive(turn, role, set(), approach)
    if not (isinstance(outcome, tuple) and outcome[0] == "exec" and "cat" in outcome[1]):
        problems.append(f"expected a cat fallback, got {outcome!r}")

    # 第 2 步：拿到 cat 的输出（文件内容）-> 拼 prompt 丢给 LLM 要 executeCmd
    turn = turn_with(round_no=41, phaseTask=task_text, lastCmdResult="[exitCode:0]\nSEED=12345")
    outcome = runner.drive(turn, role, set(), approach)
    if not (isinstance(outcome, tuple) and outcome[0] == "prompt" and "SEED=12345" in outcome[1]):
        problems.append(f"expected extract prompt with file content, got {outcome!r}")

    # 第 3 步：LLM 回了取数命令 -> 原样作为 executeCmd 交给沙盒
    turn = turn_with(round_no=42, phaseTask=task_text, llmResp='python3 -c "print(12345)"')
    outcome = runner.drive(turn, role, set(), approach)
    if not (isinstance(outcome, tuple) and outcome[0] == "exec" and "12345" in outcome[1]):
        problems.append(f"expected executeCmd from llmResp, got {outcome!r}")

    # 第 4 步：拿到执行结果 -> 再拼 prompt，要求 LLM 整理成任务要求的结构
    turn = turn_with(round_no=43, phaseTask=task_text, lastCmdResult="[exitCode:0]\n12345")
    outcome = runner.drive(turn, role, set(), approach)
    if not (isinstance(outcome, tuple) and outcome[0] == "prompt" and "12345" in outcome[1]):
        problems.append(f"expected format prompt with command output, got {outcome!r}")

    # 第 5 步：LLM 给出结构化答案 -> 以开拓者身份 submitAnswer（接口文档 2.3）
    turn = turn_with(round_no=44, phaseTask=task_text, llmResp='{"answer": 12345}')
    outcome = runner.drive(turn, role, set(), approach)
    if not (
        isinstance(outcome, tuple)
        and outcome[0] == 10011
        and outcome[1].get("action") == "submitAnswer"
        and outcome[1].get("taskAnswer") == '{"answer": 12345}'
    ):
        problems.append(f"expected submitAnswer, got {outcome!r}")
    return problems


def task_fallback_problem() -> list[str]:
    """Sandbox discovery when cat fails, and the retry cap on a silent LLM.

    两条故障路径：
    1. cat 因为路径不对而失败（退出码非 0）-> 自动补一条"扫出沙盒里所有 .md"的命令；
    2. LLM 一直不回话 -> 阶段重试封顶后放弃该任务点，避免开拓者被永久卡死。
    """
    from agent.protocol import Turn
    from agent.taskflow import DISCOVERY_COMMAND, TaskRunner

    problems: list[str] = []
    base = (10, 24)
    zones = [
        {"neutralType": "challengerTaskPoint1", "pos": {"x": 14, "y": 14}},
        {"neutralType": "challengerTaskPoint2", "pos": {"x": 17, "y": 17}},
    ]
    task_text = "任务1：请阅读 ./missing/notes.md 并回答。"
    approach = lambda *args, **kwargs: None  # noqa: E731

    def turn_with(**extra) -> object:
        data = payload(extra.pop("round_no", 40), base, zones=zones)
        # 故意不给 playerTasks：本测试只驱动状态机，不需要任务点元数据
        data["teamOur"]["playerTasks"] = []
        data.update(extra)
        return Turn.load(data)

    runner = TaskRunner()
    # 摆到"已发出 cat、等回执"的阶段
    runner.phase = "read_cmd"
    runner.role_id = 10011
    runner.task_text = task_text
    runner.command = "cat -- ./missing/notes.md"
    runner.phase_round = 40
    role = next(unit for unit in turn_with(round_no=40).ours if unit.unit_id == 10011)

    # cat 失败（退出码 1 + No such file）-> 换成扫描沙盒的兜底命令
    turn = turn_with(
        round_no=41,
        phaseTask=task_text,
        lastCmdResult="[exitCode:1]\ncat: ./missing/notes.md: No such file or directory",
    )
    outcome = runner.drive(turn, role, set(), approach)
    if not (
        isinstance(outcome, tuple)
        and outcome[0] == "exec"
        and outcome[1] == DISCOVERY_COMMAND
    ):
        problems.append(f"expected sandbox discovery command, got {outcome!r}")

    # 扫描成功 -> 用扫回来的内容拼 prompt，流程继续
    turn = turn_with(
        round_no=42,
        phaseTask=task_text,
        lastCmdResult="[exitCode:0]\n./notes.md\nSEED=777",
    )
    outcome = runner.drive(turn, role, set(), approach)
    if not (isinstance(outcome, tuple) and outcome[0] == "prompt" and "SEED=777" in outcome[1]):
        problems.append(f"expected prompt from discovered files, got {outcome!r}")

    # a silent LLM must not stall the pioneer for ever
    # 每 4 回合算一次"阶段超时"，因此这里每次把回合号推后 6 回合，强行触发重试
    runner = TaskRunner()
    runner.phase = "llm1"
    runner.role_id = 10011
    runner.task_text = task_text
    runner.phase_round = 10
    round_no = 20
    for _ in range(6):
        turn = turn_with(round_no=round_no, phaseTask=task_text, llmResp="")
        runner.drive(turn, role, set(), approach)
        round_no += 6
    # 防的是"卡死"：重试封顶后应当已经离开 llm1 并推进到下一个任务点（index >= 2）
    if runner.phase == "llm1" or runner.index < 2:
        problems.append(
            f"retry cap failed: still stuck at phase={runner.phase} index={runner.index}"
        )
    return problems


def wall_voucher_robustness_problem() -> list[str]:
    """墙的 level 为 0（字段缺失/脏数据）时不能抛 KeyError。

    历史问题：``_pending_wall_work`` 里用 ``WALL_VOUCHER[wall.level]`` 取值，
    ``level`` 为 0 直接 KeyError；一旦发生，该回合响应会退化成空指令，而且条件
    不会自愈、之后每回合都会重演，等于整局瘫痪。这里锁死"按 1 级墙处理"的加固行为。
    """
    from agent.brain import _pending_wall_work
    from agent.protocol import Turn

    base = (10, 24)
    layout = tactics.layout(Pos(*base))
    data = payload(140, base)          # 第 2 天，计划表里会走到 upgrade 步骤
    roles = data["teamOur"]["roles"]
    broken = layout.upgrade_walls[0]   # 只把一面"重点墙"的 level 故意弄坏
    for index, cell in enumerate(layout.walls):
        roles.append(
            {
                "id": 40000 + index,
                "pos": {"x": cell.x, "y": cell.y},
                "roleType": "wall",
                "health": 1000,
                "level": 0 if cell == broken else 1,
            }
        )
    turn = Turn.load(data)
    try:
        pending = _pending_wall_work(turn, layout)
    except Exception as error:  # noqa: BLE001 - 这里断言的就是"不许抛异常"
        return [f"level=0 的墙让 _pending_wall_work 抛异常: {error!r}"]
    if (broken, "WallUpgradeVoucher1") not in pending:
        return [f"level=0 的墙没有被按 1 级处理, pending={pending}"]
    return []


def round_log_problem() -> list[str]:
    """Every round must land in the JSONL round log with request + response."""
    import json
    import tempfile

    from agent.roundlog import RECORDER

    problems: list[str] = []
    # 故意多套一层不存在的目录，顺带验证日志器会自建父目录
    path = Path(tempfile.mkdtemp(prefix="roundlog-")) / "nested" / "rounds.jsonl"
    RECORDER.configure(path=str(path), echo=False)
    try:
        brain._STATE = brain.MatchState()
        response = brain.decide(payload(1, (10, 24)))
    finally:
        # 无论断言是否失败都要关掉文件日志，避免污染后续 case 的临时目录
        RECORDER.configure(path=None, echo=False)

    if not path.is_file():
        return [f"round log file was not created at {path}"]
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 只喂了 1 个回合，就该只有 1 行（一行 = 一回合的 request+response）
    if len(lines) != 1:
        problems.append(f"expected exactly 1 round log line, got {len(lines)}")
        return problems
    entry = json.loads(lines[0])
    if entry.get("roundNo") != 1:
        problems.append(f"round log entry has wrong roundNo: {entry.get('roundNo')!r}")
    request = entry.get("request") or {}
    # 抽查一个请求字段，确认落盘的是完整 request 而不是摘要
    if request.get("mapInfo", {}).get("width") != 41:
        problems.append("round log entry is missing the full request payload")
    logged = entry.get("response") or {}
    # 落盘的 response 必须与 decide() 的返回值逐字一致
    if logged != response:
        problems.append("round log response does not match the returned response")
    if "elapsedMs" not in entry:
        problems.append("round log entry is missing elapsedMs")
    return problems


def main() -> int:
    from agent.roundlog import RECORDER as ROUND_LOG

    # 默认静音：只有 round_log_problem 自己会临时打开文件日志
    ROUND_LOG.configure(path=None, echo=False)  # individual tests opt in
    problems: list[str] = []

    # 1) exactly on the left/right boundary
    # x=20 是左右脚本的分界（agent 里 x<20 走左、否则走右），这里只要求不崩
    problems += drive("x=20", lambda r: payload(r, (20, 16)))

    # 2) base destroyed
    # 基地 0 血 = 已被摧毁（任务书 4.5.1）；agent 应优雅降级而不是抛异常
    problems += drive("dead station", lambda r: payload(r, (10, 24), station_health=0))

    # 3) no station at all (freshly destroyed / weird payload)
    # 连基地单位都不存在：layout 无从计算，必须安全返回空指令而不是崩
    problems += drive("no station", lambda r: payload(r, None))

    # 4) zones only, no units, no task list
    # 只有中立元素、没有任何己方单位
    zones = [
        {"neutralType": "stone", "pos": {"x": 5, "y": 5}},
        {"neutralType": "vendor", "pos": {"x": 6, "y": 5}},
        {"neutralType": "weaponShop", "pos": {"x": 7, "y": 5}},
        {"neutralType": "challengerTaskPoint1", "pos": {"x": 8, "y": 5}},
        {"neutralType": "challengerTaskPoint2", "pos": {"x": 9, "y": 5}},
    ]
    problems += drive("zones only", lambda r: payload(r, None, zones=zones))

    # 5) day/night boundary round numbers must be classified consistently
    # 基地 x<20 用左侧脚本、x>20 用右侧脚本（用户口述规则），x=20 归右侧
    if tactics.side_of(Pos(19, 5)) != tactics.LEFT:
        problems.append("side_of(19) should be left")
    if tactics.side_of(Pos(20, 5)) != tactics.RIGHT:
        problems.append("side_of(20) should be right")

    problems += task_pipeline_problem()
    problems += task_fallback_problem()
    problems += round_log_problem()
    problems += wall_voucher_robustness_problem()

    # the .md may be named relative to a directory we do not know about
    # 探针：任务里给的相对目录是错的，但同名文件就在工程树里 -> 应按文件名递归找到
    from agent.taskflow import read_local_files

    probe = ROOT / "tests" / "_tmp_ref_check.md"
    probe.write_text("RECURSIVE-OK", encoding="utf-8")
    try:
        if "RECURSIVE-OK" not in read_local_files(("nope/_tmp_ref_check.md",)):
            problems.append("recursive .md lookup by file name failed")
    finally:
        # 探针文件用完即删，保持工作区干净
        probe.unlink(missing_ok=True)

    print("edge cases:", "OK" if not problems else "FAILED")
    for problem in problems:
        print("  -", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
