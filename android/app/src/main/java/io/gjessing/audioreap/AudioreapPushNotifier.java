package io.gjessing.audioreap;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

/**
 * Posts audioreap's notifications. One channel, "Downloads", default importance.
 *
 * Nothing here is ongoing or silent: every notification this app posts is a thing that
 * finished while the user was elsewhere, and is dismissed by tapping it. There is no
 * "audioreap is running" notice to keep, because nothing runs — see AudioreapPushAlarm.
 */
final class AudioreapPushNotifier {
    private AudioreapPushNotifier() {}

    static String channelId(Context context) {
        return context.getString(R.string.downloads_notification_channel_id);
    }

    /**
     * Register the channel. Android 8+ silently drops a notification whose channel was
     * never created, and creating an existing channel is a no-op, so this runs on every
     * entry point that might post rather than once somewhere clever.
     */
    static void ensureChannels(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;

        NotificationChannel downloads = new NotificationChannel(
            channelId(context),
            context.getString(R.string.downloads_notification_channel_name),
            NotificationManager.IMPORTANCE_DEFAULT
        );
        downloads.setDescription(context.getString(R.string.downloads_notification_channel_description));
        manager.createNotificationChannel(downloads);
    }

    /**
     * Post one event.
     *
     * The tag is the server's event id — "album:<id>", "job:<id>" — so a batch notified
     * twice replaces its own entry instead of stacking a second copy, and two different
     * batches never collapse into one line.
     */
    static void post(Context context, AudioreapPushPoll.Event event) {
        Notification notification = new NotificationCompat.Builder(context, channelId(context))
            .setSmallIcon(R.drawable.ic_notification)
            .setColor(ContextCompat.getColor(context, R.color.colorPrimary))
            .setContentTitle(event.title)
            .setContentText(event.body)
            // Album and track names run long on a phone; let the shade expand to it.
            .setStyle(new NotificationCompat.BigTextStyle().bigText(event.body))
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .setContentIntent(launchIntent(context, event))
            .build();
        try {
            NotificationManagerCompat.from(context).notify(event.id, 0, notification);
        } catch (SecurityException ignored) {
            // POST_NOTIFICATIONS revoked between the permission check and the post.
        }
    }

    /**
     * Launch MainActivity carrying the path this event belongs to.
     *
     * `FLAG_UPDATE_CURRENT` with a per-event request code: without a distinct code
     * Android would reuse one PendingIntent across every notification, and every tap
     * would open whichever event was posted last.
     */
    private static PendingIntent launchIntent(Context context, AudioreapPushPoll.Event event) {
        Intent intent = new Intent(context, MainActivity.class)
            .setAction(Intent.ACTION_MAIN)
            .addCategory(Intent.CATEGORY_LAUNCHER)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(AudioreapNotificationLink.PATH_EXTRA, event.path);
        return PendingIntent.getActivity(
            context,
            event.id.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
    }
}
