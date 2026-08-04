/// <reference path="../../types/akashic-dashboard.d.ts" />
import { type ReactElement } from "react";
import { Chip, api } from "@akashic/dashboard-ui";

interface Overview {
  state: Record<string, unknown> | null;
  effect_count: number;
}

interface FetchPage {
  items: Record<string, unknown>[];
  total: number;
}

function _score(value: unknown): string {
  return typeof value === "number" ? value.toFixed(3) : "-";
}

function _delta(value: unknown): string {
  if (typeof value !== "number") return "-";
  return value > 0 ? `+${value.toFixed(3)}` : value.toFixed(3);
}

function _shortTs(value: unknown): string {
  const text = String(value || "");
  if (!text) return "-";
  const d = new Date(text);
  if (Number.isNaN(d.getTime())) return text;
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function _escape(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function _effectLabel(value: unknown): string {
  const effect = String(value || "");
  if (effect === "raise_send_bar") return "提高发送阈值";
  if (effect === "lower_send_bar") return "降低发送阈值";
  return effect || "-";
}

function _toneCell(value: unknown): string {
  const text = String(value || "-");
  const tone = text === "raise_send_bar" ? "warning" : text === "lower_send_bar" ? "success" : "muted";
  return `<span class="${window.AkashicDashboard.ui.cx.badge(tone)}">${_escape(_effectLabel(text))}</span>`;
}

function EmotionDetail(props: { item: Record<string, unknown> | null }): ReactElement {
  const item = props.item;
  if (!item) {
    return <div className="detail-empty"><div className="detail-empty-title">情绪影响详情</div><div className="detail-empty-text">选择一条记录，查看这次主动任务的情绪影响。</div></div>;
  }
  const delta = typeof item.threshold_delta === "number" ? item.threshold_delta : null;
  return (
    <main className="emotion-detail" aria-labelledby="emotion-detail-title">
      <header className="emotion-detail__header">
        <div>
          <p>主动决策输入</p>
          <h2 id="emotion-detail-title">这次情绪如何改变发送阈值</h2>
          <span>{String(item.tick_id || "未关联任务")}</span>
        </div>
        <Chip tone={String(item.expected_effect) === "raise_send_bar" ? "warning" : "success"}>{_effectLabel(item.expected_effect)}</Chip>
      </header>

      <section className="emotion-threshold" aria-label="阈值变化">
        <div><span>原始阈值</span><strong>{_score(item.base_threshold)}</strong></div>
        <span className="emotion-threshold__arrow" aria-hidden="true">→</span>
        <div><span>应用情绪后</span><strong>{_score(item.final_threshold)}</strong></div>
        <div className={`emotion-threshold__delta${delta !== null && delta > 0 ? " is-up" : " is-down"}`}>
          <span>变化</span><strong>{_delta(delta)}</strong>
        </div>
      </section>

      <section className="emotion-coordinates" aria-labelledby="emotion-coordinates-title">
        <div className="emotion-section-heading">
          <div><p>VAD 模型</p><h3 id="emotion-coordinates-title">情绪坐标</h3></div>
          <span>语气：{String(item.tone_label || "未标注")}</span>
        </div>
        <VadGauge label="愉悦度" low="消极" high="积极" value={item.valence} />
        <VadGauge label="唤醒度" low="平静" high="激活" value={item.arousal} />
        <VadGauge label="支配度" low="受控" high="主导" value={item.dominance} />
      </section>

      <TextDisclosure title="查看写入主动流程的提示词" text={String(item.prompt_section || "")} />
      <TextDisclosure title="查看技术元数据" text={JSON.stringify(item.metadata || {}, null, 2)} />
    </main>
  );
}

function VadGauge(props: { label: string; low: string; high: string; value: unknown }): ReactElement {
  const numeric = typeof props.value === "number" ? props.value : 0;
  const position = Math.max(0, Math.min(100, ((numeric + 1) / 2) * 100));
  return (
    <div className="emotion-gauge">
      <div className="emotion-gauge__label"><strong>{props.label}</strong><code>{_score(props.value)}</code></div>
      <div className="emotion-gauge__track" aria-hidden="true"><i style={{ left: `${position}%` }} /></div>
      <div className="emotion-gauge__ends"><span>{props.low}</span><span>{props.high}</span></div>
    </div>
  );
}

function TextDisclosure(props: { title: string; text: string }): ReactElement {
  return (
    <details className="emotion-disclosure">
      <summary>{props.title}</summary>
      <pre>{props.text || "-"}</pre>
    </details>
  );
}

window.AkashicDashboard.registerPlugin({
  id: "emotion",
  label: "情绪决策",
  viewLabel: "情绪决策",
  pageSize: 50,
  rowKey: "id",

  countTitle(total: number): string {
    return `共 ${total} 条情绪影响`;
  },

  columns: [
    { key: "created_at", label: "时间", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "expected_effect", label: "影响", width: 132, renderCell: _toneCell },
    { key: "tone_label", label: "语气", width: 112 },
    { key: "valence", label: "愉悦", width: 66, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "arousal", label: "唤醒", width: 66, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "threshold_delta", label: "阈值变化", width: 82, fmt: "delta", cellClass: "mono cell-metric", align: "right" },
    { key: "tick_id", label: "任务", flex: true, cellClass: "mono content-preview", rawTitle: true },
  ],

  async getCount(): Promise<number | null> {
    try {
      const overview = await api<Overview>("/api/dashboard/emotion/overview");
      return overview.effect_count || 0;
    } catch {
      return null;
    }
  },

  async fetchPage({ page, pageSize }: { page: number; pageSize: number }) {
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    const data = await api<FetchPage>(`/api/dashboard/emotion/effects?${params.toString()}`);
    return { items: data.items || [], total: data.total || 0 };
  },

  async fetchDetail(item: Record<string, unknown>) {
    return api<Record<string, unknown>>(`/api/dashboard/emotion/effects/${item.id}`);
  },

  Detail: EmotionDetail,

  formatters: {
    score: (value: unknown) => _score(value),
    delta: (value: unknown) => _delta(value),
    "mono-time": (value: unknown) => _shortTs(value),
  },
});
