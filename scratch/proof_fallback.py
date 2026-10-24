"""实证两个后果：
① 真实错误（连接 11 次耗尽）被伪装成 UnboundLocalError；
② 伪装后的 UnboundLocalError 不是 LLMError/OSError，FallbackProvider 捕不到
   → 跨供应商兜底链**完全失效**（本该换第二家，结果整轮直接失败）。
"""
import sys

sys.path.insert(0, ".")

from core.llm import dump, openai_compat
from core.llm.dump import scope as dump_scope
from core.llm.fallback import FallbackProvider

dump.set_sink(lambda rec: None)          # 等价 create_app 里的 set_sink(...)
openai_compat.sleep_cancellable = lambda *a, **k: None   # 跳过退避，快速跑完


def dead(url, headers, body):
    raise ConnectionError("Remote end closed connection without response")


def alive(url, headers, body):
    raise ConnectionError("第二家也被调用了（说明兜底生效）")


def main():
    primary = openai_compat.OpenAICompatProvider(
        "http://127.0.0.1:1", "k", "m", transport=dead, stream_transport=dead)
    backup = openai_compat.OpenAICompatProvider(
        "http://127.0.0.1:2", "k", "m", transport=alive, stream_transport=alive)

    fb = FallbackProvider(primary, label="A", alternates=lambda: [("B", backup)])
    with dump_scope("proj-x", "chat-b727a5390deb"):
        try:
            fb.chat([{"role": "user", "content": "hi"}], max_tokens=16)
        except BaseException as e:                     # noqa: BLE001
            print("最终抛出:", type(e).__name__, "|", e)
            print("是否走了兜底（应看到 B 的痕迹）:",
                  "B 被调用" in str(e) or "第二家" in str(e))


if __name__ == "__main__":
    main()
