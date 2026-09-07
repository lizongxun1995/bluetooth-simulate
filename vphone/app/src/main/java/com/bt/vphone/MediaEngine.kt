package com.bt.vphone

import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.MediaMetadata
import android.media.MediaPlayer
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.io.File
import kotlin.concurrent.thread

/**
 * 门B 引擎: MediaSession 播放器 —— 元数据(标题/歌手/专辑/时长)全部代码任意设,
 * 1s 心跳推进进度, 自动连播, 车机 AVRCP 按键(播放/暂停/切歌/拖进度)回流到回调并落
 * EventLog(CAR_PLAY/CAR_NEXT/... 可断言)。
 *
 * 两种出声模式:
 *  A) 真实音频: 播放列表第 5 列指向乐库文件(/media/upload 从 PC 推送) → MediaPlayer
 *     解码播放, 时长/进度取真实值, 同样绑定 A2DP 输出 —— 车机播真歌;
 *  B) 静音流: 无文件时 AudioTrack 输出静音保活 A2DP, 元数据仍任意伪造。
 * 真机系统栈负责把 MediaSession 翻成 AVRCP 推给车机。
 */
object MediaEngine {
    /** 第 5 列 path=乐库文件名(可空)。时长给 0 时用真实音频时长。 */
    data class Track(
        val title: String, val artist: String, val album: String,
        val dur: Int, val path: String? = null
    )

    lateinit var app: Context
    private var session: MediaSession? = null
    private val main = Handler(Looper.getMainLooper())

    var title = "VPhone 待机"
    var artist = ""
    var album = ""
    var durationSec = 240
    var posSec = 0
        private set
    var playing = false
        private set
    var autoAdvance = true
    var playlist = mutableListOf<Track>()
        private set
    var plIndex = 0
        private set

    /** 静音流默认开: 无真实音频文件时保持 A2DP 活跃(车机才会进"播放中") */
    @Volatile private var silenceWanted = true
    private var audioTrack: AudioTrack? = null
    private var silenceThread: Thread? = null

    // 真实音频播放器(模式A)
    private var mp: MediaPlayer? = null
    private var realFor: File? = null   // 当前 MediaPlayer 绑定的文件

    // 音频焦点: 真播放器行为 —— play 时抢占 USAGE_MEDIA 焦点, 车机 AVRCP 按键路由跟着焦点走
    private var focusListener: AudioManager.OnAudioFocusChangeListener? = null
    private var focused = false

    fun init(ctx: Context) {
        app = ctx.applicationContext
    }

    fun musicDir(): File = File(CallEngine.app.filesDir, "music").apply { mkdirs() }

    fun start() {
        if (session != null) return
        session = MediaSession(app, "VPhoneMedia").apply {
            setCallback(object : MediaSession.Callback() {
                override fun onPlay() {
                    EventLog.add(EventLog.CAR_PLAY, "car", "车机按键【播放】")
                    doPlay()
                }

                override fun onPause() {
                    EventLog.add(EventLog.CAR_PAUSE, "car", "车机按键【暂停】")
                    doPause()
                }

                override fun onSkipToNext() {
                    EventLog.add(EventLog.CAR_NEXT, "car", "车机按键【下一曲】")
                    doAdvance(+1)
                }

                override fun onSkipToPrevious() {
                    EventLog.add(EventLog.CAR_PREV, "car", "车机按键【上一曲】")
                    doAdvance(-1)
                }

                override fun onStop() {
                    EventLog.add(EventLog.CAR_STOP, "car", "车机按键【停止】")
                    doPause()
                }

                override fun onSeekTo(pos: Long) {
                    posSec = (pos / 1000).toInt()
                    mp?.let { try { it.seekTo(posSec * 1000) } catch (_: Exception) {} }
                    pushState()
                    EventLog.add(EventLog.CAR_SEEK, "car", "车机按键【拖进度】→ ${posSec}s")
                }
            }, main)
            isActive = true
        }
        pushMeta()
        pushState()
        Log.i(CallEngine.TAG, "MediaSession 已激活")
    }

    // ---------------- 命令 ----------------

    fun setTrack(title: String, artist: String, album: String, dur: Int): String {
        if (title.isNotBlank()) this.title = title
        if (artist.isNotBlank()) this.artist = artist
        if (album.isNotBlank()) this.album = album
        if (dur > 0) durationSec = dur
        posSec = 0
        pushMeta()
        pushState()
        return "已推送: ${trackDesc()}"
    }

