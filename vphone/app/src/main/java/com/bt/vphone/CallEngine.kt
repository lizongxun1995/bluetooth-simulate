package com.bt.vphone

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.telecom.CallAudioState
import android.telecom.Connection
import android.telecom.DisconnectCause
import android.telecom.PhoneAccount
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import android.util.Log

/**
 * 门C 核心引擎: 虚拟电话账号(managed ConnectionService)的多路呼叫状态机。
 * 呼叫能力全部模拟 —— 不需要 SIM、不发起真实蜂窝呼叫。
 * 依据: Android Telecom 官方框架, managed ConnectionService 创建的呼叫
 * "在蓝牙设备(车机/耳机)上可见且可控" —— 车机接听/挂断按键回流到
 * VConnection.onAnswer/onDisconnect, 即门C 判定证据。
 *
 * 多路模型(第八轮, 对齐真机 GSM 呼叫等待语义):
 *   - 最多两路: 1 active + 1 (held | ringing等待 | dialing)
 *   - 通话中 incoming = 呼叫等待(CALL_WAITING); answer 自动保持当前 active
 *   - 双通话时 hold(on=0)/swap = 切换; 挂掉 active 后 held 自动恢复
 *     (第十一轮纠正: 真机 GSM CHLD=1 语义 = 释放 active 时网络自动取回保持路,
 *      第八轮"held 保持不自动恢复"是错误假设, 实测车机用户预期=对端继续有声)
 *   - 任一时刻最多一路 active —— activate() 统一编排"先保持其它 active"
 *
 * 单路状态转换(真机基准: 任何失败/拆线路径都必须归回 idle, 不留悬挂 connection):
 *   idle → ringing   (incoming 注入 / 车机侧无)
 *   idle → dialing   (dial 指令 / 车机 ATD)
 *   ringing → active (answer 指令 / 车机接听 onAnswer)
 *   ringing → idle   (车机拒接 onReject / 指令挂断 / 系统拆线 onAbort)
 *   dialing → active (3s 自动接通 / answer)
 *   active ⇄ held    (hold / onHold / onUnhold)
 *   any → idle       (hangup / onDisconnect / onAbort)
 */
object CallEngine {
    const val TAG = "VPhone"
    const val EVT_ACTION = "com.bt.vphone.EVT"

    private val main = Handler(Looper.getMainLooper())

    lateinit var app: Context

    /** 一路通话记录。conn/state 只在主线程读写(Dispatcher.onMain/Telecom 回调/定时器)。
     *  audio* = 该路的"对端说话"绑定(第九轮): HFP 单 SCO, 车机只听得到 active 那路,
     *  每路各自记文件/循环/进度, 通话切换时 CallAudioEngine.followForeground() 跟随换源。 */
    class CallRec(val number: String, var conn: Connection?, var state: String) {
        var audioName: String? = null
        var audioLoop: Boolean = false
        var audioPosMs: Int = 0
    }

    private val calls = mutableListOf<CallRec>()

    /** 前景通话(优先级 active > ringing > dialing > held) —— 对外"单通话视图" */
    private fun foreground(): CallRec? =
        calls.firstOrNull { it.state == "active" }
            ?: calls.firstOrNull { it.state == "ringing" }
            ?: calls.firstOrNull { it.state == "dialing" }
            ?: calls.firstOrNull { it.state == "held" }

    /** 当前 active 那路(可能无) —— 对端音频可听性的判据, 给 CallAudioEngine 用 */
    fun activeRec(): CallRec? = calls.firstOrNull { it.state == "active" }

    /** 派生只读视图: 既有读点(CallAudioEngine/MediaEngine/Dispatcher/GUI)零改动 */
    val state: String get() = foreground()?.state ?: "idle"
    val number: String get() = foreground()?.number ?: ""

    var lastEvent: String = "无"
    /** 车机拨出(ATD)后 3s 自动置通话中(模拟对端摘机); 关掉则停在拨号态等 answer */
    var autoAnswerOutgoing = true
    /** 本方 dial() 指令置位 → onCreateOutgoingConnection 消费, 区分"指令拨出/车机ATD拨出" */
    @Volatile private var dialFromCmd = false

    fun markDialFromCmd() { dialFromCmd = true }

    fun consumeDialFromCmd(): Boolean {
        val v = dialFromCmd
        dialFromCmd = false
        return v
    }

