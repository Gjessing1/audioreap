package io.gjessing.audioreap;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

/**
 * When audioreap next asks the server what has finished downloading.
 *
 * A download is exactly the thing you walk away from: an album takes minutes, and the
 * whole point of a notification is that the app is closed while it runs. The obvious
 * implementation — hold a connection open and listen — is the one Android will not give
 * an app for free: a socket that outlives the activity needs a foreground service, and a
 * foreground service must post a permanent "audioreap is running" notice in the shade.
 * Waking on an alarm and asking costs one small HTTPS request and nothing in the shade.
 *
 * `setAndAllowWhileIdle` is the alarm type that matters: an ordinary alarm is deferred to
 * the next maintenance window once the phone enters Doze, so an album queued at midnight
 * would go unmentioned until morning. This one fires through Doze, at the cost of being
 * rate-limited to roughly one wake per nine minutes while idle — which is why the idle
 * interval below sits past that rather than pretending to be faster than the platform is.
 *
 * Not an *exact* alarm. Exact alarms are for things the user scheduled — an appointment,
 * a timer — and Android increasingly treats them as a permission-worthy claim. A finished
 * download has no appointed time; a few minutes of slack is invisible, so the inexact form
 * is both the honest declaration and the one the system can batch with other apps' wakes.
 */
final class AudioreapPushAlarm {
    private static final String TAG = "AudioreapPush";
    static final String ACTION_POLL = "io.gjessing.audioreap.PUSH_POLL";
    private static final int REQUEST_CODE = 4711;

    /**
     * Screen on: the user is around, and a review queue they are waiting on is worth
     * knowing about promptly. Nothing is being held open, so the cost is one request.
     */
    private static final long INTERVAL_AWAKE_MS = 5 * 60_000L;
    /** Screen off: past Doze's own rate limit, where a shorter interval would be fiction. */
    private static final long INTERVAL_ASLEEP_MS = 15 * 60_000L;

    private AudioreapPushAlarm() {}

    /** Arm the next check if this device holds a credential. Safe to call repeatedly. */
    static void enable(Context context) {
        Context appContext = context.getApplicationContext();
        if (AudioreapPreferences.getPushToken(appContext) == null) return;
        if (AudioreapPreferences.getServerUrl(appContext) == null) return;
        armNext(appContext);
    }

    /** Stop asking. Nothing else to tear down — there is no service and no socket. */
    static void disable(Context context) {
        Context appContext = context.getApplicationContext();
        AlarmManager alarmManager = appContext.getSystemService(AlarmManager.class);
        PendingIntent pending = pollIntent(appContext, PendingIntent.FLAG_NO_CREATE);
        if (pending == null) return;
        if (alarmManager != null) alarmManager.cancel(pending);
        pending.cancel();
    }

    /**
     * Schedule the check after this one. Called on every fire rather than set as a
     * repeating alarm: `setAndAllowWhileIdle` has no repeating form, and re-arming each
     * time is what lets the interval follow the screen.
     */
    static void armNext(Context context) {
        Context appContext = context.getApplicationContext();
        AlarmManager alarmManager = appContext.getSystemService(AlarmManager.class);
        if (alarmManager == null) return;
        long interval = isAwake(appContext) ? INTERVAL_AWAKE_MS : INTERVAL_ASLEEP_MS;
        try {
            // ELAPSED_REALTIME, not RTC: this is "in five minutes", and a clock
            // correction or a timezone change must not move it.
            alarmManager.setAndAllowWhileIdle(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + interval,
                pollIntent(appContext, PendingIntent.FLAG_UPDATE_CURRENT)
            );
        } catch (Exception error) {
            Log.w(TAG, "could not schedule the next check", error);
        }
    }

    private static boolean isAwake(Context context) {
        PowerManager power = context.getSystemService(PowerManager.class);
        return power != null && power.isInteractive();
    }

    private static PendingIntent pollIntent(Context context, int flags) {
        Intent intent = new Intent(context, AudioreapPushReceiver.class).setAction(ACTION_POLL);
        return PendingIntent.getBroadcast(
            context,
            REQUEST_CODE,
            intent,
            flags | PendingIntent.FLAG_IMMUTABLE
        );
    }
}
