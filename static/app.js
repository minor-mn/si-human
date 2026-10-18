import { createClient, AnamEvent } from "https://cdn.jsdelivr.net/npm/@anam-ai/js-sdk@4.27.1/+esm";

const $ = (id) => document.getElementById(id);
const prepareBtn = $("prepare-btn");
const startBtn = $("start-btn");
const stopBtn = $("stop-btn");
const muteBtn = $("mute-btn");
const statusEl = $("status");
const providerSelect = $("provider");
const PREPARATION_KEY = "si-human.preparationId";

let anamClient = null;
let preparationId = null;
let pendingGreeting = null; // 人物への接続に失敗した場合も、準備済みの挨拶を再試行で使う
let conversationId = null; // 再接続しても同じ会話を続けるために保持
let cameraStream = null;
let lastHandledMessageId = null;
let inFlight = null; // { controller, correlationId }
let speechEndedAt = null;
let muted = false;
let llmName = "";

async function loadConfig() {
  const config = await readJson(await fetch("api/config"));
  providerSelect.replaceChildren(
    ...config.providers.map((p) => new Option(p === "claude" ? "Claude" : "OpenAI", p)),
  );
  providerSelect.value = config.default_provider;
  prepareBtn.disabled = false;
  let savedId;
  try { savedId = localStorage.getItem(PREPARATION_KEY); } catch {}
  if (!savedId) return;
  prepareBtn.disabled = true;
  setStatus("保存した準備データを読み込んでいます…");
  try {
    const prepared = await readJson(await fetch(`api/preparations/${encodeURIComponent(savedId)}`));
    usePreparedData(prepared, true);
  } catch (error) {
    if (error.status === 404 || error.status === 409) rememberPreparation(null);
    showIdleControls();
    setStatus(`保存した準備データを読み込めませんでした: ${error.message}`);
  }
}

function setStatus(text) {
  statusEl.textContent = text;
}

function showIdleControls() {
  const canConnect = Boolean(preparationId || conversationId);
  prepareBtn.disabled = false;
  prepareBtn.textContent = conversationId ? "新しい話題を準備" : preparationId ? "準備し直す" : "接続準備";
  prepareBtn.classList.toggle("secondary", canConnect);
  startBtn.hidden = !canConnect;
  startBtn.disabled = !canConnect;
  startBtn.textContent = conversationId ? "続きから接続" : "接続する";
  providerSelect.disabled = false;
}

function rememberPreparation(id) {
  try {
    if (id) localStorage.setItem(PREPARATION_KEY, id);
    else localStorage.removeItem(PREPARATION_KEY);
  } catch {}
}

function usePreparedData(prepared, restored = false) {
  preparationId = prepared.preparation_id;
  rememberPreparation(preparationId);
  if (prepared.provider) providerSelect.value = prepared.provider;
  if (prepared.news) renderNews(prepared.news);
  showIdleControls();
  const warning = prepared.warnings?.length ? ` ${prepared.warnings.join(" ")}` : "";
  setStatus(`${restored ? "保存した準備データを復元しました。" : "準備ができました。"}「接続する」を押してください。${warning}`);
}

