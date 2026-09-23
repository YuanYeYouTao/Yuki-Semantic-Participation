# Yuki V6 群聊负载实验（2026-09-23）

> 本文所有回放数字和 90 秒支持窗只对应当时的旧代码。后续已取消随机累积门槛并更改观察队列及支持时效；不能将这些数字当成新版的效果预测。现行设计见 [实施状态](implementation.md)。

## 目的与证据层级

检验 V6 在群聊连续接话中的行为：真人不再 @、多人交错、密集短消息、稀疏观察、明确结束、长期沉默时，控制器何时提出机会，宿主何时接纳，主 Agent 是否实际发送。

本轮已经完成服务器**元数据汇总**、短场景与近全量合成负载、控制器离线回放和隔离 Jev 合成探针。它没有重放真实正文，没有调用主 Agent，没有让 QQ 发消息，也没有证明线上社交效果。

| 层级 | 输入与运行 | 能回答 | 当前状态 |
| --- | --- | --- | --- |
| 0：负载校准 | 生产 `chat_events` 的只读聚合，仅留比例和分位数 | 消息间隔、成员集中度、@、引用、媒体形状 | 已做，明细留在 `private-data/` |
| 1：控制器 | 合成中文、开发者单点语义标签、真实 `ObservationSession`/`Controller`、虚拟时钟、记录型 NO_REPLY | 稀疏队列、支持有效期、提议和边界竞争 | 已做短场景、多种子、近全量逐日回放与静默唤醒探针 |
| 2：语义观测 | 从同一合成场景取固定小集，生产容器中的真实 `jev-1.13.0` 隔离评分 | 固定场景上的模型分类、请求成本、时延 | 已做 12 次；不等于真实中文准确率 |
| 3：宿主影子 | 隔离副本输入同一冻结事件流、真实 Jev 与主 Agent；发送端替换为记录器 | 实际接纳、工具/NO_REPLY/发送意图和恢复 | 尚未做 |
| 4：小群验收 | 明确批准的真实群试验、运行中可撤回 | 实际 QQ 入站与发送效果 | 尚未做 |

## 服务器校准来源

在 Yuki 服务器上对 `/opt/yuki-qqbot/data/qq_ai_bot.db` 用 SQLite `mode=ro`、`query_only=ON` 汇总最近 30 天的群 `message` / `keeper` 事件。脚本只在服务器内处理来源 ID、正文长度和段类型；输出没有群号、成员 ID、正文、原始段、单条时间戳或数据库文件。少于 100 条真人消息的群不进入校准。两个满足门槛的群分别是高频与低频档；第三个样本不足，不能用来估计分布。此处只发布舍入后的汇总，以便审查生成参数。

| 30 天档位 | 真人消息 | 活跃成员 | 相邻真人消息 ≤60 秒 | 间隔中位数 / p90 | 前三人发言占比 | @ Yuki 段 | 媒体段 | 平台引用 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 高频 | 约 1.45 万 | 13 | 87.5% | 10 / 85 秒 | 71.8% | 22.1% | 6.1% | 15.7% |
| 低频 | 约 0.15 万 | 9 | 77.1% | 12 / 700 秒 | 75.6% | 7.1% | 14.9% | 6.2% |

这两个档位的活跃日消息中位数约为 434 和 29。`@ Yuki` 是结构化 @ 段，平台引用是原始引用标记；二者**不等于**“希望 Yuki 接话”的语义标签。历史 `reply_to_event_id` 在迁移前覆盖不足，不能拿 30 天内部引用率代替平台引用率。最近 1 天的高频群有 361 条真人消息，其中 77.2% 的相邻间隔不超过 60 秒，说明单一 30 天分布也会随日期变化。

