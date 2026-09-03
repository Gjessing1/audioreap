package io.gjessing.audioreap;

import android.webkit.WebView;
import androidx.activity.OnBackPressedCallback;
import com.getcapacitor.Bridge;

/**
 * Routes the system Back press through the web app before the activity exits.
 *
 * The page is asked directly, by evaluating {@code window.audioreapBack()} in the
 * WebView: audioreap is served from a remote origin, so a plain global is the one channel
 * that does not depend on Capacitor's plugin JS having been injected into this document
 * and on an asynchronous listener registration having completed first — the trap that
 * leaves Back exiting the app from anywhere.
 *
 * audioreap's UI is server-rendered pages plus a few overlays (the jump palette, a
 * confirm dialog, the mobile nav sheet), so the contract is narrower than a single-page
 * app's: the page answers **true** when it closed an overlay, and anything else means it
 * had nothing to close. Only then does the WebView's own history — which for this app is
 * the page-to-page trail the user actually walked — get a say, and the activity finishes
 * at the start of it.
 */
final class AudioreapBackNavigation extends OnBackPressedCallback {
    private static final String ASK_WEB_APP = "window.audioreapBack ? window.audioreapBack() : false";

    private final MainActivity activity;
    private boolean awaitingWebApp;

    AudioreapBackNavigation(MainActivity activity) {
        super(true);
        this.activity = activity;
    }

    @Override
    public void handleOnBackPressed() {
        Bridge bridge = activity.getBridge();
        if (bridge == null) {
            activity.finish();
            return;
        }
        // Asking the page is asynchronous; ignore presses that arrive meanwhile.
        if (awaitingWebApp) return;
        awaitingWebApp = true;

        WebView webView = bridge.getWebView();
        bridge.eval(ASK_WEB_APP, value -> {
            awaitingWebApp = false;
            if ("true".equals(value)) return;
            // Finish directly rather than re-dispatching through the dispatcher:
            // predictive back (default from targetSdk 36) does not support re-entering
            // onBackPressed() from inside a callback.
            if (webView.canGoBack()) webView.goBack();
            else activity.finish();
        });
    }
}
