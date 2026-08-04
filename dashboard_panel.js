// ../emotion/dashboard_panel.tsx
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
  const delta = typeof item.threshold_delta === "number" ? item.threshold_delta : null;
  return /* @__PURE__ */ jsxs("main", { className: "emotion-detail", "aria-labelledby": "emotion-detail-title", children: [
    /* @__PURE__ */ jsxs("header", { className: "emotion-detail__header", children: [
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("p", { children: "\u4E3B\u52A8\u51B3\u7B56\u8F93\u5165" }),
        /* @__PURE__ */ jsx("h2", { id: "emotion-detail-title", children: "\u8FD9\u6B21\u60C5\u7EEA\u5982\u4F55\u6539\u53D8\u53D1\u9001\u9608\u503C" }),
        /* @__PURE__ */ jsx("span", { children: String(item.tick_id || "\u672A\u5173\u8054\u4EFB\u52A1") })
      ] }),
      /* @__PURE__ */ jsx(Chip, { tone: String(item.expected_effect) === "raise_send_bar" ? "warning" : "success", children: _effectLabel(item.expected_effect) })
    ] }),
    /* @__PURE__ */ jsxs("section", { className: "emotion-threshold", "aria-label": "\u9608\u503C\u53D8\u5316", children: [
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "\u539F\u59CB\u9608\u503C" }),
        /* @__PURE__ */ jsx("strong", { children: _score(item.base_threshold) })
      ] }),
      /* @__PURE__ */ jsx("span", { className: "emotion-threshold__arrow", "aria-hidden": "true", children: "\u2192" }),
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx("span", { children: "\u5E94\u7528\u60C5\u7EEA\u540E" }),
        /* @__PURE__ */ jsx("strong", { children: _score(item.final_threshold) })
      ] }),
      /* @__PURE__ */ jsxs("div", { className: `emotion-threshold__delta${delta !== null && delta > 0 ? " is-up" : " is-down"}`, children: [
        /* @__PURE__ */ jsx("span", { children: "\u53D8\u5316" }),
        /* @__PURE__ */ jsx("strong", { children: _delta(delta) })
      ] })
    ] }),
    /* @__PURE__ */ jsxs("section", { className: "emotion-coordinates", "aria-labelledby": "emotion-coordinates-title", children: [
      /* @__PURE__ */ jsxs("div", { className: "emotion-section-heading", children: [
        /* @__PURE__ */ jsxs("div", { children: [
          /* @__PURE__ */ jsx("p", { children: "VAD \u6A21\u578B" }),
          /* @__PURE__ */ jsx("h3", { id: "emotion-coordinates-title", children: "\u60C5\u7EEA\u5750\u6807" })
        ] }),
        /* @__PURE__ */ jsxs("span", { children: [
          "\u8BED\u6C14\uFF1A",
          String(item.tone_label || "\u672A\u6807\u6CE8")
        ] })
      ] }),
      /* @__PURE__ */ jsx(VadGauge, { label: "\u6109\u60A6\u5EA6", low: "\u6D88\u6781", high: "\u79EF\u6781", value: item.valence }),
      /* @__PURE__ */ jsx(VadGauge, { label: "\u5524\u9192\u5EA6", low: "\u5E73\u9759", high: "\u6FC0\u6D3B", value: item.arousal }),
      /* @__PURE__ */ jsx(VadGauge, { label: "\u652F\u914D\u5EA6", low: "\u53D7\u63A7", high: "\u4E3B\u5BFC", value: item.dominance })
    ] }),
    /* @__PURE__ */ jsx(TextDisclosure, { title: "\u67E5\u770B\u5199\u5165\u4E3B\u52A8\u6D41\u7A0B\u7684\u63D0\u793A\u8BCD", text: String(item.prompt_section || "") }),
    /* @__PURE__ */ jsx(TextDisclosure, { title: "\u67E5\u770B\u6280\u672F\u5143\u6570\u636E", text: JSON.stringify(item.metadata || {}, null, 2) })
  ] });
}
function VadGauge(props) {
  const numeric = typeof props.value === "number" ? props.value : 0;
  const position = Math.max(0, Math.min(100, (numeric + 1) / 2 * 100));
  return /* @__PURE__ */ jsxs("div", { className: "emotion-gauge", children: [
    /* @__PURE__ */ jsxs("div", { className: "emotion-gauge__label", children: [
      /* @__PURE__ */ jsx("strong", { children: props.label }),
      /* @__PURE__ */ jsx("code", { children: _score(props.value) })
    ] }),
    /* @__PURE__ */ jsx("div", { className: "emotion-gauge__track", "aria-hidden": "true", children: /* @__PURE__ */ jsx("i", { style: { left: `${position}%` } }) }),
    /* @__PURE__ */ jsxs("div", { className: "emotion-gauge__ends", children: [
      /* @__PURE__ */ jsx("span", { children: props.low }),
      /* @__PURE__ */ jsx("span", { children: props.high })
    ] })
  ] });
}
function TextDisclosure(props) {
  return /* @__PURE__ */ jsxs("details", { className: "emotion-disclosure", children: [
    /* @__PURE__ */ jsx("summary", { children: props.title }),
    /* @__PURE__ */ jsx("pre", { children: props.text || "-" })
  ] });
}
window.AkashicDashboard.registerPlugin({
  id: "emotion",
  label: "\u60C5\u7EEA\u51B3\u7B56",
  viewLabel: "\u60C5\u7EEA\u51B3\u7B56",
  pageSize: 50,
  rowKey: "id",
  countTitle(total) {
    return `\u5171 ${total} \u6761\u60C5\u7EEA\u5F71\u54CD`;
  },
  columns: [
    { key: "created_at", label: "\u65F6\u95F4", width: 96, fmt: "mono-time", cellClass: "mono cell-time", rawTitle: true },
    { key: "expected_effect", label: "\u5F71\u54CD", width: 132, renderCell: _toneCell },
    { key: "tone_label", label: "\u8BED\u6C14", width: 112 },
    { key: "valence", label: "\u6109\u60A6", width: 66, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "arousal", label: "\u5524\u9192", width: 66, fmt: "score", cellClass: "mono cell-metric", align: "right" },
    { key: "threshold_delta", label: "\u9608\u503C\u53D8\u5316", width: 82, fmt: "delta", cellClass: "mono cell-metric", align: "right" },
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
