import sys, json, re
sys.path.insert(0, ".")
from core.api import create_app

app = create_app(workspace_root="workspaces", packs_root="packs", tools_root="tools")
spec = app.openapi()
with open("artifacts/openapi.json", "w", encoding="utf-8") as f:
    json.dump(spec, f, ensure_ascii=False, indent=2)

# runtime route table from the ASGI app
rt = set()
ws = set()
for r in app.routes:
    methods = getattr(r, "methods", None)
    path = getattr(r, "path", None)
    if path is None:
        continue
    if methods:
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            rt.add((path, m.lower()))
    else:
        ws.add(path)

# static TSV
static = set()
static_ws = set()
for line in open("artifacts/routes_inventory.tsv", encoding="utf-8").read().splitlines()[1:]:
    ln, method, path, handler = line.split("\t")
    if method == "WEBSOCKET":
        static_ws.add(path)
    else:
        static.add((path, method.lower()))

# openapi operations + security
ops = []
sec_ops = []
for path, item in spec.get("paths", {}).items():
    for method, op in item.items():
        if method.lower() in ("get","post","put","delete","patch"):
            ops.append((path, method.lower()))
            if op.get("security") is not None:
                sec_ops.append((path, method, op.get("security")))

openapi_set = set(ops)
out = []
out.append("docs_url=%s openapi_url=%s redoc_url=%s" % (app.docs_url, app.openapi_url, app.redoc_url))
out.append("components.securitySchemes = %r" % spec.get("components", {}).get("securitySchemes", "<ABSENT>"))
out.append("top-level security = %r" % spec.get("security", "<ABSENT>"))
out.append("operations with explicit security = %d %r" % (len(sec_ops), sec_ops[:10]))
out.append("static HTTP routes = %d ; static WS = %d" % (len(static), len(static_ws)))
out.append("ASGI runtime HTTP ops = %d ; runtime WS = %d -> %s" % (len(rt), len(ws), sorted(ws)))
out.append("OpenAPI operations = %d" % len(openapi_set))
out.append("static - openapi (in code, missing in openapi) = %r" % sorted(static - openapi_set))
out.append("openapi - static = %r" % sorted(openapi_set - static))
out.append("runtime - openapi = %r" % sorted(rt - openapi_set))
out.append("openapi - runtime = %r" % sorted(openapi_set - rt))
out.append("WS static vs runtime: static-runtime=%r runtime-static=%r" % (static_ws - ws, ws - static_ws))
# auto framework endpoints
auto = sorted((getattr(r,"path",None)) for r in app.routes if getattr(r,"path",None) in ("/docs","/openapi.json","/redoc") or "docs" in str(getattr(r,"path","")))
out.append("framework auto endpoints = %r" % auto)
text = "\n".join(out)
open("artifacts/openapi_audit.txt","w",encoding="utf-8").write(text)
print(text)
