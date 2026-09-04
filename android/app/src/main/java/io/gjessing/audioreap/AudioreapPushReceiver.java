package io.gjessing.audioreap;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.PowerManager;
import android.util.Log;

/**
 * Every reason to ask the server what has finished arrives here: the alarm firing, or an
 * event that clears the armed alarm out from under it.
 *
 * A reboot cancels every alarm the app had set, and an in-place APK update does the same
 * without a boot broadcast following it. Without re-arming on both, notifications would
 * stay silently off until the user next opened audioreap — for a feature whose whole job
 * is telling you about a download you are not watching, indistinguishable from broken.
 *
 * A no-op on a device where notifications were never turned on: {@link
 * AudioreapPushAlarm#enable} checks for a stored credential first.
 */
public class AudioreapPushReceiver extends BroadcastReceiver {
    private static final String TAG = "AudioreapPush";
    private static final String WAKE_LOCK_TAG = "audioreap:push-poll";
    /**
     * Past the poll's own timeouts with room to spare, and far short of the minute a
     * background broadcast is allowed — a bug must never be able to pin the CPU on.
     */
    private static final long WAKE_LOCK_TIMEOUT_MS = 45_000L;

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (action == null) return;
        Context appContext = context.getApplicationContext();

        switch (action) {
            case AudioreapPushAlarm.ACTION_POLL:
                // The alarm is one-shot, so the next one is armed here whatever the poll
                // finds — including when it fails. A missed check must not end the series.
                inBackground(appContext, () -> {
                    poll(appContext);
                    AudioreapPushAlarm.armNext(appContext);
                });
                return;

            case Intent.ACTION_BOOT_COMPLETED:
            case Intent.ACTION_MY_PACKAGE_REPLACED:
                AudioreapPushAlarm.enable(appContext);
                return;

            default:
                Log.w(TAG, "ignoring unexpected action " + action);
        }
    }

    /**
     * Ask, and post whatever came back.
     *
     * The server answers with what this device has not been told about yet and advances
     * its cursor as it does, so an empty answer — the common case — is one small request
     * and nothing else.
     */
    private static void poll(Context context) {
        String serverUrl = AudioreapPreferences.getServerUrl(context);
        String token = AudioreapPreferences.getPushToken(context);
        if (serverUrl == null || token == null) return;

        AudioreapPushPoll.Result result = AudioreapPushPoll.fetch(serverUrl, token);
        if (result.status == AudioreapPushPoll.Status.UNAUTHORIZED) {
            // The server no longer knows this credential — revoked from another client,
            // or the database was restored from before this device registered. Asking
            // again can only fail; drop it and let the web app register anew the next
            // time audioreap is opened.
            AudioreapPreferences.setPushToken(context, null);
            AudioreapPushAlarm.disable(context);
            return;
        }
        if (result.events.isEmpty()) return;

        AudioreapPushNotifier.ensureChannels(context);
        for (AudioreapPushPoll.Event event : result.events) {
            AudioreapPushNotifier.post(context, event);
        }
    }

    /**
     * Keep the process — and the CPU — alive past onReceive for the network call inside.
     *
     * Both halves are needed and they are not the same thing. `goAsync` is what stops the
     * system tearing the receiver down the moment onReceive returns. The wake lock is what
     * stops the *device* going back to sleep at that same moment: AlarmManager holds a
     * wake lock only for the duration of onReceive, so without one of our own an idle
     * phone can suspend with the request half-sent, and the check silently accomplishes
     * nothing until the next wake.
     */
    private void inBackground(Context context, Runnable work) {
        PendingResult result = goAsync();
        PowerManager power = context.getSystemService(PowerManager.class);
        PowerManager.WakeLock wakeLock =
            power == null ? null : power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG);
        // Timed: the lock is released below in the ordinary case, and the timeout is the
        // backstop for a thread that never gets there.
        if (wakeLock != null) wakeLock.acquire(WAKE_LOCK_TIMEOUT_MS);

        new Thread(() -> {
            try {
                work.run();
            } catch (Exception error) {
                Log.w(TAG, "download check failed", error);
                AudioreapPushAlarm.armNext(context);
            } finally {
                result.finish();
                if (wakeLock != null && wakeLock.isHeld()) {
                    try {
                        wakeLock.release();
                    } catch (RuntimeException ignored) {
                        // Released by its own timeout between the check and here.
                    }
                }
            }
        }).start();
    }
}
