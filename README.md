# Yuki Semantic Participation

稀疏语义观测驱动的参与控制器，依据 Yuki 语义参与动力系统任务书 V6 开发。

**状态：独立库已接入 Yuki 宿主，2026-09-23 的生产只读快照显示两个群的有效 owner 为 `semantic`；当前线上接话效果尚无充分样本。**
独立人工中文标注、真实同模型影子对照及真实 QQ 端到端验收仍未完成。
数值性质、开发者合成样本和接口实测分别报告，不将它们当作群聊准确率或效果证明。

本库接收宿主已授权的有限事件，调用 Jev 获取语义观测，维护参与状态、支持范围与候选，
仅输出参与机会。宿主负责唯一控制器选择、权限、正式自主来源、Main Agent、任务及发送。
本库没有 QQ 凭据，不创建自动化，不控制 steer，也不直接读取 Yuki 数据库。

当前 Yuki 接入使用正式 SELF 来源，接纳后沿既有 WorkScheduler、主 Agent、固定工具合同和发送链执行；
静默工具回合也可通过独立回执进入 SELF 自省。此接入代码位于 Yuki 仓库，并未成为本库的运行依赖。

## 原则

- 新语义观测、未知、缺失、短期预测、明确结束分别表示。
- 稀疏调用不等于无语义运行；有来源机会要求有效语义支持，无来源 SELF 机会明确标记。
- 确认自身输出不能独自构成互惠；时间衰减回到旁观，不制造结束证据。
- 来源修订、generation 和对象边界决定支持有效性；重复观测不重复计数。
- 没有日配额；固定工具合同及原 Agent 执行机制不由此系统更改。
- 关闭 semantic 或持续故障时由宿主唯一 selector 恢复 legacy；总开关关闭时仍为 off。
- 合法 unknown 不等于 Provider 故障；回退恢复须有真实成功观测，队列过期不代表恢复。
- 语义观测确认直接邀请后立即提出机会；自主加入、回忆、联系与无来源参与由[连续机会值](docs/autonomous-evolution.md)决定。直呼名字只提高观察顺序，不是 @ 或直接触发。

## 开发

```sh
python -m pip install -e '.[dev]'
pytest -q
python -m scripts.replay_autonomous_evolution --fixture fixtures/group-chat-workload-v2-full.json.gz --output docs/evidence/autonomous-evolution-v1-replay.json
```

Jev 使用固定 `jev-1.13.0`、版本化 `v6-zh-4` rubric 与原生 `POST /v1/systemone`，密钥由调用者注入。
遵循 [TypeSafe API](https://docs.typesafe.ai/api)；不将密钥、真实对话或运行数据库提交仓库。
测试使用明确标记的合成材料，不会自动调用付费 API 或发出 QQ 消息。

不调用模型的工程对照可显式运行：

```sh
python scripts/replay_shadow.py --yuki-repo /path/to/Yuki-QQbot --output /tmp/shadow.json
```

该脚本以开发者预设语义驱动 legacy、静态门控和动态控制器，使用记录型 NO_REPLY 执行器，
不是生产影子测试，也没有比较真实主模型输出。

实施范围、真实 Jev 合成请求和剩余验收见 [进度和边界](docs/implementation.md)，
接纳、撤回、恢复和回执约定见 [宿主协议](docs/protocol.md)。
以 Yuki 服务器只读元数据校准的测试集、回放结果及群聊验收方案见 [群聊负载实验](docs/group-chat-experiment.md)。
