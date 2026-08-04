// dashboard_panel.tsx
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
function _effectLabel(value) {
  const effect = String(value || "");
  if (effect === "raise_send_bar") return "\u63D0\u9AD8\u53D1\u9001\u9608\u503C";
  if (effect === "lower_send_bar") return "\u964D\u4F4E\u53D1\u9001\u9608\u503C";
  return effect || "-";
}
function _toneCell(value) {
  const text = String(value || "-");
  const tone = text === "raise_send_bar" ? "warning" : text === "lower_send_bar" ? "success" : "muted";
  return `<span class="${window.AkashicDashboard.ui.cx.badge(tone)}">${_escape(_effectLabel(text))}</span>`;
}
function EmotionDetail(props) {
  const item = props.item;
  if (!item) {
    return /* @__PURE__ */ jsxs("div", { className: "detail-empty", children: [
      /* @__PURE__ */ jsx("div", { className: "detail-empty-title", children: "\u60C5\u7EEA\u5F71\u54CD\u8BE6\u60C5" }),
      /* @__PURE__ */ jsx("div", { className: "detail-empty-text", children: "\u9009\u62E9\u4E00\u6761\u8BB0\u5F55\uFF0C\u67E5\u770B\u8FD9\u6B21\u4E3B\u52A8\u4EFB\u52A1\u7684\u60C5\u7EEA\u5F71\u54CD\u3002" })
    ] });
  }
  return /* @__PURE__ */ jsxs("div", { className: "detail-wrap", children: [
    /* @__PURE__ */ jsx("div", { className: "detail-toolbar", children: /* @__PURE__ */ jsxs("div", { children: [
      /* @__PURE__ */ jsx("div", { className: "detail-title", children: "\u60C5\u7EEA\u5BF9\u4E3B\u52A8\u51B3\u7B56\u7684\u5F71\u54CD" }),
      /* @__PURE__ */ jsx("div", { className: "detail-subtext", children: String(item.tick_id || "") })
    ] }) }),
    /* @__PURE__ */ jsxs("div", { className: "detail-grid", children: [
      /* @__PURE__ */ jsx(DetailRow, { label: "\u9884\u671F\u5F71\u54CD", value: /* @__PURE__ */ jsx(Chip, { tone: String(item.expected_effect) === "raise_send_bar" ? "warning" : "success", children: _effectLabel(item.expected_effect) }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "\u8BED\u6C14", value: /* @__PURE__ */ jsx("code", { children: String(item.tone_label || "-") }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "\u6109\u60A6\u5EA6", value: /* @__PURE__ */ jsx("code", { children: _score(item.valence) }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "\u5524\u9192\u5EA6", value: /* @__PURE__ */ jsx("code", { children: _score(item.arousal) }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "\u652F\u914D\u5EA6", value: /* @__PURE__ */ jsx("code", { children: _score(item.dominance) }) }),
      /* @__PURE__ */ jsx(DetailRow, { label: "\u9608\u503C\u53D8\u5316", value: /* @__PURE__ */ jsxs("code", { children: [
        _score(item.base_threshold),
        " \u2192 ",
        _score(item.final_threshold)
      ] }) })
    ] }),
    /* @__PURE__ */ jsx(TextBlock, { title: "\u63D0\u793A\u8BCD\u7247\u6BB5", text: String(item.prompt_section || "") }),
    /* @__PURE__ */ jsx(TextBlock, { title: "\u5143\u6570\u636E", text: JSON.stringify(item.metadata || {}, null, 2) })
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
    /* @__PURE__ */ jsx("div", { className: "detail-content ak-plugin-pre-wrap", children: props.text || "-" })
  ] });
}
window.AkashicDashboard.registerPlugin({
  id: "emotion",
  label: "Emotion \u60C5\u7EEA",
  viewLabel: "\u60C5\u7EEA",
  pageSize: 50,
  rowKey: "id",
  countTitle(total) {
    return `\u5171 ${total} \u6761\u60C5\u7EEA\u5F71\u54CD`;
  },
  columns: [
    { key: "created_at", label: "\u65F6\u95F4", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "expected_effect", label: "\u5F71\u54CD", width: 132, renderCell: _toneCell },
    { key: "tone_label", label: "\u8BED\u6C14", width: 112, cellClass: "mono" },
    { key: "valence", label: "V", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "arousal", label: "A", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "dominance", label: "D", width: 58, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "threshold_delta", label: "\u0394", width: 58, fmt: "delta", cellClass: "mono cell-metric", align: "right" },
    { key: "tick_id", label: "\u4EFB\u52A1", flex: true, cellClass: "mono content-preview", rawTitle: true }
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
