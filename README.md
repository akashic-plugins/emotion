# emotion

Akashic emotion and proactive tuning plugin.

## v3 接入

入口是 module-level `api_version = 3` 与 `apply(ctx, config)`。Emotion 通过 Core
声明以下能力：

- `PROACTIVE_COMPONENTS`：在 exact generation 中形成 VAD prompt projection；formal
  运行由 `emotion.state` domain effect 提交 SQLite，candidate 不打开数据库。
- `BACKGROUND_JOBS`：`feedback-preference-context` Drift 完成后，使用 Core 的 LLM
  lease 和窄 documents port 合并 `PROACTIVE_CONTEXT.md` / `proactive_pending.md`。
- `AFTER_TURN_COMMITTED`：消费 Core 已提交的 typed Turn。上游若提供
  `extra.proactive_feedback`，按其稳定 identity 幂等写入；显式引用消息则按 Turn
  自带标记写入 gold feedback。
- `UI_SLOTS` 与 C09 Dashboard：移动端和桌面端只读 Emotion 自有投影，不读取
  `sessions.db`，不取得任意 workspace 句柄。

插件不再声明 v2 `Plugin`、EventBus listener、固定 `proactive_modules()` / `jobs()`
或旧 mobile/dashboard ABI。旧数据库不会在 import/apply 时自动迁移；切换前应先
停用旧 runtime 并使用独立迁移脚本（尚未将旧源删除）。

CI 的 Core pin 是 20062a715d2c5822228b327863b51c8d036119b3，因为旧 pin
5624a059348406c1f97993612adfec886b158158 没有 domain_effect_lookup_export。
该 commit 尚未发布到 Core 的公共默认分支前，CI checkout 失败属于明确的发布阻塞；
本插件必须继续在 integration Core exact worktree 上验证，不得删除 lookup seam 或放宽
candidate/formal oracle。

## 移动端看板

插件通过通用移动 UI 生命周期注册“主动状态”入口，说明用户反馈如何改变 Agent 的语气
与主动发送把握。移动端只列真正产生状态增量的反馈，不复制桌面端每个 proactive tick
的 effect 表；原始 VAD 指标默认折叠，需要时再查看。
