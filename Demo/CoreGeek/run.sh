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
# set -euo pipefail：出错立即退出（而不是带着半死不活的状态继续跑），
# 未定义变量报错，管道中任一环失败也算失败——比赛时"静默失败"比直接崩更难查。
set -euo pipefail

cd "$(dirname "$0")"

# exec 让 python 进程替换掉当前 shell，信号（判题器的 kill）能直接送达程序本体
exec python3 main3.py "${1:?usage: bash run.sh <port>}"
