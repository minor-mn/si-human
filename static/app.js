import { createClient, AnamEvent } from "https://cdn.jsdelivr.net/npm/@anam-ai/js-sdk@4.27.1/+esm";

const $ = (id) => document.getElementById(id);
const startBtn = $("start-btn");
const stopBtn = $("stop-btn");
const muteBtn = $("mute-btn");
const statusEl = $("status");
const providerSelect = $("provider");

let anamClient = null;
let conversationId = null; // 再接続しても同じ会話を続けるために保持
let cameraStream = null;
let lastHandledMessageId = null;
let inFlight = null; // { controller, correlationId }
let speechEndedAt = null;
let muted = false;
let llmName = "";

async function loadConfig() {
  const config = await (await fetch("/api/config")).json();
  providerSelect.replaceChildren(
    ...config.providers.map((p) => new Option(p === "claude" ? "Claude" : "OpenAI", p)),
  );
  providerSelect.value = config.default_provider;
}

function setStatus(text) {
  statusEl.textContent = text;
}

async function startCall() {
  startBtn.disabled = true;
  setStatus("接続中…");

  const res = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId, provider: providerSelect.value }),
  });
  if (!res.ok) {
    setStatus(`セッション作成に失敗しました (${res.status})`);
    startBtn.disabled = false;
    return;
  }
  const session = await res.json();
  conversationId = session.conversation_id;
  llmName = session.llm;
  providerSelect.disabled = true;

  anamClient = createClient(session.session_token);
  anamClient.addListener(AnamEvent.SESSION_READY, () => {
    setStatus(`${session.persona_name} と通話中（${llmName}）`);
    stopBtn.disabled = false;
    muteBtn.disabled = false;
    if (session.greeting) anamClient.talk(session.greeting);
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
    inFlight?.controller.abort();
    anamClient = null;
    stopCamera(); // マイクは SDK が止めるが、カメラはこちらで取得したので自分で止める
    setStatus("通話が終了しました（「通話を開始」で続きから再開できます。LLM を変えても履歴は引き継がれます）");
    startBtn.disabled = false;
    providerSelect.disabled = false;
    stopBtn.disabled = true;
    muteBtn.disabled = true;
  });

  // マイクの取得と WebRTC 接続は SDK が行う
  await anamClient.streamToVideoElement("persona-video");
  await startCamera();
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
    const res = await fetch("/api/chat", {
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
    ...messages.map((m) => {
      const li = document.createElement("li");
      li.className = m.role;
      li.textContent = m.content + (m.interrupted ? "（中断）" : "");
      return li;
    }),
  );
  list.parentElement.scrollTop = list.parentElement.scrollHeight;
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

startBtn.addEventListener("click", () => startCall().catch((err) => {
  console.error(err);
  // Anam SDK のエラーは、サーバーが返した具体的な理由を details.cause に持つ
  const cause = err.details?.cause;
  setStatus(`エラー: ${err.message}${cause ? `（${typeof cause === "string" ? cause : JSON.stringify(cause)}）` : ""}`);
  startBtn.disabled = false;
}));

stopBtn.addEventListener("click", () => anamClient?.stopStreaming());

muteBtn.addEventListener("click", () => {
  if (!anamClient) return;
  muted = !muted;
  if (muted) anamClient.muteInputAudio();
  else anamClient.unmuteInputAudio();
  muteBtn.textContent = muted ? "ミュート解除" : "マイクをミュート";
});
