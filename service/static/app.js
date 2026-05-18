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

/* ── Service worker registration ─────────────────────────────────────────── */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}
