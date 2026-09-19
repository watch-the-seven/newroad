"""开拓者的"自进化类"任务流水线。

判题器按回合驱动我们，整个任务要跨好几个回合完成：

    acceptTask      领取任务；判题器随后把任务原文放进 ``phaseTask``
    prompt          把「任务描述 + 参考文件 + 沙盒说明」丢给 LLM，下一回合拿到 ``llmResp``
    executeCmd      把 LLM 给的命令丢进离线沙盒，下一回合拿到 ``lastCmdResult``
    prompt          把执行结果丢回给 LLM，让它整理成任务要求的结构
    submitAnswer    提交最终答案

本模块只依赖当前回合的 payload（``phaseTask`` / ``llmResp`` / ``lastCmdResult``）
加上这里自己记的一点点状态，不依赖任何外部存储。

状态机各阶段（``TaskRunner.phase``）::

    idle ──► travel ──► accepted ──┬─(本地能读到 .md)──────────► llm1
                                   └─(读不到)─► read_cmd ──┬─(cat 成功)──► llm1
                                                          └─(cat 失败)─► discover_cmd ──► llm1
    llm1 ──(收到 llmResp)──► cmd ──(收到 lastCmdResult)──► llm2 ──(收到 llmResp)──► settle ──► idle(下一个任务点)

每个"等外部回复"的阶段都有超时（``_PHASE_TIMEOUT``）和重试上限（``_MAX_RETRIES``），
超时就重发，重试到头就放弃这个任务点，避免开拓者被卡死一整天。
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import (
    Pos,
    Turn,
    Unit,
    accept_task_command,
    distance,
    submit_answer_command,
)

LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# prompt 文案
# --------------------------------------------------------------------------
#: 系统提示词。它要同时覆盖两个阶段（取数 / 整理），因为判题器只给了
#: "prompt" 一个入口，我们只能靠提示词最后那句指令来区分当前该干哪一步。
DEMO_PROMPT = """你是《未来战争》编程大赛中「开拓者」单位背后的任务求解 Agent。

你的职责：把比赛里的「自进化类」任务做出来。你会拿到任务描述，以及任务描述里点名的参考文件（通常是 .md）。你要先给出一条能在沙盒里执行的命令，去把回答问题所需的原始信息取回来；再根据命令的真实输出，把答案整理成任务要求的最终结构。

你唯一能用的工具是 executeCmd：你回复的命令会被判题器放进一个离线 Linux 沙盒里执行，执行结果在下一轮反馈给你。沙盒里有基础 shell 和 python3，没有外网，单条命令限时 15 秒，输出超过 64KB 会被截断。

整个流程分成两个阶段，你每次只会被要求完成其中一个阶段：

1. 取数阶段：读完任务描述与参考文件，输出一条（且仅一条）沙盒命令，用来获得回答问题所需的原始数据。
2. 整理阶段：拿到上一条命令的执行结果，把它整理成任务要求的结构化答案。

