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
window.togglePanel = function(showId, ...hideIds) {
  const show = document.getElementById(showId);
  hideIds.forEach(function(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('hidden');
  });
  if (show) show.classList.toggle('hidden');
};

/* ── Batch approve checkboxes ────────────────────────────────────────────── */
/* Selected job IDs survive the 12s job-list polling swap (innerHTML wipes the
 * DOM checkboxes, so we re-apply from this set after each swap). */
const _selectedJobs = new Set();

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
  if (e.target.classList.contains('job-check')) {
    if (e.target.checked) _selectedJobs.add(e.target.value);
    else _selectedJobs.delete(e.target.value);
    _updateBatchCount();
  }
});

window.clearJobChecks = function() {
  document.querySelectorAll('.job-check').forEach(cb => { cb.checked = false; });
  _selectedJobs.clear();
  _updateBatchCount();
};

window.selectAllReview = function() {
  document.querySelectorAll('.job-check').forEach(cb => {
    cb.checked = true;
    _selectedJobs.add(cb.value);
  });
  _updateBatchCount();
};

/* Reset after HTMX swaps */
document.body.addEventListener('htmx:afterSwap', () => {
  // Restore selection a poll-driven innerHTML swap would otherwise have wiped,
  // then prune IDs whose checkbox is gone (track left the review queue).
  const present = new Set();
  document.querySelectorAll('.job-check').forEach(cb => {
    cb.checked = _selectedJobs.has(cb.value);
    present.add(cb.value);
  });
  _selectedJobs.forEach(v => { if (!present.has(v)) _selectedJobs.delete(v); });
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

/* ── HTMX global error handler ───────────────────────────────────────────── */
document.addEventListener("htmx:responseError", function(e) {
  const status = e.detail.xhr.status;
  console.error("HTMX request failed:", status);
  if (status >= 400) {
    const toast = document.createElement("div");
    toast.style.cssText = "position:fixed;bottom:20px;right:16px;z-index:9999;max-width:340px;background:var(--s1);border:1px solid var(--danger,#f87171);color:var(--danger,#f87171);padding:10px 14px;font-size:13px;border-radius:8px;display:flex;align-items:flex-start;gap:10px;box-shadow:0 4px 16px rgba(0,0,0,.4)";
    toast.innerHTML = `<span style="flex:1">Error ${status}${status >= 500 ? ' — check <a href="/health" style="color:inherit;text-decoration:underline">health</a>' : ''}</span><button onclick="this.parentElement.remove()" style="background:none;border:none;color:inherit;cursor:pointer;font-size:16px;line-height:1;flex-shrink:0">✕</button>`;
    document.body.appendChild(toast);
    setTimeout(function() { if (toast.parentElement) toast.remove(); }, 6000);
  }
});

/* ── Discography type filter (called from inline onclick in swapped content) */
window.toggleDiscoType = function(artistMbid, type, currentTypes) {
  var types = currentTypes ? currentTypes.split(',').filter(function(t){ return t; }) : [];
  var idx = types.indexOf(type);
  if (idx >= 0) { types.splice(idx, 1); } else { types.push(type); }
  var params = types.map(function(t){ return 'types=' + encodeURIComponent(t); }).join('&');
  htmx.ajax('GET', '/discography/' + artistMbid + (params ? '?' + params : ''), {target: '#disco-detail', swap: 'innerHTML'});
};

/* ── Discography text search (client-side filter within loaded releases) ─── */
window.filterDiscoReleases = function(q) {
  var lower = q.toLowerCase();
  document.querySelectorAll('#disco-detail [data-disco-title]').forEach(function(el) {
    var title = (el.getAttribute('data-disco-title') || '').toLowerCase();
    el.style.display = (!lower || title.indexOf(lower) !== -1) ? '' : 'none';
  });
};

/* ── Persisted <details> (open state survives HTMX poll swaps) ───────────────
 * Any <details data-persist-key="..."> remembers its open/closed state in
 * localStorage and re-applies it after the element is re-rendered (e.g. the
 * library stats poll every 30s). Default (no stored value) stays as authored.
 */
function applyPersistedDetails(root) {
  const scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll('details[data-persist-key]').forEach(function(d) {
    const key = 'details:' + d.getAttribute('data-persist-key');
    const stored = localStorage.getItem(key);
    if (stored !== null) d.open = (stored === '1');
    const chevron = d.querySelector('.persist-chevron');
    if (chevron) chevron.textContent = d.open ? '▼' : '▶';
    if (!d._persistBound) {
      d._persistBound = true;
      d.addEventListener('toggle', function() {
        localStorage.setItem(key, d.open ? '1' : '0');
        const ch = d.querySelector('.persist-chevron');
        if (ch) ch.textContent = d.open ? '▼' : '▶';
      });
    }
  });
}
document.addEventListener('DOMContentLoaded', function() { applyPersistedDetails(document); });
document.body.addEventListener('htmx:afterSettle', function(e) { applyPersistedDetails(e.target); });

/* ── Library: remember last in-place view, default to Tracks on entry ────────
 * Stat tiles load a view into #library-view via embed=1. We persist the last
 * tile's URL so revisiting /library reopens it; first-ever visit defaults to
 * the Tracks (browse) view instead of an empty pane.
 */
document.body.addEventListener('click', function(e) {
  const link = e.target.closest && e.target.closest('.stat-link');
  if (link) {
    const u = link.getAttribute('hx-push-url') || link.getAttribute('href');
    if (u) localStorage.setItem('library:lastView', u);
  }
});
document.addEventListener('DOMContentLoaded', function() {
  const view = document.getElementById('library-view');
  if (!view || view.children.length || typeof htmx === 'undefined') return;
  const last = localStorage.getItem('library:lastView') || '/library/browse';
  const url = last + (last.indexOf('?') >= 0 ? '&' : '?') + 'embed=1';
  htmx.ajax('GET', url, { target: '#library-view', swap: 'innerHTML' });
});

/* ── Service worker registration ─────────────────────────────────────────── */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}
