// btwinrt —— Windows 原生(WinRT)蓝牙模拟引擎, 供 Python(pythonnet)/C# 共用
// 能力: SMTC 假元数据注入 + AudioGraph 静音流(激活 A2DP) + CallControl 来电(动态取) +
//       32feet.NET scan/pair/unpair。
// 互操作依据(windemo 首轮已实测): ISystemMediaTransportControlsInterop,
//   - ISystemMediaTransportControls IID = 99FA3FF4-1742-42A6-902E-087D41F965EC
//   - 互操作 GUID: winmd=DDB0472D-C911-4A1F-86D9-DC3D71A95F5A (首选)
//                  SO帖=DD4BE2AC-9CA1-5C86-A92F-AF616C3F8B13 (兜底)
//   - vtable 前三个 IInspectable 基方法必须显式占位
// 窗口: 原生消息窗口(HWND_MESSAGE) + GetMessage 泵, 不依赖 WinForms。
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using InTheHand.Net;
using InTheHand.Net.Bluetooth;
using InTheHand.Net.Sockets;
using Windows.Devices.Bluetooth;
using Windows.Devices.Bluetooth.Rfcomm;
using Windows.Devices.Enumeration;
using Windows.Devices.Radios;
using Windows.Networking.Sockets;
using Windows.Media;
using Windows.Media.Audio;
using Windows.Media.Control;
using Windows.Media.Devices;
using Windows.Media.Render;

namespace BtWinRT;

