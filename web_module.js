export function activate(ctx) {
  return ctx.ui.inject("workbench.panels.v1", (mount) => mount.register({
    id: "emotion-decisions",
    label: "情绪决策",
    order: 40,
    render(host) {
      const panel = document.createElement("section");
      panel.className = "emotion-workbench-panel";
      panel.innerHTML = `<header><div><h1>情绪决策</h1><p>查看 Emotion 如何改变主动发送的语气与阈值。</p></div><button type="button" data-refresh>刷新</button></header><section class="emotion-overview" data-overview aria-label="当前情绪状态"><p>正在读取当前状态…</p></section><p data-status role="status" aria-live="polite"></p><div class="emotion-panel-grid"><div><div data-list></div><footer><button type="button" data-previous>上一页</button><span data-page></span><button type="button" data-next>下一页</button></footer></div><article data-detail><p>选择一条情绪影响查看详情。</p></article></div>`;
      host.replaceChildren(panel);
      const overview = panel.querySelector("[data-overview]");
      const refresh = panel.querySelector("[data-refresh]");
      const status = panel.querySelector("[data-status]");
      const list = panel.querySelector("[data-list]");
      const detail = panel.querySelector("[data-detail]");
      const pageText = panel.querySelector("[data-page]");
      const previous = panel.querySelector("[data-previous]");
      const next = panel.querySelector("[data-next]");
      let page = 1;
      let total = 0;
      let disposed = false;
      let overviewRequest = new AbortController();
      let listRequest = new AbortController();
      let detailRequest = new AbortController();

      const loadOverview = async () => {
        overviewRequest.abort();
        overviewRequest = new AbortController();
        const request = overviewRequest;
        overview.textContent = "正在读取当前状态…";
        try {
          const data = await json(ctx, "/api/dashboard/emotion/overview", request.signal);
          if (disposed || request.signal.aborted) return;
          overview.innerHTML = renderOverview(data);
        } catch (reason) {
          if (!disposed && !request.signal.aborted) showError(overview, reason);
        }
      };

      const loadList = async () => {
        listRequest.abort();
        listRequest = new AbortController();
        const request = listRequest;
        const requestedPage = page;
        status.textContent = "正在读取情绪影响…";
        try {
          const data = await json(ctx, `/api/dashboard/emotion/effects?page=${requestedPage}&page_size=25`, request.signal);
          if (disposed || request.signal.aborted) return;
          total = finiteNumber(data.total);
          renderRows(list, data.items, openDetail);
          const pages = Math.max(1, Math.ceil(total / 25));
          pageText.textContent = `${requestedPage} / ${pages}`;
          previous.disabled = requestedPage <= 1;
          next.disabled = requestedPage >= pages;
          status.textContent = total ? `共 ${total} 条情绪影响` : "还没有改变主动决策的情绪影响。";
        } catch (reason) {
          if (!disposed && !request.signal.aborted) showError(status, reason);
        }
      };

      const openDetail = async (effectId) => {
        detailRequest.abort();
        detailRequest = new AbortController();
        const request = detailRequest;
        detail.innerHTML = "<p>正在读取详情…</p>";
        try {
          const item = await json(ctx, `/api/dashboard/emotion/effects/${encodeURIComponent(effectId)}`, request.signal);
          if (disposed || request.signal.aborted) return;
          detail.innerHTML = renderDetail(item);
        } catch (reason) {
          if (!disposed && !request.signal.aborted) showError(detail, reason);
        }
      };

      refresh.addEventListener("click", () => {
        void loadOverview();
        void loadList();
      });
      previous.addEventListener("click", () => {
        if (page > 1) {
          page -= 1;
          void loadList();
        }
      });
      next.addEventListener("click", () => {
        if (page * 25 < total) {
          page += 1;
          void loadList();
        }
      });
      void loadOverview();
      void loadList();
      return () => {
        disposed = true;
        overviewRequest.abort();
        listRequest.abort();
        detailRequest.abort();
        host.replaceChildren();
      };
    },
  }));
}

