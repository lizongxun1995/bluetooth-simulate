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
 *
 * 第九轮(多路): 对端音频绑定到 CallRec —— 每路通话各自的"对端"可绑不同文件,
 * HFP 单 SCO 决定车机永远只听得到 active 那路; 通话切换(swap/answer/hold/挂断)
 * 时 followForeground() 换源: 新 active 路从自己上次的进度续播, 无绑定则静音。
 * 进度行末尾 num= 报当前出声的号码。
 */
object CallAudioEngine {

    private var mp: MediaPlayer? = null

    @Volatile var playingName: String? = null
        private set

    /** 原始文件名(无"(循环)"后缀): progress()/seek() 报进度用, playingName 是给人看的装饰名 */
    @Volatile private var rawName: String? = null

    /** 当前在播的音频属于哪路通话(第九轮: 对端音频绑定到 CallRec, 跟随前景切换) */
    private var boundRec: CallEngine.CallRec? = null

    /** /call/audio?name=: 绑定到当前 active 那路并立即起播(重发同一路 = 从头重播)。
     *  HFP 单 SCO —— 只有 active 路的声音车机听得到, 无 active 时明确拒绝(真机同构)。 */
    fun play(name: String, loop: Boolean): String {
        if (name.isBlank()) return "用法: /call/audio?name=xx.mp3&loop=1 (stop=1 停止)"
        val rec = CallEngine.activeRec()
            ?: return "无 active 通话, 对端音频须绑定在接通的一路 (先来电+接听; 当前 ${CallEngine.state})"
        val f = musicFile(name)
        if (f == null) return "音频不存在: $name (先 /media/upload?name=xx 上传, /media/files 看乐库)"
        rec.audioName = f.name
        rec.audioLoop = loop
        rec.audioPosMs = 0
        return playInternal(f.name, loop, 0, rec)
    }

    /** 实际起播(主线程)。startMs>0 用于通话切换后恢复该路自己的进度。 */
    private fun playInternal(name: String, loop: Boolean, startMs: Int, rec: CallEngine.CallRec): String {
        val f = musicFile(name) ?: return "音频不存在: $name"
        stopInternal()
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
                boundRec?.audioPosMs = 0        // 播完归零, 再切回这路从头说
                EventLog.add(EventLog.CALL_AUDIO_END, "app", "通话音频播完: ${f.name} (${rec.number})")
            }
            m.prepare()
            if (startMs > 0) try { m.seekTo(startMs) } catch (_: Exception) {}
            m.start()
            pinSco(m)
            rawName = f.name
            mp = m
            boundRec = rec
            playingName = if (loop) "${f.name}(循环)" else f.name
            "通话音频播放中: $playingName (${f.length() / 1024}KB) → ${rec.number}" +
                if (startMs > 0) " (续 ${startMs / 1000}s)" else ""
        } catch (e: Exception) {
            "通话音频播放失败: ${e.message}"
        }
    }

    /** /call/audio?stop=1: 手动停 = 对端"闭嘴", 同时解绑该路的音频(再激活不再自响起播)。 */
    fun stop(): String {
        val had = playingName
        boundRec?.let { it.audioName = null; it.audioLoop = false; it.audioPosMs = 0 }
        stopInternal()
        return if (had != null) "通话音频已停($had), 解除绑定" else "本就未在播"
    }

    /** 机械停(释放播放器+清在播态), 不动 CallRec 上的绑定 —— 通话切换内部用。 */
    private fun stopInternal() {
        mp?.let { try { it.release() } catch (_: Exception) {} }
        mp = null
        playingName = null
        rawName = null
        boundRec = null
    }

    /**
     * 通话切换/保持/挂断后由 CallEngine 调用(主线程): 让"车机听到的对端声音"跟随 active 路。
     * 真机基准: HFP 单 SCO, 只有 active 路出声 ——
     *   新 active 有绑定 → 播它的(从它上次进度续); 无绑定/无 active → 静音(绑定保留, 恢复通话再自响起播)。
     * 实际发生可闻变化时发 CALL_AUDIO_FOLLOW 事件(PC 侧断言"切换后声音变了"的证据)。
     */
    fun followForeground() {
        val wasName = playingName
        val prevRec = boundRec
        savePos()                               // 旧在播路记住进度, 切回来续播
        val rec = CallEngine.activeRec()
        when {
            rec == null ->
                if (prevRec != null) {
                    stopInternal()
                    if (wasName != null)
                        EventLog.add(EventLog.CALL_AUDIO_FOLLOW, "app", "无 active 通话 → 对端音频静音")
                }
            prevRec === rec && mp != null -> return   // 前景没变(如等待路进来), 声音不断
            rec.audioName == null ->
                if (prevRec != null) {
                    stopInternal()
                    if (wasName != null)
                        EventLog.add(EventLog.CALL_AUDIO_FOLLOW, "app",
                            "通话切换 → ${rec.number} 无对端音频, 静音")
                }
            else -> {
                val r = playInternal(rec.audioName!!, rec.audioLoop, rec.audioPosMs, rec)
                EventLog.add(EventLog.CALL_AUDIO_FOLLOW, "app",
                    "通话切换 → 对端音频跟随: ${rec.number} ${rec.audioName} " +
                        "(续 ${rec.audioPosMs / 1000}s) $r")
            }
        }
    }

    /** 在播路的进度存回它的 CallRec(followForeground 换源前调用) */
    private fun savePos() {
        val rec = boundRec ?: return
        val m = mp ?: return
        if (playingName == null) return
        try { rec.audioPosMs = m.currentPosition } catch (_: Exception) {}
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
                " loop=${if (m.isLooping) 1 else 0} num=${boundRec?.number ?: "?"}"
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

    fun status(): String =
        "callAudio=${playingName ?: "off"}" + (boundRec?.let { " num=${it.number}" } ?: "")

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
