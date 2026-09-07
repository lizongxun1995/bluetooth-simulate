package com.bt.vphone

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.telecom.Connection
import android.telecom.ConnectionRequest
import android.telecom.ConnectionService
import android.telecom.DisconnectCause
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import android.util.Log

/**
 * 门C 核心: managed ConnectionService —— Telecom 绑定本服务创建虚拟呼叫。
 * 车机(HF 角色)通过系统蓝牙栈看到来电/拨号/通话中状态;
 * 车机上的接听/拒接/挂断/保持/DTMF 按键全部落到 VConnection 的回调。
 */
class VPhoneConnectionService : ConnectionService() {

    private val main = Handler(Looper.getMainLooper())

    override fun onCreateIncomingConnection(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest?
    ): Connection {
        val tel = request?.address?.schemeSpecificPart ?: "13800138000"
        Log.i(CallEngine.TAG, "onCreateIncomingConnection 号码=$tel")
        return VConnection(tel).apply {
            setAddress(Uri.parse("tel:$tel"), TelecomManager.PRESENTATION_ALLOWED)
            setConnectionCapabilities(
                Connection.CAPABILITY_MUTE or Connection.CAPABILITY_SUPPORT_HOLD
            )
            setRinging()
            CallEngine.attach(this, tel, "ringing")
        }
    }

    override fun onCreateOutgoingConnection(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest?
    ): Connection {
        val tel = request?.address?.schemeSpecificPart ?: ""
        Log.i(CallEngine.TAG, "onCreateOutgoingConnection 号码=$tel")
        return VConnection(tel).apply {
            setAddress(Uri.parse("tel:$tel"), TelecomManager.PRESENTATION_ALLOWED)
            setConnectionCapabilities(
                Connection.CAPABILITY_MUTE or Connection.CAPABILITY_SUPPORT_HOLD
            )
            setDialing()
            CallEngine.attach(this, tel, "dialing")
            // 车机拨出(ATD)后模拟对端 3s 摘机 —— 否则车机永远停在"拨号中"
            if (CallEngine.autoAnswerOutgoing) {
                val conn = this
                main.postDelayed({
                    try {
                        if (CallEngine.state == "dialing" && CallEngine.connection == conn) {
                            conn.setActive()
                            CallEngine.setState("active")
                            CallEngine.evt("车机拨出 $tel 已自动接通(模拟对端 3s 摘机)")
                        }
                    } catch (_: Throwable) {
                    }
                }, 3000)
            }
        }
    }
}

/** 单条虚拟呼叫。车机按键回调 = 门C 判定证据, 全部落 logcat(TAG=VPhone)。 */
class VConnection(private val tel: String) : Connection() {

    private fun answered() {
        CallEngine.evt("车机/系统按了【接听】(onAnswer) 号码=$tel")
        setActive()
        CallEngine.state = "active"
    }

    override fun onAnswer() = answered()

    override fun onAnswer(videoState: Int) = answered()

    override fun onReject() {
        CallEngine.evt("车机/系统按了【拒接】(onReject) 号码=$tel")
        setDisconnected(DisconnectCause(DisconnectCause.REJECTED))
        destroy()
        CallEngine.clearFromConnection()
    }

    override fun onDisconnect() {
        CallEngine.evt("车机/系统按了【挂断】(onDisconnect) 号码=$tel")
        setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
        destroy()
        CallEngine.clearFromConnection()
    }

    override fun onHold() {
        CallEngine.evt("车机/系统按了【保持】(onHold)")
        setOnHold()
        CallEngine.setState("held")
    }

    override fun onUnhold() {
        CallEngine.evt("车机/系统取消保持(onUnhold)")
        setActive()
        CallEngine.setState("active")
    }

    override fun onPlayDtmfTone(c: Char) {
        CallEngine.evt("车机 DTMF 按键: $c")
    }

    override fun onStopDtmfTone() {
        CallEngine.evt("车机 DTMF 按键结束")
    }
}
