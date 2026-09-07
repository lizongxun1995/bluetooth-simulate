package com.bt.vphone

import android.annotation.SuppressLint
import android.bluetooth.BluetoothA2dp
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothHeadset
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.util.Log
import java.lang.reflect.Method
import java.util.concurrent.ConcurrentHashMap

/**
 * 蓝牙射频引擎: App 内扫描/配对/解配对 + A2DP/HFP 连接状态证据。
 * 配对不再依赖系统设置页 —— HTTP /bt/scan → /bt/bond 两步走:
 *   - PIN 模式: ACTION_PAIRING_REQUEST 反射 setPin("1234") 自动应答;
 *   - consent/数字比较模式: setPairingConfirmation 需系统权限多半被拒,
 *     弹确认框由 adb/u2 自动点(事件流会提示)。
 * A2DP/HFP profile 代理常驻, /bt/state 直接给出「已连 A2DP/HFP」判定。
 */
object BtEngine {
    data class DevInfo(var name: String, var rssi: Int)

    lateinit var app: Context
        private set
    private var inited = false

    private val adapter: BluetoothAdapter?
        get() = (app.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    val scanResults = ConcurrentHashMap<String, DevInfo>()

    @Volatile
    var scanning = false
        private set

    @Volatile private var a2dp: BluetoothA2dp? = null
    @Volatile private var headset: BluetoothHeadset? = null

    fun init(ctx: Context) {
        if (inited) return
        app = ctx.applicationContext
        inited = true
        val f = IntentFilter().apply {
            addAction(BluetoothDevice.ACTION_FOUND)
            addAction(BluetoothAdapter.ACTION_DISCOVERY_FINISHED)
            addAction(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
            addAction(BluetoothDevice.ACTION_PAIRING_REQUEST)
            addAction(BluetoothDevice.ACTION_ACL_CONNECTED)
            addAction(BluetoothDevice.ACTION_ACL_DISCONNECTED)
            addAction(BluetoothA2dp.ACTION_CONNECTION_STATE_CHANGED)
            addAction(BluetoothHeadset.ACTION_CONNECTION_STATE_CHANGED)
        }
        app.registerReceiver(rx, f)
        try {
            adapter?.let { ad ->
                val l = object : BluetoothProfile.ServiceListener {
                    override fun onServiceConnected(profile: Int, proxy: BluetoothProfile?) {
                        when (profile) {
                            BluetoothProfile.A2DP -> a2dp = proxy as? BluetoothA2dp
                            BluetoothProfile.HEADSET -> headset = proxy as? BluetoothHeadset
                        }
                        evt("profile 代理就绪: $profile")
                    }

                    override fun onServiceDisconnected(profile: Int) {
                        when (profile) {
                            BluetoothProfile.A2DP -> a2dp = null
                            BluetoothProfile.HEADSET -> headset = null
                        }
                    }
                }
                ad.getProfileProxy(app, l, BluetoothProfile.A2DP)
                ad.getProfileProxy(app, l, BluetoothProfile.HEADSET)
            }
        } catch (e: Exception) {
            Log.w(CallEngine.TAG, "profile 代理获取失败: ${e.message}")
        }
        Log.i(CallEngine.TAG, "BtEngine 就绪(扫描/配对/profile 代理)")
    }

    private val rx = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, i: Intent?) {
            when (i?.action) {
                BluetoothDevice.ACTION_FOUND -> {
                    val d = i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    val rssi = i.getShortExtra(BluetoothDevice.EXTRA_RSSI, 0.toShort()).toInt()
                    val name = sName(d)
                    scanResults[d.address] = DevInfo(name, rssi)
                    evt("扫描发现: $name ${d.address} ($rssi dBm)")
                }
                BluetoothAdapter.ACTION_DISCOVERY_FINISHED -> {
                    scanning = false
                    evt("扫描结束: 共 ${scanResults.size} 台")
                }
                BluetoothDevice.ACTION_BOND_STATE_CHANGED -> {
                    val d = i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    val s = i.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1)
                    evt("配对状态: ${sName(d)} ${d.address} → ${bondStr(s)}")
                    if (s == BluetoothDevice.BOND_BONDED) {
                        EventLog.add(EventLog.BT_BONDED, "bt", "已配对: ${sName(d)} ${d.address}")
                        setPrioBestEffort(d)
                        evt("已配对完成 → 若 A2DP/HFP 未自动连, 用 /bt/reconnect?mac=${d.address}")
                    }
                }
                BluetoothDevice.ACTION_PAIRING_REQUEST -> {
                    val d = i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    handlePairing(d, i.getIntExtra(BluetoothDevice.EXTRA_PAIRING_VARIANT, -1))
                }
                BluetoothDevice.ACTION_ACL_CONNECTED -> {
                    val d = i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    EventLog.add(EventLog.BT_ACL_CONNECTED, "bt", "ACL 已建立: ${sName(d)} ${d.address}")
                }
                BluetoothDevice.ACTION_ACL_DISCONNECTED -> {
                    val d = i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    EventLog.add(
                        EventLog.BT_ACL_DISCONNECTED, "bt",
                        "ACL 已断开: ${sName(d)} ${d.address} → /bt/reconnect 可发起回连"
                    )
                }
                BluetoothA2dp.ACTION_CONNECTION_STATE_CHANGED -> {
                    val d = i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    val s = i.getIntExtra(BluetoothProfile.EXTRA_STATE, -1)
                    if (s == BluetoothProfile.STATE_CONNECTED)
                        EventLog.add(EventLog.BT_A2DP_CONNECTED, "bt", "A2DP 已连: ${sName(d)}")
                    if (s == BluetoothProfile.STATE_DISCONNECTED)
                        EventLog.add(EventLog.BT_A2DP_DISCONNECTED, "bt", "A2DP 已断: ${sName(d)}")
                    evt("A2DP 状态: ${sName(d)} → ${profStr(s)}")
                }
                BluetoothHeadset.ACTION_CONNECTION_STATE_CHANGED -> {
                    val d = i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                    val s = i.getIntExtra(BluetoothProfile.EXTRA_STATE, -1)
                    if (s == BluetoothProfile.STATE_CONNECTED)
                        EventLog.add(EventLog.BT_HFP_CONNECTED, "bt", "HFP 已连: ${sName(d)}")
                    if (s == BluetoothProfile.STATE_DISCONNECTED)
                        EventLog.add(EventLog.BT_HFP_DISCONNECTED, "bt", "HFP 已断: ${sName(d)}")
                    evt("HFP 状态: ${sName(d)} → ${profStr(s)}")
                }
            }
        }
    }

    private fun handlePairing(d: BluetoothDevice, variant: Int) {
        evt("配对请求: ${sName(d)} ${d.address} 模式=${vStr(variant)}")
        val ok = try {
            if (variant == BluetoothDevice.PAIRING_VARIANT_PIN) setPin(d, "1234") else confirm(d)
        } catch (e: Throwable) {
            evt("自动应答异常: ${e.message}")
            false
        }
        if (ok) {
            evt("已自动应答配对(模式=${vStr(variant)})")
            try { rx.abortBroadcast() } catch (_: Exception) {}
        } else {
            evt("自动应答不可用 → 手机弹窗需 adb/u2 自动点(或人工点一次『配对』)")
        }
    }

    private fun setPin(d: BluetoothDevice, pin: String): Boolean = try {
        val m: Method = d.javaClass.getMethod("setPin", ByteArray::class.java)
        m.invoke(d, pin.toByteArray()) as? Boolean ?: false
    } catch (_: Throwable) {
        false
    }

    private fun confirm(d: BluetoothDevice): Boolean = try {
        val m: Method = d.javaClass.getMethod("setPairingConfirmation", Boolean::class.java)
        m.invoke(d, true) as? Boolean ?: false
    } catch (_: Throwable) {
        false
    }

    // ---------------- 命令 ----------------

    @SuppressLint("MissingPermission")
    fun scan(): String {
        val ad = adapter ?: return "无蓝牙适配器"
        if (!ad.isEnabled) return "蓝牙未开, 先 /bt/enable?on=1"
        return try {
            scanResults.clear()
            scanning = true
            if (ad.startDiscovery()) "扫描已启动(约12s) → 稍后 /bt/scan-result"
            else { scanning = false; "startDiscovery 失败(可能正在扫描/权限缺失)" }
        } catch (e: Throwable) {
            scanning = false
            "扫描失败: ${e.message} (API<31 需位置权限+定位服务开; 31+ 需 BLUETOOTH_SCAN)"
        }
    }

    fun scanResult(): String {
        if (scanResults.isEmpty()) return "(空: 先 /bt/scan; 或无位置权限/定位服务没开)"
        return scanResults.entries.sortedByDescending { it.value.rssi }.joinToString("\n") {
            "${it.value.name}|${it.key}|${it.value.rssi}"
        }
    }

    /** 支持名字片段或 MAC: /bt/bond?mac=CARKIT-1 或 00:11:22:33:44:55 */
    fun bond(target: String): String {
        val ad = adapter ?: return "无蓝牙适配器"
        if (target.isBlank()) return "用法: /bt/bond?mac=<MAC或名字片段>"
        return try {
            val bondedHit = ad.bondedDevices.firstOrNull {
                it.address.equals(target, true) || sName(it).contains(target, true)
            }
            val dev = bondedHit ?: scanResults.entries
                .firstOrNull { it.key.equals(target, true) || it.value.name.contains(target, true) }
                ?.let { ad.getRemoteDevice(it.key) } ?: ad.getRemoteDevice(target)
            when {
                dev.bondState == BluetoothDevice.BOND_BONDED ->
                    "已配对过: ${sName(dev)} ${dev.address} (重配先 /bt/unpair)"
                dev.createBond() ->
                    "createBond 已发起: ${sName(dev)} ${dev.address} → 看事件流(配对请求/状态)"
                else -> "createBond 返回 false"
            }
        } catch (e: Throwable) {
            "bond 失败: ${e.message}"
        }
    }

    fun unpair(target: String): String {
        val ad = adapter ?: return "无蓝牙适配器"
        if (target.isBlank()) return "用法: /bt/unpair?mac=<MAC或名字片段>"
        return try {
            val d = ad.bondedDevices.firstOrNull {
                it.address.equals(target, true) || sName(it).contains(target, true)
            } ?: return "未找到已配对设备: $target (/bt/state 查列表)"
            val m: Method = d.javaClass.getMethod("removeBond")
            if (m.invoke(d) as? Boolean == true) "解配对已发起: ${sName(d)} ${d.address}"
            else "removeBond 返回 false"
        } catch (e: Throwable) {
            "解配对失败: ${e.message}"
        }
    }

    /**
     * 断线重连(手机侧主动发起)。三条路:
     * ① 反射 BluetoothDevice.connect()(API 29+ 隐藏, 部分 ROM 可用);
     * ② A2DP/HFP profile 代理反射 connect();
     * ③ 兜底: 蓝牙关→2s→开, 触发系统对 priority=ON 的已配对设备自动回连。
     * 结果看事件流(ACL/HFP/A2DP 状态)或 /bt/state 的 [A2DP已连]/[HFP已连]。
     */
    fun reconnect(target: String, fallback: Boolean = true): String {
        val ad = adapter ?: return "无蓝牙适配器"
        if (target.isBlank()) return "用法: /bt/reconnect?mac=<MAC或名字片段>"
        val d = ad.bondedDevices.firstOrNull {
            it.address.equals(target, true) || sName(it).contains(target, true)
        } ?: return "未找到已配对设备: $target (/bt/state 查列表)"
        val sb = StringBuilder("重连 ${sName(d)} ${d.address}:\n")

        var devOk: Boolean? = null
        try {
            val m: Method = d.javaClass.getMethod("connect")
            devOk = m.invoke(d) as? Boolean ?: false
            sb.append("① BluetoothDevice.connect() → $devOk\n")
        } catch (e: Throwable) {
            sb.append("① device.connect 不可用(${cause(e)})\n")
        }

        var proxyOk = 0
        val a2 = a2dp
        if (a2 != null) try {
            val m: Method = a2.javaClass.getMethod("connect", BluetoothDevice::class.java)
            val r = m.invoke(a2, d) as? Boolean ?: false
            if (r) proxyOk++
            sb.append("② A2DP.connect → $r\n")
        } catch (e: Throwable) {
            sb.append("② A2DP.connect 不可用(${cause(e)})\n")
        }
        val hs = headset
        if (hs != null) try {
            val m: Method = hs.javaClass.getMethod("connect", BluetoothDevice::class.java)
            val r = m.invoke(hs, d) as? Boolean ?: false
            if (r) proxyOk++
            sb.append("③ HFP.connect → $r\n")
        } catch (e: Throwable) {
            sb.append("③ HFP.connect 不可用(${cause(e)})\n")
        }

        if (fallback && devOk != true && proxyOk == 0) {
            sb.append("④ 兜底: 蓝牙关→2s→开(系统自动回连 priority=ON 设备)\n")
            Thread {
                try {
                    setEnabled(false)
                    Thread.sleep(2000)
                    setEnabled(true)
                    evt("兜底蓝牙重启完成, 等系统自动回连…")
                } catch (_: Throwable) {
                }
            }.start()
        }
        sb.append("→ 看事件流(ACL/HFP/A2DP)或 /bt/state 的 [A2DP已连]/[HFP已连]")
        evt("发起重连: ${sName(d)}")
        return sb.toString().trim()
    }

    /**
     * 授权车机访问 联系人/通话记录/信息(PBAP/MAP) —— 车机能拉通讯录的前提。
     * 优先反射 setPhonebookAccessPermission(1)/setMessageAccessPermission(1)(隐藏API,
     * 部分ROM直接可写); 被系统权限挡住时 → 靠无障碍自动点授权弹窗(已扩到联系人授权框)
     * 或人工一次: 设置→蓝牙→该设备→「共享联系人/访问通讯录」开关。
     */
    @SuppressLint("MissingPermission")
    fun allowCarAccess(target: String): String {
        val ad = adapter ?: return "无蓝牙适配器"
        if (target.isBlank()) return "用法: /bt/allow-car?mac=<MAC或名字片段>"
        val d = ad.bondedDevices.firstOrNull {
            it.address.equals(target, true) || sName(it).contains(target, true)
        } ?: return "未找到已配对设备: $target (/bt/state 查列表)"
        val sb = StringBuilder("授权 ${sName(d)} ${d.address}:\n")

        fun permGet(name: String): String = try {
            val m: Method = d.javaClass.getMethod(name)
            when (m.invoke(d) as? Int) {
                1 -> "ALLOWED"; 2 -> "FORBIDDEN"; else -> "UNKNOWN"
            }
        } catch (e: Throwable) {
            "不可用(${cause(e)})"
        }

        fun permSet(name: String, v: Int): String = try {
            val m: Method = d.javaClass.getMethod(name, Int::class.javaPrimitiveType)
            if (m.invoke(d, v) as? Boolean == true) "OK" else "返回false"
        } catch (e: Throwable) {
            "被拒(${cause(e)})"
        }

        sb.append("PBAP联系人: 之前=${permGet("getPhonebookAccessPermission")} → ${permSet("setPhonebookAccessPermission", 1)}\n")
        sb.append("MAP信息:     之前=${permGet("getMessageAccessPermission")} → ${permSet("setMessageAccessPermission", 1)}\n")
        sb.append("SIM卡:       之前=${permGet("getSimAccessPermission")} → ${permSet("setSimAccessPermission", 1)}\n")
        sb.append("现在: PBAP=${permGet("getPhonebookAccessPermission")} MAP=${permGet("getMessageAccessPermission")}\n")
        val ok = permGet("getPhonebookAccessPermission") == "ALLOWED"
        sb.append(
            if (ok) "✓ 车机已可拉通讯录 → 车机上刷新通讯录/重连蓝牙即触发 PBAP 下载"
            else "⚠ 反射被系统权限挡住 → 走弹窗自动点(重新触发授权框)或人工一次:\n" +
                "   设置→蓝牙→${sName(d)}→开启「共享联系人/访问通讯录」"
        )
        evt("车机访问授权: ${sName(d)} PBAP=${permGet("getPhonebookAccessPermission")}")
        return sb.toString()
    }

    /** 配对完成后把 A2DP/HFP priority 置 ON(100), 保证系统自动回连愿意连它 */    private fun setPrioBestEffort(d: BluetoothDevice) {
        val proxies = listOf(a2dp to "A2DP", headset to "HFP")
        for (p in proxies) {
            val proxy = p.first ?: continue
            try {
                val m: Method = proxy.javaClass.getMethod(
                    "setPriority", BluetoothDevice::class.java, Int::class.javaPrimitiveType
                )
                m.invoke(proxy, d, 100) // PRIORITY_ON
                evt("${p.second} priority → 100(ON)")
            } catch (e: Throwable) {
                evt("${p.second} setPriority 不可用(${cause(e)})")
            }
        }
    }

    private fun cause(t: Throwable) = t.cause?.message ?: t.message ?: t.javaClass.simpleName

    @SuppressLint("MissingPermission")
    fun state(): String {
        val ad = adapter ?: return "无蓝牙适配器"
        val sb = StringBuilder()
        val enabled = try { ad.isEnabled } catch (_: Exception) { false }
        sb.append("bt=${if (enabled) "ON" else "OFF"} name=${sAdName(ad)} addr=${try { ad.address } catch (_: Exception) { "?" }}\n")
        val bonded = try {
            ad.bondedDevices
        } catch (e: Exception) {
            sb.append("bonded 查询失败: ${e.message}\n")
            emptySet()
        }
        if (bonded.isEmpty()) {
            sb.append("已配对: (无)\n")
        } else {
            for (d in bonded) {
                sb.append("已配对: ${sName(d)} ${d.address}")
                val a2 = profileHas(a2dp, d)
                val hs = profileHas(headset, d)
                if (a2) sb.append(" [A2DP已连]")
                if (hs) sb.append(" [HFP已连]")
                sb.append("\n")
            }
        }
        return sb.toString().trimEnd()
    }

    private fun profileHas(p: BluetoothProfile?, d: BluetoothDevice): Boolean = try {
        p?.connectedDevices?.any { it.address.equals(d.address, true) } == true
    } catch (_: Throwable) {
        false
    }

    @Suppress("DEPRECATION")
    fun setEnabled(on: Boolean): String {
        val ad = adapter ?: return "无蓝牙适配器"
        return try {
            when {
                on && ad.isEnabled -> "蓝牙已是开"
                !on && !ad.isEnabled -> "蓝牙已是关"
                on && ad.enable() -> "蓝牙开启中…"
                !on && ad.disable() -> "蓝牙关闭中…"
                else -> "开关请求被拒(31+ 需 BLUETOOTH_CONNECT/厂商限制)"
            }
        } catch (e: Throwable) {
            "蓝牙开关失败: ${e.message}"
        }
    }

    // ---------------- 杂项 ----------------

    private fun sName(d: BluetoothDevice): String = try { d.name ?: "?" } catch (_: Throwable) { "?" }
    private fun sAdName(ad: BluetoothAdapter): String = try { ad.name ?: "?" } catch (_: Throwable) { "?" }

    private fun bondStr(s: Int) = when (s) {
        BluetoothDevice.BOND_NONE -> "NONE(已解除)"
        BluetoothDevice.BOND_BONDING -> "PAIRING(进行中)"
        BluetoothDevice.BOND_BONDED -> "BONDED(已配对)"
        else -> "?$s"
    }

    private fun vStr(v: Int) = when (v) {
        BluetoothDevice.PAIRING_VARIANT_PIN -> "PIN输入"
        BluetoothDevice.PAIRING_VARIANT_PASSKEY_CONFIRMATION -> "数字比较确认"
        3 -> "同意即配"      // PAIRING_VARIANT_CONSENT(隐藏常量)
        1 -> "输入密钥"      // PAIRING_VARIANT_PASSKEY(隐藏常量)
        else -> "其他($v)"
    }

    private fun profStr(s: Int) = when (s) {
        BluetoothProfile.STATE_DISCONNECTED -> "DISCONNECTED"
        BluetoothProfile.STATE_CONNECTING -> "CONNECTING"
        BluetoothProfile.STATE_CONNECTED -> "CONNECTED"
        BluetoothProfile.STATE_DISCONNECTING -> "DISCONNECTING"
        else -> "?$s"
    }

    private fun evt(msg: String) {
        Log.i(CallEngine.TAG, "[蓝牙] $msg")
        try {
            app.sendBroadcast(Intent(CallEngine.EVT_ACTION).putExtra("msg", "[蓝牙] $msg"))
        } catch (_: Exception) {
        }
    }
}
