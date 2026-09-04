"""Background notifications for the Android app.

The APK is a WebView around this server's UI, so it can only be told about a finished
download while it is open — which is exactly when the user does not need telling. The
shell therefore wakes on its own alarm every few minutes and asks the server what it
has missed; this package is the server half of that conversation.

- ``devices``  mints and checks the per-device credential that poll presents
- ``pending``  decides what a device is still owed a notification for
"""
