package com.bt.vphone

import android.content.ContentProviderOperation
import android.content.pm.PackageManager
import android.os.Build
import android.provider.ContactsContract
import kotlin.concurrent.thread

/**
 * 联系人引擎: 向系统 ContactsProvider 批量写/删联系人(独立账号 vphone, 不动手机原有)。
 * 车机(HF)经 PBAP 从 ContactsProvider 拉电话本 —— 写 1 万个联系人即可测车机通讯录
 * 大列表加载/搜索/来电匹配姓名。
 *
 * 权限: READ_CONTACTS/WRITE_CONTACTS 运行时授权, PC 侧一条命令:
 *   adb shell pm grant com.bt.vphone android.permission.WRITE_CONTACTS
 *   adb shell pm grant com.bt.vphone android.permission.READ_CONTACTS
 * (vphone_lib.grant_perms() 已封装)
 *
 * 批量写为后台线程(1 万约 10~30s), /contacts/status 看进度。
 */
object ContactsEngine {
    private const val ACC_NAME = "vphone"
    private const val ACC_TYPE = "com.bt.vphone"

    @Volatile var busy = false
        private set
    @Volatile var progressDone = 0
        private set
    @Volatile var progressTotal = 0
        private set
    @Volatile var lastResult = "未执行"

    private fun ctx() = CallEngine.app

    private fun hasPerm(): Boolean {
        if (Build.VERSION.SDK_INT < 23) return true
        return ctx().checkSelfPermission(android.Manifest.permission.WRITE_CONTACTS) ==
            PackageManager.PERMISSION_GRANTED &&
            ctx().checkSelfPermission(android.Manifest.permission.READ_CONTACTS) ==
            PackageManager.PERMISSION_GRANTED
    }

    fun noPermHint() =
        "缺联系人权限 → PC 执行: vphone_ctl.py grant-perms " +
            "(或 adb shell pm grant com.bt.vphone android.permission.WRITE_CONTACTS)"

    /** 批量生成: /contacts/load?count=10000&prefix=联系人 → 联系人00001 / 13800000001 ... */
    fun load(count: Int, prefix: String): String {
        if (busy) return "批量任务进行中($progressDone/$progressTotal), /contacts/status 看进度"
        if (count !in 1..50000) return "count 需在 1..50000"
        if (!hasPerm()) return noPermHint()
        busy = true
        progressDone = 0
        progressTotal = count
        thread(name = "vphone-contacts") {
            try {
                val t0 = System.currentTimeMillis()
                val list = (1..count).map {
                    "$prefix${"%05d".format(it)}" to "138${"%08d".format(it)}"
                }
                insertBatch(list)
                lastResult = "已写入 $count 个联系人, 耗时 ${(System.currentTimeMillis() - t0) / 1000}s"
                EventLog.add(EventLog.CONTACTS_LOADED, "app", lastResult)
            } catch (e: Exception) {
                lastResult = "写入失败(第 $progressDone 个): ${e.message}"
            } finally {
                busy = false
            }
        }
        return "批量写入已启动: $count 个(名字=${prefix}00001..) → /contacts/status 看进度"
    }

    /** 导入自定义列表: 每行 姓名|号码 或 姓名,号码 (# 注释) */
    fun import(text: String): String {
        if (busy) return "批量任务进行中($progressDone/$progressTotal)"
        val list = text.split('\n', ';').mapNotNull { raw ->
            val line = raw.trim()
            if (line.isEmpty() || line.startsWith("#")) return@mapNotNull null
            val p = line.split('|', ',')
            val name = p.getOrNull(0)?.trim() ?: return@mapNotNull null
            val phone = (p.getOrNull(1) ?: "").trim()
            if (name.isEmpty() || phone.isEmpty()) null else name to phone
        }
        if (list.isEmpty()) return "无有效行(格式: 姓名|号码 或 姓名,号码)"
        if (!hasPerm()) return noPermHint()
        busy = true
        progressDone = 0
        progressTotal = list.size
        thread(name = "vphone-contacts") {
            try {
                val t0 = System.currentTimeMillis()
                insertBatch(list)
                lastResult = "已导入 ${list.size} 个联系人, 耗时 ${(System.currentTimeMillis() - t0) / 1000}s"
                EventLog.add(EventLog.CONTACTS_LOADED, "app", lastResult)
            } catch (e: Exception) {
                lastResult = "导入失败(第 $progressDone 个): ${e.message}"
            } finally {
                busy = false
            }
        }
        return "导入已启动: ${list.size} 个 → /contacts/status 看进度"
    }

