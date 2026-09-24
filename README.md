# Yuki Semantic Participation

Yuki 的群聊语义观察与自主参与控制器。它接收 Host 已授权的有限事件，调用 Jev 判断
邀请、续聊、讨论边界和开口机会，维护参与状态，并输出可供 Host 接纳的 proposal。
本库不持有 QQ 凭据，不发送消息，也不运行主 Agent。

## 当前决策流程

1. Host 提供带内部 ID、版本和讨论范围的真人事件或获准记忆。名字提及会优先进入
   Jev 观察；直呼 Yuki 不等同于 `@`，也不直接触发发言。
2. Jev 确认的明确邀请或直接续答走请求路径。`open_group`、回忆、联系和无来源
   `intrinsic` 机会使用[连续概率率](docs/autonomous-evolution.md)采样。近期真实自主 Work
   与公开表达后的接收反馈会调整后续自主调用频率；单次 Work 的消息条数不受此模型限制。
3. Host 核验来源、停止边界、群授权、唯一 owner、generation 和 Work 占用，接纳后交给
   原主 Agent。主 Agent 可以行动、显式发送或 `NO_REPLY`；只有真实回执更新控制器。

长时间没有本群真人输入时，群交流背景连续衰减，无来源机会率趋向很低但非零。
模型可偶发提出长时间沉寂群的机会，不保证发言，也不设置固定静默时长。
状态方程、参数和 Host 边界见
[模型说明](docs/autonomous-evolution.md)与[宿主协议](docs/protocol.md)。

## 仓库边界

- 本库：Jev 观测、O/H/Y/E/C 参与状态、注意与候选、连续概率采样、快照与 proposal。
- Yuki Host：canonical 身份与权限、唯一接纳、持久 Work、主 Agent、工具和发送回执。
- Host 的 `legacy` proposer 是故障恢复时的另一条现行路径；本库旧的 `rate()`、`rates()`、
  hazard 和固定 `intrinsic` 时钟已删除。旧快照字段只在恢复迁移时读取。

## 开发与回放

需要 Python 3.12 或更新版本：

```sh
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests scripts
ruff format --check src tests scripts
mypy src
python -m scripts.replay_autonomous_evolution --fixture fixtures/group-chat-workload-v2-full.json.gz --output /tmp/autonomous-replay.json
python -m scripts.replay_autonomous_evolution --fixture fixtures/group-chat-workload-v2-full.json.gz --simulated-unanswered-send --output /tmp/autonomous-unanswered.json
```

旧版冻结回放使用两条各 30 天的**合成**群轨迹，共 15,952 条真人形状消息。
旧版调参后记录 3,568 次 proposal，其中 1,007 次为无来源机会；距最近真人消息最远约
5 小时 52 分钟，41 段超过 6 小时的静默窗口中为 0。这些数值**不代表当前概率模型**。
所有 proposal 均由记录器返回 `NO_REPLY`，实际 QQ 发送为 0。
[原参数报告](docs/evidence/autonomous-evolution-v1-replay.json)
和[旧版调参报告](docs/evidence/autonomous-evolution-v2-replay.json)保留输入与源码哈希。
消息时间与规模仅按服务器私有只读聚合校准；正文和语义标签都是合成的，不能据此声称
真实群聊的发言率或社交质量。

当前概率模型在同一合成资料上的两种结果见[只启动 Work 的回放](docs/evidence/autonomous-social-feedback-no-reply.json)
与[公开发出后无人回应的回放](docs/evidence/autonomous-social-feedback-unanswered-send.json)。
两种执行器都会为每个接纳的 Work 记录一次模拟模型请求；第二种额外记录一次模拟公开发送，
并不限制该 Work 的真实消息数。报告中的调用数与发送数均为 0，因为没有访问主 Agent 或 QQ。
在两条各 30 天轨迹中，只启动 Work 的执行器得到 1,578 次 proposal、34 次无来源机会，
其中 10 次距最近真人消息超过 6 小时，最远约 34 小时；每次模拟发出且无人回应的执行器
得到 1,548 次 proposal、9 次无来源机会，其中 3 次超过 6 小时，最远约 33 小时。
这体现模型对未获回应的后续自主唤醒降频；两个数值都不是实际主 Agent 调用量或群发言量。

Jev 固定为 `jev-1.13.0`，rubric 为 `v6-zh-4`；密钥由调用者注入。
测试不会自动调用付费 API 或 QQ 发送。工程状态与尚缺的真实验收见
[实施状态](docs/implementation.md)。历史负载实验及旧参数报告仅供回溯，
不作为现行模型的效果预测。
