# poc_pkxss_entry_gate_302.ps1  (kind=poc, read-only, single-request)
# Target finding: find-029a598c22c0 (unauthorized-access-candidate, pkxss admin entry)
# Verified 2026-09-12:
#   GET /pkxss/index.php          -> HTTP/1.1 302 Found, location: pkxss_login.php   (access control PRESENT)
#   GET /pkxss/pkxss_login.php    -> 200, HTML leaks credentials: <p>admin/123456</p>
#   POST /pkxss/pkxss_login.php admin/123456 -> 200 body: Table 'pkxss.users' doesn't exist
#     (backend DB not initialized -> login path unusable, unauthorized reachability NOT confirmed)
# Conclusion: gate enforced; finding kept status=unverified with counter-evidence.
$base = 'http://127.0.0.1:8085'
Write-Host '--- GET /pkxss/index.php (expect 302 -> pkxss_login.php) ---'
curl.exe -sS -i -m 5 "$base/pkxss/index.php" | Select-Object -First 9
Write-Host '--- GET /pkxss/pkxss_login.php: credential disclosure anchor ---'
curl.exe -sS -m 5 "$base/pkxss/pkxss_login.php" | Select-String -Pattern 'admin/'