    /**
     * 注入来电的目标号码兜底: EMUI(Android 10) 上 addNewIncomingCall 的 extras 里的
     * EXTRA_INCOMING_CALL_ADDRESS 不会被填进 ConnectionRequest.address(恒为 null),
     * ConnectionService 侧用它兜底 —— 否则车机/手机来电界面永远显示 13800138000。
     * 单槽: 两路注入间隔极近时可能串号, 测试节奏保持 ≥1s 即无碍。
     */
    @Volatile var pendingIncomingNumber: String? = null

    val handle: PhoneAccountHandle by lazy {
        PhoneAccountHandle(ComponentName(app, VPhoneConnectionService::class.java), "VPHONE")
    }

    fun init(ctx: Context) {
        app = ctx.applicationContext
    }

    fun telecom(): TelecomManager = app.getSystemService(Context.TELECOM_SERVICE) as TelecomManager

    fun evt(msg: String) {
        lastEvent = msg
        Log.i(TAG, "[事件] $msg")
        try {
            app.sendBroadcast(Intent(EVT_ACTION).putExtra("msg", msg))
        } catch (_: Exception) {
        }
    }

    /** 注册"电话账号"(CAPABILITY_CALL_PROVIDER = managed)。装好后需在系统电话设置里启用一次。 */
    fun registerAccount(): String {
        return try {
            val acc = PhoneAccount.builder(handle, "VPhone 虚拟电话")
                .setCapabilities(PhoneAccount.CAPABILITY_CALL_PROVIDER)
                .addSupportedUriScheme(PhoneAccount.SCHEME_TEL)
                .setShortDescription("车载蓝牙测试·虚拟来电")
                .build()
            telecom().registerPhoneAccount(acc)
            "电话账号已注册: VPhone (状态: ${accountStatus()})"
        } catch (e: Exception) {
            "电话账号注册失败: ${e.message}"
        }
    }

    fun accountStatus(): String {
        if (Build.VERSION.SDK_INT < 23) return "已注册(API<24 无法查询启用态)"
        return try {
            val acc = telecom().getPhoneAccount(handle)
            if (acc == null) "未注册" else "已注册 enabled=${acc.isEnabled}"
        } catch (e: Exception) {
            "查询失败: ${e.message}"
        }
    }

    // ---------------- 呼令(全部模拟, 无真实蜂窝) ----------------

    fun hasCalls(): Boolean = calls.isNotEmpty()

    fun callsDesc(): String =
        if (calls.isEmpty()) "[]" else calls.joinToString(",", "[", "]") { "${it.state}:${it.number}" }

    fun incoming(number: String): String {
        if (calls.size >= 2) return "已有两路通话(GSM 上限), 第三路进不来 ${callsDesc()}"
        val waiting = calls.isNotEmpty()
        return try {
            val extras = Bundle().apply {
                putString(TelecomManager.EXTRA_INCOMING_CALL_ADDRESS, number)
            }
            pendingIncomingNumber = number
            telecom().addNewIncomingCall(handle, extras)
            // EMUI 静默失败兜底: 账号未启用时 addNewIncomingCall 不抛异常也不建连接,
            // 1.5s 后仍无记录就记事件回滚 —— 不留悬挂状态。判据须带 pendingIncomingNumber:
            // 建连成功时 onCreateIncomingConnection 会消费掉它; 只查号码在不在的话,
            // "1.5s 内已被正常挂断"的一路也会被误报成注入失败(设备实测踩过)
            main.postDelayed({
                if (calls.none { it.number == number } && pendingIncomingNumber == number) {
                    pendingIncomingNumber = null
                    EventLog.add(EventLog.CALL_ENDED, "app", "注入来电未生效(账号未启用?), 已回滚: $number")
                }
            }, 1500)
            if (waiting) "呼叫等待已注入: $number (当前通话不受影响) —— 车机应显示等待来电"
            else "来电已注入: $number —— 等待车机弹来电UI"
        } catch (e: Exception) {
            "注入来电失败: ${e.message} (多半=电话账号未启用, 先 registerAccount + 手动启用)"
        }
    }

