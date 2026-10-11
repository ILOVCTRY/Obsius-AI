"""pytest 全局夹具。

- create_app 默认 mission_poll_interval=60s（mission 自动派生兜底轮询线程）。
  测试改为 0：不启动后台线程，避免 60s 后 sweep 碰测试 SQLite/假 LLM；
  需要测轮询判跳的用例直调 `app.state.mission_poll_sweep()`（同步、可控）。
- create_app 默认 session_containers=True（会话级常驻容器 reaper）。测试改为 False：
  不起 reaper 线程、不触 docker（reaper 首轮对账会真跑 `docker ps`），网关回落 per-call。
- create_app 默认 proxy_maintenance=True（代理池自动巡检线程，30min 验证+阈值补抓）。
  测试改为 False：不起巡检线程（会真触发代理验证/抓取网络 Job）。
  需要测巡检判跳的用例直调 `core.api.app._proxy_maintenance_tick(app)`（同步、可控）。
  pytest 先导入 conftest 再收集测试模块，`from core.api.app import create_app`
  拿到的都是这里的包装版。
"""

import core.api.app as _app_mod

_orig_create_app = _app_mod.create_app


def create_app(*args, **kwargs):  # noqa: ANN001,ANN003 —— 透传签名
    kwargs.setdefault("mission_poll_interval", 0)
    kwargs.setdefault("session_containers", False)
    kwargs.setdefault("proxy_maintenance", False)
    return _orig_create_app(*args, **kwargs)


_app_mod.create_app = create_app
