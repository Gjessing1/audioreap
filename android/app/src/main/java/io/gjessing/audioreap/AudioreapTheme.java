package io.gjessing.audioreap;

import android.content.res.Configuration;
import com.getcapacitor.Bridge;

/**
 * Tells the running page which light/dark mode the device is in.
 *
 * audioreap's theme button cycles auto → dark → light, and "auto" resolves through
 * {@code prefers-color-scheme}. Inside the APK that query is not something to rely on:
 * MainActivity declares {@code uiMode} in {@code android:configChanges}, so Android never
 * recreates the activity when the device flips — deliberately, because a recreate reloads
 * the WebView and throws away wherever the user was. Whether an already-loaded page then
 * hears about the flip is left to the WebView version, so the mode is reported outright
 * instead and the difference disappears.
 *
 * The ask is an evaluation of {@code window.audioreapSystemTheme(…)} — a plain global,
 * for the same reason Back uses one: audioreap is served from a remote origin, where a
 * Capacitor listener registration is not something the shell can count on having
 * completed. A page that installs nothing (the SSO login, the connection-error page, a
 * WebView whose JS has not booted) simply ignores it.
 */
final class AudioreapTheme {
    private AudioreapTheme() {}

    /** The theme name for a configuration's {@code uiMode}, as the web app names it. */
    static String themeName(int uiMode) {
        return (uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
            ? "dark"
            : "light";
    }

    /** The ask, written so a page without the global is a no-op rather than an error. */
    static String script(String theme) {
        return "window.audioreapSystemTheme && window.audioreapSystemTheme('" + theme + "')";
    }

    /** Tell the running page which mode the device is in now. */
    static void tell(Bridge bridge, int uiMode) {
        if (bridge == null) return;
        bridge.eval(script(themeName(uiMode)), null);
    }
}
