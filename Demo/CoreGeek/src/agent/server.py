"""HTTP 入口：判题器 POST 一局状态，这里回一个指令包。

接口约定（接口文档"每局比赛将双方选手代码同时启动…"）：
* 监听 ``0.0.0.0:<port>``，端口由启动参数传入；
* 请求体是 JSON 状态，响应体必须是 ``roleCommandMap`` + ``prompt`` + ``executeCmd``；
* 单次响应 5 秒内必须返回，否则判超时（任务书第八章），所以任何异常都
  必须在本地消化掉，绝不能让某一回合没有响应。
"""

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .brain import decide
from .roundlog import RECORDER as ROUND_LOG

LOGGER = logging.getLogger(__name__)

#: ``decide()`` 意外抛异常时回给判题器的兜底响应。
#: 空指令包在格式上是合法的，只会浪费这一回合，不会计入"异常响应"次数
#: （异常只针对超时/格式错误/指令无法识别，任务书第八章）。
FALLBACK_RESPONSE: dict[str, Any] = {
    "roleCommandMap": {},
    "prompt": "",
    "executeCmd": "",
}


class Handler(BaseHTTPRequestHandler):
    # 用 HTTP/1.1 支持长连接，避免判题器每回合重新握手（握手超时是 10 秒）
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server 规定的命名
        # 按 Content-Length 精确读取请求体；判题器总是发完整 JSON
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        started = time.perf_counter()
        payload: Any = None
        try:
            payload = json.loads(raw.decode("utf-8"))
            response = decide(payload)
        except Exception:  # 任何异常都不能让服务停下来
            LOGGER.exception("decision failed")
            response = dict(FALLBACK_RESPONSE)
            # 兜底路径也要留下完整记录，否则日志里会缺回合、事后无法复盘。
            # 此时 payload 可能还没解析成功（JSON 坏了），就把原始报文也记下来。
            try:
                request = payload if isinstance(payload, dict) else {
                    "roundNo": None,
                    "raw": raw.decode("utf-8", "replace"),
                }
                ROUND_LOG.record(
                    request,
                    {"error": "decision failed", **response},
                    (time.perf_counter() - started) * 1000,
                )
            except Exception:  # 记录器自身出错也不能影响回包
                LOGGER.exception("round log failed")
        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        # 显式声明 charset，并给出 Content-Length，避免客户端等待连接关闭
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 关掉 http.server 自带的逐请求日志：每回合的完整记录由 roundlog 负责，
        # 这里再打一遍只会让 stdout 翻倍。
        return


def serve(port: int) -> None:
    """启动服务并阻塞。判题器会一直复用这个进程跑完整场比赛。"""
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
