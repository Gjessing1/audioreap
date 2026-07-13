/* ── Audio player ────────────────────────────────────────────────────────── */
const audio    = document.getElementById("audio-el");
const playerBar= document.getElementById("player-bar");
const playerTitle = document.getElementById("player-title");
const playerPlayBtn = document.getElementById("player-play");
const playerProgress = document.getElementById("player-progress");
const playerArt    = document.getElementById("player-art");
const playerArtImg = document.getElementById("player-art-img");
const playerPrev   = document.getElementById("player-prev");
const playerNext   = document.getElementById("player-next");

let currentId = null;

/* ── Play queue (derived from the DOM, never stored) ─────────────────────────
 * A "context" is the nearest [data-play-scope] ancestor of the play button that
 * matches the currently-playing id; the queue is every .play-btn[data-id] inside
 * it, in DOM order. Buttons are re-resolved fresh on every skip, so HTMX poll
 * swaps can't leave stale element references — if the current button vanished
 * (list re-rendered without it), there is simply no queue and playback just
 * stops at the end of the track, same as before queues existed.
 */
function _btnFor(id) {
  return Array.from(document.querySelectorAll(".play-btn[data-id]"))
    .find((b) => b.dataset.id === id) || null;
}

function _queueState() {
  const cur = currentId !== null ? _btnFor(currentId) : null;
  const scope = cur ? cur.closest("[data-play-scope]") : null;
  if (!scope) return { btns: [], idx: -1 };
  const btns = Array.from(scope.querySelectorAll(".play-btn[data-id]"))
    .filter((b) => !b.disabled);
  return { btns, idx: btns.indexOf(cur) };
}

function _updateSkipBtns() {
  if (!playerPrev || !playerNext) return;
  const { btns, idx } = _queueState();
  const inQueue = idx >= 0 && btns.length > 1;
  playerPrev.classList.toggle("hidden", !inQueue);
  playerNext.classList.toggle("hidden", !inQueue);
  if (inQueue) {
    playerPrev.disabled = idx === 0;
    playerNext.disabled = idx === btns.length - 1;
  }
}

/* Clicking the neighbour button re-enters the exact same inline handler the
 * user would have clicked, so per-kind behaviour (track/staged/preview) and
 * template escaping are reused rather than re-implemented here. */
function playerSkip(dir) {
  const { btns, idx } = _queueState();
  if (idx < 0) return false;
  const next = btns[idx + dir];
  if (!next) return false;
  next.click();
  return true;
}

function _setPlayerArt(url) {
  if (!playerArt || !playerArtImg) return;
  if (url) {
    playerArt.classList.remove("hidden");
    playerArtImg.src = url;   // onerror re-hides the container if it 404s
  } else {
    playerArt.classList.add("hidden");
    playerArtImg.removeAttribute("src");
  }
}

function _setMediaSession(title, artUrl) {
  if (!("mediaSession" in navigator)) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: title,
      artwork: artUrl ? [{ src: artUrl }] : [],
    });
    navigator.mediaSession.setActionHandler("previoustrack", () => playerSkip(-1));
    navigator.mediaSession.setActionHandler("nexttrack", () => playerSkip(1));
  } catch (e) { /* metadata is best-effort */ }
}

function _startPlayback(key, src, title, artUrl) {
  if (currentId === key) {
    togglePlay();
    return;
  }
  currentId = key;
  audio.src = src;
  playerTitle.textContent = title;
  playerBar.classList.remove("hidden");
  _setPlayerArt(artUrl);
  audio.play().catch(() => {});
  updatePlayBtns();
  _updateSkipBtns();
  _setMediaSession(title, artUrl);
}

window.playTrack = function(internalId, title) {
  _startPlayback(internalId, "/api/stream/" + internalId, title,
    "/library/tracks/" + internalId + "/cover-art?size=144");
};

function togglePlay() {
  if (audio.paused) audio.play().catch(() => {});
  else audio.pause();
}

if (playerPlayBtn) {
  playerPlayBtn.addEventListener("click", togglePlay);
}
if (playerPrev) playerPrev.addEventListener("click", () => playerSkip(-1));
if (playerNext) playerNext.addEventListener("click", () => playerSkip(1));
if (playerArtImg) {
  playerArtImg.addEventListener("error", () => playerArt.classList.add("hidden"));
}
if (playerArt) {
  // Zoom the playing track's art — strip any ?size= thumb param for full res
  playerArt.addEventListener("click", () => {
    const src = playerArtImg && playerArtImg.getAttribute("src");
    if (!src) return;
    const full = src.replace(/([?&])size=\d+&?/, "$1").replace(/[?&]$/, "");
    openLightbox(full, playerTitle ? playerTitle.textContent : "");
  });
}

