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
  return (
    <div className="detail-wrap">
      <div className="detail-toolbar">
        <div>
          <div className="detail-title">情绪对主动决策的影响</div>
          <div className="detail-subtext">{String(item.tick_id || "")}</div>
        </div>
      </div>
      <div className="detail-grid">
        <DetailRow label="预期影响" value={<Chip tone={String(item.expected_effect) === "raise_send_bar" ? "warning" : "success"}>{_effectLabel(item.expected_effect)}</Chip>} />
        <DetailRow label="语气" value={<code>{String(item.tone_label || "-")}</code>} />
        <DetailRow label="愉悦度" value={<code>{_score(item.valence)}</code>} />
        <DetailRow label="唤醒度" value={<code>{_score(item.arousal)}</code>} />
        <DetailRow label="支配度" value={<code>{_score(item.dominance)}</code>} />
        <DetailRow label="阈值变化" value={<code>{_score(item.base_threshold)} → {_score(item.final_threshold)}</code>} />
      </div>
      <TextBlock title="提示词片段" text={String(item.prompt_section || "")} />
      <TextBlock title="元数据" text={JSON.stringify(item.metadata || {}, null, 2)} />
    </div>
  );
}

function DetailRow(props: { label: string; value: ReactElement }): ReactElement {
  return <div className="detail-row"><div className="detail-row-label">{props.label}</div><div className="detail-row-val">{props.value}</div></div>;
}

function TextBlock(props: { title: string; text: string }): ReactElement {
  return (
    <div className="detail-block">
      <div className="detail-label">{props.title}</div>
      <div className="detail-content ak-plugin-pre-wrap">{props.text || "-"}</div>
    </div>
  );
}

window.AkashicDashboard.registerPlugin({
  id: "emotion",
  label: "Emotion 情绪",
  viewLabel: "情绪",
  pageSize: 50,
  rowKey: "id",

  countTitle(total: number): string {
    return `共 ${total} 条情绪影响`;
  },

  columns: [
    { key: "created_at", label: "时间", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "expected_effect", label: "影响", width: 132, renderCell: _toneCell },
    { key: "tone_label", label: "语气", width: 112, cellClass: "mono" },
    { key: "valence", label: "V", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "arousal", label: "A", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "dominance", label: "D", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "threshold_delta", label: "Δ", width: 58, fmt: "delta", cellClass: "mono cell-metric", align: "right" },
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
