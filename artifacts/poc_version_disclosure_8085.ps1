# poc_version_disclosure_8085.ps1  (kind=poc, read-only, single-request)
# Target finding: find-42f1be21bafc (version-disclosure)
# Evidence anchor (measured 2026-09-12, GET /):
#   Server: nginx/1.15.11
#   X-Powered-By: PHP/7.3.4
$base = 'http://127.0.0.1:8085'
curl.exe -sS -i -m 5 $base | Select-String -Pattern '^(HTTP|Server|X-Powered-By):'
