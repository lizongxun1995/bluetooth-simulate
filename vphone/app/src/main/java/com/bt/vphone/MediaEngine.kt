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
import android.os.SystemClock
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
 *
 * ── 线程模型 ── 本 object 的全部状态(playing/posSec/mp/playlist/...)只在主线程
 * 读写: MediaSession 回调、1s 心跳、Dispatcher.onMain 同步桥全部落在主线程。
 * 新的控制入口一律经 Dispatcher.onMain{} 进来, 不要从其他线程直接调。
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
                    EventLog.add(EventLog.CAR_PLAY, "car", "媒体按键【播放】(车机或手机通知栏)")
                    doPlay()
                }

                override fun onPause() {
                    EventLog.add(EventLog.CAR_PAUSE, "car", "媒体按键【暂停】(车机或手机通知栏)")
                    doPause()
                }

                override fun onSkipToNext() {
                    EventLog.add(EventLog.CAR_NEXT, "car", "媒体按键【下一曲】(车机或手机通知栏)")
                    doAdvance(+1)
                }

                override fun onSkipToPrevious() {
                    EventLog.add(EventLog.CAR_PREV, "car", "媒体按键【上一曲】(车机或手机通知栏)")
                    doAdvance(-1)
                }

                override fun onStop() {
                    EventLog.add(EventLog.CAR_STOP, "car", "媒体按键【停止】(车机或手机通知栏)")
                    doPause()
                }

                // AVRCP 规范没有"绝对定位"直传命令: 车机拖进度条要么走这里(系统栈支持时
                // 翻成绝对位置), 要么连发 FF/RW 透传键走下面两个回调 —— 三条路都得接住
                override fun onSeekTo(pos: Long) {
                    doSeek(pos, "car", "媒体按键【拖进度】(车机或手机通知栏)")
                }

                override fun onFastForward() {
                    doSeek(posSec * 1000L + 10_000L, "car", "媒体按键【快进+10s】(车机或手机通知栏)")
                }

                override fun onRewind() {
                    doSeek(posSec * 1000L - 10_000L, "car", "媒体按键【快退-10s】(车机或手机通知栏)")
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
        seekAt = 0   // 同 applyTrack: 换曲目即作废旧 seek 防回弹窗口
        seekPosMs = 0
        pushMeta()
        pushState()
        return "已推送: ${trackDesc()}"
    }

    /** 行格式: 标题|歌手|专辑|时长秒|乐库文件名 (后两列可选, 多行/;分隔, # 注释)。
     *  text 为空 = 查询当前播放列表(同格式回读, 供控制端编辑后回写)。 */
    fun loadPlaylist(text: String): String {
        if (text.isBlank()) {
            if (playlist.isEmpty()) return "# 播放列表为空"
            return playlist.joinToString("\n") {
                "${it.title}|${it.artist}|${it.album}|${it.dur}|${it.path ?: ""}"
            }
        }
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
        // 静音流必须彻底停(线程+轨), 不能只 track.pause(): 暂停的轨留着供流线程,
        // 一旦 A2DP 路由消失(车机断连/关蓝牙), write() 会退化成立即返回+丢弃数据
        // 的忙循环, 每秒几十万次写并刷爆 logcat(实测 16万行/s) —— 真机基准:
        // 暂停的播放器没有活跃音频流, 恢复播放时再重建即可
        stopSilence()
        return "已暂停: ${trackDesc()}${realTag()}"
    }

    fun next(): String {
        EventLog.add(EventLog.CMD_NEXT, "cmd", "指令下一曲")
        return doAdvance(+1)
    }

    fun prev(): String {
        EventLog.add(EventLog.CMD_PREV, "cmd", "指令上一曲")
        return doAdvance(-1)
    }

    private fun doAdvance(d: Int): String {
        if (playlist.isEmpty()) {
            // 真机基准: 没有播放列表就"无歌可切" —— 明确报出来, 而不是装作切过
            return "播放列表为空(先 /media/playlist 或 /media/track), 切歌无效"
        }
        plIndex = (plIndex + d + playlist.size) % playlist.size
        applyIndex()
        // 播放中: 换文件开播; 暂停中: 释放旧播放器(恢复时从新歌0s起), 避免挂着旧文件
        if (playing) ensureAudioOut() else stopReal()
        pushMeta()
        pushState()
        nudgeWhilePaused()
        return "切换 → ${trackDesc()}${realTag()}"
    }

    /** 跳到列表第 idx 首(0起)并按当前播放/暂停态就位 —— 播放列表管理"双击跳播"用 */
    fun jump(idx: Int): String {
        if (playlist.isEmpty()) return "播放列表为空(先在播放列表管理里添加)"
        val i = ((idx % playlist.size) + playlist.size) % playlist.size
        plIndex = i
        applyIndex()
        // 与 doAdvance 同一基准: 暂停中跳曲也要释放旧播放器, 否则 mp 仍绑旧文件
        // (status/realTag 显示旧歌, FF/RW 的 seekTo 会打到旧文件上)
        if (playing) ensureAudioOut() else stopReal()
        pushMeta()
        pushState()
        nudgeWhilePaused()
        return "跳转 → ${trackDesc()}${realTag()}"
    }

    /**
     * 部分车机栈在暂停态不主动刷新元数据显示(只认播放态变化) → 暂停中切歌/跳曲后
     * 再补推两次元数据+状态。对外不可见(接收方只能看到最终状态), 属健壮性补丁。
     */
    private fun nudgeWhilePaused() {
        if (playing) return
        main.postDelayed({ if (!playing) { pushMeta(); pushState() } }, 600)
        main.postDelayed({ if (!playing) { pushMeta(); pushState() } }, 1600)
    }

    // ---------------- 进度拖动(车机 AVRCP seek / PC 指令共用) ----------------

    /** 最近一次 seek 的时间/目标: MediaPlayer.seekTo 异步生效, 窗口内心跳优先用命令值,
     *  否则 1s 心跳可能读到旧位置把进度"弹回"一拍 */
    private var seekAt = 0L
    private var seekPosMs = 0L

    /** PC 指令拖进度: pos=目标秒 */
    fun seek(sec: Int): String = doSeek(sec * 1000L, "cmd", "指令拖进度")

    /** 拖进度核心: 夹取到 [0,时长], 真实模式同步 seekTo, 静音流模式只改元数据位置 */
    private fun doSeek(targetMs: Long, src: String, label: String): String {
        val dMs = durationSec * 1000L
        val ms = targetMs.coerceIn(0L, if (dMs > 0) dMs else targetMs)
        posSec = (ms / 1000).toInt()
        seekAt = System.currentTimeMillis()
        seekPosMs = ms
        mp?.let { try { it.seekTo(ms.toInt()) } catch (_: Exception) {} }
        pushState()
        EventLog.add(
            EventLog.CAR_SEEK, src,
            "$label→${posSec}s/${durationSec}s${if (mp == null) " (静音流模式, 仅元数据)" else ""}"
        )
        return "已跳转: ${trackDesc()}${realTag()}"
    }

    fun setAutoAdvance(on: Boolean): String {
        autoAdvance = on
        return "autoAdvance=$on"
    }

    /** 把 playlist[plIndex] 就位为当前曲目(全部字段复位统一走 applyTrack) */
    private fun applyIndex() = applyTrack(playlist[plIndex])

    /**
     * 换曲统一复位: doAdvance/jump/loadPlaylist 换曲目全走这里。
     * seekAt/seekPosMs 必须清零 —— 它们是"最近一次 seek 的防回弹窗口"(1.5s),
     * 真机基准: 切歌瞬间进度就是新歌 0s, 不能被上一首的 seek 窗口把进度闪回旧值。
     */
    private fun applyTrack(t: Track) {
        title = t.title
        artist = t.artist
        album = t.album
        // 第4列给0(时长未知, 真实音频播起后取实际值)时沿用上次值, 不能清成0
        // (旧版清0 → 车机在切歌瞬间/暂停切歌时显示 00:00 总时长)
        durationSec = if (t.dur > 0) t.dur else maxOf(durationSec, 1)
        posSec = 0
        seekAt = 0
        seekPosMs = 0
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
            if (silenceWanted) resumeOrStartSilence()
        }
    }

    /** 静音流: 已存在(可能是暂停态)则 resume, 否则新建 —— startSilence 遇已存在会直接 return, 暂停后永远恢复不了 */
    private fun resumeOrStartSilence() {
        val t = audioTrack
        if (t != null) {
            try { if (t.playState != AudioTrack.PLAYSTATE_PLAYING) t.play() } catch (_: Exception) {}
            pinA2dp()
            return
        }
        startSilence()
    }

    private fun startReal(f: File): Boolean {
        stopReal()
        forceMediaRoute()
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
                // post 到主线程再推进: 在 onCompletion 回调里直接 release 自己会死锁/抛错
                // (旧版startReal→stopReal→release 就发生在回调内 → "放完不播下一曲"的根因)
                main.post { handleTrackEnd() }
            }
            m.prepare()   // 本地文件同步 prepare, 快
            m.start()
            val d = m.duration / 1000
            if (d > 0) durationSec = d   // 真实时长优先于播放列表第4列
            mp = m
            realFor = f
            if (!pinPlayerA2dp(m)) {
                // A2DP 输出设备可能晚于播放就绪(车机重连时序) → 后台补绑直到成功或播放器被换
                thread(isDaemon = true, name = "vphone-pin") {
                    repeat(20) {
                        Thread.sleep(500)
                        val cur = mp ?: return@thread
                        if (cur !== m) return@thread
                        if (pinPlayerA2dp(cur)) return@thread
                    }
                }
            }
            Log.i(CallEngine.TAG, "真实音频播放: ${f.name} ${durationSec}s")
            true
        } catch (e: Exception) {
            Log.w(CallEngine.TAG, "真实音频播放失败(${e.message}) → 回退静音流")
            stopReal()
            if (silenceWanted) startSilence()
            false
        }
    }

    /**
     * EMUI 坑: SCO 通话通道挂着时(通话音频残留/HFP 抢占), 媒体会被混进 8kHz 窄带通话通道
     * 或压制 A2DP → 车机上"电话音质"+断断续续。播真实音频前强制释放 SCO。
     * 通话中(ringing/dialing/active/held)不抢 —— 那时 SCO 属于通话本身。
     */
    private fun forceMediaRoute() {
        try {
            val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val cs = CallEngine.state
            if (am.isBluetoothScoOn && cs != "active" && cs != "ringing" && cs != "dialing" && cs != "held") {
                am.isBluetoothScoOn = false
                am.stopBluetoothSco()
                EventLog.add("MEDIA_SCO_RELEASED", "app", "播放前释放挂起的SCO, 媒体改走A2DP")
                Log.w(CallEngine.TAG, "释放挂起的 SCO, 媒体改走 A2DP")
            }
        } catch (t: Throwable) {
            Log.w(CallEngine.TAG, "SCO 释放失败: ${t.message}")
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

    /** 最近一次"播完处理"时刻: onCompletion 与心跳兜底可能先后到达, 2s 内去重防双切 */
    private var lastEndAt = 0L

    /**
     * 曲目播完的统一处理(真实音频 onCompletion / 心跳兜底共用):
     * 自动连播且还有别的曲目 → 切下一曲; 否则停尾(playing=false、进度钉在总时长、
     * 停心跳、停静音流) —— 真机基准: 单曲/关连播时播完即停, 两种出声模式行为一致。
     */
    private fun handleTrackEnd() {
        val now = SystemClock.elapsedRealtime()
        if (now - lastEndAt < 2000) return
        lastEndAt = now
        if (autoAdvance && playlist.size > 1) {
            doAdvance(+1)
        } else {
            playing = false
            posSec = durationSec
            pushState()
            stopTicker()   // 停心跳, 否则时间乱跳
            stopSilence()  // 停尾与暂停同基准: 不留暂停态供流线程(见 doPause 注释)
        }
    }

    private fun startTicker() {
        // 调用方全在主线程(Dispatcher.onMain/回调), ticker!=null 判断天然原子
        if (ticker != null) return
        val r = object : Runnable {
            override fun run() {
                val m = mp
                if (m != null) {
                    // 真实模式: 进度取 MediaPlayer 实际位置; 刚下发 seek 的窗口内播放器
                    // 还可能报旧位置, 用命令值兜住。播完主要靠 onCompletion, 这里加一道
                    // 兜底(个别栈 seek 到精确结尾不回调 completion → 停在结尾"播放中")
                    posSec = try {
                        if (seekAt > 0 && System.currentTimeMillis() - seekAt < 1500)
                            (seekPosMs / 1000).toInt()
                        else m.currentPosition / 1000
                    } catch (_: Exception) { posSec }
                    try {
                        if (!m.isPlaying && durationSec > 0 && posSec >= durationSec)
                            handleTrackEnd()
                    } catch (_: Exception) {}
                } else {
                    // 静音流模式: 心跳自增; 播完走同一个 handleTrackEnd(与真实模式一致)
                    posSec++
                    if (posSec >= durationSec) handleTrackEnd()
                }
                pushState()
                // handleTrackEnd 停尾路径会在本运行体内 stopTicker(ticker=null),
                // 此时不能再自复活 —— 只在 ticker 仍指向自己时续期
                if (ticker === this) main.postDelayed(this, 1000)
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
                        PlaybackState.ACTION_STOP or
                        // FF/RW 回调已实现(onFastForward/onRewind)却不宣告的话,
                        // 部分车机会把快进/快退键置灰 —— 真机播放器都宣告这两个能力
                        PlaybackState.ACTION_FAST_FORWARD or PlaybackState.ACTION_REWIND
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
        // 暂停态不起流: 孤儿暂停轨 + 路由消失 = write 忙循环刷爆 logcat(见 doPause 注释);
        // 只记开关, 恢复播放时 ensureAudioOut 会按需建流
        if (on && playing) resumeOrStartSilence() else if (on) return "静音流已开(播放后将保活A2DP)"
        if (!on) stopSilence()
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
                var fastHits = 0   // 连续"瞬间返回"的写次数 —— 正常写应阻塞约 1s/次
                // playing 由主线程写: 此处读到旧值最多多写一拍, 无害; 但绝不能在
                // 暂停后继续供流(见 doPause 注释的忙循环事故)
                while (silenceWanted && audioTrack != null && playing) {
                    try {
                        val w0 = SystemClock.elapsedRealtime()
                        val n = audioTrack?.write(buf, 0, buf.size) ?: break
                        val costMs = SystemClock.elapsedRealtime() - w0
                        if (n < 0) {
                            Log.w(CallEngine.TAG, "静音流写返回 $n, 线程退出")
                            break
                        }
                        // 写 1s 数据却不阻塞: 输出路由已消失, 数据被直接丢弃 ——
                        // 继续转只会烧 CPU + 刷爆 logcat, 立刻退出交给自愈逻辑
                        if (costMs < 200) {
                            if (++fastHits >= 5) {
                                Log.w(CallEngine.TAG, "静音流写异常顺畅(${fastHits}次<200ms), 判定路由已失效, 退出供流")
                                break
                            }
                        } else fastHits = 0
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
                    // 意外退出(A2DP 建立瞬间路由切换等) → 自愈重建, 保证流不断。
                    // 必须带 playing 检查: 暂停中(doPause 已停流)不重建 —— 真机基准:
                    // 暂停的播放器被系统中断后不该自己恢复出声, 否则车机看到
                    // "状态是暂停、流却活跃"的矛盾画面
                    if (playing) {
                        Log.w(CallEngine.TAG, "静音流意外退出(播放中), 2s 后自动重建")
                        main.postDelayed({
                            if (silenceWanted && mp == null && playing) {
                                stopSilence()
                                startSilence()
                            }
                        }, 2000)
                    }
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

    /** MediaPlayer 绑 A2DP: setPreferredDevice 系 API 28+。返回是否绑定成功(失败可重试) */
    private fun pinPlayerA2dp(m: MediaPlayer): Boolean {
        if (Build.VERSION.SDK_INT < 28) return false
        return try {
            val a2 = findA2dpDevice()
            if (a2 != null) {
                m.preferredDevice = a2
                Log.i(CallEngine.TAG, "真实音频已绑定 A2DP 输出: ${a2.productName}")
                true
            } else {
                Log.w(CallEngine.TAG, "未见 A2DP 输出设备(可能未连), 真实音频暂用默认路由")
                false
            }
        } catch (t: Throwable) {
            Log.w(CallEngine.TAG, "真实音频 A2DP 绑定失败: ${t.message}")
            false
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

    private fun silenceState(): String {
        val t = audioTrack ?: return "off"
        return when (t.playState) {
            AudioTrack.PLAYSTATE_PLAYING -> "running"
            AudioTrack.PLAYSTATE_PAUSED -> "paused"
            else -> "state${t.playState}"
        }
    }

    fun status(): String =
        "media=${if (playing) "playing" else "paused"} " +
            "track=\"$title\"/$artist pos=${posSec}s dur=${durationSec}s " +
            "playlist=${playlist.size}首 idx=$plIndex autoAdvance=$autoAdvance " +
            "real=${if (mp != null) realFor?.name else "none"} " +
            "silence=${silenceState()}/${if (silenceWanted) "on" else "off"} focus=${if (focused) "held" else "none"}"

    /** 音质/断续问题排查: 实际输出设备 + SCO 状态 + 绑定情况一目了然 */
    fun diag(): String {
        val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val devs = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            .joinToString(", ") { devName(it.type) }
        val pref = if (Build.VERSION.SDK_INT >= 23)
            (mp?.preferredDevice ?: audioTrack?.preferredDevice)?.productName ?: "未绑"
        else "SDK<23"
        val mpPlaying = try { mp?.isPlaying } catch (_: Exception) { null }
        return "playing=$playing mpIsPlaying=$mpPlaying real=${realFor?.name ?: "none"}\n" +
            "silence=${silenceState()} scoOn=${am.isBluetoothScoOn} musicActive=${am.isMusicActive} preferred=$pref\n" +
            "outputs=[$devs]"
    }

    private fun devName(t: Int): String = when (t) {
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "A2DP"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "SCO"
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "SPK"
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "WIRE_HP"
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> "WIRE_HS"
        else -> "type$t"
    }

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
