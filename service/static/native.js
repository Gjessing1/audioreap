/* ── Android app bridge ───────────────────────────────────────────────────────
 *
 * audioreap ships as an APK (a Capacitor shell in android/) whose WebView loads this
 * very UI from the server. So this file is the whole web half of the app: everything
 * here is a no-op in a browser, and the browser is still the primary target.
 *
 * Three jobs, in order of how often they matter:
 *
 *   1. System bars. Android hands the page the full screen, including the strips under
 *      the clock and the gesture bar. app.css reserves them (--app-safe-*); this tells
 *      Android what colour they sit on and whether to paint its icons light or dark.
 *   2. Back. The shell asks the page before it exits — see window.audioreapBack below.
 *   3. Updates. A deploy updates the app, because the app is served. The APK itself
 *      changes rarely, and when it does the phone finds out from /api/app/version.
 *   4. Notifications. A download is the thing you walk away from, so the shell asks the
 *      server what finished while the app was closed. This side only turns that on and
 *      off — the asking happens in the APK, on an alarm, with the app not running.
 *
 * Every native call is optional and failure is swallowed: this UI is served from the
 * server, so it is routinely NEWER than the installed APK and must keep working on a
 * shell that has never heard of the method it is calling.
 */
(function () {
  'use strict';

  function plugins() {
    var cap = window.Capacitor;
    return (cap && cap.Plugins) || null;
  }

  function nativePlugin() {
    var p = plugins();
    return (p && p.AudioreapNative) || null;
  }

  var isNative = nativePlugin() !== null;
  window.audioreapIsNative = function () { return isNative; };
  if (isNative) document.documentElement.classList.add('is-android-app');

  /* ── System bars ──────────────────────────────────────────────────────────
   * What sits behind both bars is the chrome, not the page body: the sticky top nav
   * and the bottom tab bar, both --s1. Matching that is what makes the bars read as
   * part of audioreap instead of a strip of the launch theme.
   */
  function chromeColor() {
    var value = getComputedStyle(document.documentElement).getPropertyValue('--s1').trim();
    return /^#[0-9a-f]{6}$/i.test(value) ? value : null;
  }

  function applySystemBars(resolved) {
    var p = plugins();
    if (!p) return;
    var color = chromeColor();
    // SystemBars names the style after the *background*, so the system paints the
    // inverse onto it: 'LIGHT' asks for dark clock/wifi/battery icons.
    var style = resolved === 'dark' ? 'DARK' : 'LIGHT';
    Promise.resolve()
      .then(function () { return p.SystemBars && p.SystemBars.setStyle({ style: style }); })
      .then(function () {
        // Second, always: setStyle repaints the window decor from the Android theme on
        // its way through, which would undo this.
        if (color && p.AudioreapNative && p.AudioreapNative.setSystemBarsColor) {
          return p.AudioreapNative.setSystemBarsColor({ color: color });
        }
      })
      .catch(function () { /* an APK older than this page, or no bridge at all */ });
  }

  // base.html resolves auto/dark/light and announces the outcome here.
  document.addEventListener('audioreap:theme', function (event) {
    applySystemBars(event.detail && event.detail.resolved);
  });

  /* ── The device's own light/dark mode ─────────────────────────────────────
   * What the theme button's "auto" resolves to. In a browser that is
   * prefers-color-scheme; in the app it is not, because the activity deliberately
   * survives a day/night flip (a recreate would reload the WebView and lose the page)
   * and an already-loaded WebView is then free to keep answering the query with the
   * mode it launched in.
   *
   * Two directions. The shell pushes every *change* by evaluating
   * window.audioreapSystemTheme(...) — defined by the theme controller in base.html —
   * and this asks for the current value once at load, because a page that just loaded
   * has missed every change so far. The controller may not have run yet when the answer
   * arrives, so it is also parked on audioreapSystemThemeValue for it to pick up.
   */
  (function () {
    var plugin = nativePlugin();
    if (!plugin || !plugin.getSystemTheme) return;
    plugin.getSystemTheme().then(function (result) {
      if (!result || typeof result.dark !== 'boolean') return;
      var theme = result.dark ? 'dark' : 'light';
      window.audioreapSystemThemeValue = theme;
      if (typeof window.audioreapSystemTheme === 'function') window.audioreapSystemTheme(theme);
    }).catch(function () { /* an APK older than this page */ });
  })();

  /* ── Back ─────────────────────────────────────────────────────────────────
   * The shell evaluates this on every system Back press (AudioreapBackNavigation).
   * A plain global, not a Capacitor listener: this page is served from a remote
   * origin, where the shell cannot count on plugin JS having been injected and an
   * async listener registration having completed before the first press.
   *
   * Answer true only when the press was *consumed* by closing something. Anything
   * else means the shell should go back a page, and finally leave the app — which is
   * the right behaviour for a server-rendered UI whose history is the trail the user
   * actually walked.
   */
  window.audioreapBack = function () {
    var palette = document.getElementById('jump-palette');
    if (palette && !palette.classList.contains('hidden')) {
      if (window.closeJumpPalette) window.closeJumpPalette();
      else palette.classList.add('hidden');
      return true;
    }
    var dialog = document.getElementById('confirm-dialog');
    if (dialog && dialog.open) {
      dialog.close('cancel');
      return true;
    }
    var nav = document.getElementById('main-nav');
    if (nav && nav.classList.contains('nav-open')) {
      nav.classList.remove('nav-open');
      var toggle = document.getElementById('nav-toggle');
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
      return true;
    }
    return false;
  };

  /* ── Updates ──────────────────────────────────────────────────────────────
   * What the Settings page paints its "Android app" row from. Two independent facts:
   * what this device runs (native, absent in a browser) and what the server publishes
   * (always available, so a browser can offer the first install).
   */
  function installed() {
    var plugin = nativePlugin();
    if (!plugin) return Promise.resolve(null);
    return plugin.getInfo().catch(function () { return null; });
  }

  function published() {
    return fetch('/api/app/version', { cache: 'no-store' })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (release) {
        if (!release ||
            !Number.isSafeInteger(release.versionCode) ||
            typeof release.versionName !== 'string' ||
            typeof release.apkUrl !== 'string' ||
            typeof release.sha256 !== 'string' ||
            typeof release.bytes !== 'number') {
          return null;
        }
        return release;
      })
      .catch(function () { return null; });
  }

  window.audioreapAppRelease = function () {
    return Promise.all([installed(), published()]).then(function (both) {
      var device = both[0];
      var release = both[1];
      return {
        native: isNative,
        installed: device,
        release: release,
        updateAvailable: !!(device && release && release.versionCode > device.versionCode),
        downloadUrl: release ? new URL(release.apkUrl, window.location.origin).href : null,
      };
    });
  };

  /* ── Background notifications ─────────────────────────────────────────────
   * Only the shell can post a notification while audioreap is closed, and only the
   * page can mint the credential it needs — the poll runs in a broadcast receiver,
   * outside the session this document is holding. So enabling is a handshake: the
   * page asks the server for a device token, hands it across the bridge, and the
   * shell asks Android for permission and arms its alarm.
   */
  function pushPlugin() {
    var plugin = nativePlugin();
    return plugin && plugin.pushStatus && plugin.enablePush && plugin.disablePush
      ? plugin : null;   // an APK older than this page: no notifications, no error
  }

  function unregister(token) {
    if (!token) return Promise.resolve();
    return fetch('/api/push/device/unregister', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token }),
    }).catch(function () { /* the device already stopped polling; the row is stale, not harmful */ });
  }

  /** What this device does today: {supported, enabled, granted}. Never rejects. */
  window.audioreapPushState = function () {
    var plugin = pushPlugin();
    if (!plugin) return Promise.resolve({ supported: false, enabled: false, granted: false });
    return plugin.pushStatus()
      .then(function (status) {
        return { supported: true, enabled: !!status.enabled, granted: !!status.granted };
      })
      .catch(function () { return { supported: false, enabled: false, granted: false }; });
  };

  /**
   * Turn them on. Resolves {enabled, granted} — `granted: false` is the user declining
   * Android's permission prompt, which is an outcome to explain, not an error.
   */
  window.audioreapEnablePush = function () {
    var plugin = pushPlugin();
    if (!plugin) return Promise.resolve({ enabled: false, granted: false });
    return fetch('/api/push/device', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: 'android' }),
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) {
        if (!payload || typeof payload.token !== 'string') {
          return { enabled: false, granted: false };
        }
        return plugin.enablePush({ token: payload.token }).then(function (status) {
          // A credential the shell did not keep would poll from nowhere for ever.
          // Hand it straight back rather than leaving a row that can never be used.
          if (!status || !status.enabled) {
            return unregister(payload.token).then(function () {
              return { enabled: false, granted: !!(status && status.granted) };
            });
          }
          return { enabled: true, granted: true };
        });
      })
      .catch(function () { return { enabled: false, granted: false }; });
  };

  /** Turn them off, and revoke the credential rather than orphaning it on the server. */
  window.audioreapDisablePush = function () {
    var plugin = pushPlugin();
    if (!plugin) return Promise.resolve();
    return plugin.disablePush()
      .then(function (result) { return unregister(result && result.token); })
      .catch(function () { /* nothing stored to drop */ });
  };

  /**
   * Open a link outside the WebView. The one that matters is the APK: Android installs
   * a package from a download, and a WebView cannot save one — so it goes to the system
   * download manager, which is also why /api/app/download needs no authentication.
   */
  window.audioreapOpenExternal = function (url) {
    var plugin = nativePlugin();
    if (!plugin || !plugin.openExternal) {
      window.open(url, '_blank', 'noopener');
      return Promise.resolve();
    }
    return plugin.openExternal({ url: url }).catch(function () {
      window.open(url, '_blank', 'noopener');
    });
  };
})();
