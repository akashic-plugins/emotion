---
name: feedback-preference-context
description: 从当前普通 Drift proposal 的 Emotion 反馈批次归纳稳定主动偏好，并通过普通 Emotion tool 原子提交。
---

# Feedback Preference Context

## 输入与结果

当前 Wake Turn 已携带一个 Emotion-owned Drift proposal。它包含稳定的
`proposal_id`、`revision`、当前偏好上下文和最多 10 条冻结反馈。不要重新读取
workspace、数据库、记忆文件或旧 proactive 文档。

审核完整批次后，必须调用普通工具 `emotion_commit_preference_context` 恰好一次：

```json
{
  "proposal_id": "proposal 中的原值",
  "revision": "proposal 中的原值",
  "context": "完整、简短、可直接用于以后主动判断的偏好上下文",
  "candidates": [
    {
      "effect": "boost",
      "confidence": "medium",
      "topic": "明确的单一主题",
      "action": "提高同一主题候选的优先级",
      "evidence": [12]
    }
  ]
}
```

工具会在 Emotion 自有 SQLite 的一个事务中同时保存完整结果、替换 current
context、推进 feedback cursor。工具失败时让本 Turn 失败暴露；不要伪造成功。

## 判断规则

1. `effect` 只能是 `block`、`boost`、`verify`、`timing`、`tone`。
2. `confidence` 只能是 `low`、`medium`、`high`。
3. `evidence` 只能引用当前 proposal `events[].id`；不得引用批次外事实。
4. 追问不自动等于喜欢。单条弱信号优先用 `verify` 或 `tone`。
5. `block` 需要明确反感、纠错或无价值；弱负反馈用 `verify` 收窄。
6. `timing` 只描述时机、频率和打扰条件，不冒充兴趣判断。
7. topic 和 action 必须同宽，不把单一对象扩大成整个类别。
8. 没有稳定候选时，`candidates` 传空数组，`context` 原样保留。
9. 不调用 `message_push`，不编辑任何 Markdown，不读写 `state.json` 或
   `history.json`，不提交另一套 Drift terminal。

TurnCommit 后，Emotion 会用普通 Drift selection 对账本次 revision。若本 Turn
没有成功调用工具，完整 proposal receipt 会保留为 `completed_without_commit`，
下一轮产生同一批次的新 revision，而不是丢掉证据或偷偷推进 cursor。