    /** 只删 vphone 账号写入的联系人(按 ACCOUNT_TYPE 定点), 手机原有联系人不碰 */
    fun clear(): String {
        if (busy) return "批量任务进行中($progressDone/$progressTotal)"
        if (!hasPerm()) return noPermHint()
        busy = true
        progressDone = 0
        progressTotal = 0
        thread(name = "vphone-contacts") {
            try {
                val n = ctx().contentResolver.delete(
                    ContactsContract.RawContacts.CONTENT_URI,
                    "${ContactsContract.RawContacts.ACCOUNT_TYPE}=?", arrayOf(ACC_TYPE)
                )
                lastResult = "已删除 $n 个 vphone 联系人"
                EventLog.add(EventLog.CONTACTS_CLEARED, "app", lastResult)
            } catch (e: Exception) {
                lastResult = "删除失败: ${e.message}"
            } finally {
                busy = false
            }
        }
        return "清空已启动(仅 vphone 账号联系人)"
    }

    fun count(): String {
        if (!hasPerm()) return noPermHint()
        val ours = countWhere(
            ContactsContract.RawContacts.CONTENT_URI,
            "${ContactsContract.RawContacts.ACCOUNT_TYPE}=? AND ${ContactsContract.RawContacts.DELETED}=0",
            arrayOf(ACC_TYPE)
        )
        val total = countWhere(ContactsContract.Contacts.CONTENT_URI, null, null)
        return "vphone=$ours total=$total"
    }

    fun status(): String =
        "contacts ${if (busy) "busy $progressDone/$progressTotal" else "idle"} " +
            "count=${try { count() } catch (e: Exception) { "查询失败:${e.message}" }} last=$lastResult"

    // ---------------- 内部 ----------------

    private fun countWhere(uri: android.net.Uri, sel: String?, args: Array<String>?): Int =
        ctx().contentResolver.query(
            uri, arrayOf(ContactsContract.RawContacts._ID), sel, args, null
        )?.use { it.count } ?: -1

    private fun insertBatch(list: List<Pair<String, String>>) {
        val cr = ctx().contentResolver
        // ContentProvider 限制: 两次 yield 之间最多 500 op → 每批 ≤150 联系人(450 op)+批尾 yield
        list.chunked(150).forEach { part ->
            val ops = arrayListOf<ContentProviderOperation>()
            for ((idx, item) in part.withIndex()) {
                val (name, phone) = item
                val last = idx == part.size - 1
                val rawIdx = ops.size
                ops += ContentProviderOperation
                    .newInsert(ContactsContract.RawContacts.CONTENT_URI)
                    .withValue(ContactsContract.RawContacts.ACCOUNT_NAME, ACC_NAME)
                    .withValue(ContactsContract.RawContacts.ACCOUNT_TYPE, ACC_TYPE)
                    .build()
                ops += ContentProviderOperation
                    .newInsert(ContactsContract.Data.CONTENT_URI)
                    .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawIdx)
                    .withValue(
                        ContactsContract.Data.MIMETYPE,
                        ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE
                    )
                    .withValue(
                        ContactsContract.CommonDataKinds.StructuredName.DISPLAY_NAME, name
                    )
                    .build()
                val phoneOp = ContentProviderOperation
                    .newInsert(ContactsContract.Data.CONTENT_URI)
                    .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, rawIdx)
                    .withValue(
                        ContactsContract.Data.MIMETYPE,
                        ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE
                    )
                    .withValue(ContactsContract.CommonDataKinds.Phone.NUMBER, phone)
                    .withValue(
                        ContactsContract.CommonDataKinds.Phone.TYPE,
                        ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE
                    )
                if (last) phoneOp.withYieldAllowed(true)   // 批尾让出, 防 >500 op 限制
                ops += phoneOp.build()
            }
            cr.applyBatch(ContactsContract.AUTHORITY, ops)
            progressDone += part.size
        }
    }
}
