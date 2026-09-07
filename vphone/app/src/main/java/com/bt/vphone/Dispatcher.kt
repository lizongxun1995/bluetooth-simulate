package com.bt.vphone

/**
 * 命令分发中枢: HTTP(/call/incoming?number=...) 与 adb 广播(cmd=incoming) 共用一套路由。
 * 返回纯文本结果(例外: /events 返回 JSON), 所有动作/事件落 logcat(TAG=VPhone)。
 * body = POST 请求体二进制(目前仅 /media/upload 用, 广播通道无 body)。
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
        "auto_outgoing" to "/call/auto-outgoing",
        "call_audio" to "/call/audio",
        "track" to "/media/track",
        "play" to "/media/play",
        "pause" to "/media/pause",
        "next" to "/media/next",
        "prev" to "/media/prev",
        "silence" to "/media/silence",
        "autoadvance" to "/media/autoadvance",
        "playlist" to "/media/playlist",
        "jump" to "/media/jump",
        "seek" to "/media/seek",
        "files" to "/media/files",
        "diag" to "/media/diag",
        "del" to "/media/del",
        "bt_state" to "/bt/state",
        "scan" to "/bt/scan",
        "scan_result" to "/bt/scan-result",
        "bond" to "/bt/bond",
        "unpair" to "/bt/unpair",
        "disconnect" to "/bt/disconnect",
        "reconnect" to "/bt/reconnect",
        "allow_car" to "/bt/allow-car",
        "bt_name" to "/bt/name",
        "bt_enable" to "/bt/enable",
        "contacts_load" to "/contacts/load",
        "contacts_import" to "/contacts/import",
        "contacts_clear" to "/contacts/clear",
        "contacts_count" to "/contacts/count",
        "contacts_status" to "/contacts/status",
        "events" to "/events",
        "status" to "/status",
        "help" to "/help"
    )

    fun cmd(cmd: String, q: Map<String, String>): String {
        val path = if (cmd.startsWith("/")) cmd else (alias[cmd] ?: "/$cmd")
        return http(path, q)
    }

    fun http(path: String, q: Map<String, String>, body: ByteArray? = null): String {
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
                "/call/auto-outgoing" -> CallEngine.setAutoOutgoing((q["on"] ?: "1") != "0")
                "/call/audio" ->
                    if (q["stop"] == "1") CallAudioEngine.stop()
                    else CallAudioEngine.play(
                        q["name"] ?: "", (q["loop"] ?: "0") == "1"
                    )

                "/media/track" -> MediaEngine.setTrack(
                    q["title"] ?: "", q["artist"] ?: "", q["album"] ?: "",
                    q["duration"]?.toIntOrNull() ?: 240
                )
                "/media/play" -> MediaEngine.play()
                "/media/pause" -> MediaEngine.pause()
                "/media/next" -> MediaEngine.next()
                "/media/prev" -> MediaEngine.prev()
                "/media/status" -> MediaEngine.status()
                "/media/diag" -> MediaEngine.diag()
                "/media/silence" -> MediaEngine.setSilence((q["on"] ?: "1") != "0")
                "/media/autoadvance" -> {
                    MediaEngine.setAutoAdvance((q["on"] ?: "1") != "0")
                }
                "/media/playlist" -> MediaEngine.loadPlaylist(q["text"] ?: "")
                "/media/jump" -> MediaEngine.jump(q["idx"]?.toIntOrNull() ?: 0)
                "/media/seek" -> MediaEngine.seek(q["pos"]?.toIntOrNull() ?: 0)
                "/media/upload" -> MediaEngine.saveUpload(
                    q["name"] ?: "", body ?: ByteArray(0)
                )
                "/media/files" -> MediaEngine.files()
                "/media/del" -> MediaEngine.del(q["name"] ?: "")

                "/bt/state" -> BtEngine.state()
                "/bt/scan" -> BtEngine.scan()
                "/bt/scan-result" -> BtEngine.scanResult()
                "/bt/bond" -> BtEngine.bond(q["mac"] ?: q["name"] ?: "")
                "/bt/unpair" -> BtEngine.unpair(q["mac"] ?: q["name"] ?: "")
                "/bt/disconnect" -> BtEngine.disconnect(
                    q["mac"] ?: q["name"] ?: "", (q["force"] ?: "0") == "1"
                )
                "/bt/reconnect" -> BtEngine.reconnect(
                    q["mac"] ?: q["name"] ?: "", (q["fallback"] ?: "1") != "0"
                )
                "/bt/allow-car" -> BtEngine.allowCarAccess(q["mac"] ?: q["name"] ?: "")
                "/bt/name" -> BtEngine.setName(q["name"] ?: "")
                "/bt/enable" -> BtEngine.setEnabled((q["on"] ?: "1") != "0")

                "/contacts/load" -> ContactsEngine.load(
                    q["count"]?.toIntOrNull() ?: 100, q["prefix"] ?: "联系人"
                )
                "/contacts/import" -> ContactsEngine.import(
                    q["text"] ?: (body?.toString(Charsets.UTF_8) ?: "")
                )
                "/contacts/clear" -> ContactsEngine.clear()
                "/contacts/count" -> ContactsEngine.count()
                "/contacts/status" -> ContactsEngine.status()

                "/events" -> EventLog.json(q["since"]?.toIntOrNull() ?: 0)

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
            "${CallAudioEngine.status()}\n" +
            "${ContactsEngine.status()}\n" +
            "最近事件: ${EventLog.lastLine()}\n" +
            "---- 控制面 ----\n" +
            "HTTP: curl \"http://<手机IP>:8800/call/incoming?number=13800138000\"\n" +
            "     (PC 亦可 adb forward tcp:18800 tcp:8800 后用 127.0.0.1:18800)\n" +
            "adb : adb shell am broadcast -a com.bt.vphone.CMD --es cmd incoming --es number 13800138000\n" +
            "路径: /call/incoming|dial|answer|hangup|hold|dtmf|audio-bt|auto-outgoing|audio  \n" +
            "     /media/track|play|pause|next|prev|jump|seek|silence|autoadvance|playlist|upload|files|del|diag  \n" +
            "     /bt/state|scan|scan-result|bond|unpair|disconnect|reconnect|name|enable  \n" +
            "     /contacts/load|import|clear|count|status  /events?since=N(JSON,断言用)"
}
