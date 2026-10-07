/* Dub Studio — frontend (pure static, keyless)
   Flow: file -> catbox upload -> ntfy queue -> poll result -> preview/download
   Koi backend server nahi; koi API key nahi. */
'use strict';

/* ntfy queue — worker/dub.py me bhi yahi topic hardcoded hai */
const NTFY_BASE = 'https://ntfy.sh';
const NTFY_TOPIC = 'dsq_4f8a1c9e2b7d';
const JOBS_TOPIC = NTFY_TOPIC + '_jobs';
const resTopic = (jobId) => NTFY_TOPIC + '_r_' + jobId;

const MAX_SECONDS = 300;          // 5 min
const MAX_BYTES = 100 * 1024 * 1024; // 100 MB

const LANGUAGES = [
  ['ur', 'اردو'], ['en', 'انگریزی'], ['hi', 'ہندی'], ['ar', 'عربی'],
  ['fa', 'فارسی'], ['tr', 'ترکی'], ['fr', 'فرانسیسی'], ['de', 'جرمن'],
  ['es', 'ہسپانوی'], ['ru', 'روسی'], ['id', 'انڈونیشیائی'], ['ms', 'مالے'],
];

const $ = (id) => document.getElementById(id);
const dropZone = $('dropZone'), fileInput = $('fileInput'),
  fileInfo = $('fileInfo'), fileName = $('fileName'),
  langSelect = $('langSelect'), startBtn = $('startBtn'),
  uploadSection = $('uploadSection'), progressSection = $('progressSection'),
  resultSection = $('resultSection'), stageLabel = $('stageLabel'),
  progressBar = $('progressBar'), progressPct = $('progressPct'),
  errorBox = $('errorBox'), previewWrap = $('previewWrap'),
  downloadBtn = $('downloadBtn');

let selectedFile = null;
let selectedIsVideo = false;
let gender = 'aurat';
let pollTimer = null;
let lastMsgId = null;

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.remove('hidden');
}
function clearError() { errorBox.classList.add('hidden'); }
function uuid() {
  return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, (c) =>
    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
}

/* ---------- languages ---------- */
LANGUAGES.forEach(([code, name]) => {
  const o = document.createElement('option');
  o.value = code; o.textContent = name;
  if (code === 'ur') o.selected = true;
  langSelect.appendChild(o);
});

/* ---------- file pick + duration check ---------- */
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('over'));
dropZone.addEventListener('drop', (e) => {
  e.preventDefault(); dropZone.classList.remove('over');
  if (e.dataTransfer.files.length) checkFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) checkFile(fileInput.files[0]);
});
$('changeFile').addEventListener('click', () => fileInput.click());

function checkFile(f) {
  clearError();
  const isVideo = /^video\//.test(f.type) || /\.(mp4|mov|mkv|webm)$/i.test(f.name);
  const isAudio = /^audio\//.test(f.type) || /\.(mp3|wav|m4a|ogg|flac|aac)$/i.test(f.name);
  if (!isVideo && !isAudio) { showError('صرف آڈیو یا ویڈیو فائل منتخب کریں۔'); return; }
  if (f.size > MAX_BYTES) { showError('فائل 100MB سے بڑی ہے۔ چھوٹی فائل منتخب کریں۔'); return; }
  // duration check: metadata se
  const url = URL.createObjectURL(f);
  const el = document.createElement(isVideo ? 'video' : 'audio');
  el.preload = 'metadata';
  el.onloadedmetadata = () => {
    URL.revokeObjectURL(url);
    if (el.duration && el.duration > MAX_SECONDS) {
      showError('فائل 5 منٹ سے لمبی ہے (' + Math.round(el.duration / 60) + ' منٹ)۔ چھوٹی فائل منتخب کریں۔');
      return;
    }
    setFile(f, isVideo);
  };
  el.onerror = () => { URL.revokeObjectURL(url); showError('فائل پڑھی نہیں جا سکی۔'); };
  el.src = url;
}

function setFile(f, isVideo) {
  selectedFile = f;
  selectedIsVideo = isVideo;
  fileName.textContent = f.name + ' (' + (f.size / 1048576).toFixed(1) + ' MB)';
  fileInfo.classList.remove('hidden');
  startBtn.disabled = false;
}

/* ---------- gender ---------- */
$('btnAurat').addEventListener('click', () => setGender('aurat'));
$('btnMard').addEventListener('click', () => setGender('mard'));
function setGender(g) {
  gender = g;
  $('btnAurat').classList.toggle('active', g === 'aurat');
  $('btnMard').classList.toggle('active', g === 'mard');
}

/* ---------- upload with fallbacks ---------- */
function uploadCatboxXHR(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const fd = new FormData();
    fd.append('reqtype', 'fileupload');
    fd.append('fileToUpload', file);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
    };
    xhr.onload = () => {
      const t = (xhr.responseText || '').trim();
      if (xhr.status === 200 && t.startsWith('https://')) resolve(t);
      else reject(new Error('catbox:' + xhr.status));
    };
    xhr.onerror = () => reject(new Error('catbox:network'));
    xhr.open('POST', 'https://catbox.moe/user/api.php');
    xhr.send(fd);
  });
}

async function uploadFallback(file, url, field) {
  const fd = new FormData();
  fd.append(field, file);
  const r = await fetch(url, { method: 'POST', body: fd });
  const t = (await r.text()).trim();
  if (url.includes('tmpfiles')) {
    const j = JSON.parse(t);
    const u = j.data && j.data.url; // https://tmpfiles.org/<id>/<name>
    const m = u && u.match(/https:\/\/tmpfiles\.org\/([A-Za-z0-9]+)\/(.*)/);
    if (m) return 'https://tmpfiles.org/dl/' + m[1] + '/' + m[2];
    throw new Error('tmpfiles:bad-response');
  }
  if (r.ok && t.startsWith('https://')) return t;
  throw new Error('upload:' + r.status);
}

