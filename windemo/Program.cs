// windemo —— 调用 btwinrt 引擎的控制台版 Demo (首轮 CLI 保持不变, 新增 endpoints/silence)
// 引擎细节(SMTC 互操作/静音流/动态 CallControl/scan/pair)见 ../btwinrt/Engine.cs
// 显式 Main+[STAThread]: 复刻首轮成功形态(SMTC 互操作对线程套间敏感, 见 btwinrt.log)
using System;
using System.Threading;
using BtWinRT;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.InputEncoding = System.Text.Encoding.UTF8;

        using var eng = new Engine();
        eng.OnButton += (b) => Console.WriteLine($"<< 车机按键: {b} >>");
        eng.OnScanDone += (list) =>
        {
            Console.WriteLine(list);
            Console.WriteLine("(配对建议用 MAC —— 序号在两次扫描间可能漂移)");
        };
        try
        {
            Console.WriteLine(eng.Init());
        }
        catch (Exception ex)
        {
            Console.WriteLine($"!! 引擎初始化失败: {ex.Message}");
            return;
        }
        Console.WriteLine("=== windemo 就绪 ===");
        Help();
        RunLoop(eng);
    }

    private static void RunLoop(Engine eng)
    {
    while (true)
    {
        Console.Write("> ");
        string? line = Console.ReadLine();
        if (line is null) return;
        if (string.IsNullOrWhiteSpace(line)) continue;
        string[] p = line.Trim().Split(' ', 4, StringSplitOptions.RemoveEmptyEntries);
        string cmd = p[0].ToLowerInvariant();
        try
        {
            switch (cmd)
            {
                case "quit" or "exit":
                    return;
                case "help":
                    Help();
                    break;
                case "scan":
                    eng.ScanAsync();
                    Console.WriteLine("扫描中, 约 10~20 秒...");
                    break;
                case "pair":
                    if (p.Length < 2) { Console.WriteLine("用法: pair <序号|MAC> [PIN]  (自动确认, 不弹窗; 默认PIN=1234)"); break; }
                    Console.WriteLine(eng.Pair(p[1], p.Length > 2 ? p[2] : "1234"));
                    break;
                case "repair":
                    // 11:16 台架: PairAsync 直接回 AlreadyPaired = Windows 残留链路密钥而车机侧
                    // 已删(双侧不同步)。force: 先 RemoveDevice 清记录再走全新配对。
                    if (p.Length < 2) { Console.WriteLine("用法: repair <序号|MAC> [PIN]  (先清 Windows 残留配对记录再全新配对)"); break; }
                    Console.WriteLine(eng.Pair(p[1], p.Length > 2 ? p[2] : "1234", force: true));
                    break;
                case "hfp":
                    if (p.Length < 2) { Console.WriteLine("用法: hfp <序号|MAC>  (激活通话音频: 先profile级, 再裸RFCOMM兜底)"); break; }
                    Console.WriteLine(eng.ConnectHandsfree(p[1]));
                    break;
                case "unpair":
                    if (p.Length < 2) { Console.WriteLine("用法: unpair <序号|MAC>"); break; }
                    Console.WriteLine(eng.Unpair(p[1]));
                    break;
                case "sessions":
                    foreach (var s in eng.ListSessions()) Console.WriteLine("  " + s);
                    break;
                case "track" or "set-track":
                    eng.SetTrack(
                        p.Length > 1 ? p[1] : "测试曲目1",
                        p.Length > 2 ? p[2] : "测试歌手",
                        "测试专辑",
                        p.Length > 3 && int.TryParse(p[3], out int len) ? len : 240);
                    Console.WriteLine("已推送(含时间轴)");
                    break;
                case "next":
                    eng.Next(); Console.WriteLine("已切下一曲并推送"); break;
                case "prev":
                    eng.Previous(); Console.WriteLine("已切上一曲并推送"); break;
                case "play":
                    eng.SetStatus("playing"); Console.WriteLine("状态=Playing"); break;
                case "pause":
                    eng.SetStatus("paused"); Console.WriteLine("状态=Paused"); break;
                case "status":
                    eng.SetStatus(p.Length > 1 ? p[1] : "playing");
                    break;
                case "endpoints":
                    foreach (var e in eng.ListAudioEndpoints()) Console.WriteLine("  " + e);
                    break;
                case "silence-on":
                    if (p.Length < 2)
                    {
                        Console.WriteLine("用法: silence-on <端点名>  (endpoints 查看; 车机端点一般叫 耳机(XX Stereo))");
                        foreach (var e in eng.ListAudioEndpoints()) Console.WriteLine("  " + e);
                        break;
                    }
                    Console.WriteLine(eng.StartSilence(p[1]));
                    eng.SetStatus("playing");
                    Console.WriteLine("(已联动: 状态=Playing)");
                    break;
                case "silence-off":
                    Console.WriteLine(eng.StopSilence());
                    break;
                case "radio":
                    Console.WriteLine(eng.RadioList());
                    break;
                case "bton":
                    Console.WriteLine(eng.BtSetPower(true));
                    break;
                case "btoff":
                    Console.WriteLine(eng.BtSetPower(false));
                    break;
                case "call":
                    Console.WriteLine(eng.CallIncoming(p.Length > 1 ? p[1] : "13800138000"));
                    break;
                case "answer":
                    Console.WriteLine(eng.Answer());
                    break;
                case "hangup":
                    Console.WriteLine(eng.HangUp());
                    break;
                default:
                    Console.WriteLine($"未知命令: {cmd} (help 查看)");
                    break;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"!! {ex.Message}");
        }
    }
}

    static void Help()
    {
        Console.WriteLine(@"命令:
  scan                       扫描经典蓝牙设备(10~20秒, 结果稍后列出)
  pair <序号|MAC> [PIN]      配对 —— 自动确认不弹窗(PairingRequested->Accept)
  repair <序号|MAC> [PIN]    重新配对 —— 先清 Windows 残留配对记录(11:16 台架
                             PairAsync 回 AlreadyPaired = 双侧不同步, 必须强制重来)
  hfp <序号|MAC>             直连车机 111E(通话音频)RFCOMM, 连后测 CallControl
  unpair <序号|MAC>          解除配对
  track <标题> [歌手] [时长s] 推送元数据+时间轴(进度条数据)
  next / prev                切曲并推送(进度归零)
  play / pause / status <s>  播放状态(playing 时进度每秒推进+静音流自检)
  sessions                   系统侧全部媒体会话(自检)
  endpoints                  音频渲染端点(车机=耳机/XX Stereo)
  silence-on <端点>          向车机端点播静音流(激活A2DP)+联动Playing
  silence-off                停静音流
  call <号码> / answer / hangup   模拟来电/标记激活/挂断
  radio / bton / btoff       射频列表/蓝牙开/蓝牙关
  quit                       退出
全部动作与事件写入 btwinrt.log (DLL 旁)");
    }
}
