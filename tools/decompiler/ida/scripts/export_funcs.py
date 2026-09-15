# IDAPython 导出脚本 v3（idat -A -S"export_funcs.py <out.json>" <binary>）
# 输出契约（DESIGN.md §9）：
#   {export_version:3, binary, meta, sections[], imports{dll:[fn]}, functions[], strings[]}
# meta/sections/imports/strings 为增强段，各自 try/except——失败给 {"error": ...}，
# 绝不阻断 functions 主路径。
import json
import math
from collections import Counter

import ida_auto
import ida_bytes
import ida_entry
import ida_funcs
import ida_hexrays
import ida_ida
import ida_nalt
import ida_name
import ida_segment
import idautils
import idc

OUT = idc.ARGV[1] if len(idc.ARGV) > 1 else "export.json"
ida_auto.auto_wait()


def shannon_entropy(data):
    if not data:
        return None
    counts = Counter(data)
    n = len(data)
    return round(-sum((c / n) * math.log(c / n, 2) for c in counts.values()), 3)


def collect_meta():
    bits = 64 if ida_ida.inf_is_64bit() else (32 if ida_ida.inf_is_32bit() else None)
    proc = ida_ida.inf_get_procname()
    if proc == "metapc":
        arch = "x86-64" if bits == 64 else "x86"
    elif proc in ("ARM", "ARMv7"):
        arch = "arm64" if bits == 64 else "arm"
    else:
        arch = proc
    entry = None
    # ELF 入口列表含动态导出，最低地址的那个不一定是 e_entry（实测会取到 GOT 区）。
    # 优先 INF_START_IP，其次 _start 符号，最后才退回第一个入口。
    try:
        start_ip = idc.get_inf_attr(idc.INF_START_IP)
        if start_ip is not None and start_ip != idc.BADADDR:
            entry = hex(start_ip)
    except Exception:  # noqa: BLE001 —— 旧版 IDA 无该属性时降级
        pass
    if entry is None:
        entries = {}
        for i in range(ida_entry.get_entry_qty()):
            ordinal = ida_entry.get_entry_ordinal(i)
            ea = ida_entry.get_entry(ordinal)
            name = ida_entry.get_entry_name(ordinal) or ida_name.get_name(ea)
            entries[name] = ea
        for cand in ("_start", "start", "_start_proc"):
            if cand in entries:
                entry = hex(entries[cand])
                break
        if entry is None and entries:
            entry = hex(next(iter(entries.values())))
    return {
        "arch": arch,
        "bits": bits,
        "endian": "be" if ida_ida.inf_is_be() else "le",
        "imagebase": hex(ida_nalt.get_imagebase()),
        "entry": entry,
        "filename": ida_nalt.get_root_filename(),
    }


def collect_sections():
    out = []
    seg = ida_segment.get_first_seg()
    while seg:
        size = seg.end_ea - seg.start_ea
        perms = (
            ("r" if seg.perm & ida_segment.SEGPERM_READ else "-")
            + ("w" if seg.perm & ida_segment.SEGPERM_WRITE else "-")
            + ("x" if seg.perm & ida_segment.SEGPERM_EXEC else "-")
        )
        entropy = None
        try:
            raw = ida_bytes.get_bytes(seg.start_ea, min(size, 1 << 20))
            entropy = shannon_entropy(raw)
        except Exception:  # noqa: BLE001 —— 未初始化段可能读失败
            entropy = None
        out.append({
            "name": ida_segment.get_segm_name(seg),
            "vaddr": hex(seg.start_ea),
            "size": size,
            "perms": perms,
            "entropy": entropy,
        })
        seg = ida_segment.get_next_seg(seg.start_ea)
    return out


def collect_imports():
    imports = {}
    qty = ida_nalt.get_import_module_qty()
    for i in range(qty):
        # 按索引取模块名（enum_import_module_qt 不接收索引，无法在循环里对应 i）
        mod_name = ida_nalt.get_import_module_name(i) or ("mod%d" % i)
        names = []

        def name_cb(_ea, name, ordinal):
            # ELF 动态符号带版本后缀（puts@@GLIBC_2.2.5），去掉以便与 calls 对齐
            nm = (name or "#%d" % ordinal).split("@@", 1)[0]
            names.append(nm)
            return True  # True=继续枚举

        ida_nalt.enum_import_names(i, name_cb)
        imports[mod_name] = names
    return imports


def collect_strings():
    # v3：字符串表（地址/内容/长度/类型/引用）。DataRefsTo 归属引用所在函数。
    out = []
    sc = idautils.Strings()
    sc.setup()
    for s in sc:
        refs = {}
        for ref in idautils.DataRefsTo(s.ea):
            owner = ida_funcs.get_func(ref)
            if owner:
                # 每函数只留一个 from_addr（同函数多处引用不重复占行）
                refs.setdefault(owner.start_ea, ref)
        # strtype 0 = C 字符串，其余（UTF-16/32…）统称 unicode
        stype = "cstr" if not getattr(s, "strtype", 0) else "unicode"
        out.append({
            "address": s.ea,
            "string": str(s),
            "length": s.length,
            "type": stype,
            "refs": [{"func_addr": fea, "from_addr": fr}
                     for fea, fr in sorted(refs.items())],
        })
    return out


functions = []
hexrays_ok = ida_hexrays.init_hexrays_plugin()
for ea in idautils.Functions():
    f = ida_funcs.get_func(ea)
    # calls：遍历函数体内所有指令的 CodeRefs（v1 只看函数首地址，漏函数中段 call）
    calls = set()
    for item in idautils.FuncItems(ea):
        for ref in idautils.CodeRefsFrom(item, 0):
            target = ida_funcs.get_func(ref)
            if target and target.start_ea != ea:
                nm = ida_name.get_name(target.start_ea)
                if nm:
                    calls.add(nm)
    entry = {
        "address": ea,
        "name": ida_name.get_name(ea),
        "size": f.size() if f else 0,
        "calls": sorted(calls),
    }
    if hexrays_ok:
        try:
            cf = ida_hexrays.decompile(ea)
            entry["pseudocode"] = str(cf)
        except Exception:  # noqa: BLE001 —— 无伪码的函数保留符号条目
            pass
    functions.append(entry)

result = {
    "export_version": 3,
    "binary": ida_nalt.get_root_filename(),
    "functions": functions,
}
for key, fn in (("meta", collect_meta), ("sections", collect_sections),
                ("imports", collect_imports), ("strings", collect_strings)):
    try:
        result[key] = fn()
    except Exception as e:  # noqa: BLE001 —— 增强段失败不拖垮整次导出
        result[key] = {"error": "%s: %s" % (type(e).__name__, e)}

# 必须显式 UTF-8：Windows 上裸 open 默认 GBK，人工中文命名/注释会写成非 UTF-8 字节，
# 后端读缓存即 UnicodeDecodeError（v3 契约缓存统一 UTF-8）。
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(result, fh, ensure_ascii=False)
idc.qexit(0)
