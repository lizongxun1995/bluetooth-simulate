package com.bt.vphone

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
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
 */
object CallEngine {
    const val TAG = "VPhone"
    const val EVT_ACTION = "com.bt.vphone.EVT"

    lateinit var app: Context
    var state: String = "idle"
    var number: String = ""
        private set
    var connection: Connection? = null
    var lastEvent: String = "无"

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
            telecom().addNewIncomingCall(handle, extras)
            setState("ringing", number)
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
            telecom().placeCall(Uri.parse("tel:" + Uri.encode(number)), extras)
            "去电已提交: $number —— 车机应显示拨号态, 随后 answer 置通话中"
        } catch (e: Exception) {
            "去电失败: ${e.message} (需授权 CALL_PHONE 且账号已启用)"
        }
    }

    fun answer(): String {
        val c = connection ?: return "无通话"
        return try {
            c.setActive()
            setState("active")
            "已置为通话中(setActive)"
        } catch (e: Exception) {
            "接听失败: ${e.message}"
        }
    }

    fun hangup(): String {
        val c = connection ?: return "无通话"
        return try {
            c.setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
            c.destroy()
            "已挂断"
        } catch (e: Exception) {
            "挂断异常: ${e.message}"
        } finally {
            connection = null
            state = "idle"
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

    fun clearFromConnection() {
        connection = null
        state = "idle"
    }

    fun status(): String = "call=$state number=$number account=[${accountStatus()}]"
}