async function readJson(response) {
  if (!response.ok) {
    let message = `処理に失敗しました (${response.status})`;
    try {
      const error = await response.json();
      if (typeof error.detail === "string") message = error.detail;
    } catch {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function postJson(path, body) {
  return readJson(await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

async function prepareConnection() {
  preparationId = null;
  rememberPreparation(null);
  conversationId = null;
  pendingGreeting = null;
  prepareBtn.disabled = true;
  prepareBtn.textContent = "準備中…";
  startBtn.hidden = true;
  startBtn.disabled = true;
  providerSelect.disabled = true;
  $("news-panel").hidden = true;
  setStatus("話題とコメントを準備しています…");

  const prepared = await postJson("api/prepare", { provider: providerSelect.value });
  usePreparedData(prepared);
}

async function startCall() {
  startBtn.disabled = true;
  prepareBtn.disabled = true;
  providerSelect.disabled = true;
  setStatus("接続中…");

  let session;
  try {
    session = await postJson("api/session", {
      ...(conversationId ? { conversation_id: conversationId } : { preparation_id: preparationId }),
      provider: providerSelect.value,
    });
  } catch (error) {
    if (error.status === 409) {
      preparationId = null;
      rememberPreparation(null);
      conversationId = null;
      pendingGreeting = null;
      $("news-panel").hidden = true;
    }
    throw error;
  }
  preparationId = null;
  rememberPreparation(null);
  conversationId = session.conversation_id;
  if (session.greeting) pendingGreeting = session.greeting;
  llmName = session.llm;
  lastHandledMessageId = null;
  speechEndedAt = null;
  muted = false;
  muteBtn.textContent = "マイクをミュート";
  if (session.news) renderNews(session.news);
  providerSelect.disabled = true;

  const client = createClient(session.session_token);
  anamClient = client;
  anamClient.addListener(AnamEvent.SESSION_READY, () => {
    if (anamClient !== client) return;
    setStatus(`${session.persona_name} と通話中（${llmName}）`);
    stopBtn.disabled = false;
    muteBtn.disabled = false;
    if (pendingGreeting) {
      anamClient.talk(pendingGreeting);
      pendingGreeting = null;
    }
  });
  anamClient.addListener(AnamEvent.USER_SPEECH_ENDED, () => {
    speechEndedAt = performance.now();
  });
  anamClient.addListener(AnamEvent.MESSAGE_HISTORY_UPDATED, onHistoryUpdated);
  anamClient.addListener(AnamEvent.TALK_STREAM_INTERRUPTED, (correlationId) => {
    // ユーザーが話し始めたら、生成途中の Claude の応答も止める
    if (inFlight?.correlationId === correlationId) inFlight.controller.abort();
  });
  anamClient.addListener(AnamEvent.CONNECTION_CLOSED, () => {
    if (anamClient !== client) return;
    inFlight?.controller.abort();
    anamClient = null;
    stopCamera(); // マイクは SDK が止めるが、カメラはこちらで取得したので自分で止める
    setStatus("通話が終了しました。「続きから接続」で再開できます。");
    showIdleControls();
    stopBtn.disabled = true;
    muteBtn.disabled = true;
  });

  // マイクの取得と WebRTC 接続は SDK が行う
  await anamClient.streamToVideoElement("persona-video");
  await startCamera();
}

function renderNews(news) {
  $("news-panel").hidden = false;
  const asOf = new Date(news.as_of).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" });
  const period = news.selection_policy === "per_source_24h_or_latest_5"
    ? "24時間以内／不足時は取得元ごとに最新5件" : "過去24時間";
  $("news-heading").textContent = `ITセキュリティ・AIの話題 ${news.items.length}件（${asOf} 日本時間・${period}）`;
  const summary = $("news-summary");
  summary.replaceChildren();
  news.items.forEach((item, index) => {
    const paragraph = document.createElement("p");
    paragraph.textContent = item.summary;
    const link = document.createElement("a");
    link.href = item.source.url;
    link.textContent = `[${index + 1}]`;
    link.title = item.source.title;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    paragraph.append(" ", link);
    summary.append(paragraph);
  });
  $("news-sources").replaceChildren(
    ...news.items.map(({ source }, index) => {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = source.url;
      const published = source.published_at
        ? new Date(source.published_at).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" }) : null;
      link.textContent = `[${index + 1}] ${source.publisher}：${source.title}${published ? `（${published} 日本時間）` : ""}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      li.append(link);
      return li;
    }),
  );
}

async function startCamera() {
  if (cameraStream) return;
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
    $("self-video").srcObject = cameraStream;
  } catch (err) {
    console.warn("カメラを使えません", err);
    $("send-camera").checked = false;
  }
}

function stopCamera() {
  if (!cameraStream) return;
  cameraStream.getTracks().forEach((track) => track.stop());
  cameraStream = null;
  $("self-video").srcObject = null;
}

// 長辺 512px の JPEG にして送る（画像トークンと転送時間を抑える）
function captureFrame() {
  const video = $("self-video");
  if (!$("send-camera").checked || !cameraStream || video.videoWidth === 0) return null;
  const scale = 512 / Math.max(video.videoWidth, video.videoHeight);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.7).split(",")[1];
}

function onHistoryUpdated(messages) {
  renderTranscript(messages);
  const last = messages.at(-1);
  if (!last || last.role !== "user" || last.id === lastHandledMessageId) return;
  lastHandledMessageId = last.id;
  respond(last.content);
}

async function respond(userText) {
  inFlight?.controller.abort(); // 前の応答がまだ流れていれば打ち切る

  const controller = new AbortController();
  const correlationId = crypto.randomUUID();
  inFlight = { controller, correlationId };
  const talkStream = anamClient.createTalkMessageStream(correlationId);
  const t0 = speechEndedAt ?? performance.now();
  let firstChunk = true;

  try {
    const res = await fetch("api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId, text: userText, image_b64: captureFrame() }),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`chat failed: ${res.status}`);

    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!value) continue;
      if (firstChunk) {
        firstChunk = false;
        logLatency(performance.now() - t0);
      }
      if (!talkStream.isActive()) {
        controller.abort();
        break;
      }
      await talkStream.streamMessageChunk(value, false);
    }
    if (talkStream.isActive()) await talkStream.endMessage();
  } catch (err) {
    if (err.name !== "AbortError") {
      console.error(err);
      if (talkStream.isActive()) await talkStream.endMessage();
    }
  } finally {
    if (inFlight?.correlationId === correlationId) inFlight = null;
  }
}

function renderTranscript(messages) {
  const list = $("transcript");
  list.replaceChildren(
    ...[...messages].reverse().map((m) => {
      const li = document.createElement("li");
      li.className = m.role;
      li.textContent = m.content + (m.interrupted ? "（中断）" : "");
      return li;
    }),
  );
  list.parentElement.scrollTop = 0;
}

function logLatency(ms) {
  const li = document.createElement("li");
  li.textContent = `${(ms / 1000).toFixed(2)} 秒  ${llmName}`;
  $("latency").prepend(li);
}

loadConfig().catch((err) => setStatus(`設定の読み込みに失敗しました: ${err.message}`));

// 自分の映像の表示・非表示（ブラウザに記憶する。記憶できない環境では毎回表示から始まる）
const showSelf = $("show-self");
function applyShowSelf() {
  $("self-video").classList.toggle("hidden", !showSelf.checked);
}
try {
  showSelf.checked = localStorage.getItem("showSelf") !== "false";
} catch {}
applyShowSelf();
showSelf.addEventListener("change", () => {
  applyShowSelf();
  try {
    localStorage.setItem("showSelf", String(showSelf.checked));
  } catch {}
});

prepareBtn.addEventListener("click", () => prepareConnection().catch((err) => {
  console.error(err);
  showIdleControls();
  setStatus(`準備に失敗しました: ${err.message}`);
}));

providerSelect.addEventListener("change", () => {
  if (!preparationId) return;
  preparationId = null;
  rememberPreparation(null);
  $("news-panel").hidden = true;
  showIdleControls();
  setStatus("LLMを変更しました。「接続準備」を押してください。");
});

startBtn.addEventListener("click", () => startCall().catch(async (err) => {
  console.error(err);
  const client = anamClient;
  anamClient = null;
  if (client) {
    try { await client.stopStreaming(); } catch {}
  }
  stopCamera();
  stopBtn.disabled = true;
  muteBtn.disabled = true;
  // Anam SDK のエラーは、サーバーが返した具体的な理由を details.cause に持つ
  const cause = err.details?.cause;
  setStatus(`エラー: ${err.message}${cause ? `（${typeof cause === "string" ? cause : JSON.stringify(cause)}）` : ""}`);
  showIdleControls();
}));

stopBtn.addEventListener("click", () => anamClient?.stopStreaming());

muteBtn.addEventListener("click", () => {
  if (!anamClient) return;
  muted = !muted;
  if (muted) anamClient.muteInputAudio();
  else anamClient.unmuteInputAudio();
  muteBtn.textContent = muted ? "ミュート解除" : "マイクをミュート";
});
