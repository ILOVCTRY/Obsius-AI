"""Small resident PyGhidra worker used for cache misses.

Protocol: one JSON object per stdin line, one JSON object per stdout line.
{"op":"disasm","addresses":["0x401000"],"max_lines":400}
{"op":"stop"}
"""
import argparse
import json
import sys


def _iter_java(value):
    if value is None:
        return
    if hasattr(value, "hasNext"):
        while value.hasNext():
            yield value.next()
        return
    try:
        for item in value:
            yield item
    except TypeError:
        return


def _instructions(listing, fn):
    for make in (
        lambda: listing.getInstructions(fn.getBody(), True),
        lambda: listing.getInstructions(fn.getBody().getMinAddress(),
                                        fn.getBody().getMaxAddress(), True),
        lambda: listing.getCodeUnits(fn.getBody().getMinAddress(),
                                     fn.getBody().getMaxAddress(), True),
    ):
        try:
            items = list(_iter_java(make()))
            if items:
                return items
        except Exception:
            pass
    return []


def _find_program_file(project, name):
    data = project.getProjectData()
    exact = data.getFile("/" + name)
    if exact is not None:
        return exact, name
    root = data.getRootFolder()
    for item in _iter_java(root.getFiles()):
        try:
            if item.isVersioned() or item.getContentType() is not None:
                return item, item.getName()
        except Exception:
            return item, item.getName()
    return None, name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-dir", required=True)
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--project-name", default="csproj")
    ap.add_argument("--program-name", required=True)
    ns = ap.parse_args()
    import pyghidra
    pyghidra.start(install_dir=ns.install_dir)
    from ghidra.app.decompiler import DecompInterface
    from ghidra.util.task import ConsoleTaskMonitor
    project = pyghidra.open_project(ns.project_dir, ns.project_name)
    program, program_name = _find_program_file(project, ns.program_name)
    if program is None:
        raise RuntimeError("Ghidra 工程中找不到程序: " + ns.program_name)
    # PyGhidra 12 exposes ProgramDB through consume_program/program_context;
    # the older Project.openProgram convenience method no longer exists.
    current, consumer = pyghidra.consume_program(project, "/" + program_name)
    fm = current.getFunctionManager()
    listing = current.getListing()
    decompiler = DecompInterface()
    decompiler.openProgram(current)
    monitor = ConsoleTaskMonitor()
    function_rows = None
    try:
        for raw in sys.stdin:
            try:
                req = json.loads(raw)
                if req.get("op") == "stop":
                    print(json.dumps({"ok": True}), flush=True)
                    break
                if req.get("op") not in ("disasm", "profile", "functions", "detail"):
                    raise ValueError("unknown op")
                limit = max(1, int(req.get("max_lines") or 400))
                result = {}
                if req.get("op") == "functions":
                    if function_rows is None:
                        function_rows = []
                        for fn in _iter_java(fm.getFunctions(True)):
                            callees = list(_iter_java(fn.getCalledFunctions(monitor)))
                            function_rows.append({
                                "address": hex(fn.getEntryPoint().getOffset()),
                                "name": fn.getName(),
                                "size": int(fn.getBody().getNumAddresses()),
                                "has_pseudo": True, "n_calls": len(callees),
                                "status": "done"})
                    result = function_rows
                    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
                    continue
                for token in req.get("addresses") or []:
                    addr = int(str(token), 16)
                    fn = fm.getFunctionAt(current.getAddressFactory().getDefaultAddressSpace().getAddress(addr))
                    if fn is None:
                        result[hex(addr)] = []
                        continue
                    if req.get("op") in ("profile", "detail"):
                        refs = current.getReferenceManager()
                        callers = {}
                        for ref in _iter_java(refs.getReferencesTo(fn.getEntryPoint())):
                            try:
                                if not ref.getReferenceType().isCall():
                                    continue
                            except Exception:
                                continue
                            caller = fm.getFunctionContaining(ref.getFromAddress())
                            if caller is not None:
                                caddr = int(caller.getEntryPoint().getOffset())
                                callers[caddr] = {"address": hex(caddr),
                                                  "name": caller.getName()}
                        callees = []
                        for callee in _iter_java(fn.getCalledFunctions(monitor)):
                            callees.append({"address": hex(callee.getEntryPoint().getOffset()),
                                            "name": callee.getName()})
                        row = {"address": hex(addr), "name": fn.getName(),
                               "size": int(fn.getBody().getNumAddresses()),
                               "callers": list(callers.values()), "callees": callees}
                        if req.get("op") == "profile":
                            result[hex(addr)] = row
                            continue
                        dr = decompiler.decompileFunction(fn, 60, monitor)
                        row["pseudocode"] = (dr.getDecompiledFunction().getC()
                                              if dr.decompileCompleted() and dr.getDecompiledFunction()
                                              else None)
                        lines = []
                        for ins in _instructions(listing, fn)[:limit]:
                            lines.append(f"{ins.getAddress()}  {ins}")
                        row["disasm_lines"] = lines
                        result[hex(addr)] = row
                        continue
                    lines = []
                    for ins in _instructions(listing, fn)[:limit]:
                        lines.append(f"{ins.getAddress()}  {ins}")
                    result[hex(addr)] = lines
                print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({"ok": False, "error": str(exc)[:400]}), flush=True)
    finally:
        decompiler.dispose()
        try:
            current.release(consumer)
        finally:
            project.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