if (audio) {
  audio.addEventListener("play", () => {
    playerPlayBtn && (playerPlayBtn.textContent = "⏸");
    updatePlayBtns();
  });
  audio.addEventListener("pause", () => {
    playerPlayBtn && (playerPlayBtn.textContent = "▶");
    updatePlayBtns();
  });
  audio.addEventListener("timeupdate", () => {
    if (audio.duration && playerProgress) {
      playerProgress.value = (audio.currentTime / audio.duration) * 100;
    }
  });
  audio.addEventListener("ended", () => {
    if (playerSkip(1)) return;   // advance within the current context
    currentId = null;
    updatePlayBtns();
    _updateSkipBtns();
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
document.body.addEventListener("htmx:afterSwap", () => {
  updatePlayBtns();
  _updateSkipBtns();
});

window.playPreview = function(ref, title) {
  const key = "preview:" + ref;
  const btn = _btnFor(key);   // preview art (YT thumbnail) rides on the button
  _startPlayback(key, "/api/preview?ref=" + encodeURIComponent(ref),
    title + " (preview)", (btn && btn.dataset.art) || null);
};


/* ── Play staged (review) files ─────────────────────────────────────────── */
window.playJobStaged = function(jobId, title) {
  _startPlayback("staged:" + jobId, "/jobs/" + jobId + "/stream",
    title + " (staged)", "/jobs/" + jobId + "/cover-art");
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

/* ── Cover-art lightbox ──────────────────────────────────────────────────── */
/* One shared overlay, created on first use. openLightbox(src, caption) shows
 * the full-size image; click anywhere or Esc closes it. No history/pushState
 * integration — HTMX owns popstate on pages using hx-push-url. */
let _lightbox = null;

function _ensureLightbox() {
  if (_lightbox) return _lightbox;
  const el = document.createElement("div");
  el.id = "ar-lightbox";
  el.className = "hidden";
  el.innerHTML =
    '<figure><img alt=""><figcaption></figcaption></figure>';
  el.addEventListener("click", closeLightbox);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !el.classList.contains("hidden")) closeLightbox();
  });
  document.body.appendChild(el);
  _lightbox = el;
  return el;
}

window.openLightbox = function (src, caption) {
  const el = _ensureLightbox();
  const img = el.querySelector("img");
  const cap = el.querySelector("figcaption");
  img.src = src;
  cap.textContent = caption || "";
  cap.style.display = caption ? "" : "none";
  el.classList.remove("hidden");
};

window.closeLightbox = function () {
  if (!_lightbox || _lightbox.classList.contains("hidden")) return;
  _lightbox.classList.add("hidden");
  _lightbox.querySelector("img").removeAttribute("src");
};

/* Delegated: any [data-lightbox] element zooms on click (survives HTMX swaps).
 * data-lightbox="<url>" uses that URL; an empty value uses the inner <img>'s
 * current src (for art whose onerror chain may have swapped the source).
 * The "change" strip inside editable art tiles keeps its own handler. */
document.body.addEventListener("click", function (e) {
  const zoom = e.target.closest("[data-lightbox]");
  if (!zoom || e.target.closest(".rv-art-change")) return;
  const img = zoom.querySelector("img");
  // Thumbnail failed → placeholder is showing; a zoom would just be broken
  if (img && (!img.getAttribute("src") || img.style.display === "none")) return;
  const url = zoom.dataset.lightbox || (img && img.src);
  if (!url) return;
  openLightbox(url, zoom.dataset.lightboxCaption || "");
});

/* ── Batch approve checkboxes ────────────────────────────────────────────── */
/* Selected job IDs survive the 12s job-list polling swap (innerHTML wipes the
 * DOM checkboxes, so we re-apply from this set after each swap). */
const _selectedJobs = new Set();

function _updateBatchCount() {
  const toolbar = document.getElementById('batch-toolbar');
  const label   = document.getElementById('batch-count-label');
  if (!toolbar) return;
  const checked = document.querySelectorAll('.job-check:checked').length;
  // Toolbar stays pinned at all times (even with nothing selected); only the
  // count label changes. The Approve/Reject buttons no-op on an empty selection.
  toolbar.classList.remove('hidden');
  label.textContent = checked + ' selected';
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

/* Select every track checkbox within a single album batch group. */
window.selectAlbumChecks = function(groupId) {
  const group = document.getElementById(groupId);
  if (!group) return;
  group.querySelectorAll('.job-check').forEach(cb => {
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

/* ── Pause the job-list poll while reviewing ─────────────────────────────────
 * #job-list refreshes every 12s with an innerHTML swap. If a review card is
 * expanded (or the user is typing in a field inside the list), that swap wipes
 * the card and discards in-progress edits. Cancel the *poll* (only the request
 * originating from #job-list itself) in those cases; the manual Refresh button
 * and per-card status polls have a different originating element and still run.
 * Active jobs keep updating because each active card self-polls /jobs/status.
 */
document.body.addEventListener('htmx:beforeRequest', function(e) {
  const elt = e.detail && e.detail.elt;
  if (!elt || elt.id !== 'job-list') return;
  const list = document.getElementById('job-list');
  if (!list) return;
  const reviewing = list.querySelector('.card-review');
  const active = document.activeElement;
  const editing = active && list.contains(active) &&
    /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName);
  if (reviewing || editing) e.preventDefault();
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
  var url = '/discography/' + artistMbid + (params ? '?' + params : '');
  htmx.ajax('GET', url, {target: '#disco-detail', swap: 'innerHTML'});
  try { history.replaceState(null, '', url); } catch (e) {}
};

/* ── Discography text search (client-side filter within loaded releases) ─── */
var _discoFilter = '';
window.filterDiscoReleases = function(q) {
  _discoFilter = q || '';
  var lower = _discoFilter.toLowerCase();
  document.querySelectorAll('#disco-detail [data-disco-title]').forEach(function(el) {
    var title = (el.getAttribute('data-disco-title') || '').toLowerCase();
    el.style.display = (!lower || title.indexOf(lower) !== -1) ? '' : 'none';
  });
};

/* Toggling a release-type chip reloads #disco-detail, which re-renders the
 * "Filter releases…" box empty. Restore the in-flight text filter and re-apply
 * it once the new content settles so the user's filter survives the chip click. */
document.body.addEventListener('htmx:afterSettle', function(e) {
  if (!_discoFilter) return;
  if (!e.target || (e.target.id !== 'disco-detail' && !e.target.querySelector('#disco-search'))) return;
  var box = document.getElementById('disco-search');
  if (box) box.value = _discoFilter;
  window.filterDiscoReleases(_discoFilter);
});

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

/* ── Pull-to-refresh (opt-in per page) ───────────────────────────────────────
 * Pages opt in with either:
 *   data-ptr-url + data-ptr-target  — htmx GET into a target (Jobs list)
 *   data-ptr-reload                 — full page reload (Library overview)
 * Touch-only; activates when the page is scrolled to the top and the user
 * drags down past a threshold. Native browser pull-to-refresh is suppressed
 * (overscroll-behavior) only on opted-in pages so the two never fight.
 */
(function () {
  if (!('ontouchstart' in window)) return;
  var target = document.querySelector('[data-ptr-url]');
  var reloadEl = document.querySelector('[data-ptr-reload]');
  if (!target && !reloadEl) return;

  document.documentElement.style.overscrollBehaviorY = 'contain';
  document.body.style.overscrollBehaviorY = 'contain';

  var THRESH = 70;
  var startY = null, pulling = false, dist = 0, busy = false;

  var chip = document.createElement('div');
  chip.id = 'ptr-chip';
  chip.innerHTML = '<span class="ptr-arrow">↓</span><span class="ptr-text">Pull to refresh</span>';
  document.body.appendChild(chip);
  var arrow = chip.querySelector('.ptr-arrow');
  var text = chip.querySelector('.ptr-text');

  function setDist(d) {
    dist = d;
    chip.classList.toggle('ptr-visible', d > 12 || busy);
    var ready = d >= THRESH;
    chip.classList.toggle('ptr-ready', ready);
    text.textContent = ready ? 'Release to refresh' : 'Pull to refresh';
  }

  function doRefresh() {
    busy = true;
    chip.classList.add('ptr-busy', 'ptr-visible');
    arrow.textContent = '↻';
    text.textContent = 'Refreshing…';
    function done() {
      busy = false;
      arrow.textContent = '↓';
      chip.classList.remove('ptr-busy', 'ptr-visible', 'ptr-ready');
    }
    if (target) {
      var p = htmx.ajax('GET', target.getAttribute('data-ptr-url'), {
        target: target.getAttribute('data-ptr-target') || ('#' + target.id),
        swap: 'innerHTML'
      });
      if (p && typeof p.then === 'function') p.then(done, done);
      else setTimeout(done, 1500);
    } else {
      location.reload();
    }
  }

  window.addEventListener('touchstart', function (e) {
    if (busy) { startY = null; return; }
    startY = window.scrollY > 0 ? null : e.touches[0].clientY;
  }, { passive: true });

  window.addEventListener('touchmove', function (e) {
    if (startY === null || busy) return;
    var d = e.touches[0].clientY - startY;
    if (d > 0 && window.scrollY <= 0) { pulling = true; setDist(d); }
    else if (pulling) { pulling = false; setDist(0); }
  }, { passive: true });

  window.addEventListener('touchend', function () {
    if (pulling && dist >= THRESH) doRefresh();
    else if (!busy) chip.classList.remove('ptr-visible', 'ptr-ready');
    startY = null; pulling = false; dist = 0;
  });
})();
