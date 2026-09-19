# CoreGeek —— 《未来战争》v1.0 参赛 agent

按基地坐标二选一的逐日战术脚本实现的 HTTP 机器人。判题器 POST 一局状态，本程序回一个
JSON 指令包（`roleCommandMap` + `prompt` + `executeCmd`）。

## 运行

```bash
bash run.sh <port>          # 判题器约定的入口
python3 main3.py <port>     # 等价的直接入口
```

Python ≥ 3.11，无第三方依赖。

### 判题器启动约定（文档只给了这些）

`docs/接口文档.md` 里关于启动只有三行：必须以 httpserver 方式监听端口、端口由系统启动时传入、
以及一句 **"样例：bash run.sh port"**（`ip 地址均为 0.0.0.0`）。`docs/任务书.md` **完全没有**
启动相关的规定。

也就是说，文档**没有**承诺：只会传一个参数、从哪个目录启动、用 `bash` 还是 `sh`、怎么终止进程。
所以这几处都按"最保守"处理，改动前请先看理由：

| 位置 | 处理 | 为什么 |
| --- | --- | --- |
| `run.sh` | 只用 `set -eu`，**故意不用 `pipefail`** | `pipefail` 不是 POSIX：dash（Debian/Ubuntu 的 `/bin/sh`）会报 `Illegal option -o pipefail` 并让脚本立刻退出，判题器连不上 = 0 分。实测：修复前 `dash run.sh` 起不来，修复后 bash/dash/sh 都能跑；本脚本没有任何管道，`pipefail` 保护不了任何东西 |
| `run.sh` / `main3.py` | 多余的参数一律忽略 | 任务书第八章写明"参赛代码异常退出后不再被拉起"，为参数个数较真等于直接 0 分 |
| `main3.py` | `os.chdir(工程目录)` | 任务 `.md` 兜底查找的**第一个候选根就是 `Path.cwd()`**（`taskflow._search_roots`），判题器从哪启动我们控制不了；统一 CWD 也能让所有相对路径行为可预期。（回合日志默认走包内固定路径 `src/agent/../../logs/`，本来就是 CWD 无关的） |
| `main3.py` | 日志走 stdout、`level=INFO` | 判题器捕获的 stdout 往往是我们唯一能拿到的现场记录；调高 level 会让回合日志的 stdout 那份整体消失 |

## 代码结构

| 文件 | 作用 |
| --- | --- |
| `src/agent/protocol.py` | 请求/响应的数据模型：`Pos/Unit/Robot/Turn`、指令构造器、地图查询 |
| `src/agent/tactics.py` | 两套基地布局（`x<20` 左、`x>20` 右）+ 逐日计划表 + 夜间修补阈值 |
| `src/agent/brain.py` | 每回合决策：把计划表翻译成"这一步谁做什么" |
| `src/agent/grid.py` | 八方向 A* 寻路 |
| `src/agent/taskflow.py` | 开拓者自进化任务状态机与 prompt 构造 |
| `src/agent/roundlog.py` | 逐回合完整 request/response 记录（JSONL + stdout） |
| `src/agent/server.py` | HTTP 服务与异常兜底 |
| `main3.py` / `run.sh` | 判题器入口（`bash run.sh <port>`） |
| `tests/mock_judge.py` | 离线 mock 判题器：跑满 1300 回合并校验指令合法性 |
| `tests/test_edge.py` | 边界与任务链路单测 |
| `tests/trace.py` | 逐回合调试打印工具 |

> 全部源码均为中文注释，关键规则处标注了出处（《任务书》第 x.y 节 /《接口文档》第 x.y 节）。

## 决策模型

`brain.decide()` 每回合做三件事：

1. `MatchState.sync()`：记住基地坐标 → 选定左/右脚本，记住工人 A/B 的 ID。
2. 按 `(天数, 昼夜, 角色)` 取出一条**有序步骤表**（`tactics.py`）。
3. 从头遍历步骤表，第一个产生动作的步骤胜出；每个角色每回合至多一条指令。

因为每回合都从表头重新求值，所以"钱不够就少买""先修补再升级"这类条件会自动在下
一回合重试，不需要维护长计划。

## 自进化任务流程

`taskflow.py` 的状态机严格按下面顺序推进（每步占一回合）：

```
走到任务点 → acceptTask → [读任务描述里提到的 .md → 拼 prompt] → prompt
           → executeCmd(上一回合 LLM 的回复) → prompt(附执行结果)
           → submitAnswer(LLM 整理后的答案) → 下一个任务点
```

