package io.gjessing.audioreap;

import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeWebViewClient;

/**
 * Turns a failed page load into something the user can act on.
 *
 * A self-hosted server is off far more often than a hosted one — it is a machine at home,
 * behind a VPN, or simply moved — and the WebView's own answer to that is a blank frame.
 */
final class AudioreapWebViewClient extends BridgeWebViewClient {
    private final MainActivity activity;

    AudioreapWebViewClient(Bridge bridge, MainActivity activity) {
        super(bridge);
        this.activity = activity;
    }

    @Override
    public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
        super.onReceivedError(view, request, error);
        if (request.isForMainFrame()) activity.showConnectionError();
    }
}
