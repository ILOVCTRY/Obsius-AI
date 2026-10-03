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

args = list(getScriptArgs())
OUT = args[0]
ADDRS = args[1] if len(args) > 1 else ""
try:
    MAX_LINES = int(args[2]) if len(args) > 2 else 400
except (TypeError, ValueError):
    MAX_LINES = 400

prog = currentProgram
fm = prog.getFunctionManager()
listing = prog.getListing()

def _instructions(f):
    for make in (
        lambda: listing.getInstructions(f.getBody(), True),
        lambda: listing.getInstructions(f.getBody().getMinAddress(),
                                        f.getBody().getMaxAddress(), True),
        lambda: listing.getCodeUnits(f.getBody().getMinAddress(),
                                     f.getBody().getMaxAddress(), True),
    ):
        try:
            it = make()
            found = []
            while it.hasNext():
                found.append(it.next())
            if found:
                return found
        except Exception:
            pass
    try:
        entry = f.getEntryPoint()
        cur = listing.getInstructionAt(entry)
        if cur is None:
            cur = listing.getInstructionContaining(f.getEntryPoint())
        if cur is None:
            space = prog.getAddressFactory().getDefaultAddressSpace()
            cur = listing.getInstructionAt(space.getAddress(entry.getOffset()))
        end = f.getBody().getMaxAddress()
        found = []
        seen = set()
        while cur is not None and cur.getAddress().compareTo(end) <= 0:
            key = str(cur.getAddress())
            if key in seen:
                break
            seen.add(key)
            found.append(cur)
            cur = listing.getInstructionAfter(cur)
        return found
    except Exception:
        pass
    return []

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
        for n, ins in enumerate(_instructions(f)):
            if n >= MAX_LINES:
                break
            try:
                addr = str(ins.getAddress())
            except Exception:
                addr = "?"
            try:
                text = str(ins)
            except Exception:
                try:
                    text = "%s %s" % (ins.getMnemonicString(),
                                       ins.getDefaultOperandRepresentation(0))
                except Exception:
                    text = "<instruction>"
            lines.append("%s  %s" % (addr, text))
    except Exception:
        pass
    result[hex(entry)] = lines
    if len(result) == len(want):
        break

# 必须显式 UTF-8（Jython 用 io.open）：Windows host 裸 open 默认 GBK
with io.open(OUT, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(result, ensure_ascii=False))
print("disasm %d functions -> %s" % (len(result), OUT))
