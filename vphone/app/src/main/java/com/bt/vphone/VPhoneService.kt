package com.bt.vphone

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log

/** 前台常驻服务: 电话账号注册 + MediaSession + HTTP 控制面, 防后台被杀。 */
class VPhoneService : Service() {

    private lateinit var server: ControlServer
    private var wakeLock: android.os.PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        CallEngine.init(this)
        MediaEngine.init(this)
        BtEngine.init(this)
        val acct = CallEngine.registerAccount()
        Log.i(CallEngine.TAG, acct)
        MediaEngine.start()
        server = ControlServer(PORT).also { it.start() }
        startForeground(1, notification("VPhone 虚拟手机运行中", "HTTP :$PORT · 电话账号见日志 · logcat TAG=VPhone"))
        // EMUI 后台省电会限流解码线程 → A2DP 欠载爆音(滋滋)。持部分 WakeLock 免降频。
        wakeLock = (getSystemService(Context.POWER_SERVICE) as android.os.PowerManager)
            .newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "vphone:core").apply {
                setReferenceCounted(false)
                acquire()
            }
        Log.i(
            CallEngine.TAG,
            "服务就绪: HTTP 0.0.0.0:$PORT (PC: adb forward tcp:18800 tcp:$PORT → curl 127.0.0.1:18800)"
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        server.stop()
        MediaEngine.release()
        try { wakeLock?.release() } catch (_: Exception) {}
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun notification(title: String, text: String): Notification {
        val channelId = "vphone"
        if (Build.VERSION.SDK_INT >= 26) {
            val ch = NotificationChannel(channelId, "VPhone 常驻服务", NotificationManager.IMPORTANCE_LOW)
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
        val flags = if (Build.VERSION.SDK_INT >= 23) PendingIntent.FLAG_IMMUTABLE else 0
        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), flags)
        val builder =
            if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, channelId)
            else @Suppress("DEPRECATION") Notification.Builder(this)
        return builder
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_vphone)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
    }

    companion object {
        const val PORT = 8800

        fun ensureStarted(ctx: Context) {
            val it = Intent(ctx, VPhoneService::class.java)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(it)
            else ctx.startService(it)
        }
    }
}
