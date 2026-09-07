package com.bt.vphone

import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.MediaMetadata
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import kotlin.concurrent.thread

/**
 * 门B 引擎: MediaSession 播放器 —— 元数据(标题/歌手/专辑/时长)全部代码任意设,
 * 1s 心跳推进进度, 自动连播, 车机 AVRCP 按键(播放/暂停/切歌/拖进度)回流到回调。
 * 真机系统栈负责把 MediaSession 翻成 AVRCP 推给车机 —— 总时长/进度天然正确,
 * 不存在 Windows SMTC 那套时间轴时序坑。
 * 可选静音 AudioTrack 输出保持 A2DP 活跃(与 Windows 静音流同一思路)。
 */
object MediaEngine {
    data class Track(val title: String, val artist: String, val album: String, val dur: Int)

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

    /** 静音流默认开: 车机 A2DP 需要真实音频流才会进入"播放中"并接受播放控制 */
    @Volatile private var silenceWanted = true
    private var audioTrack: AudioTrack? = null
    private var silenceThread: Thread? = null

    // 音频焦点: 真播放器行为 —— play 时抢占 USAGE_MEDIA 焦点, 车机 AVRCP 按键路由跟着焦点走
    private var focusListener: AudioManager.OnAudioFocusChangeListener? = null
    private var focused = false

    fun init(ctx: Context) {
        app = ctx.applicationContext
    }

    fun start() {
        if (session != null) return
        session = MediaSession(app, "VPhoneMedia").apply {
            setCallback(object : MediaSession.Callback() {
                override fun onPlay() {
                    play()
                    evt("车机按键: 【播放】")
                }

                override fun onPause() {
                    pause()
                    evt("车机按键: 【暂停】")
                }

                override fun onSkipToNext() {
                    next()
                    evt("车机按键: 【下一曲】")
                }

                override fun onSkipToPrevious() {
                    prev()
                    evt("车机按键: 【上一曲】")
                }

                override fun onStop() {
                    pause()
                    evt("车机按键: 【停止】")
                }

                override fun onSeekTo(pos: Long) {
                    posSec = (pos / 1000).toInt()
                    pushState()
                    evt("车机按键: 【拖进度】→ ${posSec}s")
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

    /** 行格式: 标题|歌手|专辑|时长秒 (多行或以 ; 分隔, # 注释) */
    fun loadPlaylist(text: String): String {
        val list = text.split('\n', ';').mapNotNull { raw ->
            val line = raw.trim()
            if (line.isEmpty() || line.startsWith("#")) return@mapNotNull null
            val p = line.split('|')
            Track(
                p.getOrElse(0) { "" }, p.getOrElse(1) { "" }, p.getOrElse(2) { "" },
                p.getOrElse(3) { "" }.toIntOrNull() ?: 240
            )
        }
        if (list.isEmpty()) return "播放列表为空(无有效行)"
        playlist = list.toMutableList()
        plIndex = 0
        applyIndex()
        pushMeta()
        pushState()
        return "播放列表 ${playlist.size} 首, 当前: ${trackDesc()}"
    }

    fun play(): String {
        playing = true
        session?.isActive = true   // 每次播放重新抢占媒体会话(防被其它 App 顶掉)
        takeFocus()
        pushMeta()
        pushState()
        startTicker()
        if (silenceWanted) startSilence()
        pinA2dp()   // A2DP 可能晚于建流转连, 每次播放补绑一次
        return "播放中: ${trackDesc()} (音频焦点=${if (focused) "已获取" else "未获取"})"
    }

    fun pause(): String {
        playing = false
        pushState()
        stopTicker()
        return "已暂停: ${trackDesc()}"
    }

    fun next(): String = advance(+1)
    fun prev(): String = advance(-1)

    private fun advance(d: Int): String {
        if (playlist.isNotEmpty()) {
            plIndex = (plIndex + d + playlist.size) % playlist.size
            applyIndex()
        } else {
            posSec = 0
        }
        pushMeta()
        pushState()
        return "切换 → ${trackDesc()}"
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

    // ---------------- 时间轴 1s 心跳 ----------------

    private var ticker: Runnable? = null

    private fun startTicker() {
        if (ticker != null) return
        val r = object : Runnable {
            override fun run() {
                posSec++
                if (posSec >= durationSec) {
                    if (autoAdvance && playlist.size > 1) advance(+1) else posSec = 0
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
                        if (silenceWanted) {
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
     * 修法: 把静音轨道硬绑到 A2DP 输出设备(setPreferredDevice)。
     */
    private fun pinA2dp() {
        if (Build.VERSION.SDK_INT < 23) return
        try {
            val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val a2 = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                .firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP }
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

    private fun evt(msg: String) {
        Log.i(CallEngine.TAG, "[媒体事件] $msg")
        try {
            app.sendBroadcast(Intent(CallEngine.EVT_ACTION).putExtra("msg", "[媒体] $msg"))
        } catch (_: Exception) {
        }
    }

    fun status(): String =
        "media=${if (playing) "playing" else "paused"} " +
            "track=\"$title\"/$artist pos=${posSec}s dur=${durationSec}s " +
            "playlist=${playlist.size}首 idx=$plIndex autoAdvance=$autoAdvance " +
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
        abandonFocus()
        session?.release()
        session = null
    }
}