function renderOverview(data) {
  const state = data && typeof data.state === "object" && data.state ? data.state : null;
  const behavior = data && typeof data.current_behavior === "object" && data.current_behavior
    ? data.current_behavior
    : null;
  if (!state || !behavior) {
    return `<p>还没有可用的情绪状态。已记录 ${finiteNumber(data && data.effect_count)} 条情绪影响。</p>`;
  }
  return `<div><span>当前语气</span><strong>${escapeHtml(behavior.tone_label || "未标注")}</strong></div><div><span>发送阈值</span><strong>${escapeHtml(effectLabel(behavior.expected_effect))}</strong><small>${escapeHtml(deltaText(behavior.threshold_delta))}</small></div><div><span>情绪坐标</span><strong>愉悦 ${escapeHtml(score(state.valence))} · 唤醒 ${escapeHtml(score(state.arousal))} · 支配 ${escapeHtml(score(state.dominance))}</strong></div>`;
}

function renderRows(target, items, openDetail) {
  target.replaceChildren();
  if (!Array.isArray(items) || !items.length) {
    target.innerHTML = "<p>没有可展示的情绪影响。</p>";
    return;
  }
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "emotion-panel-row";
    button.innerHTML = `<strong>${escapeHtml(effectLabel(item.expected_effect))}</strong><span>${escapeHtml(shortTime(item.created_at))} · ${escapeHtml(String(item.tone_label || "未标注"))}</span><small>愉悦 ${escapeHtml(score(item.valence))} · 唤醒 ${escapeHtml(score(item.arousal))} · 阈值 ${escapeHtml(deltaText(item.threshold_delta))}</small>`;
    button.addEventListener("click", () => void openDetail(item.id));
    target.append(button);
  }
}

function renderDetail(item) {
  const delta = deltaText(item.threshold_delta);
  return `<header><div><p>主动决策输入</p><h2>这次情绪如何改变发送阈值</h2><span>${escapeHtml(String(item.tick_id || "未关联任务"))}</span></div><strong>${escapeHtml(effectLabel(item.expected_effect))}</strong></header><dl class="emotion-detail-metrics"><div><dt>原始阈值</dt><dd>${escapeHtml(score(item.base_threshold))}</dd></div><div><dt>应用情绪后</dt><dd>${escapeHtml(score(item.final_threshold))}</dd></div><div><dt>变化</dt><dd>${escapeHtml(delta)}</dd></div></dl><section><h3>情绪坐标</h3><p>语气：${escapeHtml(String(item.tone_label || "未标注"))}</p><dl class="emotion-detail-coordinates"><div><dt>愉悦度</dt><dd>${escapeHtml(score(item.valence))}</dd></div><div><dt>唤醒度</dt><dd>${escapeHtml(score(item.arousal))}</dd></div><div><dt>支配度</dt><dd>${escapeHtml(score(item.dominance))}</dd></div></dl></section><details><summary>查看写入主动流程的提示词</summary><pre>${escapeHtml(String(item.prompt_section || "-"))}</pre></details><details><summary>查看技术元数据</summary><pre>${escapeHtml(JSON.stringify(item.metadata || {}, null, 2))}</pre></details>`;
}

async function json(ctx, path, signal) {
  const response = await ctx.http.request(path, {method: "GET", signal});
  const body = await response.json();
  if (!response.ok) throw new Error(body?.detail || body?.message || `HTTP ${response.status}`);
  return body;
}

function effectLabel(value) {
  return ({raise_send_bar: "提高发送阈值", lower_send_bar: "降低发送阈值"})[value] || String(value || "-");
}

function score(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(3) : "-";
}

function deltaText(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number > 0 ? `+${number.toFixed(3)}` : number.toFixed(3);
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function shortTime(value) {
  const date = new Date(String(value || ""));
  return Number.isNaN(date.getTime()) ? "-" : new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(date);
}

function showError(target, reason) {
  target.setAttribute("role", "alert");
  target.textContent = reason instanceof Error ? reason.message : String(reason);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character]);
}
