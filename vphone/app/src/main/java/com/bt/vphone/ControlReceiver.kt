package com.bt.vphone

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * adb 控制面(USB 直连兜底, 不依赖手机入网):
 *   adb shell am broadcast -a com.bt.vphone.CMD --es cmd incoming --es number 13800138000
 *   adb shell am broadcast -a com.bt.vphone.CMD --es cmd track --es title 青花瓷 --es artist 周杰伦 --es duration 229
 * 其余 cmd: dial/answer/hangup/hold/dtmf/audio_bt/play/pause/next/prev/silence/playlist/status
 */
class ControlReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val cmd = intent.getStringExtra("cmd") ?: return
        // 引擎可能在服务未起时先被调用: 就地初始化(幂等)
        CallEngine.init(context)
        MediaEngine.init(context)
        MediaEngine.start()
        VPhoneService.ensureStarted(context)
        // 全量透传 string extras: 早期白名单只转 8 个参数, 广播通道下
        // mac/name(bond/unpair/disconnect)、pos(seek)、idx(jump)、count(contacts_load)
        // 全都拿不到值(或被路由默认值静默带偏)。非 string extra 忽略。
        val q = mutableMapOf<String, String>()
        val extras = intent.extras
        if (extras != null) for (k in extras.keySet()) extras.getString(k)?.let { q[k] = it }
        val result = Dispatcher.cmd(cmd, q)
        Log.i(CallEngine.TAG, "[adb] $cmd → $result")
    }
}
