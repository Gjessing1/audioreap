/* ── Audio player ────────────────────────────────────────────────────────── */
const audio    = document.getElementById("audio-el");
const playerBar= document.getElementById("player-bar");
const playerTitle = document.getElementById("player-title");
const playerPlayBtn = document.getElementById("player-play");
const playerProgress = document.getElementById("player-progress");

let currentId = null;

window.playTrack = function(internalId, title) {
  if (currentId === internalId) {
    togglePlay();
    return;
  }
  currentId = internalId;
  audio.src = "/api/stream/" + internalId;
  playerTitle.textContent = title;
  playerBar.classList.remove("hidden");
  audio.play().catch(() => {});
  updatePlayBtns();
};

function togglePlay() {
  if (audio.paused) audio.play().catch(() => {});
  else audio.pause();
}

if (playerPlayBtn) {
  playerPlayBtn.addEventListener("click", togglePlay);
}

if (audio) {
  audio.addEventListener("play", () => {
    playerPlayBtn && (playerPlayBtn.textContent = "⏸");
    updatePlayBtns();
  });
  audio.addEventListener("pause", () => {
    playerPlayBtn && (playerPlayBtn.textContent = "▶");
  });
  audio.addEventListener("timeupdate", () => {
    if (audio.duration && playerProgress) {
      playerProgress.value = (audio.currentTime / audio.duration) * 100;
    }
  });
  audio.addEventListener("ended", () => {
    currentId = null;
    updatePlayBtns();
  });
}

if (playerProgress) {
  playerProgress.addEventListener("input", () => {
    if (audio.duration) audio.currentTime = (playerProgress.value / 100) * audio.duration;
  });
}

function updatePlayBtns() {
  document.querySelectorAll(".play-btn[data-id]").forEach((btn) => {
    btn.classList.toggle("playing", btn.dataset.id === currentId);
    btn.textContent = (btn.dataset.id === currentId && !audio.paused) ? "⏸" : "▶";
  });
}

/* Re-attach after HTMX swaps */
document.body.addEventListener("htmx:afterSwap", () => updatePlayBtns());

window.playPreview = function(ref, title) {
  const previewUrl = "/api/preview?ref=" + encodeURIComponent(ref);
  if (currentId === "preview:" + ref) {
    togglePlay();
    return;
  }
  currentId = "preview:" + ref;
  audio.src = previewUrl;
  playerTitle.textContent = title + " (preview)";
  playerBar.classList.remove("hidden");
  audio.play().catch(() => {});
  updatePlayBtns();
};

/* ── Acquire (bypass hx-vals JSON-in-JSON problem) ───────────────────────── */
window.acquireTrack = async function(btn) {
  const targetId = btn.dataset.target;
  const target = document.getElementById(targetId);
  if (!target) return;

  btn.disabled = true;
  btn.textContent = "Queuing…";

  try {
    const resp = await fetch("/api/acquire", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "HX-Request": "true",          // tell server to return HTML card
      },
      body: JSON.stringify({
        provider_name: "ytdlp",
        provider_ref: btn.dataset.ref,
        candidate_json: btn.dataset.json,
        query: btn.dataset.query || "",
      }),
    });
    const html = await resp.text();
    target.outerHTML = html;
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Acquire";
    console.error("Acquire failed:", err);
  }
};

/* ── Play staged (review) files ─────────────────────────────────────────── */
window.playJobStaged = function(jobId, title) {
  const key = 'staged:' + jobId;
  if (currentId === key) { togglePlay(); return; }
  currentId = key;
  audio.src = '/jobs/' + jobId + '/stream';
  playerTitle.textContent = title + ' (staged)';
  playerBar.classList.remove('hidden');
  audio.play().catch(() => {});
  updatePlayBtns();
};

/* ── Toggle panels mutually exclusive ───────────────────────────────────── */
window.togglePanel = function(showId, hideId) {
  const show = document.getElementById(showId);
  const hide = document.getElementById(hideId);
  if (hide) hide.classList.add('hidden');
  if (show) show.classList.toggle('hidden');
};

/* ── Batch approve checkboxes ────────────────────────────────────────────── */
function _updateBatchCount() {
  const toolbar = document.getElementById('batch-toolbar');
  const label   = document.getElementById('batch-count-label');
  if (!toolbar) return;
  const checked = document.querySelectorAll('.job-check:checked').length;
  if (checked > 0) {
    toolbar.classList.remove('hidden');
    label.textContent = checked + ' selected';
  } else {
    toolbar.classList.add('hidden');
  }
}

/* Event delegation — no inline handlers needed, works after HTMX swaps */
document.body.addEventListener('change', function(e) {
  if (e.target.classList.contains('job-check')) _updateBatchCount();
});

window.clearJobChecks = function() {
  document.querySelectorAll('.job-check').forEach(cb => { cb.checked = false; });
  _updateBatchCount();
};

window.selectAllReview = function() {
  document.querySelectorAll('.job-check').forEach(cb => { cb.checked = true; });
  _updateBatchCount();
};

/* Reset after HTMX swaps */
document.body.addEventListener('htmx:afterSwap', () => {
  _updateBatchCount();
  updatePlayBtns();
});

/* ── Library MB apply (fills edit form fields without full page swap) ─────── */
window.applyMbToLibraryEditor = function(trackId, recordingId, title, artist, album, year) {
  const card = document.getElementById('browse-' + trackId);
  if (!card) return;
  const set = (name, val) => {
    const el = card.querySelector('[name="' + name + '"]');
    if (el && val) el.value = val;
  };
  set('mb_recording_id', recordingId);
  set('title', title);
  set('artist', artist);
  set('album', album);
  set('year', year);
  // Close the MB panel
  const panel = document.getElementById('mb-edit-panel-' + trackId);
  if (panel) panel.classList.add('hidden');
};

/* ── Review card keyboard shortcuts (a=approve, r=reject, s=MB search, p=play) ── */
let _hoveredCard = null;

document.body.addEventListener("mouseover", function(e) {
  const card = e.target.closest(".card-review");
  if (card) _hoveredCard = card;
});
document.body.addEventListener("mouseleave", function(e) {
  if (!e.relatedTarget || !e.relatedTarget.closest(".card-review")) _hoveredCard = null;
}, true);

document.addEventListener("keydown", function(e) {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const card = _hoveredCard;
  if (!card) return;

  if (e.key === "a") {
    e.preventDefault();
    const btn = card.querySelector(".rv-approve");
    if (btn && !btn.disabled) btn.click();
  } else if (e.key === "r") {
    e.preventDefault();
    const btn = card.querySelector(".rv-reject");
    if (btn) btn.click();
  } else if (e.key === "s") {
    e.preventDefault();
    const mbBtn = Array.from(card.querySelectorAll(".rv-actions button")).find(b => b.textContent.trim().startsWith("Search MB"));
    if (mbBtn) mbBtn.click();
  } else if (e.key === "p") {
    e.preventDefault();
    const playBtn = Array.from(card.querySelectorAll(".rv-actions button")).find(b => b.textContent.trim().startsWith("▶"));
    if (playBtn) playBtn.click();
  }
});

/* ── Service worker registration ─────────────────────────────────────────── */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}