公开群聊研究只作形状交叉检查。2016 年 WhatsApp 样本中 59.6% 的相邻消息不超过 1 分钟，且成员贡献不均衡；它的平台、年代和群结构与 Yuki 不同，因此**没有**拿它的比例替代服务器实测。[研究原文](https://dl.ifip.org/db/conf/networking/networking2016iop/IoP-9.pdf)

## 测试集构造

`fixtures/group-chat-workload-v1.json` 全部是重新编写的中文消息和匿名 `P1…Pn` 角色。高频轨迹取 120 条、约 4.2 小时；低频轨迹取 60 条、约 20.9 小时。消息间隔用服务器分位数的分段逆函数生成，并把分位点打散；成员、@、媒体和引用按聚合份额分配。生成后的 ≤60 秒比例为 85.7% / 76.3%，前三人占比为 72.5% / 76.7%。这两个短轨迹模拟群聊片段，**不冒充**完整 24 小时活跃日。

内容语义比例没有从生产正文估计。`choose_act` 中的“邀请、续问、别人之间聊天”等权重是可审查的压力假设；每条 `act/information/floor/boundary` 标签均由生成规则和手工场景给出，不是人工盲标，更不是 Jev 观测。时间间隔只匹配边际分布，未拟合真实的连续活跃/沉默转移或昼夜节律；另设密集突发场景专门压测队列。未来可在不泄露原始时间序列的前提下，汇总间隔状态转移矩阵和时段分层，再扩展全天轨迹。

另有 4 个聚焦场景，共 23 条真人消息：

| 场景 | 关键检查 |
| --- | --- |
| `no_at_continuation` | 直接 @ 后 Yuki 有真实发言锚点；后续不 @ 的引用、无引用追问，以及 90 秒后新邀请 |
| `closure_and_reopen` | 单纯谢谢、明确“已解决”、别人开新话题和同一人重新邀请 |
| `burst_and_ambiguous_floor` | 22 秒内 8 条多人插话；部分消息明确留给别人 |
| `stop_and_silence` | 明确要求 Yuki 停止，随后其他话题和长期沉默 |

## 已运行的结果

控制器单种子回放：203 条合成真人消息，141 次记录型语义评分，57 条待评焦点过期，22 次 proposal；16 次 proposal 的支持来源没有 @ Yuki。所有 proposal 都由 NO_REPLY 记录器结束，实际发送数为 0。[逐场景轨迹](evidence/group-chat-workload-v1-replay.json)

固定同一消息流与语义标签，仅改变控制器随机种子跑 20 次：proposal 为 **21–30 次**，中位数 25.5；含无 @ 来源的 proposal 为 **14–22 次**，中位数 18.5。[多种子报告](evidence/group-chat-workload-v1-multiseed.json) 这些是“机会提出次数”，不能叫作 Yuki 发言数或接话成功率。

**免 @ 的时间窗：**控制器对已观测的群聊候选将支持有效期设为该消息后 **90 秒**；仅凭显式引用关系推测的续接最多沿原候选保留到原消息后 **45 秒**。新的、被 Jev 判定适合 Yuki 接话的真人消息可以建立新支持，所以不存在进入语境后统一倒计时结束的“会话有效期”。实际是否接话还要经过控制器提议、宿主接纳和主 Agent 决策；本轮没有测得线上可持续接话时长。

`no_at_continuation` 中，`a2`（不 @、引用 Yuki 发言）在单种子回放第 1062 虚拟秒提出机会，距该真人消息 42 秒；后面的无 @ 新邀请 `a4` 也提出机会。密集突发场景只完成 3 次评分，5 条焦点到期，说明“每条消息都被 Jev 看过”不成立。

**发现两个待修的边界窗口：**

1. `stop_and_silence` 的停止消息 `f2` 于 4020 秒进入；旧邀请 `f1` 于 4050 秒产生 proposal，而 `f2` 到 4060 秒才被评分。用户已经要求停止，但新提议仍可能先经过控制器。宿主后续接纳是否另行阻断，本轮没有验证。
2. `closure_and_reopen` 的“已经解决”消息 `c3` 在待评队列中过期，未进入语义观测；旧邀请 `c1` 在其到达后提出 proposal。长期结束边界可能因此缺失。

20 次种子回放中，每次至少有 1 次 proposal 落在一个更新的、尚未评分的显式结束/停止消息之后；中位数 2 次。两条测试以严格 `xfail` 留作回归哨兵，后续修复后应变为通过。[聚焦测试](../tests/test_group_chat_experiment.py)

真实 Jev 在生产容器里对 12 条**合成**边界消息完成 12 次隔离请求，输入 15,677 token、输出 2,880 token；中位单次时延约 0.31 秒，最慢约 5.8 秒。交际类别与开发者标签一致 9/12，回应机会归属一致 6/12。后者包含“谢谢”“已解决”“停止”等标签本身可能有争议的例子，不能写成准确率；需要独立中文标注和分歧复核。[Jev 结果](evidence/group-chat-jev-synthetic-2026-09-23.json)

生产只读快照显示当前两个有足够历史样本的群 owner 为 `semantic`，但查询时尚无 `semantic` 接纳 run；当前绑定启用后的真人消息样本仍很少，尚不足以判断线上 V6 会不会自主接话。30 天内的旧 `legacy` run 也不能算作 V6 的效果。

## 近全量 30 天负载与静默唤醒（第二版）

第一版 203 条消息只能压测局部边界，规模确实太小。第二版以再次读取的 Yuki 服务器只读聚合为冻结基准，构造两条完整 30 天长度的合成轨迹：高频群 **14,488 条真人 + 8,300 条 Yuki 形状消息**，低频群 **1,464 条真人 + 353 条 Yuki 形状消息**，合计 **24,605 条事件**。[压缩测试集](../fixtures/group-chat-workload-v2-full.json.gz) 与[校准报告](evidence/group-chat-workload-v2-calibration.json)可复核。Yuki 形状消息是外生环境上下文，逐条标为 `exogenous_context`；绝不计作本次回放中 Yuki 自主发出的消息。

生成器只读取仓库忽略的 `private-data/` 聚合。真人和 Yuki 消息总数、活跃天数、24 小时总量、成员份额、@、媒体与平台引用配额来自服务器；每天的量先扰动再打乱，秒级时间、角色编号、话题、全部正文和语义标签重新合成。文本长度按服务器分位数生成，高频群中位数 / p90 为 20 / 71 字，低频群为 6 / 27 字；两群各有数千与数百种不同文本。用户身份、群号、原消息时间戳、原正文都没有进入测试集。

| 指标 | 高频：服务器 → 合成 | 低频：服务器 → 合成 |
| --- | ---: | ---: |
| 真人消息数 | 14,488 → 14,488 | 1,464 → 1,464 |
| 相邻真人消息中位数 | 10.42 → 10.30 秒 | 12.41 → 13.14 秒 |
| 相邻真人消息 p90 | 84.68 → 80.22 秒 | 699.65 → 691.67 秒 |
| ≤60 秒比例 | 87.46% → 87.75% | 77.10% → 77.10% |
| >30 分钟比例 | 1.60% → 1.79% | 7.31% → 6.97% |

合成轨迹分别包含 107 / 65 个超过 1 小时的真人消息空档，最长约 34 / 75 小时。小时和消息长度边际分布较接近，但合成的话题关系、邀请频率、Yuki 历史消息归因仍是实验假设；不能把这种规模上的贴合说成真实语义贴合。

[近全量逐日回放](evidence/group-chat-workload-v2-replay.json)覆盖 48 个有事件的合成群日、全部 **15,952 条真人消息和 8,653 条外生 Yuki 形状消息**。在虚拟 2 秒 tick、开发者标签及记录型 NO_REPLY 下，产生 10,636 次模拟语义评分、5,277 条焦点到期、1,274 次控制器 proposal，其中 675 次含无 @ 来源。1,202 次 proposal 的前 2 秒没有任何新事件，432 次距上条真人消息至少 30 秒；距上条真人消息至少 90 秒的 proposal 为 0。另有 26 次 proposal 落在尚未评分的合成结束/停止消息之后，进一步暴露边界竞态。**所有实际发送均为 0**：这些数字不是线上接话频率，也不是 Jev、宿主或主 Agent 的预测输出。回放在合成午夜重置控制器，跨日状态连续性未验证。

**无新消息触发的唤醒需要分层记录。**当前 Yuki 宿主的 `SemanticParticipationService._loop` 每约 2 秒调用 `tick`，对已保存的群作用域继续评分、推进控制器并可能接纳 proposal；现有宿主测试 `test_observer_to_proposal_to_outbox_uses_the_same_self_work_path` 在没有第二条入站消息的后续 tick 上成功创建 SELF 工作，本轮复跑通过。隔离的[静默探针](evidence/group-chat-idle-wake-v1.json)用 2 秒虚拟 tick 和开发者语义标签跑 20 个种子：单条邀请后的 **20/20** 次 proposal 在最后一条入站消息后 **32–62 秒**出现；普通闲聊为 **0/20**。它证明代码路径允许“消息之后无需再次触发也能提出机会”，不证明主 Agent 会发言。

这个唤醒不是无限期凭空发言。普通群聊候选有效期为源消息后 90 秒；记忆候选也需要宿主最近 600 秒内见过真人，并通过独立语义评分。既没有近期真人上下文，也没有有效候选时，定时 tick 本身不制造话题。生产容器本轮为 healthy，semantic 开关已启用且 Jev 密钥存在；只读运行记录显示两个群当前 effective owner 为 `semantic`，但至本次快照尚无 semantic 接纳 run，因此**线上静默自主发言次数无法从现有样本确认**。旧 legacy run 的最近真人消息间隔均不超过 30 秒，不代表 V6 的静默能力。

## 下一轮验收设计

1. **修边界竞态并复测。** 对未评分的同单元新消息设置接纳屏障，尤其结束/停止；不能凭文本伪造语义结论。定义“停止先到、旧 proposal 后到”和“停止焦点队列过期”的失败用例，检查提议与宿主接纳两层。目标是 20 种子里这两类违规均为 0。
2. **继续改进负载形状。** 已纳入小时量、同人连续发言和消息长度；下一版在服务器汇总短/长间隔状态转移、同一话题的持续时间与更长留出窗口，不导出原始时间序列。冻结高频、低频、峰值、夜间四档。
3. **独立标注。** 从完全合成但更自然的中文场景中建立盲标集；至少两人分别标注邀请/续聊/别人交流/结束/停止、对象归属与回应机会，记录分歧。真实群聊正文若将来要参与标注，须另行确定私有处理流程，不进入公开仓库。
4. **隔离宿主影子。** 在真实 Yuki 代码和数据库模式的临时副本中重放同一冻结流，Provider 可真实调用，主 Agent 可运行，但发送工具替换为记录器，禁止 QQ 网关。对比 semantic、legacy、简单静态门控，分别统计 Jev 请求/费用、proposal、CAS 接纳、NO_REPLY、工具回执、拟发送次数、重复来源和重启恢复；主模型提示词、工具顺序和授权保持一致。
5. **真实小群。** 在离线和影子安全门通过后，再做有限真实验收；至少记录参与机会的来源、人工判断是否该接、是否实际发送、用户回应和停止命令响应。历史代码测试、合成探针和生产群行为分列，不能相互替代。

## 复现

项目需要 Python ≥3.12。服务器汇总程序只读，并应将输出保存到已忽略的 `private-data/`：

```powershell
Get-Content -Raw scripts\profile_group_metadata.py |
  ssh yuki-server python3 - --days 30 --min-human 100 |
  Set-Content private-data\group_metadata_2026-09-23.json
python scripts\group_chat_experiment.py build --profile private-data\group_metadata_2026-09-23.json --output fixtures\group-chat-workload-v1.json
python scripts\group_chat_experiment.py replay --fixture fixtures\group-chat-workload-v1.json --output docs\evidence\group-chat-workload-v1-replay.json
python scripts\group_chat_experiment.py replay --fixture fixtures\group-chat-workload-v1.json --output docs\evidence\group-chat-workload-v1-multiseed.json --runs 20

Get-Content -Raw scripts\profile_group_metadata.py |
  ssh yuki-server python3 - --days 30 --min-human 100 |
  Set-Content -Encoding utf8 private-data\group_metadata_full_2026-09-23.json
python -m scripts.build_full_group_workload --profile private-data\group_metadata_full_2026-09-23.json --output fixtures\group-chat-workload-v2-full.json.gz --report docs\evidence\group-chat-workload-v2-calibration.json
python -m scripts.idle_wake_experiment --trials 20 --output docs\evidence\group-chat-idle-wake-v1.json
python -m scripts.replay_full_group_workload --fixture fixtures\group-chat-workload-v2-full.json.gz --output docs\evidence\group-chat-workload-v2-replay.json
pytest -q
```

`probe_group_scenarios.py` 只在已配置 Jev 密钥的隔离运行环境中显式执行；其输入必须是合成 fixture，输出只含选择分布、usage、时延和错误类别。不要让测试器调用 Yuki 的发送工具或将真实消息正文加入 fixture。
