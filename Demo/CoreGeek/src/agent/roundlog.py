"""逐回合完整 request / response 记录器。

判题器每回合发一个请求、等一个响应；这里把两边**原文**都落盘，
方便赛后复盘：哪一回合下了什么指令、当时场上的完整状态是什么。

同时写两个地方（默认都开）：

* ``logs/rounds.jsonl``（包目录下），每回合一行 JSON::

      {"roundNo": 85, "elapsedMs": 1.2, "request": {...}, "response": {...}}

  以追加方式打开、每回合写完就关（等于每回合 flush），进程被杀也不会丢历史；
  该路径不可写时自动退到系统临时目录。

* ``agent.roundlog`` 这个 logger（输出到 stdout），每回合两行::

      ROUND 85 REQUEST {...}
      ROUND 85 RESPONSE {...}

  比赛时 stdout 会被判题器捕获，往往是我们唯一能拿到的现场记录。

环境变量开关::

    AGENT_ROUND_LOG=<path>        日志文件路径；设为 "-" 关闭文件日志
    AGENT_ROUND_LOG_STDOUT=0      关闭 stdout 那份完整记录（文件仍写）
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

ENV_PATH = "AGENT_ROUND_LOG"
ENV_ECHO = "AGENT_ROUND_LOG_STDOUT"

#: 日志文件上限。一场比赛约 1300 回合、每回合几 KB，正常远低于此；
#: 上限只是防止异常情况下把沙盒磁盘写满。
_MAX_BYTES = 64 * 1024 * 1024
#: 环境变量里被认为是"关闭"的取值
_DISABLED_VALUES = {"", "-", "0", "off", "false", "no", "none", "null"}


def default_path() -> str:
    """默认日志路径：``<包所在工程根>/logs/rounds.jsonl``。

    ``roundlog.py`` 位于 ``<root>/src/agent/`` 下，所以 ``parents[2]`` 就是
    工程根（``Demo/CoreGeek``）。取不到时退回当前工作目录。
    """
    here = Path(__file__).resolve()
    root = here.parents[2] if len(here.parents) > 2 else Path.cwd()
    return str(root / "logs" / "rounds.jsonl")


class RoundRecorder:
    """线程安全的回合记录器。

    判题器用多线程 HTTP 服务发请求，所以所有状态读写都在锁里；
    用 ``RLock`` 是因为 ``_emit()`` 持锁时会再调 ``_write()``（可重入）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._configured = False        # 是否已从环境变量初始化过
        self.path: str | None = None    # None 表示不写文件
        self.echo = True                # 是否同时写 stdout
        self._bytes = 0                 # 已写入字节数，用于 _MAX_BYTES 判断
        self._file_disabled = False     # 文件日志已关闭（写失败或超限）
        self._created_dir = False       # 父目录已尝试创建过，避免每回合 mkdir
        self._fallback_done = False     # 是否已经退到临时目录（只退一次）

    # -- 配置 --------------------------------------------------------------
    def configure(self, path: str | None | object = ..., echo: bool | None = None) -> None:
        """显式配置（主要给测试用）。

        * ``configure()``            重新按环境变量初始化；
        * ``configure(path=None)``   关闭文件日志；
        * ``configure(path="...")``  指定日志路径。

        ``...``（Ellipsis）作为默认值是"没传这个参数"的哨兵，与显式传 ``None``
        （关闭文件）区分开。
        """
        with self._lock:
            if path is ... and echo is None:
                # 无参调用 → 回到"由环境变量决定"的状态
                self._configured = False
                self._configure()
                return
            if path is not ...:
                self.path = None if path is None else str(path)
                # 换路径时把"失败过/超限/建过目录"的状态一并复位
                self._file_disabled = False
                self._created_dir = False
                self._fallback_done = False
                self._bytes = 0
            if echo is not None:
                self.echo = bool(echo)
            self._configured = True

    def _configure(self) -> None:
        """懒初始化：真正要写第一条日志时才读环境变量。

        这样测试可以在 import 之后再设置/覆盖开关，也避免在导入阶段就动文件系统。
        """
        if self._configured:
            return
        self._configured = True
        raw = os.environ.get(ENV_PATH)
        candidate = default_path() if raw is None else raw.strip()
        self.path = None if candidate.lower() in _DISABLED_VALUES else candidate
        self.echo = os.environ.get(ENV_ECHO, "1").strip().lower() not in _DISABLED_VALUES
        if self.path:
            LOGGER.info("full round log -> %s", self.path)

    # -- 对外接口 ----------------------------------------------------------
    def record(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        elapsed_ms: float = 0.0,
    ) -> None:
        """记录一回合。``request`` 是判题器原文，``response`` 是我们回的原文。

        ``elapsed_ms`` 是本次决策耗时，判题器要求 5 秒内响应，这个字段方便
        事后排查"哪一回合算得慢"。
        """
        self._configure()
        entry = {
            "roundNo": request.get("roundNo"),
            "elapsedMs": round(elapsed_ms, 2),
            "request": request,
            "response": response,
        }
        self._emit(entry, pretty=True)

    def marker(self, event: str, **extra: Any) -> None:
        """写入一条事件标记（不是回合记录），例如新一局开始。"""
        self._configure()
        self._emit({"event": event, "ts": round(time.time(), 3), **extra}, pretty=False)

    # -- 内部实现 ----------------------------------------------------------
    def _emit(self, entry: dict[str, Any], *, pretty: bool) -> None:
        """把一条记录同时送到 stdout 和文件。

        ``pretty=True`` 时 stdout 拆成 REQUEST / RESPONSE 两行，方便肉眼和 grep；
        文件里始终是一行一个完整 JSON 对象，方便程序解析。
        """
        with self._lock:
            if self.echo:
                if pretty:
                    LOGGER.info("ROUND %s REQUEST %s", entry.get("roundNo"),
                                _dumps(entry.get("request")))
                    LOGGER.info("ROUND %s RESPONSE %s", entry.get("roundNo"),
                                _dumps(entry.get("response")))
                else:
                    LOGGER.info("ROUND EVENT %s", _dumps(entry))
            self._write(_dumps(entry))

    def _write(self, line: str) -> None:
        """按上限判断后落盘；超限只关文件日志，stdout 继续。"""
        with self._lock:
            if self.path is None or self._file_disabled:
                return
            payload = f"{line}\n"
            size = len(payload.encode("utf-8"))
            if self._bytes + size > _MAX_BYTES:
                self._file_disabled = True
                LOGGER.warning(
                    "round log exceeded %d bytes, file logging stopped (stdout continues)",
                    _MAX_BYTES,
                )
                return
            if self._append(payload):
                self._bytes += size

    def _append(self, payload: str) -> bool:
        """真正写文件。第一次失败就退到临时目录重试一次，再失败就永久关闭文件日志。

        返回值表示是否写入成功（用于累加 ``_bytes``）。
        """
        assert self.path is not None
        for attempt in range(2):
            try:
                if not self._created_dir:
                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                    self._created_dir = True
                # 追加 + 立刻关闭：崩溃时最多丢当回合，不会丢整场
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(payload)
                return True
            except OSError as error:
                if attempt == 0 and not self._fallback_done:
                    # 只退一次：默认路径不可写时改用系统临时目录
                    self._fallback_done = True
                    self.path = str(
                        Path(tempfile.gettempdir()) / f"rounds-{os.getpid()}.jsonl"
                    )
                    self._created_dir = False
                    LOGGER.warning(
                        "cannot write round log (%s); falling back to %s", error, self.path
                    )
                    continue
                self._file_disabled = True
                LOGGER.warning(
                    "cannot write round log to %s (%s); stdout logging continues",
                    self.path,
                    error,
                )
                return False
        return False


def _dumps(value: Any) -> str:
    """紧凑 JSON：不转义中文（便于直接阅读），去掉多余空格（省体积）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


#: 全局单例。brain/server 直接 ``from .roundlog import RECORDER`` 使用。
RECORDER = RoundRecorder()
