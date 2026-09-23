# 宿主与控制器协议

以下协议已由独立库和 Yuki Host 实现并做定向验证；真实 QQ 社交效果仍须单独验收。
Yuki 接线与权限的现行说明见其 `docs/architecture/semantic-participation.md`。

## 身份、观测与讨论单元

每个 `Controller` / `ObservationSession` 只属于一个 `(conversation_id, generation)`。
创建新 generation 使用新的实例及持久 key；旧响应不能进入新实例。
Host 先完成 canonical 入库、可见性和授权，再传递有版本的 `ScopedEvent`。
平台消息凭据只在接入层解析引用，之后 `reply_to` 使用可信内部来源引用，不靠正文或 QQ 消息号重建身份。

`thread`、`target` 和歧义候选组合由 Host 给出。Jev 只能从既有 `unit_options` 或 unknown 中选择，
不能制造 Person、讨论 ID 或授权对象；`new` 也必须是 Host 预先提供的选项。
适配器映射结果后，Controller 再核验完整分布、唯一赢家及其所属选项，原 Snapshot 不被改写。
target 是语义对象，不能成为读取私人资料的 principal。

Host 可以对包含机器人称呼的焦点标记 `observation_priority`，使其在稀疏队列中先交给 Jev；
此标记不是 `@`、回复授权或候选资格。Jev 若判断为明确邀请/续聊且楼层留给 Yuki，
即便旧讨论 `unit_selection=unknown`，Controller 也只可采用 Host 已给出的 `new` 单元，
且须核对该单元的 thread 和作者目标；其它歧义继续保持未知。
明确邀请的合格观测直接提出机会；非请求式候选比较连续净机会值，不等待随机强度积分。
Host 对新的 proposal 仍执行来源、
停止边界、同会话占用、代际、权限和 Main Agent 接纳检查。

Jev 请求采用紧凑语义投影：中性局部引用、原文、作者角色、讨论与对象关系、引用关系、相对时间和省略说明。
scope、generation、revision、sequence 等本地管理字段留在原 Snapshot 中校验，不发送给模型。
小快照保留完整焦点和已提供的引用锚点，最多六条上下文；超界先整条去除非关键 context。
实际 UTF-8 请求默认上限 16,000 字节，仍超界记本地 `input_too_large`，不计 Provider 故障，
不无限重评同一材料。字节不是 token，token 只记录 Provider usage。

interaction、information、floor 是新资格所需的必需维度；缺失或非法时保留诊断并计健康失败。
合法 unknown 不计失败；若交际行为和楼层均明确表示邀请 Yuki，信息新旧或旧讨论归属 unknown 不单独否决邀请。其余缺乏明确交际行为或楼层的 unknown 不生成资格或停止边界；软状态仍按完整概率分布更新。
单个辅助 boundary 缺失不等于整个 Provider 故障。明确停止和重新邀请的作用范围均由 boundary 观测约束。

## 接纳与执行

```text
Host canonical 入库、来源版本与授权检查
  → session.observe(ScopedEvent)
  → await session.evaluate_due(now, active=...)
  → Host 唯一 selector 更新 off / legacy / semantic 及 epoch
  → controller.advance(now, controller_epoch=..., host_available=..., intrinsic_allowed=...)
  → 保存 State（含 observer_checkpoint）
  → Host submit_proposal；同 proposal_id 返回同 run 或明确拒绝
  → 原有 WorkScheduler / Main Agent / 工具与发送链
  → controller.observe_run_feedback(Feedback)
  → 保存 State
```

`host_available` 是宿主当前的接纳事实，不由 Jev 决定。Proposal 不是发送授权。
Host 再核验 scope/generation、owner/epoch、来源版本与可见性、全部支持、Space、Presence 和已有 Work 占用。
慢观测不占全局接纳锁；原子写事务不等待 Jev、主模型、网关，也不扫描历史。
同一 proposal 和已消费来源由持久记录防重，busy 不冒充已消费。

Yuki 的唯一 selector 覆盖 legacy 和 semantic。缺 key、关闭 semantic 或持续 Provider 故障时可选 legacy，
master off 始终是 off。合法 unknown 不触发 fallback，已进入 degraded 后不能因队列过期自行恢复；
配置可用时仍可进行稀疏健康观测，真实成功才清除故障状态。当前没有生产 shadow 模式配置。

接纳产生正式 SELF initiative run，与唯一 `initiative:<run_id>` Work 绑定。SELF 不借用最近一位发言者权限，
没有人类 user/person principal；当前社交读写范围是本群授权历史、群可见及公开 SELF 记忆和本群普通发送。
持久工作区、终端与计算继续使用原工具执行边界，不能借此获得私聊或宿主管理权限。
它继续使用主 Agent 固定提示词及工具声明，执行处检查权限，不建立第二套 Agent Runtime。
最终文字默认内部返回；公开表达走显式发送工具，NO_REPLY 合法，调度器不自动补一句收尾。

## 来源、记忆种子与撤回

Host 发现来源撤回或版本变化，先调用 `observe_source_change`，再送入已授权的新版本。
解释及其上下文依赖、派生预测一并失效。context 出现在快照中不代表它已被作为焦点评分。
明确引用可短期续接已有真实支持，但不取代待评语义，也不能绕过结束、对象或 generation 边界。
重新开放必须有独立、范围一致的明确邀请证据，不能由沉默衰减或自身发言制造。

Yuki 的 memory-only seed 来自 active、verified、当前可读且 lineage 合法的群/SELF 记忆。
读取使用固定本轮时间上界和 `(change_time, id)` 前行游标，`change_time=max(updated_at, valid_from)`；
即使一页没有合法 lineage 也推进，未来生效材料到期后才进入，旧材料不按时钟回绕重投。
真正新增 evidence 同事务更新 fact 的变更时间，重复 evidence 不更新。每轮读取与 lineage 查询有界，
Host 持久化游标和已接纳来源；contact target 不提供 Person 私有记忆访问权。

## 回执、自报与恢复

`Feedback.effects` 区分 `message`、`compute`、`tool`。只有真实发送回执更新自身发言痕迹；
发送尝试、文件写入和计算完成都不等于发言。分条消息按逻辑表达 ID 去重，`actual_targets` 取真实目标。
Yuki 的工具证据严格为 event 或 initiative run 二选一；静默工具回合通过独立增量回执进入 SELF 自省，
不伪造聊天事件。错误、NO_REPLY 和运行中断也不会触发独立的罐头对外回复。

恢复使用原 State：队列、请求序号、健康和退避都持久化。在途 Jev 评分没有外部业务效果，
只能重排仍新鲜且获准的材料；在途 Host 提议则先按 proposal_id 查接纳结果，不能换 ID 重提。
已接纳 run 按原 Work、预算、执行 ID、回执和原 Presence 续跑。owner/epoch 变化只影响新接纳，
不重建旧 Work；generation 失效仍中止旧执行。迟到效果可审计，但不能复活终态或重复发消息。

`SelfDelta` 是可选自报。Yuki 已接入 `<yuki-state>` 尾段剥离，并按实际 run/request/response 防重；
在公开文字、语音和后续历史前移除控制尾段。解析失败不要求主模型补交，子 Agent 不能冒充主回合，
mood 不产生他人语义证据。

SnapshotStore 是单线程同步 SQLite CAS 存储，不跨线程共享连接，不在事务内等待外部工作。
公网服务、多租户身份验证、远程发送和独立 Agent Runtime 均不在本库内。
