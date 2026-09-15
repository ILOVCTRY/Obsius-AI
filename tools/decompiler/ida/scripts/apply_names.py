# IDAPython headless 写回脚本（idat -A -S"apply_names.py <in.json> <out.json>" <db.i64|idb>）
# 对**已存在的 IDA 数据库**跑（不是原始样本）：func_kb 的人工命名/注释回写进 .i64。
# 输入契约：
#   {"items": [{"address": 4200000(整数), "name": "check_flag"(可选),
#               "comment": "函数注释"(可选)}]}
# 输出契约：
#   {"results": [{"address": "0x..", "ok": true} |
#                {"address": "0x..", "ok": false, "error": "..."}]}
# 单条失败只记 error，绝不中断后续条目；收尾显式 save_database 落盘。
import json

import ida_auto
import ida_funcs
import ida_name
import idc

IN = idc.ARGV[1] if len(idc.ARGV) > 1 else "apply_in.json"
OUT = idc.ARGV[2] if len(idc.ARGV) > 2 else "apply_out.json"
ida_auto.auto_wait()

with open(IN, "r", encoding="utf-8") as fh:
    payload = json.load(fh)

# IDA 9.x：SN_* 命名标志已从 idc 迁到 ida_name（idc.SN_FORCE 不存在，AttributeError）
flags = ida_name.SN_NOWARN | ida_name.SN_FORCE
results = []
for item in payload.get("items", []):
    ea = int(item["address"])
    errors = []
    try:
        name = item.get("name")
        if name:
            # SN_FORCE 替换同名旧项；返回 False 表示命名被 IDA 拒绝（非法字符等）
            if not idc.set_name(ea, name, flags):
                errors.append("set_name rejected")
        comment = item.get("comment")
        if comment:
            # 函数重复注释（可重复注释）；ea 必须落在某函数内
            if ida_funcs.get_func(ea) is None:
                errors.append("address not in a function")
            elif not idc.set_func_cmt(ea, comment, 1):
                errors.append("set_func_cmt failed")
    except Exception as e:  # noqa: BLE001 —— 单条异常落结果，不中断整批
        errors.append("%s: %s" % (type(e).__name__, e))
    results.append({"address": hex(ea), "ok": not errors,
                    **({"error": "; ".join(errors)} if errors else {})})

# headless -A 退出时通常自动保存，这里显式落盘兜底（0=不压缩）
idc.save_database(idc.get_idb_path(), 0)
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump({"results": results}, fh, ensure_ascii=False)
idc.qexit(0)
