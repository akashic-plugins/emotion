// plugins/emotion/dashboard_panel.tsx
import { Chip, api } from "@akashic/dashboard-ui";
import { jsx, jsxs } from "react/jsx-runtime";
function _score(value) {
  return typeof value === "number" ? value.toFixed(3) : "-";
}
function _delta(value) {
  if (typeof value !== "number") return "-";
  return value > 0 ? `+${value.toFixed(3)}` : value.toFixed(3);
}
function _shortTs(value) {
  const text = String(value || "");
  if (!text) return "-";
  const d = new Date(text);
  if (Number.isNaN(d.getTime())) return text;
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function _escape(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
function _toneCell(value) {
  const text = String(value || "-");
  const tone = text === "raise_send_bar" ? "warning" : text === "lower_send_bar" ? "success" : "muted";
  return `<span class="${window.AkashicDashboard.ui.cx.badge(tone)}">${_escape(text)}</span>`;
}
function EmotionDetail(props) {
  const item = props.item;
  if (!item) {
    return /* @__PURE__ */ jsxs("div", { className: "detail-empty", children: [
      /* @__PURE__ */ jsx("div", { className: "detail-empty-title", children: "VAD Effect" }),
      /* @__PURE__ */ jsx("div", { className: "detail-empty-text", children: "\u70B9\u5F00\u4E00\u6761 effect \u540E\uFF0C\u8FD9\u91CC\u4F1A\u663E\u793A\u8FD9\u6B21\u4E3B\u52A8 tick \u7684\u60C5\u7EEA\u5F71\u54CD\u3002" })
    ] });
  }
  return /* @__PURE__ */ jsxs("div", { className: "detail-wrap", children: [
    /* @__PURE__ */ jsx("div", { className: "detail-toolbar", children: /* @__PURE__ */ jsxs("div", { children: [
      /* @__PURE__ */ jsx("div", { className: "detail-title", children: "VAD \u4E3B\u52A8\u5F71\u54CD" }),
      /* @__PURE__ */ jsx("div", { className: "detail-subtext", children: String(item.tick_id || "") })
    ] }) }),
    /* @__PURE__ */ jsxs("div", { className: "detail-grid", children: [
      /* @__PURE__ */ jsx(DetailRow, { label: "expected", value: /* @__PURE__ */ jsx(Chip, { tone: String(item.expected_effect) === "raise_send_bar" ? "warning" : "success", children: String(item.expected_effect || "-") }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "tone", value: /* @__PURE__ */ jsx("code", { children: String(item.tone_label || "-") }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "V", value: /* @__PURE__ */ jsx("code", { children: _score(item.valence) }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "A", value: /* @__PURE__ */ jsx("code", { children: _score(item.arousal) }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "D", value: /* @__PURE__ */ jsx("code", { children: _score(item.dominance) }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "threshold", value: /* @__PURE__ */ jsxs("code", { children: [
        _score(item.base_threshold),
        " \u2192 ",
        _score(item.final_threshold)
      ] }) })
    ] }),
    /* @__PURE__ */ jsx(TextBlock, { title: "Prompt Section", text: String(item.prompt_section || "") }),
    /* @__PURE__ */ jsx(TextBlock, { title: "Metadata", text: JSON.stringify(item.metadata || {}, null, 2) })
  ] });
}
function DetailRow(props) {
  return /* @__PURE__ */ jsxs("div", { className: "detail-row", children: [
    /* @__PURE__ */ jsx("div", { className: "detail-row-label", children: props.label }),
    /* @__PURE__ */ jsx("div", { className: "detail-row-val", children: props.value })
  ] });
}
function TextBlock(props) {
  return /* @__PURE__ */ jsxs("div", { className: "detail-block", children: [
    /* @__PURE__ */ jsx("div", { className: "detail-label", children: props.title }),
    /* @__PURE__ */ jsx("div", { className: "detail-content whitespace-pre-wrap", children: props.text || "-" })
  ] });
}
window.AkashicDashboard.registerPlugin({
  id: "emotion",
  label: "Emotion",
  viewLabel: "emotion",
  pageSize: 50,
  rowKey: "id",
  countTitle(total) {
    return `\u5171 ${total} \u6761 effect`;
  },
  columns: [
    { key: "created_at", label: "Time", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "expected_effect", label: "Effect", width: 124, renderCell: _toneCell },
    { key: "tone_label", label: "Tone", width: 136, cellClass: "mono" },
    { key: "valence", label: "V", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "arousal", label: "A", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "dominance", label: "D", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "threshold_delta", label: "\u0394", width: 58, fmt: "delta", cellClass: "mono cell-metric", align: "right" },
    { key: "tick_id", label: "Tick", flex: true, cellClass: "mono content-preview", rawTitle: true }
  ],
  async getCount() {
    try {
      const overview = await api("/api/dashboard/emotion/overview");
      return overview.effect_count || 0;
    } catch {
      return null;
    }
  },
  async fetchPage({ page, pageSize }) {
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    const data = await api(`/api/dashboard/emotion/effects?${params.toString()}`);
    return { items: data.items || [], total: data.total || 0 };
  },
  async fetchDetail(item) {
    return api(`/api/dashboard/emotion/effects/${item.id}`);
  },
  Detail: EmotionDetail,
  formatters: {
    score: (value) => _score(value),
    delta: (value) => _delta(value),
    "mono-time": (value) => _shortTs(value)
  }
});
