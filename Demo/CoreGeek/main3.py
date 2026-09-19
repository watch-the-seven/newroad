#!/usr/bin/env python3
"""参赛程序入口。

判题器按接口文档用 ``bash run.sh <port>`` 拉起程序，因此这里只做三件事：
1. 校验并解析端口参数；
2. 把工作目录切到本文件所在目录（这样相对路径、日志目录都稳定，
   不受判题器从哪个目录启动影响）；
3. 把 ``src/`` 加进模块搜索路径，然后交给 ``agent.server`` 起 HTTP 服务。
"""

import logging
import os
import sys
from pathlib import Path


def main() -> None:
    # 判题器只会传一个参数：端口号
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python main3.py <port>")
    port = int(sys.argv[1])

    root = Path(__file__).resolve().parent
    # chdir 到工程目录：日志(rounds.jsonl)按相对路径落在这里，
    # 任务用的 .md 兜底查找也会以这里为根。
    os.chdir(root)
    # 包源码在 src/ 下（src 布局），运行时手工加进 sys.path
    sys.path.insert(0, str(root / "src"))

    # 日志走 stdout：判题器会捕获它，比赛时这份输出往往是我们唯一能拿到的现场记录
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
    )

    # 放在 chdir/sys.path 之后再导入，确保能定位到 agent 包
    from agent.server import serve

    logging.info("listening on 0.0.0.0:%d", port)
    serve(port)


if __name__ == "__main__":
    main()
