package com.bt.vphone

/**
 * 命令分发中枢: HTTP(/call/incoming?number=...) 与 adb 广播(cmd=incoming) 共用一套路由。
 * 返回纯文本结果, 同时所有动作/事件落 logcat(TAG=VPhone)。
 */
object Dispatcher {

    /** 短命令别名 → HTTP 路径(adb 广播用简写, HTTP 用完整路径)。 */
    private val alias = mapOf(
        "incoming" to "/call/incoming",
        "dial" to "/call/dial",
        "answer" to "/call/answer",
        "hangup" to "/call/hangup",
        "hold" to "/call/hold",
        "dtmf" to "/call/dtmf",
        "audio_bt" to "/call/audio-bt",
        "track" to "/media/track",
        "play" to "/media/play",
        "pause" to "/media/pause",
        "next" to "/media/next",
        "prev" to "/media/prev",
        "silence" to "/media/silence",
        "autoadvance" to "/media/autoadvance",
        "playlist" to "/media/playlist",
        "status" to "/status",
        "help" to "/help"
    )

    fun cmd(cmd: String, q: Map<String, String>): String {
        val path = if (cmd.startsWith("/")) cmd else (alias[cmd] ?: "/$cmd")
        return http(path, q)
    }

    fun http(path: String, q: Map<String, String>): String {
        val p = path.trimEnd('/')
        return try {
            when (p) {
                "", "/", "/status", "/help" -> statusAll()

                "/call/incoming" -> CallEngine.incoming(q["number"] ?: "13800138000")
                "/call/dial" -> CallEngine.dial(q["number"] ?: "10086")
                "/call/answer" -> CallEngine.answer()
                "/call/hangup" -> CallEngine.hangup()
                "/call/hold" -> CallEngine.hold((q["on"] ?: "1") != "0")
                "/call/dtmf" -> CallEngine.dtmf(q["key"] ?: "")
                "/call/audio-bt" -> CallEngine.audioBluetooth()

                "/media/track" -> MediaEngine.setTrack(
                    q["title"] ?: "", q["artist"] ?: "", q["album"] ?: "",
                    q["duration"]?.toIntOrNull() ?: 240
                )
                "/media/play" -> MediaEngine.play()
                "/media/pause" -> MediaEngine.pause()
                "/media/next" -> MediaEngine.next()
                "/media/prev" -> MediaEngine.prev()
                "/media/status" -> MediaEngine.status()
                "/media/silence" -> MediaEngine.setSilence((q["on"] ?: "1") != "0")
                "/media/autoadvance" -> {
                    MediaEngine.setAutoAdvance((q["on"] ?: "1") != "0")
                }
                "/media/playlist" -> MediaEngine.loadPlaylist(q["text"] ?: "")

                else -> "未知路径: $p\n${statusAll()}"
            }
        } catch (e: Exception) {
            "!! ${e.message}"
        }
    }

    fun statusAll(): String =
        "VPhone 虚拟手机 OK\n" +
            "${CallEngine.status()}\n" +
            "${MediaEngine.status()}\n" +
            "最近事件: ${CallEngine.lastEvent}\n" +
            "---- 控制面 ----\n" +
            "HTTP: curl \"http://<手机IP>:8800/call/incoming?number=13800138000\"\n" +
            "     (PC 亦可 adb forward tcp:18800 tcp:8800 后用 127.0.0.1:18800)\n" +
            "adb : adb shell am broadcast -a com.bt.vphone.CMD --es cmd incoming --es number 13800138000\n" +
            "路径: /call/incoming|dial|answer|hangup|hold|dtmf|audio-bt  /media/track|play|pause|next|prev|silence|autoadvance|playlist"
}
