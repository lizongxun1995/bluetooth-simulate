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
 * 门C 核心引擎: 虚拟电话账号(managed ConnectionService)的呼叫状态机。
 * 呼叫能力全部模拟 —— 不需要 SIM、不发起真实蜂窝呼叫。
 * 依据: Android Telecom 官方框架, managed ConnectionService 创建的呼叫
 * "在蓝牙设备(车机/耳机)上可见且可控" —— 车机接听/挂断按键回流到
 * VConnection.onAnswer/onDisconnect, 即门C 判定证据。
 *
 * 状态转换(真机基准: 任何失败/拆线路径都必须归回 idle, 不留悬挂 connection):
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
    var state: String = "idle"
    var number: String = ""
        private set
    var connection: Connection? = null
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

    fun incoming(number: String): String {
        if (connection != null) return "已有通话(state=$state), 先 hangup"
        return try {
            val extras = Bundle().apply {
                putString(TelecomManager.EXTRA_INCOMING_CALL_ADDRESS, number)
            }
            pendingIncomingNumber = number
            telecom().addNewIncomingCall(handle, extras)
            setState("ringing", number)
            // EMUI 静默失败兜底: 账号未启用时 addNewIncomingCall 不抛异常也不建连接,
            // 1.5s 后仍无 connection 就回滚 idle —— 否则 state 卡在 ringing,
            // hangup 又报"无通话"救不回, 后续来电全被"已有通话"挡死
            main.postDelayed({
                if (state == "ringing" && connection == null) {
                    clearFromConnection()
                    EventLog.add(EventLog.CALL_ENDED, "app", "注入来电未生效(账号未启用?), 已回滚 idle")
                }
            }, 1500)
            "来电已注入: $number —— 等待车机弹来电UI"
        } catch (e: Exception) {
            "注入来电失败: ${e.message} (多半=电话账号未启用, 先 registerAccount + 手动启用)"
        }
    }

    fun dial(number: String): String {
        if (connection != null) return "已有通话(state=$state), 先 hangup"
        return try {
            val extras = Bundle().apply {
                putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, handle)
            }
            markDialFromCmd()
            telecom().placeCall(Uri.parse("tel:" + Uri.encode(number)), extras)
            "去电已提交: $number —— 车机应显示拨号态, 随后 answer 置通话中"
        } catch (e: Exception) {
            // 失败不留脏标记: dialFromCmd 若残留, 下次车机 ATD 会被误判成"指令拨出"
            dialFromCmd = false
            "去电失败: ${e.message} (需授权 CALL_PHONE 且账号已启用)"
        }
    }

    fun answer(): String {
        val c = connection ?: return "无通话"
        return try {
            c.setActive()
            setState("active")
            EventLog.add(EventLog.CALL_ACTIVE, "cmd", "指令接听 → active")
            "已置为通话中(setActive)"
        } catch (e: Exception) {
            "接听失败: ${e.message}"
        }
    }

    fun hangup(): String {
        val c = connection ?: return "无通话"
        EventLog.add(EventLog.CALL_ENDED, "cmd", "指令挂断 ($number)")
        return try {
            c.setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
            c.destroy()
            "已挂断"
        } catch (e: Exception) {
            "挂断异常: ${e.message}"
        } finally {
            clearFromConnection()
        }
    }

    fun hold(on: Boolean): String {
        val c = connection ?: return "无通话"
        return try {
            if (on) c.setOnHold() else c.setActive()
            setState(if (on) "held" else "active")
            "保持: $state"
        } catch (e: Exception) {
            "保持失败: ${e.message}"
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
        val c = connection ?: return "无通话"
        return try {
            c.setAudioRoute(CallAudioState.ROUTE_BLUETOOTH)
            "已请求蓝牙通话音频路由(SCO)"
        } catch (e: Throwable) {
            "音频路由失败: ${e.message}"
        }
    }

    // ---------------- 由 VPhoneConnectionService 回调 ----------------

    fun attach(conn: Connection, tel: String, initial: String) {
        connection = conn
        number = tel
        state = initial
        evt("呼叫连接建立: $initial 号码=$tel")
    }

    fun setState(s: String, tel: String? = null) {
        state = s
        if (tel != null) number = tel
        evt("呼叫状态 → $s ($number)")
    }

    /** 通话结束统一清理(指令挂断/车机挂断/拒接/系统拆线共用) —— 真机基准:
     *  挂断即归 idle、号码清空、通话音频停(SCO 已掉, 残留只会占着播放器)。 */
    fun clearFromConnection() {
        connection = null
        state = "idle"
        number = ""
        pendingIncomingNumber = null
        try { CallAudioEngine.stop() } catch (_: Throwable) {}
    }

    fun status(): String = "call=$state number=$number account=[${accountStatus()}]"
}
