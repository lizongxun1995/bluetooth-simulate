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
        val q = mapOf(
            "number" to (intent.getStringExtra("number") ?: ""),
            "key" to (intent.getStringExtra("key") ?: ""),
            "on" to (intent.getStringExtra("on") ?: ""),
            "title" to (intent.getStringExtra("title") ?: ""),
            "artist" to (intent.getStringExtra("artist") ?: ""),
            "album" to (intent.getStringExtra("album") ?: ""),
            "duration" to (intent.getStringExtra("duration") ?: ""),
            "text" to (intent.getStringExtra("text") ?: "")
        )
        val result = Dispatcher.cmd(cmd, q)
        Log.i(CallEngine.TAG, "[adb] $cmd → $result")
    }
}
