function number(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function signed(value) {
  const numberValue = Number(value || 0);
  return `${numberValue >= 0 ? "+" : ""}${numberValue.toFixed(2)}`;
}

function shortTime(value) {
  const date = new Date(String(value || ""));
  if (Number.isNaN(date.getTime())) return String(value || "—");
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function toneLabel(value) {
  if (value === "bright_confident") return "轻快自信";
  if (value === "careful_tentative") return "谨慎试探";
  if (value === "low_confidence") return "低打扰";
  if (value === "calm") return "平静";
  return "自然";
}

export function thresholdLabel(value) {
  if (value === "raise_send_bar") return "更谨慎";
  if (value === "lower_send_bar") return "更愿意主动";
  return "保持门槛";
}

export function influenceLabel(value) {
  if (value === "explicit_quote") return "明确引用";
  if (value === "topic_follow") return "继续话题";
  if (value === "no_topic_follow") return "没有继续";
  return "反馈";
}

function influenceImpact(item) {
  const dominance = Number(item.dominance_delta || 0);
  if (dominance > 0) return `主动把握 +${Math.round(dominance * 100)}`;
  if (dominance < 0) return `主动把握 ${Math.round(dominance * 100)}`;
  return "状态未改变";
}

function influenceRow(item) {
  const row = document.createElement("article");
  row.className = `emotion-mobile-influence emotion-mobile-influence--${item.source_type}`;
  const label = document.createElement("span");
  label.className = "emotion-mobile-influence__label";
  label.textContent = influenceLabel(item.source_type);
  const time = document.createElement("time");
  time.textContent = shortTime(item.created_at);
  const preview = document.createElement("strong");
  preview.textContent = item.user_preview || "（没有消息摘要）";
  const impact = document.createElement("span");
  impact.className = "emotion-mobile-influence__impact";
  impact.textContent = influenceImpact(item);
  row.append(label, time, preview, impact);
  return row;
}

const dashboard = {
  mount(host, context) {
    let active = true;
    host.className += " emotion-mobile";
    host.innerHTML = `
      <div class="emotion-mobile-loading" role="status">正在读取主动状态…</div>
      <div class="emotion-mobile-content" hidden>
        <section class="emotion-mobile-overview" aria-label="当前主动状态">
          <div class="emotion-mobile-state">
            <strong>自然</strong>
            <span>当前语气</span>
          </div>
          <div class="emotion-mobile-signals">
            <div class="emotion-mobile-threshold"><strong>保持门槛</strong><span>主动发送</span></div>
            <div class="emotion-mobile-feedback"><strong>0</strong><span>有效影响</span></div>
          </div>
        </section>
        <button type="button" class="emotion-mobile-metrics-trigger" aria-expanded="false">
          <span>查看状态指标</span><span class="emotion-mobile-metrics-chevron" aria-hidden="true"></span>
        </button>
        <div class="emotion-mobile-metrics" aria-hidden="true" inert>
          <dl>
            <div><dt>愉悦度</dt><dd data-metric="valence">—</dd></div>
            <div><dt>活跃度</dt><dd data-metric="arousal">—</dd></div>
            <div><dt>主动把握</dt><dd data-metric="dominance">—</dd></div>
          </dl>
        </div>
        <section class="emotion-mobile-history" aria-labelledby="emotion-mobile-history-title">
          <header><h2 id="emotion-mobile-history-title">最近影响</h2><span></span></header>
          <div class="emotion-mobile-influences"></div>
        </section>
      </div>`;
    const loading = host.querySelector(".emotion-mobile-loading");
    const content = host.querySelector(".emotion-mobile-content");
    const metrics = host.querySelector(".emotion-mobile-metrics");
    const metricsTrigger = host.querySelector(".emotion-mobile-metrics-trigger");
    metricsTrigger.addEventListener("click", () => {
      const expanded = metricsTrigger.getAttribute("aria-expanded") !== "true";
      metricsTrigger.setAttribute("aria-expanded", String(expanded));
      metrics.setAttribute("aria-hidden", String(!expanded));
      metrics.inert = !expanded;
      metrics.classList.toggle("is-expanded", expanded);
      metricsTrigger.classList.toggle("is-expanded", expanded);
    });

    context.query("emotion.bootstrap", { limit: 30 }).then((bootstrap) => {
      if (!active) return;
      const overview = bootstrap.overview || {};
      const history = { items: bootstrap.items || [] };
      const state = overview.state || {};
      const behavior = overview.current_behavior || {};
      const tone = String(behavior.tone_label || "neutral");
      const expected = String(behavior.expected_effect || "tone_only");
      const stateBlock = host.querySelector(".emotion-mobile-state");
      stateBlock.classList.add(`emotion-mobile-state--${tone}`);
      stateBlock.querySelector("strong").textContent = toneLabel(tone);
      const threshold = host.querySelector(".emotion-mobile-threshold");
      threshold.classList.add(`emotion-mobile-threshold--${expected}`);
      threshold.querySelector("strong").textContent = thresholdLabel(expected);
      host.querySelector(".emotion-mobile-feedback strong").textContent = number(overview.influence_count);
      for (const name of ["valence", "arousal", "dominance"]) {
        host.querySelector(`[data-metric="${name}"]`).textContent = signed(state[name]);
      }
      const items = Array.isArray(history.items) ? history.items : [];
      host.querySelector(".emotion-mobile-history header span").textContent = `${number(items.length)} 条`;
      const list = host.querySelector(".emotion-mobile-influences");
      if (items.length === 0) {
        const empty = document.createElement("p");
        empty.className = "emotion-mobile-empty";
        empty.textContent = "还没有反馈改变主动状态。";
        list.append(empty);
      } else {
        list.append(...items.map(influenceRow));
      }
      loading.remove();
      content.hidden = false;
    }).catch((error) => {
      if (!active) return;
      loading.className = "emotion-mobile-loading error";
      loading.textContent = error instanceof Error ? `主动状态读取失败：${error.message}` : "主动状态读取失败";
    });
    return () => { active = false; };
  },
};

export default {
  slots: {},
  dashboard,
};
