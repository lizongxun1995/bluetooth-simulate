package com.bt.vphone

import android.content.Intent
import android.util.Log

/**
 * 事件总线: 车机回流按键/呼叫状态/蓝牙链路 全部落成结构化事件,
 * PC 侧经 /events?since=N 拉取 JSON 做断言(wait_event), 不再依赖解析 logcat 文本。
 *
 * 事件三要素: type(英文稳定, 断言用) / src(来源) / detail(中文, 人读)。
 *   src: car=车机按键回流  cmd=PC 指令  app=App 内部状态  bt=蓝牙链路  sys=系统(Telecom)侧
 * 环形缓冲 500 条, logcat 同步打一行(TAG=VPhone), 广播 EVT_ACTION 同步发。
 */
object EventLog {
    data class Ev(val id: Int, val ts: Long, val type: String, val src: String, val detail: String)

    private val buf = ArrayDeque<Ev>()
    private const val CAP = 500
    private var nextId = 0

    /** 车机接听 */  const val CAR_ANSWER = "CAR_ANSWER"
    /** 车机拒接 */  const val CAR_REJECT = "CAR_REJECT"
    /** 车机挂断 */  const val CAR_HANGUP = "CAR_HANGUP"
    /** 车机拨出(ATD) */ const val CAR_DIAL = "CAR_DIAL"
    /** 车机保持/恢复 */ const val CAR_HOLD = "CAR_HOLD"; const val CAR_UNHOLD = "CAR_UNHOLD"
    /** 车机 DTMF 按键 */ const val CAR_DTMF = "CAR_DTMF"
    /** 车机媒体按键 */ const val CAR_PLAY = "CAR_PLAY"; const val CAR_PAUSE = "CAR_PAUSE"
    const val CAR_NEXT = "CAR_NEXT"; const val CAR_PREV = "CAR_PREV"
    const val CAR_STOP = "CAR_STOP"; const val CAR_SEEK = "CAR_SEEK"
    /** 呼叫状态机 */ const val RING_IN = "RING_IN"; const val CALL_ACTIVE = "CALL_ACTIVE"
    const val CALL_HELD = "CALL_HELD"; const val CALL_ENDED = "CALL_ENDED"
    /** 媒体/音频 */ const val CMD_PLAY = "CMD_PLAY"; const val CMD_PAUSE = "CMD_PAUSE"
    const val CMD_NEXT = "CMD_NEXT"; const val CMD_PREV = "CMD_PREV"
    const val MEDIA_TRACK_END = "MEDIA_TRACK_END"; const val MEDIA_UPLOAD = "MEDIA_UPLOAD"
    const val CALL_AUDIO_END = "CALL_AUDIO_END"
    /** 蓝牙链路 */ const val BT_ACL_CONNECTED = "BT_ACL_CONNECTED"
    const val BT_ACL_DISCONNECTED = "BT_ACL_DISCONNECTED"
    const val BT_DISCONNECT = "BT_DISCONNECT"
    const val BT_DISCONNECT_FAILED = "BT_DISCONNECT_FAILED"
    const val BT_A2DP_CONNECTED = "BT_A2DP_CONNECTED"
    const val BT_A2DP_DISCONNECTED = "BT_A2DP_DISCONNECTED"
    const val BT_HFP_CONNECTED = "BT_HFP_CONNECTED"
    const val BT_HFP_DISCONNECTED = "BT_HFP_DISCONNECTED"
    const val BT_BONDED = "BT_BONDED"
    /** 联系人 */ const val CONTACTS_LOADED = "CONTACTS_LOADED"
    const val CONTACTS_CLEARED = "CONTACTS_CLEARED"

    fun add(type: String, src: String, detail: String): Ev {
        val ev = synchronized(buf) {
            nextId += 1
            val e = Ev(nextId, System.currentTimeMillis(), type, src, detail)
            buf.addLast(e)
            while (buf.size > CAP) buf.removeFirst()
            e
        }
        Log.i(CallEngine.TAG, "[事件#${ev.id} ${ev.type}] ($src) ${ev.detail}")
        try {
            CallEngine.app.sendBroadcast(
                Intent(CallEngine.EVT_ACTION).putExtra("msg", "[${ev.type}] ${ev.detail}")
            )
        } catch (_: Exception) {
        }
        return ev
    }

    fun json(sinceId: Int): String {
        val list: List<Ev>
        val last: Int
        synchronized(buf) {
            last = nextId
            list = buf.filter { it.id > sinceId }
        }
        val sb = StringBuilder("{\"last\":$last,\"count\":${list.size},\"events\":[")
        list.forEachIndexed { i, e ->
            if (i > 0) sb.append(',')
            sb.append(
                "{\"id\":${e.id},\"ts\":${e.ts},\"type\":\"${esc(e.type)}\"," +
                    "\"src\":\"${esc(e.src)}\",\"detail\":\"${esc(e.detail)}\"}"
            )
        }
        sb.append("]}")
        return sb.toString()
    }

    fun lastLine(): String =
        synchronized(buf) { buf.lastOrNull()?.let { "#${it.id} ${it.type} ${it.detail}" } ?: "无" }

    private fun esc(s: String): String = s
        .replace("\\", "\\\\").replace("\"", "\\\"")
        .replace("\n", " ").replace("\r", " ")
}
