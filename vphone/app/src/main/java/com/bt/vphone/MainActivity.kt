package com.bt.vphone

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.widget.Button
import android.widget.TextView
import android.widget.Toast

/**
 * 一次性装配页: 授权 → 启动常驻服务 → 注册并启用电话账号。
 * 日常控制全部走 HTTP/adb, 这个界面只在装机时用一次。
 */
class MainActivity : Activity() {

    private lateinit var tv: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        CallEngine.init(this)
        MediaEngine.init(this)

        tv = findViewById(R.id.tvStatus) as TextView

        btn(R.id.btnService).setOnClickListener {
            VPhoneService.ensureStarted(this)
            toast("常驻服务已启动(HTTP :${VPhoneService.PORT})")
            refresh()
        }
        btn(R.id.btnAcct).setOnClickListener {
            toast(CallEngine.registerAccount())
            refresh()
        }
        btn(R.id.btnAcctSettings).setOnClickListener { openAccountSettings() }
        btn(R.id.btnIncoming).setOnClickListener {
            VPhoneService.ensureStarted(this)
            toast(CallEngine.incoming("13800138000"))
        }
        btn(R.id.btnHangup).setOnClickListener { toast(CallEngine.hangup()) }
        btn(R.id.btnRefresh).setOnClickListener { refresh() }

        requestPerms()
        VPhoneService.ensureStarted(this)
        refresh()
    }

    private fun btn(id: Int): Button = findViewById(id) as Button

    private fun requestPerms() {
        if (Build.VERSION.SDK_INT < 23) return // 6.0 以下装机即授权
        val wanted = mutableListOf(
            Manifest.permission.CALL_PHONE,
            Manifest.permission.READ_PHONE_STATE
        )
        if (Build.VERSION.SDK_INT >= 26) wanted.add(Manifest.permission.ANSWER_PHONE_CALLS)
        // 蓝牙扫描/配对: 31- 靠位置权限出扫描结果, 31+ 用新粒度权限
        if (Build.VERSION.SDK_INT >= 31) {
            wanted.add(Manifest.permission.BLUETOOTH_SCAN)
            wanted.add(Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            wanted.add(Manifest.permission.ACCESS_FINE_LOCATION)
        }
        val need = wanted.filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (need.isNotEmpty()) {
            try {
                requestPermissions(need.toTypedArray(), 1)
            } catch (e: Exception) {
                // 定制设备(车机开发板/工控屏等)可能没有标准权限弹窗 Activity
                // (CUSTOMIZE_REQUEST_PERMISSIONS 由厂商 launcher 处理或缺失) → 不崩,
                // 提示走 PC 侧 pm grant 授权(vphone_lib.grant_perms 已封装)
                toast("系统权限弹窗不可用(${e.javaClass.simpleName}) → PC 执行 vphone_ctl.py grant-perms 授权")
                Log.w(CallEngine.TAG, "requestPermissions 不可用: ${e.message}")
            }
        }
    }

    /** 跳到电话账号启用页(厂商入口不一, 失败则提示手动路径)。 */
    private fun openAccountSettings() {
        try {
            val it = Intent("android.telecom.action.CHANGE_PHONE_ACCOUNT_SETTINGS")
            it.putExtra("android.telecom.extra.PHONE_ACCOUNT_HANDLE", CallEngine.handle)
            startActivity(it)
            toast("在列表里找到 VPhone 并启用(勾选来电/拨出)")
        } catch (e: Exception) {
            toast("直达入口不可用: 手动路径=拨号盘→设置(⚙)→呼叫账号/其他 → 启用 VPhone")
        }
    }

    private fun refresh() {
        CallEngine.registerAccount() // 幂等, 顺带确保已注册
        val ip = localIp()
        tv.text =
            "机型: ${Build.MANUFACTURER} ${Build.MODEL} (Android ${Build.VERSION.RELEASE})\n\n" +
                "电话账号: ${CallEngine.accountStatus()}\n" +
                "${CallEngine.status()}\n" +
                "${MediaEngine.status()}\n\n" +
                "控制面:\n" +
                "· HTTP: http://$ip:${VPhoneService.PORT}/status\n" +
                "  (PC 无需 WiFi: adb reverse tcp:${VPhoneService.PORT} tcp:${VPhoneService.PORT}\n" +
                "   然后 curl http://127.0.0.1:${VPhoneService.PORT}/status)\n" +
                "· adb : am broadcast -a com.bt.vphone.CMD --es cmd incoming --es number 13800138000\n\n" +
                "事件观察: adb logcat -s VPhone"
    }

    private fun localIp(): String {
        return try {
            @Suppress("Deprecation")
            val wifi = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            val raw = wifi.connectionInfo.ipAddress
            String.format(
                "%d.%d.%d.%d", raw and 0xff, raw shr 8 and 0xff,
                raw shr 16 and 0xff, raw shr 24 and 0xff
            )
        } catch (e: Exception) {
            "(见 WiFi 设置)"
        }
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }
}
