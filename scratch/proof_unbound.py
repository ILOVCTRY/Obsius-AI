"""实证：11 次连接重试耗尽后，真实 LLMError 被 capture_request 吞掉
→ openai_compat 走到 `if status != 200` 时 status 未绑定 → UnboundLocalError。

与生产现场一致：事件流里 chat.retry attempt=1..10，status=connection，
随后落 chat.error category=unknown / "cannot access local variable 'status'"。
"""
import sys

sys.path.insert(0, ".")

from core.llm import dump, openai_compat
from core.llm.dump import scope as dump_scope

# 服务进程里 create_app 无条件 set_sink(requests.store)，故 _record_enabled 在有
# scope 时为 True —— 现场正是这个组合。
dump.set_sink(lambda rec: None)
# 把 11 次退避压成瞬间（2+4+8+16+30…都跳过），只为快速跑完真实重试链
openai_compat.sleep_cancellable = lambda *a, **k: None


def boom(url, headers, body):
    raise ConnectionError("Remote end closed connection without response")


def main():
    with dump_scope("proj-x", "chat-b727a5390deb"):
        p = openai_compat.OpenAICompatProvider(
            "http://127.0.0.1:1", "k", "m",
            transport=boom, stream_transport=boom)
        try:
            p.chat([{"role": "user", "content": "hi"}], max_tokens=16)
        except BaseException as e:                      # noqa: BLE001
            print("=== 上层实际收到的异常 ===")
            print("类型:", type(e).__name__)
            print("文本:", e)


if __name__ == "__main__":
    main()
