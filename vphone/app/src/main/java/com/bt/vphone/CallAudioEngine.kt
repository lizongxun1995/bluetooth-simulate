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
 * 进度: /call/audio-status 一行查询(上位机进度条), /call/audio?seek=N 拖动。
 */
object CallAudioEngine {

    private var mp: MediaPlayer? = null

    @Volatile var playingName: String? = null
        private set

    /** 原始文件名(无"(循环)"后缀): progress()/seek() 报进度用, playingName 是给人看的装饰名 */
    @Volatile private var rawName: String? = null

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
                rawName = null
                EventLog.add(EventLog.CALL_AUDIO_END, "app", "通话音频播完: ${f.name}")
            }
            m.prepare()
            m.start()
            pinSco(m)
            rawName = f.name
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
        rawName = null
        return if (had != null) "通话音频已停($had)" else "本就未在播"
    }

    /**
     * /call/audio-status: 一行进度, 风格对齐 /media/status, 供上位机 1s 轮询画进度条。
     * 未播放(从未播/手动停/自然播完)统一 off; 播放与通话状态无关 —— 无通话但音频在播
     * 时照实返回 playing(是否该播由上位机结合事件流判断, 此处不耦合)。
     * 在播判据是 playingName 而非 mp: 自然播完回调只置空名字不 release 播放器
     * (onCompletion 里 release 自身会死锁, 见 MediaEngine 同型事故)。
     */
    fun progress(): String {
        val m = mp
        if (playingName == null || m == null) return "callAudio=off"
        return try {
            val pos = m.currentPosition / 1000
            val dur = try { m.duration / 1000 } catch (_: Exception) { 0 }
            "callAudio=playing name=\"${rawName ?: playingName}\" pos=${pos}s dur=${dur}s" +
                " loop=${if (m.isLooping) 1 else 0}"
        } catch (_: Exception) {
            "callAudio=off"
        }
    }

    /** /call/audio?seek=N: 拖动通话音频进度, 钳制 [0,dur]。未播放明确报错, 不静默成功也不误起播。 */
    fun seek(sec: Int): String {
        val m = mp
        if (playingName == null || m == null)
            return "未在播放，无可拖动进度 (先 /call/audio?name=xx.mp3)"
        return try {
            val dur = try { m.duration / 1000 } catch (_: Exception) { 0 }
            val target = if (dur > 0) sec.coerceIn(0, dur) else maxOf(sec, 0)
            m.seekTo(target * 1000)
            "通话音频进度: ${target}s/${dur}s"
        } catch (e: Exception) {
            "!! 通话音频拖动失败: ${e.message}"
        }
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
