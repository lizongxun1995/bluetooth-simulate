package com.bt.vphone

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.media.MediaPlayer
import android.os.Build
import android.util.Log
import java.io.File

/**
 * 通话音频引擎: 虚拟通话中进行车机播放自定义音频 —— 模拟"对端说话"。
 *
 * 原理: MediaPlayer 用 USAGE_VOICE_COMMUNICATION + CONTENT_TYPE_SPEECH,
 * 系统在通话中会把该 usage 路由到 HFP SCO 下行 → 车机扬声器放出;
 * 再 setPreferredDevice(TYPE_BLUETOOTH_SCO) 双保险(EMUI 类 SCO/媒体路由互抢的坑)。
 * 音频文件来自 /media/upload 上传的乐库(music 目录), 与门B共用。
 *
 * 用法: 先有通话(active, 车机接听或 answer/auto-outgoing) → /call/audio?name=xx.mp3
 * 车机听筒/扬声器即播放该音频; 无通话时播放无意义(SCO 未建立, 车机听不到)。
 */
object CallAudioEngine {

    private var mp: MediaPlayer? = null

    @Volatile var playingName: String? = null
        private set

    fun play(name: String, loop: Boolean): String {
        if (name.isBlank()) return "用法: /call/audio?name=xx.mp3&loop=1 (stop=1 停止)"
        val f = musicFile(name)
        if (f == null) return "音频不存在: $name (先 /media/upload?name=xx 上传, /media/files 看乐库)"
        stop()
        return try {
            val m = MediaPlayer()
            m.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            m.setDataSource(f.absolutePath)
            m.isLooping = loop
            m.setOnCompletionListener {
                playingName = null
                EventLog.add(EventLog.CALL_AUDIO_END, "app", "通话音频播完: ${f.name}")
            }
            m.prepare()
            m.start()
            pinSco(m)
            mp = m
            playingName = if (loop) "${f.name}(循环)" else f.name
            val warn = if (CallEngine.state != "active")
                "  ⚠ 当前无通话(state=${CallEngine.state}), SCO 未建立车机听不到 —— 先来电+接听"
            else ""
            "通话音频播放中: $playingName (${f.length() / 1024}KB)$warn"
        } catch (e: Exception) {
            "通话音频播放失败: ${e.message}"
        }
    }

    fun stop(): String {
        val had = playingName
        mp?.let { try { it.release() } catch (_: Exception) {} }
        mp = null
        playingName = null
        return if (had != null) "通话音频已停($had)" else "本就未在播"
    }

    fun status(): String = "callAudio=${playingName ?: "off"}"

    /** EMUI 坑同门B: 把播放器硬绑 SCO 输出, 防被切到别的通道。MediaPlayer 系 API 28+。 */
    private fun pinSco(m: MediaPlayer) {
        if (Build.VERSION.SDK_INT < 28) return
        try {
            val am = CallEngine.app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val sco = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                .firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO }
            if (sco != null) {
                m.preferredDevice = sco
                Log.i(CallEngine.TAG, "通话音频已绑 SCO 输出: ${sco.productName}")
            } else {
                Log.w(CallEngine.TAG, "未见 SCO 输出设备(通话未建立/未路由蓝牙), 走系统默认通话路由")
            }
        } catch (t: Throwable) {
            Log.w(CallEngine.TAG, "SCO 路由绑定失败: ${t.message}")
        }
    }

    private fun musicFile(name: String): File? {
        val dir = File(CallEngine.app.filesDir, "music")
        val f = File(dir, name.substringAfterLast('/').substringAfterLast('\\'))
        return if (f.isFile) f else null
    }
}
