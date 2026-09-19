"""《未来战争》参赛 agent 包。

模块划分：

* ``protocol``  —— 判题器接口的数据模型与指令构造（纯翻译，无决策）
* ``tactics``   —— 与基地坐标绑定的布局常量 + 逐日战术计划表
* ``brain``     —— 每回合决策：把计划表翻译成三单位的具体动作
* ``grid``      —— 八方向 A* 寻路
* ``taskflow``  —— 开拓者的自进化任务流水线（LLM + 沙盒）
* ``roundlog``  —— 逐回合完整 request/response 记录
* ``server``    —— HTTP 服务入口
"""
