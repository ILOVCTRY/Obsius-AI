# Ghidra postScript（Jython）：按地址列表反汇编指定函数 → {hex_addr: ["地址  指令", ...]}
# 用法：
#   首次（导入+分析+落工程）：analyzeHeadless <projDir> <projName> \
#       -import <bin> -scriptPath tools/decompiler/ghidra/scripts \
#       -postScript disasm_funcs.py <out.json> <addr,addr,...>
#   之后（复用既有工程，免重分析）：analyzeHeadless <projDir> <projName> \
#       -process -noanalysis -scriptPath tools/decompiler/ghidra/scripts \
#       -postScript disasm_funcs.py <out.json> <addr,addr,...>
# 供工作台 Ghidra 模式「大样本按需反汇编」用：只反汇编请求的函数，写 out.json。
# 第三个参数（可选）= 单函数行数上限，缺省 400。
import io
import json

OUT = args[0]
ADDRS = args[1] if len(args) > 1 else ""
try:
    MAX_LINES = int(args[2]) if len(args) > 2 else 400
except (TypeError, ValueError):
    MAX_LINES = 400

prog = currentProgram
fm = prog.getFunctionManager()
listing = prog.getListing()

want = set()
for tok in str(ADDRS).split(","):
    tok = tok.strip()
    if not tok:
        continue
    try:
        want.add(int(tok, 16))
    except ValueError:
        pass

result = {}
for f in fm.getFunctions(True):
    try:
        entry = f.getEntryPoint().getOffset()
    except Exception:
        continue
    if entry not in want:
        continue
    lines = []
    try:
        it = listing.getInstructions(f.getBody(), True)
        n = 0
        while it.hasNext() and n < MAX_LINES:
            ins = it.next()
            lines.append("%s  %s" % (ins.getAddress().toString(), ins.toString()))
            n += 1
    except Exception:
        pass
    result[hex(entry)] = lines
    if len(result) == len(want):
        break

# 必须显式 UTF-8（Jython 用 io.open）：Windows host 裸 open 默认 GBK
with io.open(OUT, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(result, ensure_ascii=False))
print("disasm %d functions -> %s" % (len(result), OUT))
