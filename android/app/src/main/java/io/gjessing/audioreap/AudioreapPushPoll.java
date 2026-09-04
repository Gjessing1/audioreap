package io.gjessing.audioreap;

import android.util.Log;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Asks audioreap what this device has missed: one short authenticated GET, parsed, closed.
 *
 * Written against {@link HttpURLConnection} rather than a client library — a single JSON
 * GET needs no dependency, and the shell has none beyond Capacitor for the same reason.
 *
 * The request runs inside a broadcast receiver's `goAsync` window, under a wake lock, so
 * both timeouts are deliberately short: a server that is unreachable must fail fast and
 * leave the retry to the next alarm rather than hold the phone awake waiting for it. Even
 * both timeouts back to back stay well inside the minute a background broadcast is given.
 */
final class AudioreapPushPoll {
    private static final String TAG = "AudioreapPush";
    private static final int CONNECT_TIMEOUT_MS = 10_000;
    private static final int READ_TIMEOUT_MS = 10_000;

    /** What the poll saw, which decides whether the device keeps its credential. */
    enum Status {
        /** The answer was read — {@link Result#events} holds it, possibly empty. */
        OK,
        /** Offline, server down, or a transient error. The next alarm retries. */
        UNAVAILABLE,
        /** The server does not know this credential. Retrying cannot help. */
        UNAUTHORIZED,
    }

    /** One thing worth telling the user about: a finished batch, or a track that failed. */
    static final class Event {
        final String id;
        final String title;
        final String body;
        /** Where in the web UI it happened, as a path on the configured server. */
        final String path;

        Event(String id, String title, String body, String path) {
            this.id = id;
            this.title = title;
            this.body = body;
            this.path = path;
        }
    }

    static final class Result {
        final Status status;
        final List<Event> events;

        Result(Status status, List<Event> events) {
            this.status = status;
            this.events = events;
        }
    }

    private AudioreapPushPoll() {}

    /** Blocking. The caller owns the thread. */
    static Result fetch(String serverUrl, String token) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(serverUrl + "/api/push/pending").openConnection();
            if (!(connection instanceof HttpsURLConnection)) {
                // AudioreapPreferences only ever stores https URLs; this is
                // belt-and-braces against a credential ever going out in the clear.
                Log.w(TAG, "refusing to send the device credential over a non-HTTPS connection");
                return new Result(Status.UNAUTHORIZED, List.of());
            }
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Authorization", "Bearer " + token);
            connection.setRequestProperty("Accept", "application/json");
            connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
            connection.setReadTimeout(READ_TIMEOUT_MS);
            connection.setUseCaches(false);
            // A 302 here is an SSO gateway offering a login page, which is a failure to
            // report rather than an HTML body to chase.
            connection.setInstanceFollowRedirects(false);

            int status = connection.getResponseCode();
            if (status == HttpURLConnection.HTTP_UNAUTHORIZED
                || status == HttpURLConnection.HTTP_FORBIDDEN) {
                Log.w(TAG, "the server rejected this device's credential (" + status + ")");
                return new Result(Status.UNAUTHORIZED, List.of());
            }
            if (status != HttpURLConnection.HTTP_OK) {
                Log.d(TAG, "push poll returned " + status);
                return new Result(Status.UNAVAILABLE, List.of());
            }

            StringBuilder body = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(connection.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) body.append(line);
            }
            return new Result(Status.OK, parse(body.toString()));
        } catch (Exception error) {
            // Offline, DNS failure, timeout: all ordinary on a phone, none exceptional.
            Log.d(TAG, "push poll failed: " + error);
            return new Result(Status.UNAVAILABLE, List.of());
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    /**
     * Read the `events` array out of the response.
     *
     * Package-private so it can be exercised off-device: the parse is where a silent
     * break costs a missed notification rather than a crash. An event missing its id or
     * title is dropped rather than defaulted — the id is the shade tag that keeps a
     * repost from stacking, and a notification with no name is not worth a buzz.
     */
    static List<Event> parse(String json) {
        List<Event> events = new ArrayList<>();
        try {
            JSONArray items = new JSONObject(json).optJSONArray("events");
            if (items == null) return events;
            for (int i = 0; i < items.length(); i++) {
                JSONObject item = items.optJSONObject(i);
                if (item == null) continue;
                String id = item.optString("id", "");
                String title = item.optString("title", "");
                if (id.isEmpty() || title.isEmpty()) continue;
                events.add(new Event(
                    id,
                    title,
                    item.optString("body", ""),
                    AudioreapNotificationLink.safePath(item.optString("url", ""))
                ));
            }
        } catch (Exception error) {
            Log.w(TAG, "unreadable push response", error);
        }
        return events;
    }
}
