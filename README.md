# emotion

Akashic emotion and proactive tuning plugin.

## 移动端看板

插件通过通用移动 UI 生命周期注册“主动状态”入口，说明用户反馈如何改变 Agent 的语气
与主动发送把握。移动端只列真正产生状态增量的反馈，不复制桌面端每个 proactive tick
的 effect 表；原始 VAD 指标默认折叠，需要时再查看。
