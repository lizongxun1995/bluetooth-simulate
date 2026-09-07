package com.bt.vphone

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * 配对弹窗自动确认(无障碍) —— 最后一道无人值守防线。
 * PIN 模式由 BtEngine 反射 setPin 静默应答; consent/数字比较模式 App 无系统权限
 * (setPairingConfirmation 需 BLUETOOTH_PRIVILEGED), 系统弹确认框 → 本服务自动点「配对/确定」。
 * 一次性启用(adb, vphone_lib.enable_autoconfirm 已封装):
 *   settings put secure enabled_accessibility_services com.bt.vphone/.AutoPairService
 *   settings put secure accessibility_enabled 1
 * 防误点: 仅当窗口内容含配对特征词时动作; 只点肯定词按钮(精确匹配), 绝不点含否定词的。
 */
class AutoPairService : AccessibilityService() {

    /** 按钮文字精确匹配(trim 后), 避免误点「取消配对」这类含"配对"字样的否定按钮 */
    private val positives = listOf("配对", "确定", "允许", "是", "OK", "Pair", "Allow", "Yes")
    private val negatives = listOf("取消", "拒绝", "不配对", "取消配对", "Cancel", "Deny", "No", "Reject")
    private var lastClickAt = 0L

    override fun onAccessibilityEvent(e: AccessibilityEvent?) {
        val t = e?.eventType ?: return
        if (t != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
            t != AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
        ) return
        val root = rootInActiveWindow ?: return
        val pkg = root.packageName?.toString() ?: ""
        if (pkg == "com.bt.vphone") return
        if (System.currentTimeMillis() - lastClickAt < 1500) return

        val texts = mutableListOf<String>()
        collect(root, texts, 0)
        val looksPairing = pkg == "com.android.bluetooth" || texts.any {
            it.contains("配对") || it.contains("pairing", true) ||
                (it.contains("bluetooth", true) && it.contains("pair", true))
        }
        if (!looksPairing) return

        val btn = findPositive(root, 0) ?: return
        val label = btn.text?.toString() ?: btn.contentDescription?.toString() ?: "?"
        val clicked = clickIt(btn)
        if (clicked) {
            lastClickAt = System.currentTimeMillis()
            evt("配对弹窗已自动点「$label」(窗口=$pkg)")
        }
    }

    private fun collect(n: AccessibilityNodeInfo, out: MutableList<String>, depth: Int) {
        if (depth > 25 || out.size > 200) return
        n.text?.toString()?.let { if (it.isNotBlank()) out.add(it) }
        for (i in 0 until n.childCount) n.getChild(i)?.let { collect(it, out, depth + 1) }
    }

    private fun findPositive(n: AccessibilityNodeInfo, depth: Int): AccessibilityNodeInfo? {
        if (depth > 25) return null
        val label = n.text?.toString()?.trim() ?: n.contentDescription?.toString()?.trim() ?: ""
        if (label.isNotEmpty()) {
            val neg = negatives.any { label.contains(it, true) }
            if (!neg && positives.any { label.equals(it, true) }) return n
        }
        for (i in 0 until n.childCount) n.getChild(i)?.let { findPositive(it, depth + 1) }?.let { return it }
        return null
    }

    private fun clickIt(n: AccessibilityNodeInfo): Boolean {
        var cur: AccessibilityNodeInfo? = n
        var up = 0
        while (cur != null && up < 5) {
            if (cur.isClickable && cur.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
            cur = cur.parent
            up++
        }
        return false
    }

    override fun onInterrupt() {}

    private fun evt(msg: String) {
        Log.i(CallEngine.TAG, "[无障碍] $msg")
        try {
            sendBroadcast(Intent(CallEngine.EVT_ACTION).putExtra("msg", "[无障碍] $msg"))
        } catch (_: Exception) {
        }
    }
}
