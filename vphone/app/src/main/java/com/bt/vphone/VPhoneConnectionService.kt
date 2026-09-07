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
 * 车机上的接听/拒接/挂断/保持/DTMF 按键全部落到 VConnection 的回调并落 EventLog。
 */
class VPhoneConnectionService : ConnectionService() {

    private val main = Handler(Looper.getMainLooper())

    override fun onCreateIncomingConnection(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest?
    ): Connection {
        // EMUI 上 request.address 恒为 null(见 CallEngine.pendingIncomingNumber 注释) → 兜底
        val tel = request?.address?.schemeSpecificPart
            ?: CallEngine.pendingIncomingNumber ?: "13800138000"
        CallEngine.pendingIncomingNumber = null
        Log.i(CallEngine.TAG, "onCreateIncomingConnection 号码=$tel")
        EventLog.add(EventLog.RING_IN, "cmd", "注入来电 $tel (车机应弹来电UI)")
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
        // 来处区分: CallEngine.dial() 置位=PC 指令; 未置位=车机 ATD 直接拨出
        val fromCar = !CallEngine.consumeDialFromCmd()
        EventLog.add(
            if (fromCar) EventLog.CAR_DIAL else "CMD_DIAL", if (fromCar) "car" else "cmd",
            if (fromCar) "车机拨出(ATD) 号码=$tel" else "指令拨出 号码=$tel"
        )
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
                            EventLog.add(
                                EventLog.CALL_ACTIVE, "app",
                                "拨出 $tel 自动接通(模拟对端 3s 摘机)"
                            )
                        }
                    } catch (_: Throwable) {
                    }
                }, 3000)
            }
        }
    }
}

/** 单条虚拟呼叫。车机按键回调 = 门C 判定证据, 全部落 EventLog + logcat(TAG=VPhone)。 */
class VConnection(private val tel: String) : Connection() {

    private fun answered() {
        EventLog.add(EventLog.CAR_ANSWER, "car", "车机按了【接听】(onAnswer) 号码=$tel")
        setActive()
        CallEngine.state = "active"
    }

    override fun onAnswer() = answered()

    override fun onAnswer(videoState: Int) = answered()

    override fun onReject() {
        EventLog.add(EventLog.CAR_REJECT, "car", "车机按了【拒接】(onReject) 号码=$tel")
        setDisconnected(DisconnectCause(DisconnectCause.REJECTED))
        destroy()
        CallEngine.clearFromConnection()
    }

    override fun onDisconnect() {
        EventLog.add(EventLog.CAR_HANGUP, "car", "车机按了【挂断】(onDisconnect) 号码=$tel")
        setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
        destroy()
        CallEngine.clearFromConnection()
    }

    override fun onHold() {
        EventLog.add(EventLog.CAR_HOLD, "car", "车机按了【保持】(onHold) 号码=$tel")
        setOnHold()
        CallEngine.setState("held")
    }

    override fun onUnhold() {
        EventLog.add(EventLog.CAR_UNHOLD, "car", "车机取消保持(onUnhold) 号码=$tel")
        setActive()
        CallEngine.setState("active")
    }

    override fun onPlayDtmfTone(c: Char) {
        EventLog.add(EventLog.CAR_DTMF, "car", "车机 DTMF 按键: $c")
    }

    override fun onStopDtmfTone() {
        Log.i(CallEngine.TAG, "车机 DTMF 按键结束")
    }
}
