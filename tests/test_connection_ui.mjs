import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8")
  .replace(/^import[^\n]*\n/, "");

class Element {
  constructor(attributes = "") {
    this.hidden = /\bhidden\b/.test(attributes);
    this.disabled = /\bdisabled\b/.test(attributes);
    this.checked = /\bchecked\b/.test(attributes);
    this.value = "";
    this.textContent = "";
    this.children = [];
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  async dispatch(name) {
    if (name === "click" && (this.disabled || this.hidden)) return;
    return this.listeners.get(name)?.();
  }
}

async function harness(options = {}) {
  const storage = options.storage || new Map();
  const elements = new Map();
  for (const match of html.matchAll(/<[a-z][\w-]*\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    elements.set(match[2], new Element(match[1]));
  }
  const state = {
    requests: [], sdkCalls: 0, streamCalls: 0, cameraCalls: 0, greetings: [],
    preparationFails: false, sessionError: null, sdkFails: false, restoreError: options.restoreError || null,
  };
  const event = {
    SESSION_READY: "ready", CONNECTION_CLOSED: "closed", USER_SPEECH_ENDED: "speech",
    MESSAGE_HISTORY_UPDATED: "history", TALK_STREAM_INTERRUPTED: "interrupted",
  };
  const context = vm.createContext({
    console: { error() {}, warn() {} },
    document: {
      getElementById: id => elements.get(id),
      createElement: () => new Element(),
    },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    Option: function (label, value) { this.label = label; this.value = value; },
    navigator: { mediaDevices: { getUserMedia: async () => {
      state.cameraCalls++;
      return { getTracks: () => [{ stop() {} }] };
    } } },
    fetch: async (path, requestOptions) => {
      assert.ok(!path.startsWith("/"), "API requests must preserve the deployment subpath");
      const body = requestOptions?.body ? JSON.parse(requestOptions.body) : null;
      state.requests.push({ path, body });
      let status = 200;
      let data;
      if (path === "api/config") {
        data = { providers: ["openai", "claude"], default_provider: "openai" };
      } else if (path.startsWith("api/preparations/")) {
        if (state.restoreError) {
          status = state.restoreError;
          data = { detail: "保存データを復元できません。" };
        } else {
          data = {
            preparation_id: decodeURIComponent(path.split("/").at(-1)),
            provider: options.restoredProvider || "openai", news: options.news || null, warnings: [],
          };
        }
      } else if (path === "api/prepare") {
        if (state.preparationFails) {
          status = 503;
          data = { detail: "話題を準備できませんでした。" };
        } else {
          data = { preparation_id: "prepared", news: options.news || null, warnings: [] };
        }
      } else if (path === "api/session") {
        if (state.sessionError) {
          status = state.sessionError;
          data = { detail: status === 409 ? "接続準備からやり直してください。" : "接続に失敗しました。" };
        } else {
          data = {
            conversation_id: "conversation", session_token: "test-token", persona_name: "テスト",
            llm: "test-model", news: null, greeting: body.conversation_id ? null : "準備した話題です。",
          };
        }
      } else {
        throw new Error(`Unexpected request: ${path}`);
      }
      return { ok: status === 200, status, json: async () => data };
    },
    AnamEvent: event,
    createClient: () => {
      state.sdkCalls++;
      const listeners = new Map();
      return {
        addListener: (name, callback) => listeners.set(name, callback),
        streamToVideoElement: async () => {
          state.streamCalls++;
          if (state.sdkFails) throw new Error("人物に接続できませんでした。");
          listeners.get(event.SESSION_READY)?.();
        },
        stopStreaming: async () => listeners.get(event.CONNECTION_CLOSED)?.(),
        talk: text => state.greetings.push(text),
        muteInputAudio() {}, unmuteInputAudio() {},
      };
    },
  });
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return { state, storage, element: id => elements.get(id), click: id => elements.get(id).dispatch("click") };
}

test("preparation completes before a separate click can connect", async () => {
  const ui = await harness();
  assert.equal(ui.element("prepare-btn").disabled, false);
  assert.equal(ui.element("start-btn").hidden, true);
  await ui.click("prepare-btn");
  assert.equal(ui.element("start-btn").hidden, false);
  assert.equal(ui.element("start-btn").disabled, false);
  assert.equal(ui.state.sdkCalls, 0);
  assert.equal(ui.state.cameraCalls, 0);
  assert.deepEqual(ui.state.requests.map(request => request.path), ["api/config", "api/prepare"]);

  await ui.click("start-btn");
  assert.equal(ui.state.sdkCalls, 1);
  assert.equal(ui.state.streamCalls, 1);
  assert.equal(ui.state.cameraCalls, 1);
  assert.equal(ui.state.requests.at(-1).body.preparation_id, "prepared");
  assert.deepEqual(ui.state.greetings, ["準備した話題です。"]);
  assert.equal(ui.element("prepare-btn").disabled, true);
  assert.equal(ui.element("stop-btn").disabled, false);
});

test("all prepared topics beyond the opening are shown with their source links", async () => {
  const items = Array.from({ length: 7 }, (_, index) => ({
    summary: `話題${index + 1}です。`,
    source: { title: `Report ${index}`, publisher: "Full Disclosure", url: `https://seclists.org/fulldisclosure/2026/Oct/${index}` },
  }));
  const ui = await harness({ news: { as_of: "2026-10-18T04:00:00+09:00", items } });
  await ui.click("prepare-btn");
  assert.equal(ui.element("news-summary").children.length, 7);
  assert.equal(ui.element("news-sources").children.length, 7);
  assert.equal(ui.element("news-summary").children[6].textContent, "話題7です。");
  assert.equal(ui.element("news-sources").children[6].children[0].href, items[6].source.url);
  assert.match(ui.element("news-heading").textContent, /7件/);
  assert.equal(ui.state.sdkCalls, 0);
});

test("fallback policy and original publication date are shown for old posts", async () => {
  const items = [{ summary: "脆弱性が報告されました。", source: {
    title: "Report", publisher: "Full Disclosure", url: "https://seclists.org/fulldisclosure/2026/Oct/1",
    published_at: "2026-10-15T07:43:38Z",
  } }];
  const ui = await harness({ news: { as_of: "2026-10-18T04:00:00+09:00", selection_policy: "per_source_24h_or_latest_5", items } });
  await ui.click("prepare-btn");
  assert.match(ui.element("news-heading").textContent, /取得元ごとに最新5件/);
  const source = ui.element("news-sources").children[0].children[0];
  assert.match(source.textContent, /2026\/10\/15/);
  assert.match(source.textContent, /16:43:38/);
});

test("explicit API hours and per-source limit are shown without the legacy fallback", async () => {
  const ui = await harness({ news: {
    as_of: "2026-10-18T04:00:00+09:00", selection_policy: "per_source_window", hours: 48, limit: 10, items: [],
  } });
  await ui.click("prepare-btn");
  const heading = ui.element("news-heading").textContent;
  assert.match(heading, /過去48時間/);
  assert.match(heading, /取得元ごとに最大10件/);
  assert.doesNotMatch(heading, /不足時/);
});

test("failed preparation keeps connection hidden and allows another preparation", async () => {
  const ui = await harness();
  ui.state.preparationFails = true;
  await ui.click("prepare-btn");
  assert.equal(ui.element("start-btn").hidden, true);
  assert.equal(ui.element("prepare-btn").disabled, false);
  assert.match(ui.element("status").textContent, /準備に失敗/);
  assert.equal(ui.state.sdkCalls, 0);
  ui.state.preparationFails = false;
  await ui.click("prepare-btn");
  assert.equal(ui.element("start-btn").hidden, false);
});

test("changing model invalidates prepared data", async () => {
  const ui = await harness();
  await ui.click("prepare-btn");
  ui.element("provider").value = "claude";
  await ui.element("provider").dispatch("change");
  assert.equal(ui.element("start-btn").hidden, true);
  assert.equal(ui.storage.has("si-human.preparationId"), false);
  await ui.click("prepare-btn");
  assert.equal(ui.state.requests.at(-1).body.provider, "claude");
});

test("missing server preparation returns the UI to the first step", async () => {
  const ui = await harness();
  await ui.click("prepare-btn");
  ui.state.sessionError = 409;
  await ui.click("start-btn");
  assert.equal(ui.element("start-btn").hidden, true);
  assert.equal(ui.element("prepare-btn").disabled, false);
  assert.equal(ui.state.sdkCalls, 0);
  assert.match(ui.element("status").textContent, /接続準備から/);
});

test("transient connection failure retries without repeating preparation", async () => {
  const ui = await harness();
  await ui.click("prepare-btn");
  ui.state.sessionError = 500;
  await ui.click("start-btn");
  assert.equal(ui.element("start-btn").hidden, false);
  assert.equal(ui.element("start-btn").disabled, false);
  ui.state.sessionError = null;
  await ui.click("start-btn");
  assert.equal(ui.state.requests.filter(request => request.path === "api/prepare").length, 1);
  assert.equal(ui.state.sdkCalls, 1);
});

test("reconnecting the same conversation does not repeat preparation or opening", async () => {
  const ui = await harness();
  await ui.click("prepare-btn");
  await ui.click("start-btn");
  await ui.click("stop-btn");
  assert.equal(ui.element("start-btn").textContent, "続きから接続");
  await ui.click("start-btn");
  assert.equal(ui.state.requests.at(-1).body.conversation_id, "conversation");
  assert.equal(ui.state.requests.filter(request => request.path === "api/prepare").length, 1);
  assert.equal(ui.state.sdkCalls, 2);
  assert.deepEqual(ui.state.greetings, ["準備した話題です。"]);
});

test("a failed avatar connection can retry and still read the prepared opening", async () => {
  const ui = await harness();
  await ui.click("prepare-btn");
  ui.state.sdkFails = true;
  await ui.click("start-btn");
  assert.equal(ui.element("start-btn").disabled, false);
  assert.deepEqual(ui.state.greetings, []);
  ui.state.sdkFails = false;
  await ui.click("start-btn");
  assert.equal(ui.state.requests.at(-1).body.conversation_id, "conversation");
  assert.equal(ui.state.requests.filter(request => request.path === "api/prepare").length, 1);
  assert.deepEqual(ui.state.greetings, ["準備した話題です。"]);
});

test("reloading restores in-memory preparation by ID without preparing or connecting again", async () => {
  const first = await harness();
  await first.click("prepare-btn");
  assert.equal(first.storage.get("si-human.preparationId"), "prepared");
  const reopened = await harness({ storage: first.storage, restoredProvider: "claude" });
  assert.deepEqual(reopened.state.requests.map(request => request.path), ["api/config", "api/preparations/prepared"]);
  assert.equal(reopened.element("start-btn").hidden, false);
  assert.equal(reopened.element("provider").value, "claude");
  assert.match(reopened.element("status").textContent, /復元しました/);
  assert.equal(reopened.state.sdkCalls, 0);
  assert.equal(reopened.state.cameraCalls, 0);
});

test("missing or consumed saved data is cleared without connecting", async () => {
  for (const status of [404, 409]) {
    const storage = new Map([["si-human.preparationId", "old-preparation"]]);
    const ui = await harness({ storage, restoreError: status });
    assert.equal(ui.element("start-btn").hidden, true);
    assert.equal(ui.element("prepare-btn").disabled, false);
    assert.equal(storage.has("si-human.preparationId"), false);
    assert.equal(ui.state.sdkCalls, 0);
  }
});

test("temporary restoration failure keeps the saved ID for retry", async () => {
  const storage = new Map([["si-human.preparationId", "saved-preparation"]]);
  const ui = await harness({ storage, restoreError: 503 });
  assert.equal(ui.element("start-btn").hidden, true);
  assert.equal(ui.element("prepare-btn").disabled, false);
  assert.equal(storage.get("si-human.preparationId"), "saved-preparation");
});

test("saved preparation is cleared only after the connection request succeeds", async () => {
  const ui = await harness();
  await ui.click("prepare-btn");
  ui.state.sessionError = 500;
  await ui.click("start-btn");
  assert.equal(ui.storage.get("si-human.preparationId"), "prepared");
  ui.state.sessionError = null;
  await ui.click("start-btn");
  assert.equal(ui.storage.has("si-human.preparationId"), false);
});
