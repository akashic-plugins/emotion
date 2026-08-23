# emotion

Akashic 的 Emotion 状态、反馈历史和主动偏好插件。

## v3 普通能力组合

Emotion 不拥有 React，也不是 proactive 特权模块。它只组合 Core 已有原子：

```text
RUNTIME_STARTED
  └─ TIMERS ──刷新──> emotion_context_current（可覆盖 current）
              └────> drift.proposals.v1（普通 proposal）

CONTEXT_PREPARED(channel=wake)
  └─ fresh current ──append──> extra_hints

Wake Turn 选择 proposal
  ├─ 普通 tool ──事务提交──> 完整 drift result + current preference + cursor
  └─ AFTER_TURN_COMMITTED ──对账──> selected Turn / missing-commit revision

proactive-feedback.history.v1（可选）
  └─ TIMERS ──pull page──> Emotion observation/sample + PF cursor（同一事务）
```

插件声明 `TIMERS`、`TOOL_CATALOG`、`UI_SLOTS`、`drift.proposals.v1` 和
`drift.wake.v1`。candidate 没有 `RUNTIME_STARTED`，因此不会登记 Timer、打开
Emotion DB 或产生 formal 写入。formal generation 的 current refresh 和可选 feedback
pull 各有一条职责独立的 Fiber-owned one-shot Timer 链；PF service 缺席时 pull child
保持 pending。reload 会先停止旧 child，再从 Emotion 自有 cursor 启动新 child。

当前 hint 只在 `channel=wake` 且刷新时间不超过 10 分钟时追加。它不 abort、
不 replace，也不污染 passive Turn。外部暂态 `OSError` 会记录 incident 并等待
下一 Timer；配置、schema、Content/Tool 合同错误保持 fail-loud。

## 事实 owner 与保留

| 事实 | owner | 正常语义 |
|---|---|---|
| `emotion_events` | Emotion | 真实情绪观察历史，全量追加保留 |
| `emotion_feedback_samples` | Emotion | 真实主动反馈样本，全量追加保留 |
| `emotion_drift_runs` | Emotion | 每个 proposal revision、选择和结果，全量追加保留 |
| `emotion_state` | Emotion | 当前 VAD singleton，可原位覆盖 |
| `emotion_context_current` | Emotion | 当前 VAD/presence/hint，可原位覆盖 |
| `emotion_preference_state` | Emotion | 当前偏好与已提交 cursor，可原位覆盖 |
| `pf_history_cursor` | Emotion | 已原子应用的 PF history current cursor，可原位覆盖 |
| `emotion_effects` | legacy Emotion | 冻结保留，不再新增、不删除 |
| `emotion_domain_effects` | legacy Emotion | 冻结保留，不再新增、不删除 |

空 tick 只允许刷新 current singleton，不创建 proposal/history。反馈批次先冻结进
`emotion_drift_runs`，普通 Drift 接受后由 `emotion_commit_preference_context`
在一个 SQLite transaction 中保存完整结果、替换 current preference、推进 cursor。
Turn 已提交但工具没提交时，该 revision 记为 `completed_without_commit`；下次为
同一批证据生成新 revision。重复提交相同结果幂等，identity 漂移会明确失败。

PF accepted history 由 Feedback 插件全量持有；Emotion 的 event/sample 是“这条反馈已被
Emotion 接纳、改变了什么、是否进入 Drift evidence”的独立应用账本。每页 observation、
可选 sample、VAD current 和 `pf_history_cursor` 同事务提交。普通非引用反馈在 PF accepted
前不会进入 Emotion，下一 Timer 后恰好应用一次。显式引用由 Emotion direct rule 立即
应用；PF 后续同 user message receipt 仍追加零 delta terminal 并推进 cursor/hash，但不再
增加 VAD 或重复写 Drift sample。

## 旧 proactive island 交接

旧版本曾参与两个 workspace 根文件，但它们是 proactive island 的共享事实，不归
Emotion 独占：

- `PROACTIVE_CONTEXT.md`：旧 Core `ProactiveDocuments` 读取并成对替换的当前主动规则。
- `proactive_pending.md`：旧 Drift skill 追加、旧 merge job 清空的共享候选队列。

本版本不声明 `workspace_files`，不读取、写入或清空这两个文件，也不保留
`PROACTIVE_COMPONENTS`、`BACKGROUND_JOBS`、`DRIFT_FINISHED` 或 private proactive
module。最终 Core island archive PR 应对旧文件做只读归档 receipt，记录完整 bytes、
SHA-256、原路径和时间后再 supersede；原文件不由 Emotion 删除。

## 验证边界

测试使用真实 Core DriftStore、Plugin Timer/Tool/UI composition 与隔离 SQLite，覆盖
candidate 零副作用、fresh wake hint、被动链零污染、proposal 重放、未提交重提、
工具幂等提交、TurnCommitted selection 时序、PF 双顺序组合、分页事务、显式引用单计数、
暂态 Timer 重臂和 reload 单 Timer。
这些是隔离 E2E fixture，不声称 hua-home formal activation 或线上 provider E2E。