    fun dial(number: String): String {
        if (calls.size >= 2) return "已有两路通话(GSM 上限), 无法再拨 ${callsDesc()}"
        return try {
            val extras = Bundle().apply {
                putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, handle)
            }
            markDialFromCmd()
            telecom().placeCall(Uri.parse("tel:" + Uri.encode(number)), extras)
            if (calls.isNotEmpty()) "第二路去电已提交: $number —— 接通时当前通话自动保持"
            else "去电已提交: $number —— 车机应显示拨号态, 随后 answer 置通话中"
        } catch (e: Exception) {
            // 失败不留脏标记: dialFromCmd 若残留, 下次车机 ATD 会被误判成"指令拨出"
            dialFromCmd = false
            "去电失败: ${e.message} (需授权 CALL_PHONE 且账号已启用)"
        }
    }

    /** 接听: 优先 ringing(含呼叫等待), 其次 dialing(手动接通去电)。
     *  接等待来电时当前 active 自动保持 —— 真机 GSM 行为。 */
    fun answer(): String {
        val rec = calls.firstOrNull { it.state == "ringing" }
            ?: calls.firstOrNull { it.state == "dialing" }
            ?: return "无来电/去电可接 ${callsDesc()}"
        return try {
            activate(rec, EventLog.CALL_ACTIVE, "cmd", "指令接听 ${rec.number}")
            "已置为通话中: ${rec.number}"
        } catch (e: Exception) {
            "接听失败: ${e.message}"
        }
    }

    /** 保持/恢复。on=1 保持当前 active; on=0 恢复 held —— 双通话时即"切换"语义。 */
    fun hold(on: Boolean): String {
        if (on) {
            val rec = calls.firstOrNull { it.state == "active" }
                ?: return "无可保持的通话(active 才能保持) ${callsDesc()}"
            return try {
                holdRec(rec, EventLog.CALL_HELD, "cmd", "指令保持")
                "已保持: ${rec.number}"
            } catch (e: Exception) {
                "保持失败: ${e.message}"
            }
        }
        val rec = calls.firstOrNull { it.state == "held" }
            ?: return "无保持中的通话可恢复 ${callsDesc()}"
        val swap = calls.any { it.state == "active" }
        return try {
            activate(rec, EventLog.CALL_ACTIVE, "cmd",
                if (swap) "指令恢复(切换) ${rec.number}" else "指令恢复 ${rec.number}")
            if (swap) "已切换 → 通话中: ${rec.number}" else "已恢复: ${rec.number}"
        } catch (e: Exception) {
            "恢复失败: ${e.message}"
        }
    }

    /** 双通话切换: 挂起 active、激活 held(与 hold(on=0) 等价, 语义显式)。 */
    fun swap(): String {
        val activeRec = calls.firstOrNull { it.state == "active" }
        val heldRec = calls.firstOrNull { it.state == "held" }
        if (activeRec == null || heldRec == null)
            return "切换需要一路 active + 一路 held ${callsDesc()}"
        return try {
            activate(heldRec, EventLog.CALL_ACTIVE, "cmd", "指令切换 → ${heldRec.number}")
            "已切换 → 通话中: ${heldRec.number}, 保持中: ${activeRec.number}"
        } catch (e: Exception) {
            "切换失败: ${e.message}"
        }
    }

    /** 挂断。number=null 挂前景(active>ringing>dialing>held, 对齐车机红键);
     *  number=号码 挂指定一路; number="all" 全挂复位。挂掉 active 后若剩 held,
     *  teardown 内自动取回保持路(真机 CHLD=1 语义, 第十一轮)。 */
    fun hangup(number: String? = null): String {
        if (number == "all") {
            if (calls.isEmpty()) return "本就无通话"
            val n = calls.size
            calls.toList().forEach {
                // teardown 不发事件(契约: 事件由调用方先发), 全挂也要逐路发 CALL_ENDED,
                // 否则脚本清场后的挂断断言会空窗; autoResume=false: 全挂不触发
                // "挂 active 自动恢复 held"(否则中途响起一声再被挂, 事件流脏)
                EventLog.add(EventLog.CALL_ENDED, "cmd", "指令挂断 (${it.number}) [全挂]")
                teardown(it, autoResume = false)
            }
            return "已全部挂断(${n}路) ${callsDesc()}"
        }
        val rec = (if (number != null) calls.firstOrNull { it.number == number } else foreground())
            ?: return if (number != null) "没有这路通话: $number ${callsDesc()}" else "无通话"
        return try {
            EventLog.add(EventLog.CALL_ENDED, "cmd", "指令挂断 (${rec.number})")
            teardown(rec)
            "已挂断: ${rec.number}" + if (calls.isNotEmpty()) " (剩余 ${callsDesc()})" else ""
        } catch (e: Exception) {
            "挂断异常: ${e.message}"
        }
    }

    fun dtmf(c: String): String {
        val ch = c.firstOrNull() ?: return "无按键字符"
        // Connection 无出站 DTMF API; 车机侧按键会回流 VConnection.onPlayDtmfTone
        evt("DTMF 按键模拟: $ch (等车机侧回流 onPlayDtmfTone)")
        return "DTMF $ch 已记录为事件"
    }

    fun setAutoOutgoing(on: Boolean): String {
        autoAnswerOutgoing = on
        return "车机拨出自动接通=$on (3s 后置通话中)"
    }

    /** 加分项: 请求把通话音频路由到蓝牙(SCO) —— 观察车机是否建立通话音频链路。 */
    fun audioBluetooth(): String {
        val c = foreground()?.conn ?: return "无通话"
        return try {
            c.setAudioRoute(CallAudioState.ROUTE_BLUETOOTH)
            "已请求蓝牙通话音频路由(SCO)"
        } catch (e: Throwable) {
            "音频路由失败: ${e.message}"
        }
    }

    // ---------------- 状态编排(主线程; 车机回流/指令共用) ----------------

    /**
     * 激活一路 = 先保持其它 active(GSM: 任一时刻最多一路 active), 再 setActive 本路。
     *  stateEvt 为 null 时不发 CALL_ACTIVE —— 车机接听沿用旧例只发 CAR_ANSWER。
     */
    fun activate(rec: CallRec, stateEvt: String?, src: String, msg: String) {
        calls.filter { it !== rec && it.state == "active" }.forEach {
            // 编排内的保持不触发 follow —— 中间态"无 active"会误报静音事件,
            // 等 rec 激活后结尾统一 follow 一次, 事件说的才是最终语义
            holdRec(it, EventLog.CALL_HELD, "app", "接听/激活 ${rec.number}, 原通话自动保持", follow = false)
        }
        rec.conn?.setActive()
        rec.state = "active"
        if (stateEvt != null) EventLog.add(stateEvt, src, "$msg → active")
        evt("呼叫状态 → active (${rec.number})")
        // 兜底: 来电响铃期间用户手动放了音乐的话, 接通这一刻也要停(真机=通话焦点)
        MediaEngine.pauseForCall("通话激活 ${rec.number}")
        CallAudioEngine.followForeground()      // 换了 active 路 → 车机听到的对端音频跟随
    }

    fun holdRec(rec: CallRec, stateEvt: String, src: String, msg: String, follow: Boolean = true) {
        rec.conn?.setOnHold()
        rec.state = "held"
        EventLog.add(stateEvt, src, "$msg (${rec.number})")
        evt("呼叫状态 → held (${rec.number})")
        if (follow && activeRec() == null) CallAudioEngine.followForeground()   // 保持后无 active → 静音
    }

    /** 机械拆线一路(事件由调用方先发): 移除记录; 最后一路才全清(通话音频随末路停)。
     *  autoResume: 挂的是 active 且剩 held 时, 真机 GSM(CHLD=1 语义)会自动取回保持路 ——
     *  剩余 held 自动激活(其对端音频 followForeground 续播); hangup("all") 传 false。 */
    fun teardown(rec: CallRec, autoResume: Boolean = true) {
        try {
            rec.conn?.setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
            rec.conn?.destroy()
        } catch (_: Throwable) {
        } finally {
            calls.removeAll { it === rec }
            if (calls.isEmpty()) clearAll()
            else if (autoResume && activeRec() == null) {
                val held = calls.firstOrNull { it.state == "held" }
                if (held != null)
                    activate(held, EventLog.CALL_ACTIVE, "app",
                        "active挂断 → 保持路自动恢复(真机CHLD=1)")
                else
                    CallAudioEngine.followForeground()   // 剩的是 ringing(等待), 声音停即可
            } else CallAudioEngine.followForeground()
        }
    }

    // ---------------- 由 VPhoneConnectionService 回调 ----------------

    fun attach(conn: Connection, tel: String, initial: String): CallRec {
        val rec = CallRec(tel, conn, initial)
        calls.add(rec)
        // 真机: 电话铃声一响/一拨出, 媒体就丢音频焦点暂停(不是等接通) —— 进度停走,
        // AVRCP 给车机的也是 paused; 全部通话结束 resumeAfterCall 自动续播
        if (initial == "ringing") MediaEngine.pauseForCall("来电 $tel")
        else if (initial == "dialing") MediaEngine.pauseForCall("去电 $tel")
        evt("呼叫连接建立: $initial 号码=$tel")
        return rec
    }

    /** 通话结束统一清理(全部拆完才到这) —— 真机基准: 挂断即归 idle、号码清空、
     *  通话音频停(SCO 已掉, 残留只会占着播放器); 音频焦点归还 → 媒体自动续播。 */
    fun clearAll() {
        calls.clear()
        pendingIncomingNumber = null
        try { CallAudioEngine.stop() } catch (_: Throwable) {}
        try { MediaEngine.resumeAfterCall() } catch (_: Throwable) {}
    }

    fun status(): String =
        "call=$state number=$number calls=${callsDesc()} account=[${accountStatus()}]"
}
