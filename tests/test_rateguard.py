"""rateguard 测试（限速纪律，借鉴 dsh-scanner-tools rateDiscipline）。

纯函数单测：四条规则的正反例 + 分段/路径/连写边界；集成：网关拦截落 audit.deny。
"""

import pytest

from core.runtime import ExecutionGateway, GatewayDenied, rateguard


# ---------- nmap：全端口必须带限速 ----------

def test_nmap_full_port_without_throttle_rejected():
    reason = rateguard.check_rate("nmap -p- -sV 10.0.0.1")
    assert reason and "限速纪律" in reason and "nmap" in reason
    # 分段（&&）也拦
    assert rateguard.check_rate("echo hi && nmap -p 1-65535 10.0.0.1")


def test_nmap_full_port_with_throttle_passes():
    assert rateguard.check_rate("nmap -p- -T3 --max-rate 200 10.0.0.1") is None
    assert rateguard.check_rate("nmap -p- -T2 10.0.0.1") is None
    assert rateguard.check_rate("nmap --ports 1-65535 --max-parallelism 64 10.0.0.1") is None
    # 连写 --max-rate=200
    assert rateguard.check_rate("nmap -p- --max-rate=200 10.0.0.1") is None


def test_nmap_partial_port_no_throttle_needed():
    assert rateguard.check_rate("nmap -p 80,443 10.0.0.1") is None
    assert rateguard.check_rate("nmap --top-ports 1000 10.0.0.1") is None


# ---------- masscan：--rate 阈值 ----------

def test_masscan_rate_threshold():
    assert rateguard.check_rate("masscan 10.0.0.0/24 -p80 --rate 5000")
    assert rateguard.check_rate("masscan 10.0.0.0/24 -p80 --rate=2000")
    assert rateguard.check_rate("masscan 10.0.0.0/24 -p80 --rate 1000") is None
    # 未带 --rate 不拦（masscan 默认 100 p/s 本就保守）
    assert rateguard.check_rate("masscan 10.0.0.0/24 -p80") is None


# ---------- ffuf / hydra：必须显式限速 ----------

def test_ffuf_requires_rate():
    assert rateguard.check_rate("ffuf -u http://x/FUZZ -w words.txt")
    assert rateguard.check_rate("ffuf -u http://x/FUZZ -w words.txt -rate 50") is None
    assert rateguard.check_rate("ffuf -u http://x/FUZZ -w words.txt -rl 30") is None


def test_hydra_requires_t():
    assert rateguard.check_rate("hydra -l admin -P pass.txt ssh://10.0.0.1")
    assert rateguard.check_rate("hydra -l admin -P pass.txt -t 8 ssh://10.0.0.1") is None
    # 连写 -t8
    assert rateguard.check_rate("hydra -l admin -P pass.txt -t8 ssh://10.0.0.1") is None


# ---------- 边界 ----------

def test_non_scan_commands_pass():
    assert rateguard.check_rate("dir") is None
    assert rateguard.check_rate("echo nmap -p-") is None  # 首词不是工具不误伤
    assert rateguard.check_rate("") is None


def test_pipeline_segment_identifies_tool():
    # 管道里 nmap 仍按段识别
    assert rateguard.check_rate("nmap -p- 10.0.0.1 | grep open")


def test_binname_path_and_exe():
    assert rateguard.check_rate("/usr/bin/nmap -p- 10.0.0.1")
    assert rateguard.check_rate("C:\\tools\\nmap.exe -p- 10.0.0.1")
    assert rateguard.check_rate("C:\\tools\\nmap.exe -p- -T3 10.0.0.1") is None


# ---------- 集成：网关拦截落 audit.deny ----------

@pytest.fixture()
def bb(tmp_path):
    from core.blackboard import Blackboard
    board = Blackboard(str(tmp_path / "rate.db"))
    yield board
    board.close()


def test_gateway_denies_unthrottled_scan(bb):
    pid = bb.create_project("rate-1", "pentest")["id"]
    gw = ExecutionGateway(bb=bb)
    with pytest.raises(GatewayDenied, match="限速纪律"):
        gw.run("nmap -p- 10.0.0.1", runtime="host", threat_class="trusted", project_id=pid)
    deny = [e for e in bb.recent_events(pid) if e["kind"] == "audit.deny"]
    assert len(deny) == 1
    assert "限速纪律" in deny[0]["payload"]["reason"]
