package com.bt.vphone

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.telecom.CallAudioState
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
 * 多路: 每条 VConnection 持有自己的 CallRec, 回调只动本路 —— 呼叫等待/切换由
 * CallEngine.activate/holdRec 统一编排"先保持其它 active"。
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
        val waiting = CallEngine.hasCalls()   // 通话中来电 = 呼叫等待(判定须在 attach 之前)
        EventLog.add(
            EventLog.RING_IN, "cmd",
            if (waiting) "注入来电 $tel (呼叫等待: 已有通话, 车机应显示等待提示)"
            else "注入来电 $tel (车机应弹来电UI)"
        )
        if (waiting) EventLog.add(
            EventLog.CALL_WAITING, "app",
            "第二路来电等待中 $tel, 当前 ${CallEngine.callsDesc()}"
        )
        return VConnection(tel).apply {
            setAddress(Uri.parse("tel:$tel"), TelecomManager.PRESENTATION_ALLOWED)
            setConnectionCapabilities(
                Connection.CAPABILITY_MUTE or Connection.CAPABILITY_SUPPORT_HOLD or
                    Connection.CAPABILITY_HOLD
            )
            setRinging()
            rec = CallEngine.attach(this, tel, "ringing")
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
            if (fromCar) EventLog.CAR_DIAL else EventLog.CMD_DIAL,
            if (fromCar) "car" else "cmd",
            if (fromCar) "车机拨出(ATD) 号码=$tel" else "指令拨出 号码=$tel"
        )
        return VConnection(tel).apply {
            setAddress(Uri.parse("tel:$tel"), TelecomManager.PRESENTATION_ALLOWED)
            setConnectionCapabilities(
                Connection.CAPABILITY_MUTE or Connection.CAPABILITY_SUPPORT_HOLD or
                    Connection.CAPABILITY_HOLD
            )
            setDialing()
            val r = CallEngine.attach(this, tel, "dialing")
            rec = r
            // 车机拨出(ATD)后模拟对端 3s 摘机 —— 否则车机永远停在"拨号中"。
            // 身份双校验(第十二轮): 记录仍在状态机里(owns) 且仍处拨号态 ——
            // 3s 内被挂掉的路 state 已被 teardown 置 "ended", 不得再"幽灵接通"
            if (CallEngine.autoAnswerOutgoing) {
                main.postDelayed({
                    try {
                        if (CallEngine.owns(r) && r.state == "dialing") {
                            CallEngine.activate(
                                r, EventLog.CALL_ACTIVE, "app",
                                "拨出 ${r.number} 自动接通(模拟对端 3s 摘机)"
                            )
                        }
                    } catch (_: Throwable) {
                    }
                }, 3000)
            }
        }
    }
}

/** 单条虚拟呼叫(持有自己的 CallRec)。车机按键回调 = 门C 判定证据, 全部落 EventLog。 */
class VConnection(private val tel: String) : Connection() {

    internal lateinit var rec: CallEngine.CallRec

    private fun answered() {
        EventLog.add(EventLog.CAR_ANSWER, "car", "车机按了【接听】(onAnswer) 号码=$tel")
        // stateEvt=null: 车机接听沿用旧例只发 CAR_ANSWER 不发 CALL_ACTIVE;
        // 若另一路在通话中, activate 内部自动保持它(发 CALL_HELD/app)
        try {
            CallEngine.activate(rec, null, "car", "")
        } catch (_: Throwable) {
        }
    }

    override fun onAnswer() = answered()

    override fun onAnswer(videoState: Int) = answered()

    override fun onReject() {
        EventLog.add(EventLog.CAR_REJECT, "car", "车机按了【拒接】(onReject) 号码=$tel")
        try {
            setDisconnected(DisconnectCause(DisconnectCause.REJECTED))
            destroy()
        } catch (_: Throwable) {
        }
        CallEngine.teardown(rec)
    }

    override fun onDisconnect() {
        EventLog.add(EventLog.CAR_HANGUP, "car", "车机按了【挂断】(onDisconnect) 号码=$tel")
        try {
            setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
            destroy()
        } catch (_: Throwable) {
        }
        CallEngine.teardown(rec)
    }

    override fun onAbort() {
        // Telecom 系统侧主动拆线(如来电长时间未接被系统取消): 不接这层的话
        // 记录悬挂、状态卡 ringing, 后续通话全被"已有两路"挡死
        EventLog.add(EventLog.CALL_ENDED, "sys", "系统侧拆线(onAbort) 号码=$tel")
        try {
            setDisconnected(DisconnectCause(DisconnectCause.ERROR))
            destroy()
        } catch (_: Throwable) {
        }
        CallEngine.teardown(rec)
    }

    override fun onHold() {
        EventLog.add(EventLog.CAR_HOLD, "car", "车机按了【保持】(onHold) 号码=$tel")
        // 真机 CHLD 只作用于 active: 响铃/拨号路不存在"保持", 按键照记但不改状态(第十二轮)
        if (rec.state != "active") return
        try {
            CallEngine.holdRec(rec, EventLog.CALL_HELD, "car", "车机保持")
        } catch (_: Throwable) {
        }
    }

    override fun onUnhold() {
        EventLog.add(EventLog.CAR_UNHOLD, "car", "车机取消保持(onUnhold) 号码=$tel")
        // stateEvt=null: 沿用车机回流只发 CAR_* 的旧例; 若另一路 active 自动保持
        try {
            CallEngine.activate(rec, null, "car", "")
        } catch (_: Throwable) {
        }
    }

    override fun onPlayDtmfTone(c: Char) {
        EventLog.add(EventLog.CAR_DTMF, "car", "车机 DTMF 按键: $c")
    }

    /** 通话音频路由变化: 车机通话界面/手机通话 UI 的"声音切换"(蓝牙/听筒/扬声器)
     *  最终都落到这 —— 对端音频必须跟着路由走。第十二轮前 CallAudioEngine 把播放器
     *  硬绑 SCO, 切到手机后手机无声、车机照响(真机: 切哪出哪)。 */
    override fun onCallAudioStateChanged(state: CallAudioState?) {
        state?.let { CallAudioEngine.onRouteChanged(it.route) }
    }

    override fun onStopDtmfTone() {
        Log.i(CallEngine.TAG, "车机 DTMF 按键结束")
    }
}
