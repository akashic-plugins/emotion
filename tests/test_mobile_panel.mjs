import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../mobile_panel.js", import.meta.url), "utf8");
const panel = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

class FakeElement {
  constructor() {
    this.attributes = new Map();
    this.children = [];
    this.className = "";
    this.hidden = false;
    this.inert = false;
    this.listeners = new Map();
    this.textContent = "";
    this.classList = {
      add: (...names) => this.#setClasses(names, true),
      toggle: (name, enabled) => {
        this.#setClasses([name], enabled);
        return enabled;
      },
      contains: (name) => this.className.split(" ").includes(name),
    };
  }

  #setClasses(names, enabled) {
    const current = new Set(this.className.split(" ").filter(Boolean));
    for (const name of names) enabled ? current.add(name) : current.delete(name);
    this.className = Array.from(current).join(" ");
  }

  addEventListener(name, listener) { this.listeners.set(name, listener); }
  append(...children) { this.children.push(...children); }
  click() { this.listeners.get("click")?.(); }
  getAttribute(name) { return this.attributes.get(name); }
  remove() { this.removed = true; }
  setAttribute(name, value) { this.attributes.set(name, value); }
}

class BlockElement extends FakeElement {
  constructor() {
    super();
    this.strong = new FakeElement();
  }

  querySelector(selector) { return selector === "strong" ? this.strong : null; }
}

class DashboardHost extends FakeElement {
  constructor() {
    super();
    this.loading = new FakeElement();
    this.content = new FakeElement();
    this.content.hidden = true;
    this.state = new BlockElement();
    this.threshold = new BlockElement();
    this.feedback = new BlockElement();
    this.metricsTrigger = new FakeElement();
    this.metricsTrigger.setAttribute("aria-expanded", "false");
    this.metrics = new FakeElement();
    this.metrics.inert = true;
    this.metrics.setAttribute("aria-hidden", "true");
    this.metricValues = Object.fromEntries(["valence", "arousal", "dominance"].map((name) => [name, new FakeElement()]));
    this.total = new FakeElement();
    this.list = new FakeElement();
  }

  set innerHTML(_value) {}

  querySelector(selector) {
    if (selector.startsWith("[data-metric=")) {
      return this.metricValues[selector.match(/"(.+)"/)[1]];
    }
    return {
      ".emotion-mobile-loading": this.loading,
      ".emotion-mobile-content": this.content,
      ".emotion-mobile-state": this.state,
      ".emotion-mobile-threshold": this.threshold,
      ".emotion-mobile-feedback strong": this.feedback.strong,
      ".emotion-mobile-metrics-trigger": this.metricsTrigger,
      ".emotion-mobile-metrics": this.metrics,
      ".emotion-mobile-history header span": this.total,
      ".emotion-mobile-influences": this.list,
    }[selector];
  }
}

test("mobile navigation describes the emotion task instead of raw VAD", () => {
  assert.equal(panel.default.navigation.label, "主动状态");
  assert.match(panel.default.navigation.description, /语气和主动发送/);
  assert.equal(typeof panel.default.dashboard.mount, "function");
});

test("domain labels translate state into user tasks", () => {
  assert.equal(panel.toneLabel("bright_confident"), "轻快自信");
  assert.equal(panel.toneLabel("neutral"), "自然");
  assert.equal(panel.thresholdLabel("raise_send_bar"), "更谨慎");
  assert.equal(panel.thresholdLabel("lower_send_bar"), "更愿意主动");
  assert.equal(panel.influenceLabel("explicit_quote"), "明确引用");
});

test("mobile panel is plugin-owned and keeps raw metrics behind disclosure", () => {
  assert.match(source, /查看状态指标/);
  assert.match(source, /aria-hidden="true" inert/);
  assert.match(source, /最近影响/);
  assert.doesNotMatch(source, /window\.AkashicDashboard/);
});

test("dashboard keeps raw metrics inert until the user asks for them", async () => {
  globalThis.document = { createElement() { return new FakeElement(); } };
  const host = new DashboardHost();
  panel.default.dashboard.mount(host, {
    request(method) {
      if (method === "emotion.overview") {
        return Promise.resolve({
          state: { valence: 0.1, arousal: 0.2, dominance: 0.3 },
          current_behavior: { tone_label: "neutral", expected_effect: "tone_only" },
          influence_count: 1,
        });
      }
      return Promise.resolve({ items: [] });
    },
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(host.content.hidden, false);
  assert.equal(host.metrics.getAttribute("aria-hidden"), "true");
  assert.equal(host.metrics.inert, true);
  host.metricsTrigger.click();
  assert.equal(host.metricsTrigger.getAttribute("aria-expanded"), "true");
  assert.equal(host.metrics.getAttribute("aria-hidden"), "false");
  assert.equal(host.metrics.inert, false);
  assert.equal(host.metrics.classList.contains("is-expanded"), true);
});