- `.md` 路径用正则从 `phaseTask` 里提取。找不到时的兜底顺序：
  1. 按原路径在本机读（进程 CWD、包目录、`src`、工程根目录）；
  2. 在本机按**文件名递归查找**（任务里的相对路径可能不是相对我们进程的）；
  3. 以上都失败 → 用 `executeCmd` 在沙盒里 `cat` 回来；
  4. `cat` 的退出码非 0（路径不对）→ 自动补一条 `find . -maxdepth 4 -name '*.md' … cat`
     把沙盒里的 .md 全扫回来。
- 每个阶段有 4 回合超时保护，最多重发 3 次；LLM 或沙盒一直不回话就放弃该任务点去做下一个，
  不会把开拓者卡死。任务点在冷却中（`coldDownRounds > 0`）时到旁边等，不发 `acceptTask`。
- 只有第 1~3 天白天做任务，每天任务点 1、任务点 2 各一次。

## 与口述脚本的有意偏差

1. **夜里不建造围墙，但可以用修补包修墙**。任务书 4.4 规定 `build` 仅工人在白天
   可用，所以夜里不会有任何 `build` 动作：第 1 天白天没建完的围栏要等第 2 天白天才
   补——左侧脚本第 68 回合（第 1 天白天内）建完 16 面；右侧脚本第 1 天只建了 10 面，
   第 156 回合（第 2 天白天）补齐；夜里被打出的洞口同样等第二天白天重建。
   按口述要求"白天没修完就没修完吧"，不做夜间兜底。
   而**围墙修补包走的是 `use`，夜里照常使用**——第 4 天起工人夜间的
   `night_repair` 步骤就是干这个的。
2. **购买顺序**：`挖矿卖矿` 是一个"永远有事做"的步骤，所以按口述顺序把它放在买券/买包
   之后，否则后面的购买步骤永远轮不到。效果等价（每回合都会回头检查购买条件）。
3. **围墙升级券按当前等级选**：`level1→2` 用 `WallUpgradeVoucher1`，`level2→3` 用
   `WallUpgradeVoucher2`，买多少由"还差几面墙"和当前金币决定，钱不够就少买。
4. **基地升级券是持续目标**：第 1~3 天只要没持有、基地还是 1 级、钱够 100 就去买一张；
   夜里（第 1~3 天）也会补买，第 4 天起不再为此跑动。基地血量 <500 时夜里用掉它
   （基地升级券不受"夜里不能修围墙"限制）。
5. **围墙修补包按"库存目标"买**：第 4 天买到 5 个、第 5 天 6 个、第 6~10 天各 10 个。
   夜里由 `night_repair` 把掉血到阈值以下的墙补满（阈值 400→900 按天递增），
   白天用它修补"已经 3 级但掉血"的墙（`_step_upgrade` 里
   `wall.level == 3 and health < 2000` 的分支），用掉之后第二天再补货。
6. `x == 20` 这种边界情况归入右侧脚本。

## 日志

每回合的**完整 request 与 response 原样记录**，两个去处：

1. **文件** `logs/rounds.jsonl`（每回合一行 JSON，追加写、每轮 flush，进程挂了也不丢）：

   ```json
   {"roundNo":85,"elapsedMs":3.42,"request":{...判题器原文...},"response":{...我们回的原文...}}
   ```

   路径不可写时自动退到系统临时目录；超过 64MB 停止写文件（stdout 继续）。
2. **stdout**（判题器捕获的那份），每回合两行：

   ```
   ROUND 85 REQUEST {"roundNo":85,...}
   ROUND 85 RESPONSE {"roleCommandMap":{...},"prompt":"","executeCmd":""}
   ```

   另外保留原来那行人类可读摘要 `r85 d1 NIGHT gold=20 [10010:move ...]`。

开关（环境变量）：

```bash
AGENT_ROUND_LOG=/path/to/rounds.jsonl   # 换日志路径；AGENT_ROUND_LOG=- 关掉文件日志
AGENT_ROUND_LOG_STDOUT=0                # 关掉 stdout 的完整记录（文件仍写）
```

`decide()` 抛异常时 `server.py` 也会把该回合的请求和兜底响应记进去，不会缺回合；
新一轮比赛开始（回合号回退）会写一条 `{"event":"match-reset"}` 分隔。

## 本地验证

```bash
python3 tests/mock_judge.py     # 离线 mock 判题器，跑完左右两套脚本各 1300 回合
python3 tests/trace.py 10 24 1 75   # 逐回合打印（基地x 基地y 起始回合 结束回合）
```

`mock_judge.py` 校验：指令格式零错误、每个角色每回合至多一条、三座火箭位置正确、
16 面墙建成并保持、10 面关键墙升到 3 级、6 次任务走完整条 LLM/沙盒链路、买包数量符合日程。
