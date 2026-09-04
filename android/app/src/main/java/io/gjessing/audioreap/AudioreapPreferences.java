package io.gjessing.audioreap;

import android.content.Context;
import android.content.SharedPreferences;
import java.net.URI;

/**
 * The two things this shell has to remember: which audioreap server it is a front end
 * for, and — once the user turns notifications on — the credential it checks in with.
 *
 * The APK ships with no address baked in — audioreap is self-hosted, so the server is the
 * user's own — and the web app itself is served from there. Everything else the app knows
 * (theme, playback state, the review queue) lives on that origin, not here.
 */
final class AudioreapPreferences {
    private static final String PREFS = "audioreap_native";
    private static final String SERVER_URL = "server_url";
    private static final String PUSH_TOKEN = "push_token";

    private AudioreapPreferences() {}

    static String getServerUrl(Context context) {
        return normalizeServerUrl(prefs(context).getString(SERVER_URL, null));
    }

    static void setServerUrl(Context context, String serverUrl) {
        String normalized = normalizeServerUrl(serverUrl);
        if (normalized == null) throw new IllegalArgumentException("Invalid audioreap server URL");
        prefs(context).edit().putString(SERVER_URL, normalized).apply();
    }

    /**
     * The bearer token this device polls with, or null when notifications are off.
     *
     * Private app storage, which is the same place the WebView keeps the session cookie
     * that could mint another one — so this stores nothing that was not already there,
     * and it is a credential for exactly one read-only route (/api/push/pending).
     */
    static String getPushToken(Context context) {
        String token = prefs(context).getString(PUSH_TOKEN, null);
        return token == null || token.isBlank() ? null : token;
    }

    /** Store a freshly minted credential, or null to forget the one held. */
    static void setPushToken(Context context, String token) {
        SharedPreferences.Editor editor = prefs(context).edit();
        if (token == null || token.isBlank()) {
            editor.remove(PUSH_TOKEN);
        } else {
            editor.putString(PUSH_TOKEN, token.trim());
        }
        editor.apply();
    }

    /**
     * The scheme-and-host root of a typed address, or null when it is not one.
     *
     * HTTPS only, and no path, query, fragment or userinfo: the value becomes the
     * WebView's server origin and the base every same-origin check is made against, so a
     * URL carrying anything beyond the origin would quietly widen what the shell trusts.
     * Cleartext is refused outright — the manifest sets {@code usesCleartextTraffic=false},
     * so an http:// address would fail to load with no explanation at all.
     */
    static String normalizeServerUrl(String raw) {
        if (raw == null || raw.isBlank()) return null;
        try {
            URI uri = new URI(raw.trim());
            String path = uri.getRawPath();
            if (!"https".equalsIgnoreCase(uri.getScheme()) ||
                uri.getHost() == null ||
                uri.getUserInfo() != null ||
                uri.getRawQuery() != null ||
                uri.getRawFragment() != null ||
                (path != null && !path.isEmpty() && !"/".equals(path))) {
                return null;
            }
            return new URI("https", null, uri.getHost(), uri.getPort(), null, null, null).toASCIIString();
        } catch (Exception ignored) {
            return null;
        }
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
