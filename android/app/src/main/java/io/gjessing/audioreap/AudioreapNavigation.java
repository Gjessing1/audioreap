package io.gjessing.audioreap;

import android.net.Uri;
import java.util.HashSet;
import java.util.Set;

/**
 * Decides which URLs stay inside the WebView.
 *
 * The configured audioreap origin always does. So does the SSO detour in front of it:
 * a self-hosted audioreap usually sits behind a gateway (TinyAuth here, but the shape is
 * generic), which bounces the WebView to a login origin and back. That origin is not
 * known ahead of time, so it is admitted on evidence rather than by name — a login URL
 * counts only when one of its return parameters points back at an origin already in the
 * chain. Once admitted it stays admitted for the session, which is what lets the
 * provider's own multi-step flow (consent, passkey, error pages) complete.
 *
 * Everything else — a link out to MusicBrainz, YouTube, a cover-art host — is left to
 * Capacitor's default, which hands it to a real browser.
 */
final class AudioreapNavigation {
    private static final String[] RETURN_PARAMETERS = {
        "redirect_uri", "redirect", "return_url", "returnUrl", "rd", "continue"
    };

    private final Uri serverOrigin;
    private final Set<String> authenticationOrigins = new HashSet<>();

    AudioreapNavigation(String serverUrl) {
        serverOrigin = Uri.parse(serverUrl);
    }

    boolean shouldAllow(Uri target) {
        if (!"https".equals(target.getScheme())) return false;
        if (sameOrigin(target, serverOrigin)) return true;
        if (isAuthenticationEntry(target)) {
            authenticationOrigins.add(origin(target));
            return true;
        }
        return authenticationOrigins.contains(origin(target));
    }

    /** Trust a new login origin only when it returns to an origin already in the chain. */
    private boolean isAuthenticationEntry(Uri target) {
        for (String name : RETURN_PARAMETERS) {
            String value = queryParameter(target, name);
            if (value == null) continue;
            Uri returnUrl = Uri.parse(value);
            if (sameOrigin(returnUrl, serverOrigin) || authenticationOrigins.contains(origin(returnUrl))) {
                return true;
            }
        }
        return false;
    }

    /** {@link Uri#getQueryParameter} throws on an opaque URI; a login URL is never one. */
    private static String queryParameter(Uri uri, String name) {
        try {
            return uri.isHierarchical() ? uri.getQueryParameter(name) : null;
        } catch (UnsupportedOperationException ignored) {
            return null;
        }
    }

    private static boolean sameOrigin(Uri left, Uri right) {
        return left.getScheme() != null &&
            left.getScheme().equalsIgnoreCase(right.getScheme()) &&
            left.getHost() != null &&
            left.getHost().equalsIgnoreCase(right.getHost()) &&
            effectivePort(left) == effectivePort(right);
    }

    private static int effectivePort(Uri uri) {
        if (uri.getPort() >= 0) return uri.getPort();
        return "https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80;
    }

    private static String origin(Uri uri) {
        return uri.getScheme() + "://" + uri.getEncodedAuthority();
    }
}
