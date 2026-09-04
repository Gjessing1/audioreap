package io.gjessing.audioreap;

import android.content.Intent;
import android.text.TextUtils;

/**
 * Where a tapped notification takes you.
 *
 * The server says which page an event belongs to — the review queue for a batch waiting
 * at the gate, the failed list for one that gave up — and sends it as a *path*, never a
 * whole URL. The server this shell talks to is the user's own, so the origin is already
 * settled here; accepting a full URL from the response would let a compromised or
 * mistyped server point the WebView anywhere, and there is nothing that a path cannot
 * express.
 *
 * The path is re-checked twice on the way: once as it is parsed out of the response, and
 * again when it comes back off an Intent. An Intent extra is the wider door of the two —
 * anything on the device can send this activity an Intent — so nothing between here and
 * `loadUrl` trusts it.
 */
final class AudioreapNotificationLink {
    /** Intent extra carrying the in-app path a notification refers to. */
    static final String PATH_EXTRA = "notificationPath";

    /** Where a notification without a usable path lands: the queue itself. */
    static final String DEFAULT_PATH = "/jobs";

    private AudioreapNotificationLink() {}

    /**
     * The given path if it is one this shell will navigate to, else {@link #DEFAULT_PATH}.
     *
     * Rooted, and not protocol-relative: "//evil.example.com/x" is a valid *path* to a
     * URL parser and a different origin to a browser. The character allowlist keeps out
     * everything that could turn one URL into two — whitespace, a backslash, an
     * authority marker — rather than trying to enumerate what an attack would look like.
     */
    static String safePath(String raw) {
        if (raw == null) return DEFAULT_PATH;
        String path = raw.trim();
        if (path.length() < 1 || path.length() > 200) return DEFAULT_PATH;
        if (!path.startsWith("/") || path.startsWith("//")) return DEFAULT_PATH;
        if (!path.matches("[A-Za-z0-9/_.,~=&?%+:@!$'()*\\-]+")) return DEFAULT_PATH;
        return path;
    }

    /** The path a launch intent points at, or null when it is an ordinary launch. */
    static String pathFrom(Intent intent) {
        if (intent == null || intent.getExtras() == null) return null;
        Object raw = intent.getExtras().get(PATH_EXTRA);
        if (!(raw instanceof String)) return null;
        return safePath((String) raw);
    }

    /** The absolute URL for a path, on the server this install is configured against. */
    static String urlFor(String serverUrl, String path) {
        if (TextUtils.isEmpty(serverUrl) || path == null) return null;
        return serverUrl.replaceAll("/+$", "") + safePath(path);
    }
}
