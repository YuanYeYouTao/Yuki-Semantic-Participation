# 宿主与控制器协议

每个 `Controller` / `ObservationSession` 只属于一个 `(conversation_id, generation)`。
创建新 generation 使用新的实例及持久 key；旧响应不能进入新实例。

正常顺序：

```text
宿主 canonical 入库与授权
  → session.observe(ScopedEvent)
  → await session.evaluate_due(now, active=...)
  → controller.advance(now, controller_epoch=..., host_available=...)
  → 保存 State（含 observer_checkpoint）
  → 宿主 submit_proposal；同 proposal_id 返回同 run 或明确拒绝
  → controller.observe_run_feedback(Feedback)
  → 保存 State
```

`host_available` 是宿主当前的接纳事实，不由 Jev 决定。不得把返回的 Proposal 当成发送授权。
模型不可用时 `ProviderHealth` 只提出健康事实，由宿主唯一 selector 决定何时回退。

宿主必须再核验：scope/generation、owner/epoch、来源版本与可见性、语义支持、群及 Presence、
已有 Agent/Work 占用。新增接纳的原子事务不能包含 Jev、主模型、网关或历史扫描。

`Feedback.effects` 每项必须区分 `message`、`compute`、`tool`。只有网关确认的真实 `message`
可更新自身发言痕迹；发送尝试、文件写入、计算完成都不等于发言。分条消息使用同一个逻辑表达
ID 表示社会表达，不按 QQ 分条次数计数。`actual_targets` 是真实目标依据，不是模型猜测。

调用者发现来源撤回或版本变化时，先调用 `observe_source_change`，然后重新送入已授权的新版本。
控制器不自行查询 Yuki 数据库；观察请求准备时也检查来源仍在当前有效集合内。

恢复必须使用原 State；ObservationSession 从其中恢复有界队列、请求序号与健康状态。
在途 Jev 评分无外部业务效果，只能重排仍新鲜的原材料；在途宿主提议先按 proposal_id 查结果。
不能把两种“在途”混成一套重试。

`SelfDelta` 是可选自报。解析失败不要求主模型补交；宿主必须在 QQ、语音和记忆提取前剥离
控制尾段。当前只有解析库，宿主生产输出尚未接入它。

协议只接收可信宿主操作。公网服务、多租户身份验证、远程发送和独立 Agent Runtime 均不在此库内。
