"""idat 无窗口 MCP bootstrap（cyberstrike-pro 自有代码，不改 vendor 任何文件）。

用法（由 core/tools/ida_mcp_manager.py 拉起）：
  idat -A -S<本文件路径> -o<db路径> <样本>   （端口经环境变量 CYBERSTRIKE_IDA_MCP_PORT）
手工验证也可：  idat -A -S"<本文件路径> <port>" -o<db路径> <样本>（ARGV 传端口）

职责（对应 DESIGN.md §7 逆向工作流「按需拉起 IDA-MCP」）：
1. 等 IDA 自动分析完成（auto_wait）；
2. 复用 vendor ida-pro-mcp 的 serve（background=True，daemon 线程），端口由
   管理器分配（13338+，避开人手 GUI 实例的 13337）；
3. 主线程保活并代跑同步队列——idat -A 的 -S 脚本一返回进程即退出（daemon
   线程随死）。

同步队列（2026-09-20 实测定稿）：vendor 的工具调用经 idaapi.execute_sync(MFF_WRITE)
投递到 IDA 主线程队列，但 IDA 9.3 的 Python 绑定不导出任何主线程泵函数
（process_ui_events / qsleep 均不存在，已 grep _ida_kernwin.pyd 确认），阻塞在
-S 脚本里的主线程永远不消化该队列 → 编排类调用死锁。解法：monkeypatch
idaapi.execute_sync，把调用放入自建 Python 队列，主线程循环取出执行——
IDA SDK 调用仍全部落在主线程串行跑（与 execute_sync 同等保证，比 inline 跑在
HTTP 线程安全）。

已知取舍：vendor sync_wrapper 的超时用 sys.settrace 实现，只在执行线程生效；
经本桥后工具在主线程跑，trace 不生效 → 单个卡死的调用会一直占着主线程
（IDA 的 API 调用本身极少卡死；管理器侧有 ready_timeout/空闲回收兜底）。
"""

import os
import queue
import sys
import time

import ida_auto
import ida_pro
import idc


def _install_execute_sync_bridge(sync_queue: "queue.Queue") -> None:
    """把 idaapi.execute_sync 改道自建队列（必须先于 vendor import）。"""
    import idaapi

    def execute_sync_bridge(fn, reqf):
        if ida_pro.is_main_thread():
            return fn()  # 主线程直调（bootstrap 自身初始化路径）
        box: queue.Queue = queue.Queue()
        sync_queue.put((fn, box))
        return box.get()[1]

    idaapi.execute_sync = execute_sync_bridge


def main() -> None:
    # 端口优先取环境变量：manager 侧 -S 必须是无空格单参数（Windows list2cmdline
    # 会对 -S"script port" 的内嵌引号做 \" 转义，IDA 自家解析器不认——实测
    # "could not locate file"），带参形态只留给手工验证。
    argv = idc.ARGV
    env_port = os.environ.get("CYBERSTRIKE_IDA_MCP_PORT")
    port = int(env_port) if env_port else (int(argv[1]) if len(argv) > 1 else 13338)
    host = argv[2] if len(argv) > 2 else "127.0.0.1"

    ida_auto.auto_wait()

    sync_queue: queue.Queue = queue.Queue()
    _install_execute_sync_bridge(sync_queue)

    vendor_root = os.path.dirname(os.path.abspath(argv[0]))
    if vendor_root not in sys.path:
        sys.path.insert(0, vendor_root)

    from ida_mcp import MCP_SERVER, IdaMcpHttpRequestHandler, init_caches

    try:
        init_caches()
    except Exception as e:  # noqa: BLE001 —— 与 vendor run() 同口径：缓存失败不阻断 serve
        print(f"[bootstrap] init_caches failed: {e}")

    MCP_SERVER.serve(host, port, request_handler=IdaMcpHttpRequestHandler)
    print(f"[bootstrap] MCP ready: http://{host}:{port}/mcp")

    while True:
        try:
            fn, box = sync_queue.get(timeout=0.05)
        except queue.Empty:
            continue
        try:
            box.put((True, fn()))
        except BaseException as e:  # noqa: BLE001 —— 异常也要回传，否则 HTTP 线程永久阻塞
            box.put((False, e))


main()
