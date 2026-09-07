package com.bt.vphone

import android.util.Log
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import kotlin.concurrent.thread

/**
 * 手写极简 HTTP 服务(零第三方依赖): 局域网任意控制端可访问, PC 亦可经
 * adb reverse tcp:8800 tcp:8800 用 127.0.0.1 直达。只支持查询串参数(GET/POST 同)。
 */
class ControlServer(private val port: Int) {

    @Volatile private var running = false
    private var server: ServerSocket? = null

    fun start() {
        if (running) return
        running = true
        thread(isDaemon = true, name = "vphone-http") {
            try {
                server = ServerSocket(port, 8, InetAddress.getByName("0.0.0.0"))
                Log.i(CallEngine.TAG, "HTTP 控制面监听 0.0.0.0:$port")
                while (running) {
                    val sock = server?.accept() ?: break
                    thread(isDaemon = true) { handle(sock) }
                }
            } catch (e: Exception) {
                Log.w(CallEngine.TAG, "HTTP 服务退出: ${e.message}")
            }
        }
    }

    fun stop() {
        running = false
        try {
            server?.close()
        } catch (_: Exception) {
        }
    }

    private fun handle(sock: Socket) {
        try {
            sock.use { s ->
                s.soTimeout = 8000
                val ins = s.getInputStream()
                val head = readHead(ins)
                if (head.isEmpty()) return
                val lines = String(head, Charsets.ISO_8859_1).split("\r\n")
                val first = lines.firstOrNull() ?: return
                val parts = first.split(" ")
                if (parts.size < 2) {
                    respond(s, "400 Bad Request", "bad request")
                    return
                }
                val fullPath = parts[1]
                val qm = fullPath.indexOf('?')
                val path = if (qm >= 0) fullPath.substring(0, qm) else fullPath
                val query = if (qm >= 0) parseQuery(fullPath.substring(qm + 1)) else emptyMap()
                val result = Dispatcher.http(path, query)
                Log.i(CallEngine.TAG, "[HTTP] $path → $result")
                respond(s, "200 OK", result)
            }
        } catch (_: Exception) {
        }
    }

    /** 读到 \r\n\r\n 为止(请求体忽略, 参数全在查询串)。 */
    private fun readHead(ins: InputStream): ByteArray {
        val out = ByteArrayOutputStream()
        var state = 0
        while (state < 4) {
            val c = ins.read()
            if (c < 0) break
            out.write(c)
            state = when {
                state == 0 && c == '\r'.code -> 1
                state == 1 && c == '\n'.code -> 2
                state == 2 && c == '\r'.code -> 3
                state == 3 && c == '\n'.code -> 4
                else -> if (c == '\r'.code) 1 else 0
            }
        }
        return out.toByteArray()
    }

    private fun parseQuery(q: String): Map<String, String> {
        val map = mutableMapOf<String, String>()
        for (pair in q.split('&')) {
            if (pair.isEmpty()) continue
            val i = pair.indexOf('=')
            if (i < 0) map[URLDecoder.decode(pair, "UTF-8")] = ""
            else map[URLDecoder.decode(pair.substring(0, i), "UTF-8")] =
                URLDecoder.decode(pair.substring(i + 1), "UTF-8")
        }
        return map
    }

    private fun respond(s: Socket, statusLine: String, body: String) {
        val bytes = body.toByteArray(Charsets.UTF_8)
        val head = "HTTP/1.1 $statusLine\r\n" +
            "Content-Type: text/plain; charset=utf-8\r\n" +
            "Content-Length: ${bytes.size}\r\n" +
            "Connection: close\r\n\r\n"
        val os: OutputStream = s.getOutputStream()
        os.write(head.toByteArray(Charsets.ISO_8859_1))
        os.write(bytes)
        os.flush()
    }
}
