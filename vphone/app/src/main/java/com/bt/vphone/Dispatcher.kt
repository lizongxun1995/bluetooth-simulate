package com.bt.vphone

import android.os.Handler
import android.os.Looper
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 命令分发中枢: HTTP(/call/incoming?number=...) 与 adb 广播(cmd=incoming) 共用一套路由。
 * 返回纯文本结果(例外: /events 返回 JSON), 所有动作/事件落 logcat(TAG=VPhone)。
 * body = POST 请求体二进制(目前仅 /media/upload 用, 广播通道无 body)。
 *
 * ── 线程模型(重要) ─────────────────────────────────────────────────────
 * HTTP 每连接一个 worker 线程(ControlServer), 而 MediaSession 回调与 1s 心跳都在
 * 主线程 —— MediaEngine/CallEngine/ContactsEngine 的状态只允许主线程读写。
 * worker 必须经 onMain{} 同步桥进入这些引擎, 否则与主线程并发改同一份状态:
 * 双 ticker(进度双倍速)/双 MediaPlayer(两路同播)/seek 与 release 竞争皆源于此。
 * 例外: BtEngine 不走桥(bond/reconnect 阻塞数秒, 上主线程会 ANR, 其内部自带
 * @Volatile + 后台线程); EventLog 自带 synchronized, 任意线程可用。
 */
object Dispatcher {

    private val main = Handler(Looper.getMainLooper())

    /** 主线程同步桥: 已在主线程则直调; 否则 post 过去等结果(8s 超时防 worker 挂死)。 */
    private fun onMain(block: () -> String): String {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val latch = CountDownLatch(1)
        var out: String? = null
        main.post {
            out = try { block() } catch (e: Throwable) { "!! ${e.message}" }
            latch.countDown()
        }
        if (!latch.await(8, TimeUnit.SECONDS)) return "!! 引擎繁忙(主线程 8s 未响应), 请重试"
        return out ?: "!! 无结果"
    }

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

                // ---- 电话(门C): 状态机全在主线程 ----
                "/call/incoming" -> onMain { CallEngine.incoming(q["number"] ?: "13800138000") }
                "/call/dial" -> onMain { CallEngine.dial(q["number"] ?: "10086") }
                "/call/answer" -> onMain { CallEngine.answer() }
                "/call/hangup" -> onMain { CallEngine.hangup() }
                "/call/hold" -> onMain { CallEngine.hold((q["on"] ?: "1") != "0") }
                "/call/dtmf" -> onMain { CallEngine.dtmf(q["key"] ?: "") }
                "/call/audio-bt" -> onMain { CallEngine.audioBluetooth() }
                "/call/auto-outgoing" -> onMain { CallEngine.setAutoOutgoing((q["on"] ?: "1") != "0") }
                "/call/audio" ->
                    if (q["stop"] == "1") onMain { CallAudioEngine.stop() }
                    else onMain {
                        CallAudioEngine.play(q["name"] ?: "", (q["loop"] ?: "0") == "1")
                    }

                // ---- 媒体(门B): 状态机全在主线程 ----
                "/media/track" -> onMain {
                    MediaEngine.setTrack(
                        q["title"] ?: "", q["artist"] ?: "", q["album"] ?: "",
                        q["duration"]?.toIntOrNull() ?: 240
                    )
                }
                "/media/play" -> onMain { MediaEngine.play() }
                "/media/pause" -> onMain { MediaEngine.pause() }
                "/media/next" -> onMain { MediaEngine.next() }
                "/media/prev" -> onMain { MediaEngine.prev() }
                "/media/status" -> onMain { MediaEngine.status() }
                "/media/diag" -> onMain { MediaEngine.diag() }
                "/media/silence" -> onMain { MediaEngine.setSilence((q["on"] ?: "1") != "0") }
                "/media/autoadvance" -> onMain {
                    MediaEngine.setAutoAdvance((q["on"] ?: "1") != "0")
                }
                "/media/playlist" -> onMain { MediaEngine.loadPlaylist(q["text"] ?: "") }
                // jump/seek 缺参数必须报错而不是用默认 0 —— 否则广播通道漏带参数
                // 会"静默跳到第 0 首 / 静默跳回 0s", 属危险默认值
                "/media/jump" -> q["idx"]?.toIntOrNull()
                    ?.let { idx -> onMain { MediaEngine.jump(idx) } }
                    ?: "缺少 idx 参数(0 起), 用法 /media/jump?idx=2"
                "/media/seek" -> q["pos"]?.toIntOrNull()
                    ?.let { pos -> onMain { MediaEngine.seek(pos) } }
                    ?: "缺少 pos 参数(目标秒), 用法 /media/seek?pos=120"
                "/media/upload" -> onMain {
                    MediaEngine.saveUpload(q["name"] ?: "", body ?: ByteArray(0))
                }
                "/media/files" -> onMain { MediaEngine.files() }
                "/media/del" -> onMain { MediaEngine.del(q["name"] ?: "") }

                // ---- 蓝牙: BtEngine 自管线程/反射, 不上主线程桥(长阻塞会 ANR) ----
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

                // ---- 联系人: 命令受理在主线程, 批量写本身在后台线程 ----
                "/contacts/load" -> onMain {
                    ContactsEngine.load(
                        q["count"]?.toIntOrNull() ?: 100, q["prefix"] ?: "联系人"
                    )
                }
                "/contacts/import" -> onMain {
                    ContactsEngine.import(
                        q["text"] ?: (body?.toString(Charsets.UTF_8) ?: "")
                    )
                }
                "/contacts/clear" -> onMain { ContactsEngine.clear() }
                "/contacts/count" -> onMain { ContactsEngine.count() }
                "/contacts/status" -> onMain { ContactsEngine.status() }

                // EventLog 自带 synchronized, 任意线程直读
                "/events" -> EventLog.json(q["since"]?.toIntOrNull() ?: 0)

                else -> "未知路径: $p\n${statusAll()}"
            }
        } catch (e: Exception) {
            "!! ${e.message}"
        }
    }

    fun statusAll(): String =
        "VPhone 虚拟手机 OK\n" +
            onMain { CallEngine.status() } + "\n" +
            onMain { MediaEngine.status() } + "\n" +
            onMain { CallAudioEngine.status() } + "\n" +
            onMain { ContactsEngine.status() } + "\n" +
            "最近事件: ${EventLog.lastLine()}\n" +
            "---- 控制面 ----\n" +
            "HTTP: curl \"http://<手机IP>:8800/call/incoming?number=13800138000\"\n" +
            "     (PC 亦可 adb forward tcp:18800 tcp:8800 后用 127.0.0.1:18800)\n" +
            "adb : adb shell am broadcast -a com.bt.vphone.CMD --es cmd incoming --es number 13800138000\n" +
            "路径: /call/incoming|dial|answer|hangup|hold|dtmf|audio-bt|auto-outgoing|audio  \n" +
            "     /media/track|play|pause|next|prev|status|jump|seek|silence|autoadvance|playlist|upload|files|del|diag  \n" +
            "     /bt/state|scan|scan-result|bond|unpair|disconnect|reconnect|allow-car|name|enable  \n" +
            "     /contacts/load|import|clear|count|status  /events?since=N(JSON,断言用)"
}