硬性要求：
- 只输出内容本身。不要解释、不要寒暄、不要"好的/以下是"之类的话，不要 markdown 代码块围栏（除非任务明确要求保留原文格式）。
- 取数阶段只输出一条可直接执行的命令。优先用 `python3 -c` 或单个 python3 脚本一次性拿到全部所需信息，避免多次往返；命令要自包含、可重复执行。
- 命令不能依赖交互式输入，不能联网，不能读写沙盒以外的路径，不要用 sudo，不要安装依赖。
- 整理阶段严格按任务描述要求的字段名、类型、顺序、大小写输出；除非任务要求，否则不要加引号、单位、注释或额外说明。
- 信息不足时，基于已有信息给出最可能的答案，不要反问、不要输出占位符、不要输出"无法确定"。
- 任务描述里出现多个问题或字段时，全部都要回答，一个都不能漏。"""

#: 附在 prompt 里的沙盒环境说明（接口文档：executeCmd 限时 15 秒、沙盒无外网）
SANDBOX_NOTE = (
    "沙盒环境：离线 Linux，可用基础 shell 命令与 python3，无外网，"
    "单条命令执行时间不超过 15 秒，标准输出超过 64KB 会被截断。"
)

#: 两个阶段的收尾指令，靠它们让 LLM 知道这一轮该输出命令还是输出答案
EXTRACT_INSTRUCTION = "这就是任务描述，请给我对应的executeCmd来获取答案。"
FORMAT_INSTRUCTION = "这是执行的结果，请将他们组织成任务要求的结构。"

_MAX_FILE_CHARS = 40_000  # 单个参考文件塞进 prompt 的上限，防止 prompt 过长
_PHASE_TIMEOUT = 4        # 某个阶段等多少回合还没等到回复就算超时
_MAX_RETRIES = 3          # 一个阶段最多重发几次，再不行就放弃该任务点

#: 从任务描述里抠出 .md 参考文件路径（支持 ./a/b.md、/abs/x.md、a\b.md）
_MD_PATTERN = re.compile(r"([\w./\\-]+\.md)", re.IGNORECASE)
#: 识别 ``` 代码块围栏，LLM 不听话包了一层时用来剥掉
_FENCE_PATTERN = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", re.DOTALL)


def build_extract_prompt(task_text: str, md_text: str) -> str:
    """第一阶段（取数）的 prompt：任务描述 + 参考文件 + 沙盒说明 + 收尾指令。"""
    parts = [DEMO_PROMPT, "", "## 任务描述", task_text.strip()]
    if md_text.strip():
        parts += ["", "## 任务参考文件内容", md_text.strip()]
    parts += ["", "## 沙盒环境", SANDBOX_NOTE, "", EXTRACT_INSTRUCTION]
    return "\n".join(parts)


def build_format_prompt(task_text: str, md_text: str, command: str, result: str) -> str:
    """第二阶段（整理）的 prompt：再给一遍上下文，加上命令原文和它的执行结果。"""
    parts = [DEMO_PROMPT, "", "## 任务描述", task_text.strip()]
    if md_text.strip():
        parts += ["", "## 任务参考文件内容", md_text.strip()]
    parts += [
        "",
        "## 你在取数阶段给出的命令",
        command.strip(),
        "",
        "## 该命令在沙盒中的执行结果",
        result.strip(),
        "",
        FORMAT_INSTRUCTION,
    ]
    return "\n".join(parts)


def extract_md_paths(text: str) -> tuple[str, ...]:
    """从任务描述里提取所有 .md 路径，去重并保持出现顺序。

    注意 strip 的字符集里**不包含 ``.``**：否则 ``./docs/x.md`` 会被削成
    ``/docs/x.md``，把相对路径变成绝对路径。
    """
    seen: list[str] = []
    for match in _MD_PATTERN.finditer(text or ""):
        path = match.group(1).strip("`'\"()[],;: ")
        if path and path not in seen:
            seen.append(path)
    return tuple(seen)


def read_local_files(paths: tuple[str, ...]) -> str:
    """把能读到的参考文件拼成一段文本；一个都读不到就返回空串。

    每个文件加 ``### 路径`` 小标题，便于 LLM 区分多份参考文件。
    """
    chunks: list[str] = []
    for path in paths:
        content = _read_one(path)
        if content is not None:
            chunks.append(f"### {path}\n{content[:_MAX_FILE_CHARS]}")
    return "\n\n".join(chunks)


def _search_roots() -> tuple[Path, ...]:
    """本机查找参考文件时的候选根目录（进程 CWD、包目录、src、工程根、上级）。

    判题器从哪个目录启动进程不确定，任务里的相对路径也未必相对我们，
    所以多列几个根目录，用 ``dict.fromkeys`` 去重且保持顺序。
    """
    here = Path(__file__).resolve()
    parents = here.parents
    roots = [Path.cwd(), here.parent]
    roots += [parents[index] for index in (1, 2, 3) if len(parents) > index]
    return tuple(dict.fromkeys(roots))


def _read_file(candidate: Path) -> str | None:
    """读一个文件，不存在或读不了都返回 None（不抛异常）。"""
    try:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - 防御性分支
        return None
    return None


def _read_one(path: str) -> str | None:
    """四级兜底地找一个参考文件：

    1. 绝对路径直接读；
    2. 相对路径依次拼到各个候选根目录下读；
    3. 还找不到就按**文件名**在各根目录下递归搜（任务给的相对路径可能不是相对我们）；
    4. 全部失败返回 None，交给调用方改用沙盒 ``cat``。
    """
    raw = Path(path)
    roots = _search_roots()
    if raw.is_absolute():
        content = _read_file(raw)
        if content is not None:
            return content
    else:
        for root in roots:
            content = _read_file(root / raw)
            if content is not None:
                return content

    # 任务可能按别的目录写路径：退一步，按文件名在工作目录/工程树里找同名文件
    if raw.name:
        for root in roots:
            try:
                for candidate in root.glob(f"**/{raw.name}"):
                    content = _read_file(candidate)
                    if content is not None:
                        return content
            except OSError:  # pragma: no cover - 防御性分支
                continue
    return None


def cat_command(paths: tuple[str, ...]) -> str:
    """本机读不到时，改在沙盒里把文件 cat 回来（多条用 ``;`` 串起来）。

    用 ``shlex.quote`` 保证路径里的空格/特殊字符不会破坏命令。
    """
    return " ; ".join(f"cat -- {shlex.quote(path)}" for path in paths)


#: ``cat`` 也失败时（说明路径确实不对）的兜底：把沙盒里所有 .md 都打出来。
#: ``-maxdepth 4`` 限制递归深度，``head -c`` 防止输出超过接口的 64KB 上限。
DISCOVERY_COMMAND = (
    "find . -maxdepth 4 -type f -name '*.md' -print -exec cat -- {} \\; "
    "2>/dev/null | head -c 40000"
)

#: 判断沙盒返回是否"看起来失败了"的关键词（配合退出码一起用）
_FAILURE_MARKERS = (
    "no such file",
    "cannot access",
    "not found",
    "not a directory",
    "permission denied",
    "is a directory",
    "[timeout]",
)


def _looks_failed(result: str) -> bool:
    """沙盒结果是否失败。

    优先看判题器约定的 ``[exitCode:N]`` 前缀；没有前缀时退回关键词匹配。
    关键词匹配可能误判（比如正常输出里恰好含 "not found"），代价只是多发一条
    兜底命令，不影响正确性。
    """
    lowered = result.lower()
    if "[exitcode:" in lowered:
        head = lowered.split("[exitcode:", 1)[1].split("]", 1)[0].strip()
        try:
            if int(head) != 0:
                return True
        except ValueError:
            pass
    return any(marker in lowered for marker in _FAILURE_MARKERS)


def strip_fence(text: str) -> str:
    """LLM 不听话加了 ``` 围栏时，把围栏剥掉只留内容。"""
    stripped = (text or "").strip()
    match = _FENCE_PATTERN.search(stripped)
    if match and stripped.startswith("```"):
        return match.group(1).strip()
    return stripped


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------
@dataclass
class TaskRunner:
    """一次只做一个任务点，一天最多做两个（任务点1、任务点2）。"""

    day: int = 0            # 当前游戏日；跨天时自动重置流水线
    index: int = 1          # 当前任务点编号（1 或 2），>2 表示今天做完了
    phase: str = "idle"     # 状态机阶段，见模块 docstring
    task_text: str = ""     # 任务原文（从 phaseTask 抄下来，后续 prompt 复用）
    md_text: str = ""       # 参考文件内容（本机读到的或沙盒 cat 回来的）
    command: str = ""       # 取数阶段最终发给沙盒的命令
    phase_round: int = 0    # 进入当前阶段的回合号，用于超时判断
    retries: int = 0        # 当前阶段已重试次数
    role_id: int = 0        # 开拓者 ID（提交答案时要用）

    # -- 记账 --------------------------------------------------------------
    def on_round(self, turn: Turn) -> None:
        """每回合开头调用：跨天就重置，保证"每天两个任务"的节奏。"""
        if turn.day != self.day:
            self.day = turn.day
            self._reset_day()

    def _reset_day(self) -> None:
        """新的一天：回到任务点 1、清空上一天的任务上下文。"""
        self.index = 1
        self.phase = "idle"
        self.task_text = ""
        self.md_text = ""
        self.command = ""
        self.phase_round = 0
        self.retries = 0

    def _enter(self, phase: str, round_no: int) -> None:
        """切换阶段并重置该阶段的重试计数。"""
        self.phase = phase
        self.phase_round = round_no
        self.retries = 0

    def _timed_out(self, turn: Turn) -> bool:
        """当前阶段是否已经等太久了。"""
        return turn.round_no - self.phase_round > _PHASE_TIMEOUT

    # -- 主入口 ------------------------------------------------------------
    def drive(
        self,
        turn: Turn,
        role: Unit,
        claimed: set[Pos],
        approach: Any,
    ) -> tuple[Any, ...] | None:
        """推进一回合，返回值有四种：

        * ``(unit_id, 指令)``   开拓者这一回合要执行的动作（acceptTask/submitAnswer/移动）
        * ``("prompt", 文本)``  这一回合要发给 LLM 的 prompt（走响应顶层字段）
        * ``("exec", 命令)``    这一回合要丢进沙盒的命令（走响应顶层字段）
        * ``WAIT``              正在等外部回复，这一回合什么都不做（但**不能**让上层
                                继续执行后续步骤，否则开拓者会跑去干别的）
        * ``None``              今天的任务都做完了，上层可以继续执行后面的步骤
        """
        self.role_id = role.unit_id
        # 任务点在冷却 = 任务已经结束（完成/超时/离开），别再等 LLM 了
        if self._task_ended(turn):
            LOGGER.info("task point %s ended before the pipeline finished", self.index)
            self.index += 1
            self._enter("idle", turn.round_no)
            return WAIT
        if self.phase == "idle":
            return self._drive_idle(turn, role, claimed, approach)
        if self.phase == "travel":
            return self._drive_travel(turn, role, claimed, approach)
        if self.phase == "accepted":
            return self._drive_accepted(turn)
        if self.phase == "read_cmd":
            return self._drive_read_cmd(turn)
        if self.phase == "discover_cmd":
            return self._drive_discover_cmd(turn)
        if self.phase == "llm1":
            return self._drive_llm1(turn)
        if self.phase == "cmd":
            return self._drive_cmd(turn)
        if self.phase == "llm2":
            return self._drive_llm2(turn)
        if self.phase == "settle":
            # 提交后停一回合，让判题器把"任务结束"处理完，再去做下一个任务点
            if turn.round_no - self.phase_round >= 1:
                self.index += 1
                self._enter("idle", turn.round_no)
            return WAIT
        return None

    def _task_ended(self, turn: Turn) -> bool:
        """判断"任务已经结束"。

        任务结束（完成/超时/离开任务点/开拓者阵亡）后，该任务点会进入
        30 回合刷冷（任务书 5.3），所以 ``coldDownRounds > 0`` 是一个可靠信号：
        此时继续等 LLM/沙盒已经没有意义，应该放弃当前任务点。

        只在流水线中段判断，且要求已经进入该阶段至少 2 回合，
        避免把"刚领取、phaseTask 还没下发"误判成结束。
        """
        if self.phase not in ("read_cmd", "discover_cmd", "llm1", "cmd", "llm2"):
            return False
        if turn.round_no - self.phase_round < 2:
            return False
        point = turn.task_point(self.index)
        return bool(point is not None and point.cooldown > 0)

    def _retry(self, turn: Turn, phase: str, action: Any) -> Any:
        """阶段超时后的统一处理：重发（保留重试计数），重试到头就换下一个任务点。

        这里刻意不用 ``_enter()``，因为它会把 ``retries`` 清零。
        """
        self.retries += 1
        if self.retries > _MAX_RETRIES:
            LOGGER.warning(
                "task point %s stalled in %s, moving on", self.index, phase
            )
            self.index += 1
            self._enter("idle", turn.round_no)
            return WAIT
        self.phase = phase
        self.phase_round = turn.round_no
        return action()

    # -- 各阶段 ------------------------------------------------------------
    def _drive_idle(self, turn, role, claimed, approach):
        """选下一个任务点：跳过"永久没任务"的点，然后进入赶路阶段。"""
        while self.index <= 2 and self._permanently_gone(turn, self.index):
            self.index += 1
        if self.index > 2:
            return None  # 今天的两个任务点都处理完了
        self._enter("travel", turn.round_no)
        return WAIT

    def _drive_travel(self, turn, role, claimed, approach):
        """走到任务点旁边；到了就发 acceptTask。"""
        cells = turn.task_point_cells(self.index)
        if not cells:  # 地图上找不到这个任务点，跳过
            self.index += 1
            self._enter("idle", turn.round_no)
            return WAIT
        point = turn.task_point(self.index)
        if point is not None and not point.valid:
            # 该任务点的任务已经全部做完（永久不可用）→ 去下一个
            self.index += 1
            self._enter("idle", turn.round_no)
            return WAIT
        if _adjacent_to(role.pos, cells):
            if point is not None and point.cooldown > 0:
                return WAIT  # 冷却中：就地等刷新，不发无用的 acceptTask
            self._enter("accepted", turn.round_no)
            return (role.unit_id, accept_task_command())
        move = approach(turn, role, cells, claimed)
        if move is not None:
            return (role.unit_id, move)
        return WAIT  # 走不过去（被挡）就先等着

    def _drive_accepted(self, turn):
        """等判题器下发任务原文，然后决定参考文件怎么拿。"""
        if not turn.phase_task.strip():
            if self._timed_out(turn):
                # 领了任务却没拿到描述：退回赶路阶段重新领一次
                self._enter("travel", turn.round_no)
            return WAIT
        self.task_text = turn.phase_task.strip()
        paths = extract_md_paths(self.task_text)
        # 优先本机读（最快，不占回合）；读不到才去沙盒 cat
        md_text = read_local_files(paths)
        if md_text:
            self.md_text = md_text
            self._enter("llm1", turn.round_no)
            return ("prompt", build_extract_prompt(self.task_text, self.md_text))
        if paths:
            self.command = cat_command(paths)
            self._enter("read_cmd", turn.round_no)
            return ("exec", self.command)
        # 任务压根没提 .md：直接进取数阶段
        self._enter("llm1", turn.round_no)
        return ("prompt", build_extract_prompt(self.task_text, ""))

    def _drive_read_cmd(self, turn):
        """等沙盒 ``cat`` 的结果。"""
        result = turn.last_cmd_result.strip()
        if result:
            if _looks_failed(result) and self.retries < 1:
                # 文件不在任务说的位置：改发"扫出沙盒里所有 .md"的兜底命令
                self.retries += 1
                self.phase = "discover_cmd"
                self.phase_round = turn.round_no
                return ("exec", DISCOVERY_COMMAND)
            self.md_text = result
            self._enter("llm1", turn.round_no)
            return ("prompt", build_extract_prompt(self.task_text, self.md_text))
        if self._timed_out(turn):
            return self._retry(turn, "read_cmd", lambda: ("exec", self.command))
        return WAIT

    def _drive_discover_cmd(self, turn):
        """等兜底扫描命令的结果；拿到后连同已有内容一起交给 LLM。"""
        result = turn.last_cmd_result.strip()
        if result:
            self.md_text = (
                f"{self.md_text}\n{result}".strip() if self.md_text else result
            )
            self._enter("llm1", turn.round_no)
            return ("prompt", build_extract_prompt(self.task_text, self.md_text))
        if self._timed_out(turn):
            return self._retry(
                turn, "discover_cmd", lambda: ("exec", DISCOVERY_COMMAND)
            )
        return WAIT

    def _drive_llm1(self, turn):
        """等第一阶段 LLM 回复（即"取数命令"），拿到就丢给沙盒。"""
        if turn.llm_resp.strip():
            self.command = strip_fence(turn.llm_resp)
            self._enter("cmd", turn.round_no)
            return ("exec", self.command)
        if self._timed_out(turn):
            return self._retry(
                turn,
                "llm1",
                lambda: ("prompt", build_extract_prompt(self.task_text, self.md_text)),
            )
        return WAIT

    def _drive_cmd(self, turn):
        """等沙盒执行结果，拿到就发第二阶段 prompt。"""
        if turn.last_cmd_result.strip():
            self._enter("llm2", turn.round_no)
            return (
                "prompt",
                build_format_prompt(
                    self.task_text, self.md_text, self.command, turn.last_cmd_result
                ),
            )
        if self._timed_out(turn):
            return self._retry(turn, "cmd", lambda: ("exec", self.command))
        return WAIT

    def _drive_llm2(self, turn):
        """等第二阶段 LLM 回复（结构化答案），拿到就提交。"""
        if turn.llm_resp.strip():
            answer = strip_fence(turn.llm_resp)
            self._enter("settle", turn.round_no)
            return (self.role_id, submit_answer_command(answer))
        if self._timed_out(turn):
            return self._retry(
                turn,
                "llm2",
                lambda: (
                    "prompt",
                    build_format_prompt(
                        self.task_text, self.md_text, self.command, turn.last_cmd_result
                    ),
                ),
            )
        return WAIT

    # -- 小工具 ------------------------------------------------------------
    def _permanently_gone(self, turn: Turn, index: int) -> bool:
        """该任务点是否已经不可能再领任务（地图上没有 / 任务做完了）。"""
        point = turn.task_point(index)
        if point is None:
            return not turn.task_point_cells(index)
        return not point.valid


class _Wait:
    """哨兵对象：表示"这个角色这回合什么都不做，但不要往下走别的步骤"。

    用独立对象而不是 ``None``，是因为 ``None`` 在我们的约定里表示
    "这个步骤已完成，上层继续求值下一个步骤"——两者语义完全不同。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "WAIT"


WAIT = _Wait()


def _adjacent_to(origin: Pos, cells: tuple[Pos, ...]) -> bool:
    """``origin`` 是否在给定的任一格周围一格内（切比雪夫距离 ≤1）。

    任务点 2 占两格，所以这里接收一组格子，挨着任意一格都算到位。
    """
    return any(distance(origin, cell) <= 1 for cell in cells)