    /** 行格式: 标题|歌手|专辑|时长秒|乐库文件名 (后两列可选, 多行/;分隔, # 注释) */
    fun loadPlaylist(text: String): String {
        val list = text.split('\n', ';').mapNotNull { raw ->
            val line = raw.trim()
            if (line.isEmpty() || line.startsWith("#")) return@mapNotNull null
            val p = line.split('|')
            Track(
                p.getOrElse(0) { "" }, p.getOrElse(1) { "" }, p.getOrElse(2) { "" },
                p.getOrElse(3) { "" }.toIntOrNull() ?: 240,
                p.getOrNull(4)?.trim()?.ifBlank { null }
            )
        }
        if (list.isEmpty()) return "播放列表为空(无有效行)"
        playlist = list.toMutableList()
        plIndex = 0
        stopReal()
        applyIndex()
        if (playing) ensureAudioOut()
        pushMeta()
        pushState()
        return "播放列表 ${playlist.size} 首, 当前: ${trackDesc()}${realTag()}"
    }

    fun play(): String {
        EventLog.add(EventLog.CMD_PLAY, "cmd", "指令播放")
        return doPlay()
    }

    private fun doPlay(): String {
        playing = true
        session?.isActive = true   // 每次播放重新抢占媒体会话(防被其它 App 顶掉)
        takeFocus()
        ensureAudioOut()
        pushMeta()
        pushState()
        startTicker()
        pinA2dp()   // A2DP 可能晚于建流转连, 每次播放补绑一次
        return "播放中: ${trackDesc()}${realTag()} (音频焦点=${if (focused) "已获取" else "未获取"})"
    }

    fun pause(): String {
        EventLog.add(EventLog.CMD_PAUSE, "cmd", "指令暂停")
        return doPause()
    }

    private fun doPause(): String {
        playing = false
        pushState()
        stopTicker()
        try { mp?.pause() } catch (_: Exception) {}
        return "已暂停: ${trackDesc()}${realTag()}"
    }

    fun next(): String {
        EventLog.add("CMD_NEXT", "cmd", "指令下一曲")
        return doAdvance(+1)
    }

    fun prev(): String {
        EventLog.add("CMD_PREV", "cmd", "指令上一曲")
        return doAdvance(-1)
    }

    private fun doAdvance(d: Int): String {
        if (playlist.isNotEmpty()) {
            plIndex = (plIndex + d + playlist.size) % playlist.size
            applyIndex()
        } else {
            posSec = 0
        }
        if (playing) ensureAudioOut()
        pushMeta()
        pushState()
        return "切换 → ${trackDesc()}${realTag()}"
    }

    fun setAutoAdvance(on: Boolean): String {
        autoAdvance = on
        return "autoAdvance=$on"
    }

    private fun applyIndex() {
        val t = playlist[plIndex]
        title = t.title
        artist = t.artist
        album = t.album
        durationSec = t.dur
        posSec = 0
    }

    private fun trackDesc() =
        "「$title」/$artist  ${posSec}s/${durationSec}s ${if (playing) "▶" else "⏸"}"

    private fun realTag() = if (mp != null) " [真实音频:${realFor?.name}]" else ""

    // ---------------- 出声: 真实文件优先, 否则静音流 ----------------

    /** 当前曲目若配了乐库文件则返回之(带(播放)状态切换) */
    private fun realFile(): File? = playlist.getOrNull(plIndex)?.path?.let { p ->
        val f = File(musicDir(), p.substringAfterLast('/'))
        if (f.isFile) f else null
    }

    /** playing=true 时为当前曲目准备好声音输出: 有文件→MediaPlayer(真实), 无→静音流 */
    private fun ensureAudioOut() {
        val f = realFile()
        if (f != null) {
            stopSilence()
            if (mp == null || realFor != f) startReal(f)
            else try { mp?.start() } catch (_: Exception) { startReal(f) }
        } else {
            stopReal()
            if (silenceWanted) startSilence()
        }
    }

