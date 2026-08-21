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

/* ── Batch approve checkboxes ────────────────────────────────────────────── */
/* Selected job IDs survive the 12s job-list polling swap (innerHTML wipes the
 * DOM checkboxes, so we re-apply from this set after each swap). */
/* Shared confirmation dialog for HTMX, keyboard, and swipe mutations. */
(function () {
  const dialog = document.getElementById('confirm-dialog');
  if (!dialog) return;
  const title = document.getElementById('confirm-dialog-title');
  const message = document.getElementById('confirm-dialog-message');
  const consequence = document.getElementById('confirm-dialog-consequence');
  const recovery = document.getElementById('confirm-dialog-recovery');
  const accept = document.getElementById('confirm-dialog-accept');
  let resolvePending = null;
  let returnFocus = null;

  function fill(el, value) {
    el.textContent = value || '';
    el.classList.toggle('hidden', !value);
  }

  function withCount(value, count) {
    return String(value || '').replaceAll('{count}', String(count));
  }

  window.requestConfirmation = function (options) {
    options = options || {};
    const count = options.count || document.querySelectorAll('.job-check:checked').length;
    if (dialog.open) dialog.close('cancel');
    title.textContent = withCount(options.title || 'Confirm action', count);
    fill(message, withCount(options.message || 'Continue with this action?', count));
    fill(consequence, withCount(options.consequence || '', count));
    fill(recovery, withCount(options.recovery || '', count));
    accept.textContent = withCount(options.actionLabel || 'Confirm', count);
    accept.classList.toggle('confirm-dialog__accept--danger', options.variant === 'danger');
    returnFocus = options.opener || document.activeElement;
    dialog.returnValue = 'cancel';
    return new Promise(function (resolve) {
      resolvePending = resolve;
      dialog.showModal();
      accept.focus();
    });
  };

  dialog.addEventListener('close', function () {
    if (resolvePending) {
      const resolve = resolvePending;
      resolvePending = null;
      resolve(dialog.returnValue === 'confirm');
    }
    if (returnFocus && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
  });

  dialog.addEventListener('click', function (event) {
    if (event.target === dialog) dialog.close('cancel');
  });

  document.body.addEventListener('htmx:confirm', function (event) {
    const detail = event.detail || {};
    if (!detail.question) return;
    event.preventDefault();
    const elt = detail.elt || event.target;
    const data = elt.dataset || {};
    const count = data.confirmCount === 'selected'
      ? document.querySelectorAll('.job-check:checked').length
      : (data.confirmCount || '');
    window.requestConfirmation({
      opener: elt,
      count: count,
      title: data.confirmTitle,
      message: data.confirmMessage || detail.question,
      consequence: data.confirmConsequence,
      recovery: data.confirmRecovery,
      actionLabel: data.confirmActionLabel,
      variant: data.confirmVariant
    }).then(function (confirmed) {
      if (confirmed) detail.issueRequest(true);
    });
  });
})();

const _selectedJobs = new Set();

function _updateBatchCount() {
  const toolbar = document.getElementById('batch-toolbar');
  const label   = document.getElementById('batch-count-label');
  const queue = document.querySelector('.jobs-queue');
  if (!queue) return;
  const checked = queue.querySelectorAll('.job-check:checked').length;
  const view = queue.dataset.jobsView || 'review';
  if (toolbar) toolbar.classList.remove('hidden');
  if (label) label.textContent = checked + ' selected in ' + view.charAt(0).toUpperCase() + view.slice(1);
  queue.querySelectorAll('[data-requires-job-selection]').forEach(function (button) {
    button.disabled = checked === 0;
  });
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
  document.querySelectorAll('.job-check').forEach(cb => {
    cb.checked = false;
    _selectedJobs.delete(cb.value);
  });
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

/* ── Acquisition receipt return state ─────────────────────────────────────
 * A receipt links away to a stable Jobs anchor. Save the focused Acquire view
 * just before that navigation so Back (or the explicit Jobs return button)
 * restores the user's query, client-side release filter, and scroll position.
 */
(function () {
  const KEY = 'ar-acquire-return-state';

  function value(selector) {
    const el = document.querySelector(selector);
    return el ? el.value : '';
  }

  function acquireSnapshot() {
    const active = document.querySelector('#acq-tabs .acq-tab.active');
    const tab = active ? active.dataset.tab : 'search';
    const values = {
      q: value('#q'),
      discoQ: value('#disco-q'),
      genreQ: value('#disco-genre-q'),
      discoFilter: value('#disco-search'),
      playlistUrl: value('#playlist-form input[name="url"]')
    };
    const genreButton = document.querySelector('#disco-modes [data-mode="genre"]');
    const innerScrolls = {};
    document.querySelectorAll('[data-acquire-scroll][id]').forEach(function (el) {
      innerScrolls[el.id] = el.scrollTop;
    });

    let path = location.pathname + location.search;
    if (tab === 'search') {
      path = '/acquire' + (values.q ? '?q=' + encodeURIComponent(values.q) : '');
    } else if (tab === 'playlists') {
      path = '/playlists' + (values.playlistUrl ? '?url=' + encodeURIComponent(values.playlistUrl) : '');
    } else if (!/^\/discography\/[^/]+/.test(location.pathname)) {
      const discoverQ = genreButton && genreButton.classList.contains('active')
        ? values.genreQ : values.discoQ;
      path = '/discography' + (discoverQ ? '?q=' + encodeURIComponent(discoverQ) : '');
    }
    return {
      version: 1,
      savedAt: Date.now(),
      path: path,
      tab: tab,
      discoMode: genreButton && genreButton.classList.contains('active') ? 'genre' : 'artist',
      values: values,
      scrollY: window.scrollY,
      innerScrolls: innerScrolls
    };
  }

  function readSnapshot() {
    try {
      const state = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      if (!state || state.version !== 1 || Date.now() - state.savedAt > 2 * 60 * 60 * 1000) return null;
      return state;
    } catch (e) {
      return null;
    }
  }

  document.body.addEventListener('click', function (e) {
    const link = e.target.closest('[data-acquire-jobs-link]');
    if (!link || !document.getElementById('acq-tabs')) return;
    try { sessionStorage.setItem(KEY, JSON.stringify(acquireSnapshot())); } catch (err) {}
  });

  const state = readSnapshot();
  const back = document.getElementById('jobs-back-to-acquire');
  if (back && state) {
    back.href = state.path || '/acquire';
    back.classList.remove('hidden');
  }

  const tabs = document.getElementById('acq-tabs');
  if (!tabs || !state || location.pathname !== (state.path || '').split('?')[0]) return;

  const setters = {
    '#q': state.values.q,
    '#disco-q': state.values.discoQ,
    '#disco-genre-q': state.values.genreQ,
    '#playlist-form input[name="url"]': state.values.playlistUrl
  };
  Object.keys(setters).forEach(function (selector) {
    const el = document.querySelector(selector);
    if (el && setters[selector] != null) el.value = setters[selector];
  });

  const wantedTab = document.querySelector('#acq-tabs [data-tab="' + state.tab + '"]');
  if (wantedTab && !wantedTab.classList.contains('active')) wantedTab.click();
  if (state.discoMode === 'genre') {
    const genre = document.querySelector('#disco-modes [data-mode="genre"]');
    if (genre && !genre.classList.contains('active')) genre.click();
  }

  function restoreDynamicState() {
    const filter = document.getElementById('disco-search');
    if (filter && state.values.discoFilter != null) {
      filter.value = state.values.discoFilter;
      if (window.filterDiscoReleases) window.filterDiscoReleases(filter.value);
    }
    Object.keys(state.innerScrolls || {}).forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.scrollTop = state.innerScrolls[id];
    });
    window.scrollTo(0, state.scrollY || 0);
  }

  if (state.tab === 'search' && state.values.q) {
    htmx.ajax('GET', '/search/results?q=' + encodeURIComponent(state.values.q), {
      target: '#local-results', swap: 'innerHTML'
    });
    htmx.ajax('GET', '/search/cloud?q=' + encodeURIComponent(state.values.q), {
      target: '#cloud-results', swap: 'innerHTML'
    });
  } else if (state.tab === 'discover' && state.discoMode === 'genre' && state.values.genreQ) {
    htmx.ajax('GET', '/discography/genre-search?q=' + encodeURIComponent(state.values.genreQ), {
      target: '#artist-candidates', swap: 'innerHTML'
    });
  }

  document.body.addEventListener('htmx:afterSettle', restoreDynamicState);
  requestAnimationFrame(restoreDynamicState);
  setTimeout(restoreDynamicState, 300);
  setTimeout(restoreDynamicState, 1000);
  setTimeout(function () {
    document.body.removeEventListener('htmx:afterSettle', restoreDynamicState);
  }, 2500);
})();

