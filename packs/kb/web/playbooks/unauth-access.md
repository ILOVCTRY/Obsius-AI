## 已验证路径: Black-box fingerprinting of Sangfor SSL VPN gateways

Verified on vpn.zut.edu.cn (202.196.32.142) — evidence anchor find-95239ec8762c. From URL-only black-box, identify a Sangfor SSL VPN M6.8 (2017-era) via:

- HTTP `Server` header literal value `Server` (abnormal literal, not a real server name)
- `USE_NEW_PORTAL: 1` marker
- Login endpoint `/por/login_psw.csp`
- `tmlversion=6.8` version marker
- `TWFID` cookie name
- Static resource path `/com/`
- EasyConnect client installer: ~14.2MB, Last-Modified 2017-11-06
- Portal `/por/index.csp` returns 200
- TLS 1.2 only; TLS 1.3 handshake fails with alert 40 — do not misread as no-TLS/port-closed

Keep strictly separate from wEngine WebVPN (wpn/nwpn) fingerprints — same site can host both on different IPs (e.g. 202.196.32.142 vs 202.196.32.153).

## 坑: Default self-signed cert on Sangfor VPN is expected, not a finding

Verified on the same target — evidence anchor find-c5a5fcabbe48. Sangfor SSL VPN devices ship with a device-default self-signed certificate. A self-signed cert alone is NOT a certificate-risk finding; record it as info / tested_clean (here the cert-only record was a false-positive dead end, find-6c3e5b58a6be). Only escalate if hostname mismatch actually breaks the tested flow or key material is reused across unrelated hosts.