    private fun startReal(f: File): Boolean {
        stopReal()
        return try {
            val m = MediaPlayer()
            m.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build()
            )
            m.setDataSource(f.absolutePath)
            m.setOnCompletionListener {
                EventLog.add(EventLog.MEDIA_TRACK_END, "app", "真实音频播完: ${f.name}")
                if (autoAdvance && playlist.size > 1) {
                    doAdvance(+1)
                } else {
                    playing = false
                    posSec = durationSec
                    pushState()
                }
            }
            m.prepare()   // 本地文件同步 prepare, 快
            m.start()
            val d = m.duration / 1000
            if (d > 0) durationSec = d   // 真实时长优先于播放列表第4列
            mp = m
            realFor = f
            pinPlayerA2dp(m)
            Log.i(CallEngine.TAG, "真实音频播放: ${f.name} ${durationSec}s")
            true
        } catch (e: Exception) {
            Log.w(CallEngine.TAG, "真实音频播放失败(${e.message}) → 回退静音流")
            stopReal()
            if (silenceWanted) startSilence()
            false
        }
    }

    private fun stopReal() {
        mp?.let { try { it.release() } catch (_: Exception) {} }
        mp = null
        realFor = null
    }

    // ---------------- 乐库(/media/upload 推上来的文件) ----------------

    fun saveUpload(name: String, bytes: ByteArray): String {
        val safe = name.substringAfterLast('/').substringAfterLast('\\').trim()
        if (safe.isEmpty()) return "缺少 name 参数"
        if (bytes.isEmpty()) return "body 为空"
        val f = File(musicDir(), safe)
        f.writeBytes(bytes)
        EventLog.add(EventLog.MEDIA_UPLOAD, "cmd", "上传音频 $safe ${bytes.size}字节")
        return "已上传: $safe (${bytes.size}字节, ${f.length() / 1024}KB) → 播放列表第5列引用 $safe"
    }

    fun files(): String {
        val fs = musicDir().listFiles()
            ?.filter { it.isFile && it.name.contains('.') }
            ?.sortedBy { it.name } ?: return "乐库为空(先 /media/upload)"
        if (fs.isEmpty()) return "乐库为空(先 /media/upload?name=xx.mp3, body=文件字节)"
        return fs.joinToString("\n") { "${it.name}|${it.length() / 1024}KB" }
    }

    fun del(name: String): String {
        val safe = name.substringAfterLast('/')
        val f = File(musicDir(), safe)
        return if (f.isFile && f.delete()) "已删除 $safe" else "删除失败/不存在: $safe"
    }

    // ---------------- 时间轴 1s 心跳 ----------------

    private var ticker: Runnable? = null

    private fun startTicker() {
        if (ticker != null) return
        val r = object : Runnable {
            override fun run() {
                val m = mp
                if (m != null) {
                    // 真实模式: 进度取 MediaPlayer 实际位置(播完由 completion 回调处理)
                    posSec = try { m.currentPosition / 1000 } catch (_: Exception) { posSec }
                } else {
                    posSec++
                    if (posSec >= durationSec) {
                        if (autoAdvance && playlist.size > 1) doAdvance(+1) else posSec = 0
                    }
                }
                pushState()
                main.postDelayed(this, 1000)
            }
        }
        ticker = r
        main.postDelayed(r, 1000)
    }

    private fun stopTicker() {
        ticker?.let { main.removeCallbacks(it) }
        ticker = null
    }

    // ---------------- 推送到系统(→AVRCP→车机) ----------------

    private fun pushMeta() {
        session?.setMetadata(
            MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, title)
                .putString(MediaMetadata.METADATA_KEY_ARTIST, artist)
                .putString(MediaMetadata.METADATA_KEY_ALBUM, album)
                .putLong(MediaMetadata.METADATA_KEY_DURATION, durationSec * 1000L)
                .build()
        )
    }

    private fun pushState() {
        session?.setPlaybackState(
            PlaybackState.Builder()
                .setActions(
                    PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or
                        PlaybackState.ACTION_PLAY_PAUSE or PlaybackState.ACTION_SKIP_TO_NEXT or
                        PlaybackState.ACTION_SKIP_TO_PREVIOUS or PlaybackState.ACTION_SEEK_TO or
                        PlaybackState.ACTION_STOP
                )
                .setState(
                    if (playing) PlaybackState.STATE_PLAYING else PlaybackState.STATE_PAUSED,
                    posSec * 1000L,
                    if (playing) 1f else 0f
                )
                .build()
        )
    }

    // ---------------- 静音流(A2DP 保活, 可选) ----------------

    fun setSilence(on: Boolean): String {
        silenceWanted = on
        if (mp != null && on) return "静音流开关=$on (当前在放真实音频, 静音流不参与)"
        if (on) startSilence() else stopSilence()
        return if (on) "静音流已开(保持A2DP活跃)" else "静音流已关"
    }

    private fun startSilence() {
        if (audioTrack != null) return
        try {
            val rate = 44100
            val minBuf = AudioTrack.getMinBufferSize(
                rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            audioTrack = if (Build.VERSION.SDK_INT >= 23) {
                AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build()
                    )
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(rate)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build()
                    )
                    .setBufferSizeInBytes(maxOf(minBuf * 2, rate * 4)) // ≥2s
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build()
            } else {
                @Suppress("DEPRECATION")
                AudioTrack(AudioManager.STREAM_MUSIC, rate,
                    AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    maxOf(minBuf * 2, rate * 4), AudioTrack.MODE_STREAM)
            }
            audioTrack?.play()
            pinA2dp()
            silenceThread = thread(isDaemon = true, name = "vphone-silence") {
                val buf = ByteArray(rate * 2) // 1 秒 16bit 单声道全零 = 静音
                var sec = 0
                while (silenceWanted && audioTrack != null) {
                    try {
                        val n = audioTrack?.write(buf, 0, buf.size) ?: break
                        if (n < 0) {
                            Log.w(CallEngine.TAG, "静音流写返回 $n, 线程退出")
                            break
                        }
                        sec++
                        if (sec % 15 == 1)
                            Log.i(CallEngine.TAG, "静音流心跳 ${sec}s playState=${audioTrack?.playState}")
                    } catch (_: InterruptedException) {
                        break
                    } catch (e: Exception) {
                        Log.w(CallEngine.TAG, "静音流写失败: ${e.message}")
                        break
                    }
                }
                if (silenceWanted) {
                    // 意外退出(A2DP 建立瞬间路由切换等) → 自愈重建, 保证流不断
                    Log.w(CallEngine.TAG, "静音流意外退出, 2s 后自动重建")
                    main.postDelayed({
                        if (silenceWanted && mp == null) {
                            stopSilence()
                            startSilence()
                        }
                    }, 2000)
                }
            }
            Log.i(CallEngine.TAG, "静音流已启动(44.1kHz mono)")
        } catch (e: Exception) {
            Log.w(CallEngine.TAG, "静音流启动失败: ${e.message}")
            audioTrack = null
        }
    }

    /**
     * EMUI 坑: HFP 连上后系统把媒体主输出切到 SCO 通话通道(BLUETOOTH_SCO_CARKIT),
     * A2DP 不被选中 → 车机收不到流(dumpsys bluetooth_manager mIsPlaying=false)。
     * 修法: 把出声设备硬绑到 A2DP 输出(setPreferredDevice)。
     */
    private fun pinA2dp() {
        val m = mp
        if (m != null) {
            pinPlayerA2dp(m)
            return
        }
        if (Build.VERSION.SDK_INT < 23) return
        try {
            val a2 = findA2dpDevice()
            if (a2 != null) {
                audioTrack?.preferredDevice = a2
                Log.i(CallEngine.TAG, "静音流已绑定 A2DP 输出: ${a2.productName}")
            } else {
                Log.w(CallEngine.TAG, "未见 A2DP 输出设备(可能未连), 暂用系统默认路由")
            }
        } catch (t: Throwable) {
            Log.w(CallEngine.TAG, "A2DP 路由绑定失败: ${t.message}")
        }
    }

    /** MediaPlayer 绑 A2DP: setPreferredDevice 系 API 28+ */
    private fun pinPlayerA2dp(m: MediaPlayer) {
        if (Build.VERSION.SDK_INT < 28) return
        try {
            val a2 = findA2dpDevice()
            if (a2 != null) {
                m.preferredDevice = a2
                Log.i(CallEngine.TAG, "真实音频已绑定 A2DP 输出: ${a2.productName}")
            } else {
                Log.w(CallEngine.TAG, "未见 A2DP 输出设备(可能未连), 真实音频暂用默认路由")
            }
        } catch (t: Throwable) {
            Log.w(CallEngine.TAG, "真实音频 A2DP 绑定失败: ${t.message}")
        }
    }

    private fun findA2dpDevice(): AudioDeviceInfo? {
        val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        return am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            .firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP }
    }

    private fun stopSilence() {
        silenceThread?.interrupt()
        try {
            audioTrack?.stop()
        } catch (_: Exception) {
        }
        try {
            audioTrack?.release()
        } catch (_: Exception) {
        }
        audioTrack = null
        silenceThread = null
    }

    fun status(): String =
        "media=${if (playing) "playing" else "paused"} " +
            "track=\"$title\"/$artist pos=${posSec}s dur=${durationSec}s " +
            "playlist=${playlist.size}首 idx=$plIndex autoAdvance=$autoAdvance " +
            "real=${if (mp != null) realFor?.name else "none"} " +
            "silence=${if (silenceWanted) "on" else "off"} focus=${if (focused) "held" else "none"}"

    private fun takeFocus(): Boolean {
        if (focused) return true
        return try {
            val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val l = AudioManager.OnAudioFocusChangeListener { }
            val r = am.requestAudioFocus(l, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN)
            focused = r == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
            if (focused) focusListener = l
            focused
        } catch (t: Throwable) {
            Log.w(CallEngine.TAG, "音频焦点获取失败: ${t.message}")
            false
        }
    }

    private fun abandonFocus() {
        try {
            val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            focusListener?.let { am.abandonAudioFocus(it) }
        } catch (_: Throwable) {
        }
        focused = false
        focusListener = null
    }

    fun release() {
        stopTicker()
        stopSilence()
        stopReal()
        abandonFocus()
        session?.release()
        session = null
    }
}