/* Keep partial-failure retry scoped to the rows the user leaves selected. */
document.body.addEventListener('change', function (e) {
  if (!e.target.classList.contains('acquisition-failed-check')) return;
  const form = e.target.closest('.acquisition-batch-receipt__retry');
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  if (button) button.disabled = !form.querySelector('.acquisition-failed-check:checked');
});

/* Reset after HTMX swaps */
document.body.addEventListener('htmx:afterSwap', () => {
  // Restore selection a poll-driven innerHTML swap would otherwise have wiped.
  // IDs from other queue views remain in the set so tab switches are lossless.
  document.querySelectorAll('.job-check').forEach(cb => {
    cb.checked = _selectedJobs.has(cb.value);
  });
  const list = document.getElementById('job-list');
  const queue = list && list.querySelector('.jobs-queue');
  if (list && queue) {
    const url = '/jobs/list?view=' + encodeURIComponent(queue.dataset.jobsView);
    list.setAttribute('hx-get', url);
    list.dataset.ptrUrl = url;
  }
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


/* ── Unified search palette (Ctrl/Cmd+K or "/" outside inputs) ────────────────
 * The overlay lives in base.html. The initial GET /nav/jump returns local
 * matches immediately and fans out to provider fragments. This block owns
 * open/close and arrow-key/Enter activation across links and inline forms.
 */
window.openJumpPalette = function () {
  var pal = document.getElementById('jump-palette');
  var inp = document.getElementById('jump-input');
  if (!pal || !inp) return;
  pal.classList.remove('hidden');
  inp.value = '';
  document.getElementById('jump-results').innerHTML = '';
  setTimeout(function () { inp.focus(); }, 0);
};

window.closeJumpPalette = function () {
  var pal = document.getElementById('jump-palette');
  if (pal) pal.classList.add('hidden');
};

(function () {
  function items() {
    return Array.from(document.querySelectorAll('#jump-results .jump-item:not(.jump-item-disabled)'));
  }
  function moveActive(dir) {
    var list = items();
    if (!list.length) return;
    var cur = list.findIndex(function (el) { return el.classList.contains('jump-active'); });
    var next = Math.max(0, Math.min(list.length - 1, cur + dir));
    if (cur === -1) next = dir > 0 ? 0 : list.length - 1;
    list.forEach(function (el, i) { el.classList.toggle('jump-active', i === next); });
    list[next].scrollIntoView({ block: 'nearest' });
  }

  document.addEventListener('keydown', function (e) {
    var pal = document.getElementById('jump-palette');
    if (!pal) return;
    var open = !pal.classList.contains('hidden');
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && e.target.id !== 'jump-input';
    if ((e.key === 'k' && (e.metaKey || e.ctrlKey)) || (e.key === '/' && !open && !typing)) {
      e.preventDefault();
      open ? closeJumpPalette() : openJumpPalette();
      return;
    }
    if (!open) return;
    if (e.key === 'Escape') {
      closeJumpPalette();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveActive(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveActive(-1);
    } else if (e.key === 'Enter') {
      var target = document.querySelector('#jump-results .jump-active') ||
                   document.querySelector('#jump-results .jump-item:not(.jump-item-disabled)');
      if (target) {
        e.preventDefault();
        if (target.tagName === 'FORM') target.requestSubmit();
        else {
          var primary = target.querySelector('[data-jump-primary]');
          (primary || target).click();
        }
      }
    }
  });

  // Click on the backdrop (not the panel) closes
  document.addEventListener('click', function (e) {
    if (e.target && e.target.id === 'jump-palette') closeJumpPalette();
  });
})();

/* ── Review card keyboard shortcuts (a=approve, r=reject, s=MB search, p=play) ──
 * Actions follow the keyboard-focused card (or a selected card as fallback),
 * never whichever card happened to be under the mouse pointer. */
let _focusedReviewCard = null;

function _setFocusedReviewCard(card) {
  document.querySelectorAll('.job-card--focused').forEach(function (el) {
    el.classList.remove('job-card--focused');
  });
  _focusedReviewCard = card;
  if (card && card.classList.contains('job-card')) card.classList.add('job-card--focused');
}

document.body.addEventListener('focusin', function (e) {
  const card = e.target.closest('.card-review, .job-card[data-state="needs_review"]');
  if (card) _setFocusedReviewCard(card);
});
document.body.addEventListener('click', function (e) {
  const card = e.target.closest('.card-review, .job-card[data-state="needs_review"]');
  if (card) _setFocusedReviewCard(card);
});

document.body.addEventListener('htmx:afterSwap', function (e) {
  const target = (e.detail && e.detail.target) || e.target;
  let card = target && target.matches && target.matches('.card-review') ? target : null;
  if (!card && target && target.id) {
    const replacement = document.getElementById(target.id);
    if (replacement && replacement.matches('.card-review')) card = replacement;
  }
  if (!card && target && target.querySelector) card = target.querySelector('.card-review');
  if (card) {
    _setFocusedReviewCard(card);
    card.focus({ preventScroll: true });
  }
});

document.body.addEventListener('reviewApproved', function (e) {
  if (!e.detail || !e.detail.openNext) return;
  setTimeout(function () {
    const next = Array.from(document.querySelectorAll('[data-review-open]')).find(function (button) {
      return button.closest('.job-card')?.dataset.jobId !== e.detail.jobId;
    });
    if (next) {
      next.click();
      next.closest('.job-card').scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, 80);
});

document.addEventListener("keydown", function(e) {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const activeCard = document.activeElement && document.activeElement.closest
    ? document.activeElement.closest('.card-review, .job-card[data-state="needs_review"]')
    : null;
  const selectedCard = document.querySelector('.job-check:checked')?.closest('.job-card[data-state="needs_review"]');
  const card = activeCard || (_focusedReviewCard && _focusedReviewCard.isConnected ? _focusedReviewCard : null) || selectedCard;
  if (!card) return;

  if (e.key === "a") {
    e.preventDefault();
    const btn = card.querySelector(".rv-approve");
    if (btn && !btn.disabled) btn.click();
    else if (card.dataset.jobId) {
      htmx.ajax('POST', '/jobs/' + card.dataset.jobId + '/approve', {
        target: '#' + card.id, swap: 'outerHTML'
      });
    }
  } else if (e.key === "r") {
    e.preventDefault();
    const btn = card.querySelector(".rv-reject");
    if (btn) btn.click();
    else if (card.dataset.jobId) {
      window.requestConfirmation({
        opener: card,
        title: 'Reject this track?',
        message: 'Reject this review item and move its staged file to Trash.',
        recovery: 'You can undo this action or restore the file later from Failed jobs.',
        actionLabel: 'Move to Trash',
        variant: 'danger'
      }).then(function (confirmed) {
        if (confirmed) htmx.ajax('POST', '/jobs/' + card.dataset.jobId + '/reject', {
          target: '#' + card.id, swap: 'outerHTML'
        });
      });
    }
  } else if (e.key === "s") {
    e.preventDefault();
    const mbBtn = card.querySelector('[data-review-mb]');
    if (mbBtn) mbBtn.click();
    else card.querySelector('[data-review-open]')?.click();
  } else if (e.key === "p") {
    e.preventDefault();
    const playBtn = card.querySelector('[data-review-play], .play-btn');
    if (playBtn) playBtn.click();
  }
});

/* ── Swipe-to-triage review cards (touch): right = approve, left = reject ────
 * Compact needs_review cards in the jobs list only (the expanded review card
 * has form fields and its own buttons). Vertical scrolling wins: the gesture
 * captures only once horizontal movement clearly dominates (touch-action:
 * pan-y keeps native scrolling for everything else). Swipe right approves
 * with the stored metadata — identical to opening the review card and hitting
 * Approve untouched. Swipe left asks for confirmation first, since reject
 * trashes the staged file.
 */
(function () {
  if (!('ontouchstart' in window)) return;

  var card = null, startX = 0, startY = 0, dx = 0, decided = false, horizontal = false;

  function badgeFor(el, kind, label) {
    var b = el.querySelector('.swipe-badge--' + kind);
    if (!b) {
      b = document.createElement('span');
      b.className = 'swipe-badge swipe-badge--' + kind;
      b.textContent = label;
      el.appendChild(b);
    }
    return b;
  }

  function resetSwipe(el) {
    el.classList.remove('swipe-return', 'swipe-commit');
    el.style.transform = '';
    el.querySelectorAll('.swipe-badge').forEach(function (b) { b.remove(); });
  }

  function swipeThresh(el) {
    return Math.max(72, Math.min(el.offsetWidth * 0.35, 150));
  }

  document.addEventListener('touchstart', function (e) {
    card = null;
    if (e.target.closest('button, a, input, select, textarea')) return;
    var c = e.target.closest('#job-list .card[data-state="needs_review"]');
    if (!c || c.dataset.swipeBusy) return;
    card = c;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    dx = 0; decided = false; horizontal = false;
  }, { passive: true });

  document.addEventListener('touchmove', function (e) {
    if (!card || !card.isConnected) return;
    dx = e.touches[0].clientX - startX;
    var dy = e.touches[0].clientY - startY;
    if (!decided) {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
      decided = true;
      horizontal = Math.abs(dx) > Math.abs(dy) * 1.4;
    }
    if (!horizontal) return;
    e.preventDefault();   // suppress text selection / click-through while dragging
    card.classList.remove('swipe-return');
    card.style.transform = 'translateX(' + dx + 'px)';
    var th = swipeThresh(card);
    badgeFor(card, 'approve', '✓ Approve').style.opacity = dx > 0 ? Math.min(dx / th, 1) : 0;
    badgeFor(card, 'reject', '✕ Reject').style.opacity = dx < 0 ? Math.min(-dx / th, 1) : 0;
  }, { passive: false });

  function endSwipe() {
    var el = card;
    card = null;
    if (!el || !el.isConnected) return;
    if (!horizontal || Math.abs(dx) < swipeThresh(el)) {
      el.classList.add('swipe-return');
      el.style.transform = '';
      el.querySelectorAll('.swipe-badge').forEach(function (b) { b.style.opacity = 0; });
      setTimeout(function () { if (el.isConnected) resetSwipe(el); }, 220);
      return;
    }
    if (dx < 0) {
      resetSwipe(el);
      window.requestConfirmation({
        opener: el,
        title: 'Reject this track?',
        message: 'Reject this review item and move its staged file to Trash.',
        recovery: 'You can undo this action or restore the file later from Failed jobs.',
        actionLabel: 'Move to Trash',
        variant: 'danger'
      }).then(function (confirmed) {
        if (confirmed) commitSwipe(el, -1);
      });
      return;
    }
    commitSwipe(el, 1);
  }

  function commitSwipe(el, direction) {
    var jobId = el.id.replace(/^job-/, '');
    el.dataset.swipeBusy = '1';
    el.classList.add('swipe-commit');
    el.style.transform = 'translateX(' + direction * el.offsetWidth + 'px)';
    var p = htmx.ajax('POST', '/jobs/' + jobId + (direction > 0 ? '/approve' : '/reject'),
      { target: '#' + el.id, swap: 'outerHTML' });
    if (p && typeof p.catch === 'function') {
      p.catch(function () {
        delete el.dataset.swipeBusy;
        if (el.isConnected) resetSwipe(el);
      });
    }
  }

  document.addEventListener('touchend', endSwipe);
  document.addEventListener('touchcancel', endSwipe);
})();

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