// winmd 里的互操作接口 GUID (反射核实)
[ComImport]
[Guid("DDB0472D-C911-4A1F-86D9-DC3D71A95F5A")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface ISmtcInteropWinmd
{
    // IInspectable 三个基方法必须显式占位, 否则 GetForWindow 落在错误 vtable 槽位
    [PreserveSig] int GetIids(out int count, out IntPtr ids);
    [PreserveSig] int GetRuntimeClassName(out IntPtr className);
    [PreserveSig] int GetTrustLevel(out int trustLevel);
    [PreserveSig] int GetForWindow(IntPtr window, ref Guid riid, out IntPtr smtc);
}

// SO#62222833 等社区资料里的 GUID —— 兜底
[ComImport]
[Guid("DD4BE2AC-9CA1-5C86-A92F-AF616C3F8B13")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface ISmtcInteropLegacy
{
    [PreserveSig] int GetIids(out int count, out IntPtr ids);
    [PreserveSig] int GetRuntimeClassName(out IntPtr className);
    [PreserveSig] int GetTrustLevel(out int trustLevel);
    [PreserveSig] int GetForWindow(IntPtr window, ref Guid riid, out IntPtr smtc);
}

internal static class Win32
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct MSG
    {
        public IntPtr hwnd; public uint message; public IntPtr wParam; public IntPtr lParam;
        public uint time; public int ptX; public int ptY;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    internal static extern IntPtr CreateWindowExW(int dwExStyle, string lpClassName, string lpWindowName, int dwStyle,
        int x, int y, int nWidth, int nHeight, IntPtr hWndParent, IntPtr hMenu, IntPtr hInstance, IntPtr lpParam);
    [DllImport("user32.dll")] internal static extern bool DestroyWindow(IntPtr hWnd);
    [DllImport("user32.dll")] internal static extern int GetMessageW(out MSG msg, IntPtr hWnd, uint wMin, uint wMax);
    [DllImport("user32.dll")] internal static extern bool TranslateMessage(ref MSG msg);
    [DllImport("user32.dll")] internal static extern IntPtr DispatchMessageW(ref MSG msg);
    [DllImport("user32.dll")] internal static extern bool PostThreadMessageW(uint idThread, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("kernel32.dll")] internal static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] internal static extern IntPtr GetModuleHandleW(string? name);
}

/// <summary>蓝牙媒体/来电模拟引擎。事件均在 .NET 后台线程触发, GUI 侧需自行切回 UI 线程。</summary>
public sealed class Engine : IDisposable
{
    /// <summary>全部动作与事件日志(带时间戳由调用方加, 这里只发正文)。</summary>
    public event Action<string>? OnLog;
    /// <summary>车机/系统侧媒体按键(Play/Pause/Next/Previous...)。</summary>
    public event Action<string>? OnButton;
    /// <summary>扫描完成, 参数为多行文本: "序号 | 名称 | MAC | 配对态 | 连接态 | COD"。</summary>
    public event Action<string>? OnScanDone;

    private SystemMediaTransportControls? _smtc;
    private readonly StreamWriter _fileLog;
    private readonly ManualResetEventSlim _ready = new(false);
    private Thread? _windowThread;
    private uint _windowThreadId;
    private IntPtr _hwnd;
    private Exception? _initException;
    private int _trackNo = 1;
    private string _title = "测试曲目1", _artist = "测试歌手", _album = "测试专辑";
    private CallControl? _call;
    private bool _callEventsWired;
    private ulong _lastCallId;
    private List<BluetoothDeviceInfo> _devices = new();
    private AudioGraph? _graph;
    private AudioDeviceOutputNode? _outNode;
    private AudioFrameInputNode? _frameNode;
    private string _silenceEndpoint = "";
    private StreamSocket? _hfpSocket;
    private BluetoothWin32Authentication? _auth; // 系统级认证代答: 吃掉"连接码"弹窗
    private string _pin = "1234";
    // SMTC 时间轴: 不调 UpdateTimelineProperties 车机进度/时长恒为 0 (10:51 台架反馈)
    private TimeSpan _pos = TimeSpan.Zero, _len = TimeSpan.FromSeconds(240);
    private Timer? _ticker;
    private bool _disposed;
    private static readonly Guid HfServiceGuid = new("0000111e-0000-1000-8000-00805f9b34fb");

    public Engine()
    {
        // 日志放 DLL 旁边, 而不是宿主(python)目录
        string dir = Path.GetDirectoryName(typeof(Engine).Assembly.Location) ?? AppContext.BaseDirectory;
        _fileLog = new StreamWriter(Path.Combine(dir, "btwinrt.log"), append: true) { AutoFlush = true };
        Log("========== 引擎构造 ==========");
    }

    /// <summary>初始化: 消息窗口 + SMTC 互操作。失败抛异常。</summary>
    public string Init()
    {
        // 无头认证代答: 注册后系统蓝牙认证回调(配对/连接码/数字比较)全部由代码应答,
        // 不再弹 Windows 确认框 —— 11:07 台架反馈"SetServiceState 激活111E时弹连接码要手点"的根治点。
        // 注意: 引擎存活期间对本机所有蓝牙认证事件生效(测试机可接受)。
        _auth = new BluetoothWin32Authentication(OnAuthRequested);
        _windowThread = new Thread(WindowThread) { IsBackground = true, Name = "btwinrt-window" };
        _windowThread.SetApartmentState(ApartmentState.STA);
        _windowThread.Start();
        if (!_ready.Wait(15000) || _hwnd == IntPtr.Zero)
            throw new TimeoutException("窗口线程初始化超时(15s)");
        // SMTC 互操作在调用方线程做(旧 windemo 的成功形态: 主线程 STA);
        // 专用 STA 窗口线程上 QI 反而 E_NOINTERFACE(10:24 实测), 不要挪回去
        InitSmtc();
        string callState = RefreshCallControl();
        string msg = $"引擎就绪 HWND=0x{_hwnd:X}; CallControl={(_call == null ? "null(无电话设备)" : $"OK HasRinger={_call.HasRinger}")}";
        Log(msg);
        return msg;
    }

    // ---------------- SMTC 元数据 ----------------

    public void SetTrack(string title, string artist, string album, int lengthSec = 240)
    {
        ThrowIfNotReady();
        _title = string.IsNullOrEmpty(title) ? _title : title;
        _artist = string.IsNullOrEmpty(artist) ? _artist : artist;
        if (!string.IsNullOrEmpty(album)) _album = album;
        if (lengthSec > 0) _len = TimeSpan.FromSeconds(lengthSec);
        _pos = TimeSpan.Zero;
        PushTrack();
        PushTimeline();
    }

    public void Next() { ThrowIfNotReady(); _trackNo++; _title = $"测试曲目{_trackNo}"; _pos = TimeSpan.Zero; PushTrack(); PushTimeline(); }
    public void Previous() { ThrowIfNotReady(); _trackNo = Math.Max(1, _trackNo - 1); _title = $"测试曲目{_trackNo}"; _pos = TimeSpan.Zero; PushTrack(); PushTimeline(); }

    public void SetStatus(string status)
    {
        ThrowIfNotReady();
        var st = status.Trim().ToLowerInvariant() switch
        {
            "playing" => MediaPlaybackStatus.Playing,
            "paused" => MediaPlaybackStatus.Paused,
            "stopped" => MediaPlaybackStatus.Stopped,
            "closed" => MediaPlaybackStatus.Closed,
            "changing" => MediaPlaybackStatus.Changing,
            _ => MediaPlaybackStatus.Playing,
        };
        ApplyStatus(st);
    }

    /// <summary>系统侧自检: 枚举全局 SMTC 会话(确认假元数据注册进了系统)。</summary>
    public string[] ListSessions()
    {
        return Task.Run(async () =>
        {
            var mgr = await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
            var sessions = mgr.GetSessions();
            var result = new List<string>();
            foreach (var s in sessions)
            {
                try
                {
                    var p = await s.TryGetMediaPropertiesAsync();
                    result.Add($"[{s.SourceAppUserModelId}] {p.Title} / {p.Artist} / {p.AlbumTitle}  状态={s.GetPlaybackInfo().PlaybackStatus}");
                }
                catch (Exception ex)
                {
                    result.Add($"[{s.SourceAppUserModelId}] (元数据读取失败: {ex.Message})");
                }
            }
            Log($"[会话] 系统 {sessions.Count} 个媒体会话");
            foreach (var r in result) Log("[会话] " + r);
            return result.ToArray();
        }).GetAwaiter().GetResult();
    }

    // ---------------- 静音流 (激活 A2DP 的兜底) ----------------

    /// <summary>枚举音频渲染端点(车机连上后会出现"耳机/XX Stereo"类端点)。</summary>
    public string[] ListAudioEndpoints()
    {
        return Task.Run(() =>
        {
            var sel = MediaDevice.GetAudioRenderSelector();
            var devs = DeviceInformation.FindAllAsync(sel).GetAwaiter().GetResult();
            var names = devs.Select(d => d.Name).ToArray();
            Log($"[端点] 渲染端点 {names.Length} 个: {string.Join(" | ", names)}");
            return names;
        }).GetAwaiter().GetResult();
    }

    /// <summary>向指定端点渲染空帧(=静音)——让 Windows 认为有活跃音频流, 从而激活 A2DP 并让
    /// SMTC 元数据/播放状态真正推到车机。车机不在线时设备节点创建会失败并抛异常。</summary>
    public string StartSilence(string endpointName)
    {
        return Task.Run(() => StartSilenceCore(endpointName)).GetAwaiter().GetResult();
    }

    public string StopSilence()
    {
        return Task.Run(() => StopSilenceCore()).GetAwaiter().GetResult();
    }

    // ---------------- CallControl 来电 ----------------

    /// <summary>每次动态取 CallControl(配对/连上 HFP 后才有), 发起来电模拟。</summary>
    public string CallIncoming(string number)
    {
        if (string.IsNullOrEmpty(number)) number = "13800138000";
        RefreshCallControl();
        if (_call == null)
        {
            string m = "CallControl.GetDefault()=null —— 无电话设备(车机 HFP 未连/系统无通话 profile)";
            Log("[来电] " + m);
            return m;
        }
        _lastCallId = _call.IndicateNewIncomingCall(true, number);
        string ok = $"来电已送出 {number} (callId={_lastCallId}, ringer=true)";
        Log("[来电] " + ok);
        return ok;
    }

    public string Answer()
    {
        if (_call == null) return "CallControl 不可用";
        _call.IndicateActiveCall(_lastCallId);
        Log($"[通话] IndicateActiveCall({_lastCallId})");
        return $"已标记通话激活 (callId={_lastCallId})";
    }

    public string HangUp()
    {
        if (_call == null) return "CallControl 不可用";
        _call.EndCall(_lastCallId);
        Log($"[通话] EndCall({_lastCallId})");
        return $"已结束通话 (callId={_lastCallId})";
    }

    // ---------------- 配对 (WinRT 自定义配对, 免弹窗) / HFP 连接实验 ----------------

    /// <summary>配对(序号或MAC)。WinRT Custom.PairAsync + PairingRequested 自动 Accept,
    /// 全程不弹 Windows 确认框 —— 修复台架反馈"弹连接码要手动点"。PIN 流程默认 1234。
    /// force=true: 先清 Windows 侧残留配对记录再全新配对 —— 11:16 台架 PairAsync 直接回
    /// AlreadyPaired(Windows 留着链路密钥而车机侧已删, 双侧不同步), 此时必须强制重来。</summary>
    public string Pair(string target, string pin = "1234", bool force = false)
    {
        var addr = Resolve(target);
        return Task.Run(() => PairCore((ulong)addr.ToInt64(), pin, force)).GetAwaiter().GetResult();
    }

    private async Task<string> PairCore(ulong addr, string pin, bool force)
    {
        var dev = await BluetoothDevice.FromBluetoothAddressAsync(addr);
        if (dev == null)
        {
            Log($"[配对] {addr:X12} 设备对象为空(不在范围/未开可连接)");
            return $"配对失败: {addr:X12} 设备对象为空(不在范围)";
        }
        if (force && dev.DeviceInformation.Pairing.IsPaired)
        {
            bool rm = BluetoothSecurity.RemoveDevice(new BluetoothAddress(unchecked((long)addr)));
            Log($"[配对] force: Windows 侧残留配对记录, RemoveDevice({addr:X12}) -> {rm}, 1.5s 后全新配对");
            await Task.Delay(1500);
            dev = await BluetoothDevice.FromBluetoothAddressAsync(addr);
            if (dev == null)
                return "强制重配失败: 清记录后设备对象为空(等设备重新广播后再试)";
        }
        var cp = dev.DeviceInformation.Pairing.Custom;
        string accept = "";
        cp.PairingRequested += (s, e) =>
        {
            try
            {
                if (e.PairingKind == DevicePairingKinds.ProvidePin) { e.Accept(pin); accept = $"已自动提交PIN={pin}"; }
                else { e.Accept(); accept = $"已自动确认(kind={e.PairingKind})"; }
                Log($"[配对] PairingRequested kind={e.PairingKind} -> Accept");
            }
            catch (Exception ex) { accept = "Accept失败:" + ex.Message; Log($"[配对] Accept失败: {ex.Message}"); }
        };
        var kinds = DevicePairingKinds.ConfirmOnly | DevicePairingKinds.ConfirmPinMatch |
                    DevicePairingKinds.DisplayPin | DevicePairingKinds.ProvidePin;
        var r = await cp.PairAsync(kinds);
        string m = $"配对({addr:X12}) -> {r.Status}; {accept}";
        if (r.Status == DevicePairingResultStatus.AlreadyPaired)
            m += " —— Windows 侧仍记着旧配对而对端可能已删; 用\"重新配对\"(force)先清记录重来";
        Log("[配对] " + m);
        return m;
    }

    /// <summary>尝试让 Windows 电话栈接管线机 111E。两级尝试:
    /// ① SetServiceState(Handsfree,enable) —— 即 Windows 设置"连接"按钮底层的
    ///    BluetoothSetServiceState, 会激活 bthhfenum 电话设备;
    /// ② 仍无 CallControl 则退回裸 RFCOMM 占线(HfpCore, 11:06 实测: 裸 socket 连通但
    ///    CallControl=null, 应用级 socket 不激活系统电话栈)。
    /// 车机(CARKIT-1/Android栈) RFCOMM 面: 111E + 车厂私有 + deca-fade(Bluedroid调试口)。</summary>
    public string ConnectHandsfree(string target)
    {
        var addr = Resolve(target);
        return Task.Run(() => ConnectHandsfreeCore(addr)).GetAwaiter().GetResult();
    }

    private async Task<string> ConnectHandsfreeCore(BluetoothAddress addr)
    {
        var sb = new List<string>();
        var di = new BluetoothDeviceInfo(addr);
        try
        {
            di.SetServiceState(BluetoothService.Handsfree, true);
            sb.Add("SetServiceState(111E Handsfree, enable) 已提交");
        }
        catch (Exception ex)
        {
            sb.Add("SetServiceState(Handsfree) 失败: " + ex.Message);
        }
        await Task.Delay(3000); // 等电话设备注册
        sb.Add("CallControl=" + RefreshCallControl());
        if (_call == null)
        {
            try
            {
                sb.Add(await HfpCore((ulong)addr.ToInt64()));
                sb.Add("CallControl=" + RefreshCallControl());
            }
            catch (Exception ex) { sb.Add("RFCOMM 兜底: " + ex.Message); }
        }
        string m = string.Join(" | ", sb);
        Log("[HFP] " + m);
        return m;
    }

    /// <summary>尝试直接连车机 111E(Handsfree) RFCOMM, 代替"去 Windows 设置点连接"。
    /// 连上后刷新 CallControl —— 门C 自动接线实验。socket 保持到 Dispose/下次调用。</summary>
    public string HfpConnect(string target)
    {
        var addr = Resolve(target);
        return Task.Run(() => HfpCore((ulong)addr.ToInt64())).GetAwaiter().GetResult();
    }

    private async Task<string> HfpCore(ulong addr)
    {
        var dev = await BluetoothDevice.FromBluetoothAddressAsync(addr);
        if (dev == null) return $"设备对象为空: {addr:X12} (不在范围)";
        var res = await dev.GetRfcommServicesAsync(BluetoothCacheMode.Uncached);
        var services = res.Services;
        var list = services.Select(s => $"  {s.ServiceId.Uuid}").ToList();
        Log("[HFP] 车机 RFCOMM 服务 " + services.Count + " 个:\n" + string.Join("\n", list));
        var hf = services.FirstOrDefault(s => s.ServiceId.Uuid == HfServiceGuid);
        if (hf == null)
            return $"车机未暴露 111E(Handsfree) 服务(共 {services.Count} 个, 见日志) —— HFP 无法从 Windows 侧发起";

        _hfpSocket?.Dispose();
        var socket = new StreamSocket();
        try
        {
            // 已配对链路, 加密由蓝牙栈按服务要求自动协商
            await socket.ConnectAsync(hf.ConnectionHostName, hf.ConnectionServiceName);
        }
        catch (Exception ex)
        {
            socket.Dispose();
            Log($"[HFP] RFCOMM 连接失败: {ex.Message}");
            return $"RFCOMM 连接失败: {ex.Message} (端口被系统栈占用/车机拒绝?)";
        }
        _hfpSocket = socket; // 保持引用: 释放即断开
        await Task.Delay(1500); // 给系统一点时间注册电话设备
        string call = RefreshCallControl();
        string m = $"HFP RFCOMM 已连上 {addr:X12}; CallControl={call}";
        Log("[HFP] " + m);
        return m;
    }

    // ---------------- 扫描/解除配对 (32feet.NET) ----------------

    /// <summary>异步经典蓝牙扫描(10~20s), 结果经 OnScanDone 事件返回。</summary>
    public void ScanAsync()
    {
        Task.Run(() =>
        {
            try
            {
                Log("[扫描] 开始, 约10~20秒...");
                using var bc = new BluetoothClient();
                _devices = bc.DiscoverDevices().OrderBy(d => d.DeviceName).ToList();
                var lines = new List<string>();
                for (int i = 0; i < _devices.Count; i++)
                {
                    var d = _devices[i];
                    lines.Add($"{i} | {d.DeviceName} | {d.DeviceAddress} | {(d.Authenticated ? "已配对" : "未配对")} | {(d.Connected ? "已连接" : "未连接")} | COD={d.ClassOfDevice}");
                }
                Log($"[扫描] 发现 {_devices.Count} 台");
                OnScanDone?.Invoke(string.Join("\n", lines));
            }
            catch (Exception ex) { Log($"[扫描] 失败: {ex.Message}"); }
        });
    }

    /// <summary>解除配对(序号或MAC)。</summary>
    public string Unpair(string target)
    {
        var addr = Resolve(target);
        bool ok = BluetoothSecurity.RemoveDevice(addr);
        Log($"[配对] RemoveDevice({target}) -> {ok}");
        return ok ? $"已解除配对: {addr}" : $"解除失败: {addr}";
    }

    // ---------------- 射频开关 (Windows.Devices.Radios) ----------------

    /// <summary>列出全部射频(蓝牙/WiFi)及开关状态。</summary>
    public string RadioList()
    {
        return Task.Run(async () =>
        {
            var radios = await Radio.GetRadiosAsync();
            var lines = radios.Select(r => $"{r.Kind}: {r.Name} = {r.State}").ToList();
            foreach (var l in lines) Log("[射频] " + l);
            return string.Join("\n", lines);
        }).GetAwaiter().GetResult();
    }

    /// <summary>开关蓝牙射频。unpackaged 程序常被系统拒绝(DeniedBySystem/Unspecified),
    /// 此时唯一程序化备选是管理员 pnputil /disable-device|/enable-device。</summary>
    public string BtSetPower(bool on)
    {
        return Task.Run(async () =>
        {
            var radios = await Radio.GetRadiosAsync();
            var bt = radios.FirstOrDefault(r => r.Kind == RadioKind.Bluetooth);
            if (bt == null)
            {
                Log("[射频] 未找到蓝牙射频");
                return "未找到蓝牙射频";
            }
            var before = bt.State;
            var st = await bt.SetStateAsync(on ? RadioState.On : RadioState.Off);
            string m = $"蓝牙射频 SetState({(on ? "On" : "Off")}) -> {st} (原状态={before})";
            if (st != RadioAccessStatus.Allowed)
                m += " —— 系统拒绝(unpackaged 无 radioDeviceControl 权限); 备选: 管理员执行 pnputil /enable-device /disable-device";
            Log("[射频] " + m);
            return m;
        }).GetAwaiter().GetResult();
    }

    // ---------------- 内部 ----------------

    private void WindowThread()
    {
        _windowThreadId = Win32.GetCurrentThreadId();
        // 必须是顶层窗口: 消息专用窗口(STATIC+HWND_MESSAGE)会被 GetForWindow 以
        // E_INVALIDARG 拒绝(10:27 实测); 不可见(无 WS_VISIBLE)不影响
        _hwnd = Win32.CreateWindowExW(0, "STATIC", "btwinrt-smtc", 0 /*不可见顶层*/,
            -20000, -20000, 100, 60, IntPtr.Zero, IntPtr.Zero, Win32.GetModuleHandleW(null), IntPtr.Zero);
        _ready.Set();
        while (_hwnd != IntPtr.Zero && Win32.GetMessageW(out var m, IntPtr.Zero, 0, 0) > 0)
        {
            Win32.TranslateMessage(ref m);
            Win32.DispatchMessageW(ref m);
        }
    }

    private void InitSmtc()
    {
        Log($"消息窗口 HWND=0x{_hwnd:X}; InitSmtc 线程套间={Thread.CurrentThread.GetApartmentState()}");
        Guid riid = new("99FA3FF4-1742-42A6-902E-087D41F965EC"); // ISystemMediaTransportControls
        int hr = -1;
        IntPtr ptr = IntPtr.Zero;
        try
        {
            var interop = SystemMediaTransportControls.As<ISmtcInteropWinmd>();
            hr = interop.GetForWindow(_hwnd, ref riid, out ptr);
            if (hr == 0) Log("互操作 GetForWindow 成功 (GUID=winmd DDB0472D)");
            else Log($"winmd GetForWindow hr=0x{hr:X8}");
        }
        catch (Exception ex)
        {
            Log($"winmd 互操作接口失败: {ex.Message}");
        }

        if (hr != 0 || ptr == IntPtr.Zero)
        {
            try
            {
                var legacy = SystemMediaTransportControls.As<ISmtcInteropLegacy>();
                hr = legacy.GetForWindow(_hwnd, ref riid, out ptr);
                if (hr == 0) Log("互操作 GetForWindow 成功 (GUID=SO DD4BE2AC)");
            }
            catch (Exception ex) { Log($"兜底互操作失败: {ex.Message}"); }
        }
        if (hr != 0 || ptr == IntPtr.Zero)
            throw new InvalidOperationException($"GetForWindow 失败 hr=0x{hr:X8}");

        _smtc = SystemMediaTransportControls.FromAbi(ptr);
        Log("SMTC 获取成功");

        _smtc.IsEnabled = true;
        _smtc.IsPlayEnabled = true;
        _smtc.IsPauseEnabled = true;
        _smtc.IsNextEnabled = true;
        _smtc.IsPreviousEnabled = true;
        _smtc.PlaybackStatus = MediaPlaybackStatus.Paused;
        _smtc.ButtonPressed += OnButtonPressed;
    }

    private void PushTrack()
    {
        if (_smtc == null) return;
        _smtc.DisplayUpdater.Type = MediaPlaybackType.Music;
        _smtc.DisplayUpdater.MusicProperties.Title = _title;
        _smtc.DisplayUpdater.MusicProperties.Artist = _artist;
        _smtc.DisplayUpdater.MusicProperties.AlbumTitle = _album;
        _smtc.DisplayUpdater.Update();
        Log($"[元数据] 标题={_title} 歌手={_artist} 专辑={_album} 已推送");
    }

    private void OnButtonPressed(object? sender, SystemMediaTransportControlsButtonPressedEventArgs e)
    {
        Log($"[车机按键] {e.Button}");
        OnButton?.Invoke(e.Button.ToString());
        // 演示联动: 按键改变状态/换曲, 便于车屏观察变化
        if (_smtc == null) return;
        switch (e.Button)
        {
            case SystemMediaTransportControlsButton.Play:
                ApplyStatus(MediaPlaybackStatus.Playing); break;
            case SystemMediaTransportControlsButton.Pause:
                ApplyStatus(MediaPlaybackStatus.Paused); break;
            case SystemMediaTransportControlsButton.Next:
                _trackNo++; _title = $"测试曲目{_trackNo}"; _pos = TimeSpan.Zero; PushTrack(); PushTimeline(); break;
            case SystemMediaTransportControlsButton.Previous:
                _trackNo = Math.Max(1, _trackNo - 1); _title = $"测试曲目{_trackNo}"; _pos = TimeSpan.Zero; PushTrack(); PushTimeline(); break;
        }
    }

    /// <summary>状态统一入口: 置 SMTC + 管理进度心跳; 回到 Playing 时自检静音流。</summary>
    private void ApplyStatus(MediaPlaybackStatus st)
    {
        if (_smtc == null) return;
        _smtc.PlaybackStatus = st;
        Log($"[状态] PlaybackStatus={st}");
        if (st == MediaPlaybackStatus.Playing)
        {
            StartTicker();
            if (_silenceEndpoint.Length > 0)
                Task.Run(() => EnsureSilenceAlive()); // 不阻塞调用方
        }
        else
        {
            StopTicker();
        }
    }

    /// <summary>系统蓝牙认证回调: 数字比较/JustWorks 自动确认, PIN 流程代答 1234。</summary>
    private void OnAuthRequested(object? sender, BluetoothWin32AuthenticationEventArgs e)
    {
        string info = $"[认证] {e.Device?.DeviceAddress} method={e.AuthenticationMethod} attempt={e.AttemptNumber}" +
                      (string.IsNullOrEmpty(e.NumberOrPasskeyAsString) ? "" : $" 数字={e.NumberOrPasskeyAsString}");
        try
        {
            if (e.AuthenticationMethod is BluetoothAuthenticationMethod.Legacy or BluetoothAuthenticationMethod.Passkey)
            {
                e.Pin = _pin;
                Log(info + $" -> 代答PIN={_pin}");
            }
            else
            {
                e.Confirm = true; // NumericComparison / PasskeyNotification / OutOfBand
                Log(info + " -> 自动确认");
            }
        }
        catch (Exception ex)
        {
            Log(info + " 代答异常(可能仍弹窗): " + ex.Message);
        }
    }

    // ---- SMTC 时间轴: 车机的进度条/时长来自 UpdateTimelineProperties, 1s 心跳推进 ----

    private void StartTicker()
    {
        if (_ticker != null) return;
        PushTimeline();
        _ticker = new Timer(_ =>
        {
            _pos += TimeSpan.FromSeconds(1);
            if (_pos > _len) _pos = TimeSpan.Zero; // 循环播放
            PushTimeline();
        }, null, 1000, 1000);
        Log($"[时间轴] 进度启动 时长={_len.TotalSeconds:0}s");
    }

    private void StopTicker()
    {
        if (_ticker == null) return;
        _ticker.Dispose();
        _ticker = null;
        Log($"[时间轴] 进度停在 {_pos.TotalSeconds:0}s / {_len.TotalSeconds:0}s");
    }

    private void PushTimeline()
    {
        if (_smtc == null) return;
        var tp = new SystemMediaTransportControlsTimelineProperties
        {
            StartTime = TimeSpan.Zero,
            MinSeekTime = TimeSpan.Zero,
            EndTime = _len,
            MaxSeekTime = _len,
            Position = _pos,
        };
        _smtc.UpdateTimelineProperties(tp);
    }

    /// <summary>静音流"失速"自检: 车机暂停会挂起 AVDTP, 端点可能失活导致 AudioGraph 停摆
    /// (台架现象"暂停后无法继续播放")。采样 CompletedQuantumCount, 1.2s 不前进则整链重建。</summary>
    private string EnsureSilenceAlive()
    {
        if (_graph == null || _silenceEndpoint.Length == 0) return "静音流未配置";
        try
        {
            ulong q1 = _graph.CompletedQuantumCount;
            Thread.Sleep(1200);
            ulong q2 = _graph.CompletedQuantumCount;
            if (q2 > q1)
            {
                Log($"[静音流] 存活 (quantum {q1}→{q2})");
                return "静音流存活";
            }
            Log($"[静音流] 失速 (quantum {q1}→{q2}), 重建链路...");
        }
        catch (Exception ex)
        {
            Log($"[静音流] 自检异常: {ex.Message}, 重建链路...");
        }
        try
        {
            string ep = _silenceEndpoint;
            StopSilenceCore();
            return StartSilenceCore(ep);
        }
        catch (Exception ex)
        {
            return "静音流重建失败: " + ex.Message + " (端点可能已断开: 车机端重连或刷新端点后重开)";
        }
    }

    private string RefreshCallControl()
    {
        try
        {
            _call = CallControl.GetDefault();
            if (_call == null)
            {
                Log("CallControl.GetDefault() = null (无电话设备/HFP未连)");
                return "null";
            }
            if (!_callEventsWired)
            {
                _call.AnswerRequested += (s) => Log("[事件] AnswerRequested 接听请求(车机/系统侧)");
                _call.HangUpRequested += (s) => Log("[事件] HangUpRequested 挂断请求(车机/系统侧)");
                _call.AudioTransferRequested += (s) => Log("[事件] AudioTransferRequested 音频切换请求");
                _call.DialRequested += (s, e) => Log("[事件] DialRequested 外拨请求");
                _call.RedialRequested += (s, e) => Log("[事件] RedialRequested 重拨请求");
                _call.KeypadPressed += (s, e) => Log("[事件] KeypadPressed 按键");
                _callEventsWired = true;
            }
            Log($"CallControl.GetDefault() OK HasRinger={_call.HasRinger}");
            return "OK";
        }
        catch (Exception ex)
        {
            Log($"CallControl 获取异常: {ex.Message}");
            return "异常";
        }
    }

    private string StartSilenceCore(string endpointName)
    {
        StopSilenceCore();
        var sel = MediaDevice.GetAudioRenderSelector();
        var devs = DeviceInformation.FindAllAsync(sel).GetAwaiter().GetResult();
        var dev = devs.FirstOrDefault(d => d.Name == endpointName);
        if (dev == null)
            throw new InvalidOperationException($"未找到音频端点 \"{endpointName}\" (先 endpoints 查看名称)");

        var settings = new AudioGraphSettings(AudioRenderCategory.Media)
        {
            PrimaryRenderDevice = dev,
            QuantumSizeSelectionMode = QuantumSizeSelectionMode.SystemDefault,
        };
        var gr = AudioGraph.CreateAsync(settings).GetAwaiter().GetResult();
        if (gr.Status != AudioGraphCreationStatus.Success)
            throw new InvalidOperationException($"AudioGraph 创建失败: {gr.Status}");
        _graph = gr.Graph;

        var onr = _graph.CreateDeviceOutputNodeAsync().GetAwaiter().GetResult();
        if (onr.Status != AudioDeviceNodeCreationStatus.Success)
        {
            _graph.Dispose();
            _graph = null;
            throw new InvalidOperationException($"输出节点创建失败: {onr.Status} (端点未连接/车机不在线?)");
        }
        _outNode = onr.DeviceOutputNode;

        // 空帧输入节点: 不提交数据 = 输出静音, 但 WASAPI 管线真实运行
        _frameNode = _graph.CreateFrameInputNode();
        _frameNode.AddOutgoingConnection(_outNode);
        _graph.Start();
        _silenceEndpoint = endpointName;
        string msg = $"静音流已启动 → [{endpointName}] (AudioGraph 运行中)";
        Log(msg);
        return msg;
    }

    private string StopSilenceCore()
    {
        if (_graph == null) return "静音流未在运行";
        string ep = _silenceEndpoint;
        try { _graph.Stop(); } catch { }
        try { _frameNode?.Dispose(); } catch { }
        try { _outNode?.Dispose(); } catch { }
        try { _graph.Dispose(); } catch { }
        _frameNode = null; _outNode = null; _graph = null; _silenceEndpoint = "";
        Log($"静音流已停止 ({ep})");
        return $"静音流已停止 ({ep})";
    }

    private BluetoothAddress Resolve(string target)
    {
        target = target.Trim();
        if (int.TryParse(target, out int idx) && idx >= 0 && idx < _devices.Count)
            return _devices[idx].DeviceAddress;
        if (BluetoothAddress.TryParse(target.Replace(":", "").Replace("-", ""), out var addr))
            return addr;
        throw new InvalidOperationException($"无法解析目标 \"{target}\" (scan 后用序号或 MAC)");
    }

    private void ThrowIfNotReady()
    {
        if (_smtc == null) throw new InvalidOperationException("SMTC 未初始化 (先 Init)");
    }

    private void Log(string msg)
    {
        try { _fileLog.WriteLine($"{DateTime.Now:HH:mm:ss.fff} {msg}"); } catch { }
        OnLog?.Invoke(msg);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try { _ticker?.Dispose(); } catch { }
        try { _auth?.Dispose(); } catch { }
        try { _hfpSocket?.Dispose(); } catch { }
        try { Task.Run(StopSilenceCore).Wait(3000); } catch { }
        try { if (_windowThreadId != 0) Win32.PostThreadMessageW(_windowThreadId, 0x12 /*WM_QUIT*/, IntPtr.Zero, IntPtr.Zero); } catch { }
        try { _windowThread?.Join(2000); } catch { }
        try { _fileLog.WriteLine($"{DateTime.Now:HH:mm:ss.fff} 引擎退出"); _fileLog.Dispose(); } catch { }
    }
}
