/* Local, event-driven voice surface. No rendering loop or agent implementation. */
const byId = id => document.getElementById(id);
const activity = byId('activity');
const audio = byId('reply');
const mic = byId('microphone');
let busy = false;
let recording = null;
let audioUrl = null;
let permissionId = null;
let permissionTimer;

function append(speaker, text) {
  if (!text) return;
  const line = document.createElement('p');
  line.textContent = `${speaker}: ${text}`;
  const log = byId('transcript');
  log.append(line);
  while (log.children.length > 100) log.firstElementChild.remove();
  log.scrollTop = log.scrollHeight;
}

function setBusy(value) {
  busy = value;
  byId('send').disabled = value;
  mic.disabled = value;
}

async function submit(url, options) {
  if (busy) return;
  setBusy(true);
  audio.pause();
  activity.textContent = 'Thinking…';
  try {
    const response = await fetch(url, { method: 'POST', ...options });
    if (response.status === 204) {
      activity.textContent = 'No speech detected. Try again.';
      return;
    }
    if (!response.ok) throw new Error(`Request failed (${response.status}).`);
    append('You', decodeURIComponent(response.headers.get('X-User-Transcript') || ''));
    append('Hermes Hal', decodeURIComponent(response.headers.get('X-Hal-Transcript') || ''));
    const blob = await response.blob();
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = URL.createObjectURL(blob);
    audio.src = audioUrl;
    activity.textContent = 'Reply ready.';
    try { await audio.play(); } catch { activity.textContent = 'Tap play to hear the reply.'; }
  } catch (error) {
    activity.textContent = error.message;
  } finally { setBusy(false); }
}

byId('composer').addEventListener('submit', event => {
  event.preventDefault();
  const input = byId('message');
  const text = input.value.trim();
  if (!text || busy || recording) return;
  input.value = '';
  void submit('/api/say', {
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
  });
});

mic.addEventListener('click', async () => {
  if (recording) { recording.stop(); return; }
  if (busy) return;
  mic.disabled = true;
  byId('send').disabled = true;
  let stream;
  try {
    audio.pause();
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const recorder = new MediaRecorder(stream);
    recording = recorder;
    const chunks = [];
    const timer = setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, 60000);
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = () => {
      clearTimeout(timer);
      stream.getTracks().forEach(track => track.stop());
      recording = null;
      mic.textContent = 'Record message';
      mic.setAttribute('aria-pressed', 'false');
      byId('send').disabled = false;
      const data = new FormData();
      data.append('audio', new Blob(chunks, { type: recorder.mimeType }), 'recording.webm');
      void submit('/api/talk', { body: data });
    };
    recorder.start();
    mic.textContent = 'Finish recording';
    mic.setAttribute('aria-pressed', 'true');
    byId('send').disabled = true;
    activity.textContent = 'Recording — tap finish when done (60 seconds maximum).';
  } catch (error) {
    stream?.getTracks().forEach(track => track.stop());
    recording = null;
    activity.textContent = `Microphone unavailable: ${error.message}`;
  } finally {
    mic.disabled = busy;
    byId('send').disabled = busy || Boolean(recording);
  }
});

byId('stop').addEventListener('click', () => {
  audio.pause();
  activity.textContent = 'Playback stopped. Agent work continues.';
});

function hidePermission() {
  permissionId = null;
  clearTimeout(permissionTimer);
  byId('permission').hidden = true;
}

const events = new EventSource('/api/events');
events.onopen = () => { byId('connection').textContent = 'Connected to frontend'; };
events.onerror = () => {
  byId('connection').textContent = 'Connection interrupted — reconnecting…';
  hidePermission();
};
events.onmessage = event => {
  let message;
  try { message = JSON.parse(event.data); } catch { return; }
  if (message.type === 'permission_request') {
    hidePermission();
    permissionId = message.request_id;
    byId('permission-title').textContent = `Allow tool: ${message.title || 'requested action'}?`;
    byId('permission').hidden = false;
    permissionTimer = setTimeout(hidePermission, (message.timeout || 30) * 1000);
  } else if (['permission_resolved', 'permission_denied'].includes(message.type)) {
    if (message.request_id === permissionId) hidePermission();
  } else if (['tool_call', 'tool_call_update'].includes(message.type)) {
    activity.textContent = `${message.title || 'Tool'}: ${message.status || 'working'}`;
  }
};

async function decide(decision) {
  const id = permissionId;
  if (!id) return;
  hidePermission();
  try {
    const response = await fetch(`/api/permission/${encodeURIComponent(id)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision }),
    });
    if (!response.ok) throw new Error('Permission expired or could not be delivered.');
    activity.textContent = decision === 'allow' ? 'Permission granted.' : 'Permission denied.';
  } catch (error) { activity.textContent = error.message; }
}
byId('allow').onclick = () => void decide('allow');
byId('deny').onclick = () => void decide('deny');
window.addEventListener('pagehide', () => {
  events.close();
  if (recording) { recording.onstop = null; recording.stream.getTracks().forEach(t => t.stop()); }
  audio.pause();
  if (audioUrl) URL.revokeObjectURL(audioUrl);
});
window.addEventListener('pageshow', event => {
  if (event.persisted) location.reload();
});
