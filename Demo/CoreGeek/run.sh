#!/usr/bin/env bash
#
# 判题器约定的启动入口：bash run.sh <port>
# 例：bash run.sh 8080
#
# 判题器会按接口文档用这个脚本拉起参赛程序，所以我们只做最薄的一层包装：
#   * 把工作目录切到脚本所在目录，保证相对路径（日志目录、任务用到的 .md）稳定，
#     不受判题器从哪个目录启动影响；
#   * 把端口原样透传给 main3.py；缺参数时直接报错退出，避免起一个监听随机端口的进程。
#
# ⚠️ 这里**故意只写 `set -eu`，不写 `pipefail`**：
#   接口文档只说"样例：bash run.sh port"，并没有规定判题器一定用 bash 调用。
#   而 `set -o pipefail` 不是 POSIX，dash（Debian/Ubuntu 的 /bin/sh）会报
#   "Illegal option -o pipefail" 并让脚本立刻退出——判题器连不上就直接 0 分。
#   本脚本里没有任何管道，pipefail 保护不了任何东西，去掉它就能同时兼容
#   bash / dash / sh 三种调用方式。
#
# `set -eu`：出错立即退出（而不是带着半死不活的状态继续跑）、未定义变量报错——
# 比赛时"静默失败"比直接崩更难查。只透传第一个参数：多传的参数一律忽略，
# 这样判题器万一多给参数也不会把我们带崩。
set -eu

cd "$(dirname "$0")"

# exec 让 python 进程替换掉当前 shell，信号（判题器的 kill）能直接送达程序本体
exec python3 main3.py "${1:?usage: bash run.sh <port>}"
