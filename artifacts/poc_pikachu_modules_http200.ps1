# poc_pikachu_modules_http200.ps1  (kind=poc, read-only, batch status probe, bodies discarded)
# Target finding: find-b23b4d903ac0 (exposed-vulnerable-modules)
# Expected output: 15 lines, all "200<TAB>/vul/..."; measured 2026-09-12: 15/15 = 200
$base = 'http://127.0.0.1:8085'
$urls = '/vul/burteforce/burteforce.php','/vul/csrf/csrf.php','/vul/dir/dir.php','/vul/fileinclude/fileinclude.php','/vul/infoleak/infoleak.php','/vul/overpermission/op.php','/vul/rce/rce.php','/vul/sqli/sqli.php','/vul/ssrf/ssrf.php','/vul/unsafedownload/unsafedownload.php','/vul/unsafeupload/upload.php','/vul/unserilization/unserilization.php','/vul/urlredirect/urlredirect.php','/vul/xss/xss.php','/vul/xxe/xxe.php'
foreach ($u in $urls) {
  $c = curl.exe -sS -o NUL -w "%{http_code}" -m 5 ($base + $u)
  "$c`t$u"
}
