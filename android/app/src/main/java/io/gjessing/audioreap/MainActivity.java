package io.gjessing.audioreap;

import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.res.Configuration;
import android.os.Bundle;
import android.text.InputType;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.appcompat.app.AlertDialog;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.CapConfig;

/**
 * The audioreap shell.
 *
 * There is no bundled app: the WebView is pointed at the user's own audioreap server and
 * loads the same HTMX UI a browser would. That is what makes updates ordinary — a server
 * deploy is the update, and the APK only changes when the shell itself does. The
 * bundled document under assets/public is a splash for the moment before the server URL
 * is known or reachable.
 */
public class MainActivity extends BridgeActivity {
    private boolean connectionDialogVisible;
    private boolean setupDialogVisible;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        String serverUrl = AudioreapPreferences.getServerUrl(this);
        boolean debug = (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        CapConfig.Builder configBuilder = new CapConfig.Builder(this)
            .setAppendedUserAgentString("AudioreapAndroid/1")
            .setLoggingEnabled(debug)
            .setWebContentsDebuggingEnabled(debug)
            .setResolveServiceWorkerRequests(false)
            .setInitialFocus(true);
        if (serverUrl != null) configBuilder.setServerUrl(serverUrl);
        config = configBuilder.create();
        registerPlugin(AudioreapNativePlugin.class);
        super.onCreate(savedInstanceState);

        if (bridge != null && serverUrl != null) {
            bridge.setWebViewClient(new AudioreapWebViewClient(bridge, this));
        } else if (bridge != null) {
            // First launch: nothing to load until the user says where audioreap lives.
            bridge.getWebView().post(() -> showServerSetup(false));
        }
        // BridgeActivity does not consume Android's system Back action, so without a
        // callback Back finishes the activity from anywhere in the app.
        getOnBackPressedDispatcher().addCallback(this, new AudioreapBackNavigation(this));

        // Launched by tapping a notification: the WebView has loaded nothing yet, so the
        // page it points at is simply the URL to start on.
        String path = AudioreapNotificationLink.pathFrom(getIntent());
        if (path != null && serverUrl != null && bridge != null) {
            String url = AudioreapNotificationLink.urlFor(serverUrl, path);
            if (url != null) bridge.getWebView().post(() -> bridge.getWebView().loadUrl(url));
        }
    }

    /**
     * A notification tapped while audioreap is already open. `launchMode="singleTask"`
     * routes it here instead of recreating the activity, so the running WebView is asked
     * to navigate — the same full page load tapping a nav link would do, and one that
     * keeps the session (and, behind SSO, avoids re-running the handshake).
     */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String path = AudioreapNotificationLink.pathFrom(intent);
        if (path == null || bridge == null) return;
        String url = AudioreapNotificationLink.urlFor(AudioreapPreferences.getServerUrl(this), path);
        if (url != null) bridge.getWebView().post(() -> bridge.getWebView().loadUrl(url));
    }

    /**
     * The device flipped light/dark. This activity handles the change itself rather than
     * being recreated for it — a recreate reloads the WebView and loses the page — so
     * whether the page ever hears about it is left to the WebView. It is told outright
     * instead (see AudioreapTheme).
     */
    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        AudioreapTheme.tell(bridge, newConfig.uiMode);
    }

    /**
     * A flip that happened while audioreap was in the background — Android's scheduled
     * dark theme, most often — reaches a stopped activity on its own schedule, so re-state
     * the mode on the way back to the foreground. Idempotent: a page already in that theme
     * does nothing with it, and one that has not booted ignores it.
     */
    @Override
    public void onResume() {
        super.onResume();
        AudioreapTheme.tell(bridge, getResources().getConfiguration().uiMode);
    }

    /** The server did not answer. Offer the two things that ever help: retry, or move. */
    void showConnectionError() {
        if (connectionDialogVisible || isFinishing()) return;
        connectionDialogVisible = true;
        runOnUiThread(() -> new AlertDialog.Builder(this)
            .setTitle(R.string.server_unavailable_title)
            .setMessage(getString(R.string.server_unavailable_message, AudioreapPreferences.getServerUrl(this)))
            .setPositiveButton(R.string.retry, (dialog, which) -> {
                connectionDialogVisible = false;
                bridge.getWebView().reload();
            })
            .setNegativeButton(R.string.change_server, (dialog, which) -> {
                connectionDialogVisible = false;
                showServerSetup(true);
            })
            .setOnCancelListener(dialog -> connectionDialogVisible = false)
            .show());
    }

    /**
     * Ask for the server address. Not cancelable on first launch, where there is nothing
     * behind the dialog to go back to.
     */
    private void showServerSetup(boolean cancelable) {
        if (setupDialogVisible || isFinishing()) return;
        setupDialogVisible = true;

        int padding = dp(8);
        LinearLayout fields = new LinearLayout(this);
        fields.setOrientation(LinearLayout.VERTICAL);
        fields.setPadding(padding, 0, padding, 0);

        EditText input = new EditText(this);
        input.setHint(R.string.server_url_hint);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        String current = AudioreapPreferences.getServerUrl(this);
        if (current != null) {
            input.setText(current);
            input.setSelection(input.length());
        }
        fields.addView(input, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        TextView error = new TextView(this);
        error.setTextColor(0xffdc2626);
        fields.addView(error, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        AlertDialog dialog = new AlertDialog.Builder(this)
            .setTitle(R.string.server_setup_title)
            .setMessage(R.string.server_setup_help)
            .setView(fields)
            .setPositiveButton(R.string.connect, null)
            .setCancelable(cancelable)
            .create();
        dialog.setOnCancelListener(value -> setupDialogVisible = false);
        dialog.setOnDismissListener(value -> setupDialogVisible = false);
        // The click listener is attached after show() so a rejected address can report
        // itself in place instead of dismissing the dialog.
        dialog.setOnShowListener(value -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(button -> {
            String normalized = AudioreapPreferences.normalizeServerUrl(input.getText().toString());
            if (normalized == null) {
                error.setText(R.string.server_url_error);
                return;
            }
            AudioreapPreferences.setServerUrl(this, normalized);
            dialog.dismiss();
            recreate();
        }));
        dialog.show();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
