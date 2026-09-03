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