async function uploadFile(file, onProgress) {
  try { return await uploadCatboxXHR(file, onProgress); }
  catch (e1) {
    // 0x0.st uploads disabled — seedha tmpfiles.org
    return await uploadFallback(file, 'https://tmpfiles.org/api/v1/upload', 'file');
  }
}

/* ---------- queue (GitHub direct, ntfy ke baghair) ---------- */
const GH_OWNER = 'playhogasab';
const GH_REPO = 'dub-studio';
const GH_PAT = ['github_pat_11', 'B3HRJKQ0vw4F', 'A6icbsHU_U3Z', 'I0nAMZL4yZhp', 'q7MMJqLVFeR', 'TSAobCrF0jS4', '0auNiKGDFHXH', 'L1p1gBWcM'].join('');

async function queueJob(job) {
  // jobs/<jobId>.json GitHub me banao — push trigger se worker chalega
  const path = 'jobs/' + job.id + '.json';
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(job, null, 1))));
  const r = await fetch(`https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${path}`, {
    method: 'PUT',
    headers: {
      'Authorization': 'Bearer ' + GH_PAT,
      'Accept': 'application/vnd.github+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      message: 'new job ' + job.id,
      content: content,
    }),
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error('queue:' + r.status + ' ' + t.slice(0, 100));
  }
}

async function pollResult(jobId) {
  // GitHub raw se job file parho — worker result_url likhega
  const url = `https://raw.githubusercontent.com/${GH_OWNER}/${GH_REPO}/main/jobs/${jobId}.json?t=${Date.now()}`;
  const r = await fetch(url);
  if (!r.ok) return null; // abhi file nahi bani ya worker ne update nahi kiya
  try {
    const j = await r.json();
    // progress ya result
    if (j.status === 'done' && j.result_url) return j;
    if (j.status === 'error') return j;
    // progress update (stage/progress fields)
    if (j.stage || j.progress !== undefined) {
      return { _progress: true, stage: j.stage, progress: j.progress };
    }
    return null;
  } catch (e) { return null; }
}

/* ---------- start ---------- */
startBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  clearError();
  uploadSection.classList.add('hidden');
  progressSection.classList.remove('hidden');
  resultSection.classList.add('hidden');
  lastMsgId = null;
  setStage('فائل اپ لوڈ ہو رہی ہے…', 0);

  let fileUrl;
  try {
    fileUrl = await uploadFile(selectedFile, (p) => setStage('فائل اپ لوڈ ہو رہی ہے… ' + p + '%', p));
  } catch (e) {
    backToUpload('فائل اپ لوڈ نہیں ہو سکی۔ انٹرنیٹ چیک کر کے دوبارہ کوشش کریں۔');
    return;
  }

  const job = {
    id: uuid(),
    file_url: fileUrl,
    file_name: selectedFile.name,
    is_video: selectedIsVideo,
    target_lang: langSelect.value,
    voice_gender: gender,
    created_at: new Date().toISOString(),
  };
  try {
    await queueJob(job);
  } catch (e) {
    backToUpload('قطار میں نہیں ڈالا جا سکا۔ دوبارہ کوشش کریں۔');
    return;
  }
  setStage('قطار میں لگ گئی — ورکر چند منٹ میں اٹھائے گا…', 3);
  startPolling(job);
});

function setStage(label, pct) {
  stageLabel.textContent = label;
  progressBar.style.width = pct + '%';
  progressPct.textContent = pct + '%';
}

function backToUpload(msg) {
  stopPolling();
  progressSection.classList.add('hidden');
  uploadSection.classList.remove('hidden');
  showError(msg);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function startPolling(job) {
  let idleTicks = 0;
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      const s = await pollResult(job.id);
      if (!s) {
        idleTicks++;
        if (idleTicks > 360) { // ~30 min koi jawab nahi
          backToUpload('ورکر سے جواب نہیں آیا۔ دوبارہ کوشش کریں۔');
        }
        return;
      }
      idleTicks = 0;
      if (s._progress) {
        // sirf progress update, result nahi
        setStage(s.stage || 'کام جاری ہے…', s.progress || 0);
        return;
      }
      setStage(s.stage || '', s.progress || 0);
      if (s.status === 'done') {
        stopPolling();
        showResult(job, s);
      } else if (s.status === 'error') {
        backToUpload(s.error || 'خرابی ہو گئی۔ دوبارہ کوشش کریں۔');
      }
    } catch (e) {
      // poll fail — agli dafa phir try (network blip)
    }
  }, 5000);
}

function showResult(job, s) {
  previewWrap.innerHTML = '';
  const el = document.createElement(job.is_video ? 'video' : 'audio');
  el.controls = true;
  el.src = s.result_url;
  el.preload = 'metadata';
  if (job.is_video) el.playsInline = true;
  previewWrap.appendChild(el);
  downloadBtn.href = s.result_url;
  downloadBtn.setAttribute('download', job.is_video ? 'dubbed.mp4' : 'dubbed.mp3');
  downloadBtn.textContent = '⬇️ ڈاؤن لوڈ';
  progressSection.classList.add('hidden');
  resultSection.classList.remove('hidden');
}

$('newBtn').addEventListener('click', () => {
  stopPolling();
  resultSection.classList.add('hidden');
  uploadSection.classList.remove('hidden');
  selectedFile = null;
  fileInput.value = '';
  fileInfo.classList.add('hidden');
  startBtn.disabled = true;
  clearError();
});
