# CTF Reverse - Competition-Specific Patterns (Part 1)

## Table of Contents
- [Hidden Emulator Opcodes + LD_PRELOAD Key Extraction (0xFun 2026)](#hidden-emulator-opcodes--ld_preload-key-extraction-0xfun-2026)
- [Spectre-RSB SPN Cipher — Static Parameter Extraction (0xFun 2026)](#spectre-rsb-spn-cipher--static-parameter-extraction-0xfun-2026)
- [Image XOR Mask Recovery via Smoothness (VuwCTF 2025)](#image-xor-mask-recovery-via-smoothness-vuwctf-2025)
- [Shellcode in Data Section via mmap RWX (VuwCTF 2025)](#shellcode-in-data-section-via-mmap-rwx-vuwctf-2025)
- [Recursive execve Subtraction (VuwCTF 2025)](#recursive-execve-subtraction-vuwctf-2025)
- [Byte-at-a-Time Block Cipher Attack (UTCTF 2024)](#byte-at-a-time-block-cipher-attack-utctf-2024)
- [Mathematical Convergence Bitmap (EHAX 2026)](#mathematical-convergence-bitmap-ehax-2026)
- [Windows PE XOR Bitmap Extraction + OCR (srdnlenCTF 2026)](#windows-pe-xor-bitmap-extraction--ocr-srdnlenctf-2026)
- [Two-Stage Loader: RC4 Gate + VM Constraints (srdnlenCTF 2026)](#two-stage-loader-rc4-gate--vm-constraints-srdnlenctf-2026)
- [GBA ROM VM Hash Inversion via Meet-in-the-Middle (srdnlenCTF 2026)](#gba-rom-vm-hash-inversion-via-meet-in-the-middle-srdnlenctf-2026)
- [Sprague-Grundy Game Theory Binary (DiceCTF 2026)](#sprague-grundy-game-theory-binary-dicectf-2026)
- [Kernel Module Maze Solving (DiceCTF 2026)](#kernel-module-maze-solving-dicectf-2026)
- [Multi-Threaded VM with Channel Synchronization (DiceCTF 2026)](#multi-threaded-vm-with-channel-synchronization-dicectf-2026)
- [Backdoored Shared Library Detection via String Diffing (Hack.lu CTF 2012)](#backdoored-shared-library-detection-via-string-diffing-hacklu-ctf-2012)
- [Custom binfmt Kernel Module with RC4 Flat Binaries (BSidesSF 2026)](#custom-binfmt-kernel-module-with-rc4-flat-binaries-bsidessf-2026)
- [Hash-Resolved Imports / No-Import Ransomware (BSidesSF 2026)](#hash-resolved-imports--no-import-ransomware-bsidessf-2026)
- [ELF Section Header Corruption for Anti-Analysis (BSidesSF 2026)](#elf-section-header-corruption-for-anti-analysis-bsidessf-2026)
- [VM Trace Diffing Instead of Full Disassembly (CONFidence CTF 2019 Teaser)](#vm-trace-diffing-instead-of-full-disassembly-confidence-ctf-2019-teaser)

---

## Hidden Emulator Opcodes + LD_PRELOAD Key Extraction (0xFun 2026)

**Pattern (CHIP-8):** Non-standard opcode `FxFF` triggers hidden `superChipRendrer()` → AES-256-CBC decryption. Key derived from binary constants.

**Technique:**
1. Check all instruction dispatch branches for non-standard opcodes
2. Hidden opcode may trigger crypto functions (OpenSSL)
3. Use `LD_PRELOAD` hook on `EVP_DecryptInit_ex` to capture AES key at runtime:

```c
#include <openssl/evp.h>
int EVP_DecryptInit_ex(EVP_CIPHER_CTX *ctx, const EVP_CIPHER *type,
                       ENGINE *impl, const unsigned char *key,
                       const unsigned char *iv) {
    // Log key
    for (int i = 0; i < 32; i++) printf("%02x", key[i]);
    printf("\n");
    // Call original
    return ((typeof(EVP_DecryptInit_ex)*)dlsym(RTLD_NEXT, "EVP_DecryptInit_ex"))
           (ctx, type, impl, key, iv);
}
```

```bash
gcc -shared -fPIC -ldl -lssl hook.c -o hook.so
LD_PRELOAD=./hook.so ./emulator rom.ch8
```

---

## Spectre-RSB SPN Cipher — Static Parameter Extraction (0xFun 2026)

**Pattern:** Binary uses cache side channels to implement S-boxes, but ALL cipher parameters (round keys, S-box tables, permutation) are in the binary's data section.

**Key insight:** Don't try to run on special hardware. Extract parameters statically:
- 8 S-boxes × 8 output bits, 256 entries each
- Values `0x340` = bit 1, `0x100` = bit 0
- 64-byte permutation table, 8 round keys

```python
# Extract from binary data section
import struct
sbox = [[0]*256 for _ in range(8)]
for i in range(8):
    for j in range(256):
        val = struct.unpack('<I', data[sbox_offset + (i*256+j)*4 : ...])[0]
        sbox[i][j] = 1 if val == 0x340 else 0
```

**Lesson:** Side-channel implementations embed lookup tables in memory. Extract statically.

---

## Image XOR Mask Recovery via Smoothness (VuwCTF 2025)

**Pattern (Trianglification):** Image divided into triangle regions, each XOR-encrypted with `key = (mask * x - y) & 0xFF` where mask is unknown (0-255).

**Recovery:** Natural images have smooth gradients. Brute-force mask (256 values per region), score by neighbor pixel differences:

```python
import numpy as np
from PIL import Image

img = np.array(Image.open('encrypted.png'))

def score_smoothness(region_pixels, mask, positions):
    decrypted = []
    for (x, y), pixel in zip(positions, region_pixels):
        key = (mask * x - y) & 0xFF
        decrypted.append(pixel ^ key)
    # Score: sum of absolute differences between adjacent pixels
    return -sum(abs(decrypted[i] - decrypted[i+1]) for i in range(len(decrypted)-1))

for region in regions:
    best_mask = max(range(256), key=lambda m: score_smoothness(region, m, positions))
```

**Search space:** 256 candidates × N regions = trivial. Smoothness is a reliable scoring metric for natural images.

---

## Shellcode in Data Section via mmap RWX (VuwCTF 2025)

**Pattern (Missing Function):** Binary relocates data to RWX memory (mmap with PROT_READ|PROT_WRITE|PROT_EXEC) and jumps to it.

**Detection:** Look for `mmap` with PROT_EXEC flag. Embedded shellcode often uses XOR with rotating key.

**Analysis:** Extract data section, apply XOR key (try 3-byte rotating), disassemble result.

---

## Recursive execve Subtraction (VuwCTF 2025)

**Pattern (String Inspector):** Binary recursively calls itself via `execve`, subtracting constants each time.

**Solution:** Find base case and work backward. Often a mathematical relationship like `N * M + remainder`.

---

## Byte-at-a-Time Block Cipher Attack (UTCTF 2024)

**Pattern (PES-128):** First output byte depends only on first input byte (no diffusion).

**Attack:** For each position, try all 256 byte values, compare output byte with target ciphertext. One match per byte = full plaintext recovery without knowing the key.

**Detection:** Change one input byte → only corresponding output byte changes. This means zero cross-byte diffusion = trivially breakable.

---

## Mathematical Convergence Bitmap (EHAX 2026)

**Pattern (Compute It):** Binary classifies complex-plane coordinates by Newton's method convergence. The classification results, arranged as a grid, spell out the flag in ASCII art.

**Recognition:**
- Input file with coordinate pairs (x, y)
- Binary iterates a mathematical function (e.g., z^3 - 1 = 0) and outputs pass/fail
- Grid dimensions hinted by point count (e.g., 2600 = 130×20)
- 5-pixel-high ASCII art font common in CTFs

**Newton's method for z^3 - 1:**
```python
def newton_converges_to_one(px, py, max_iter=50, target_count=12):
    """Returns True if Newton's method converges to z=1 in exactly target_count steps."""
    x, y = px, py
    count = 0
    for _ in range(max_iter):
        f_real = x**3 - 3*x*y**2 - 1.0
        f_imag = 3*x**2*y - y**3
        J_rr = 3.0 * (x**2 - y**2)
        J_ri = 6.0 * x * y
        det = J_rr**2 + J_ri**2
        if det < 1e-9:
            break
        x -= (f_real * J_rr + f_imag * J_ri) / det
        y -= (f_imag * J_rr - f_real * J_ri) / det
        count += 1
        if abs(x - 1.0) < 1e-6 and abs(y) < 1e-6:
            break
    return count == target_count

# Read coordinates and render bitmap
points = [(float(x), float(y)) for x, y in ...]
bits = [1 if newton_converges_to_one(px, py) else 0 for px, py in points]
WIDTH = 130  # 2600 / 20 rows
for r in range(len(bits) // WIDTH):
    print(''.join('#' if bits[r*WIDTH+c] else '.' for c in range(WIDTH)))
```

**Key insight:** The binary is a mathematical classifier, not a flag checker. The flag is in the visual pattern of classifications, not in the binary's output. Reverse-engineer the math, apply to all coordinates, and visualize as bitmap.

---

## Windows PE XOR Bitmap Extraction + OCR (srdnlenCTF 2026)

**Pattern (Artistic Warmup):** Binary renders input text, compares rendered bitmap against expected pixel data stored XOR'd with constant in `.rdata`. No need to compute — extract expected pixels directly.

**Attack:**
1. Reverse the core check function to identify rendering and comparison logic
2. Find the expected pixel blob in `.rdata` (look for large data block referenced near comparison)
3. XOR with constant (e.g., 0xAA) to recover expected rendered DIB
4. Save as image and OCR to recover flag text

```python
import numpy as np
from PIL import Image

with open("binary.exe", "rb") as f:
    data = f.read()

# Extract from .rdata section (offsets from reversing)
blob_offset = 0xC3620  # .rdata offset to XOR'd blob
blob_size = 0x15F90     # 450 * 50 * 4 (BGRA)
blob = np.frombuffer(data[blob_offset:blob_offset + blob_size], dtype=np.uint8)
expected = blob ^ 0xAA  # XOR with constant key

# Reshape as BGRA image (dimensions from reversing)
img = expected.reshape(50, 450, 4)
channel = img[:, :, 0]  # Take one channel (grayscale text)
Image.fromarray(channel, "L").save("target.png")

# OCR with charset whitelist
import subprocess
result = subprocess.run(
    ["tesseract", "target.png", "stdout", "-c",
     "tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}_"],
    capture_output=True, text=True)
print(result.stdout)
```

**Key insight:** When a binary renders text and compares pixels, the expected pixel data is the flag rendered as an image. Extract it directly from the binary data section without needing to understand the rendering logic. OCR with charset whitelist improves accuracy for CTF flag characters.

---

## Two-Stage Loader: RC4 Gate + VM Constraints (srdnlenCTF 2026)

**Pattern (Cornflake v3.5):** Two-stage malware loader — stage 1 uses RC4 username gate, stage 2 downloaded from C2 contains VM-based password validation.

**Stage 1 — RC4 username recovery:**
```python
def rc4(key, data):
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)

# Key from binary strings, ciphertext from stored hex
username = rc4(b"s3cr3t_k3y_v1", bytes.fromhex("46f5289437bc009c17817e997ae82bfbd065545d"))
```

**Stage 2 — VM constraint extraction:**
1. Download stage 2 from C2 endpoint (e.g., `/updates/check.php`)
2. Reverse VM bytecode interpreter (typically 15-20 opcodes)
3. Extract linear equality constraints over flag characters
4. Solve constraint system (Z3 or manual)

**Key insight:** Multi-stage loaders often use simple crypto (RC4) for the first gate and more complex validation (custom VM) for the second. The VM memory may be uninitialized (all zeros), drastically simplifying constraint extraction since memory-dependent operations become constants.

---

## GBA ROM VM Hash Inversion via Meet-in-the-Middle (srdnlenCTF 2026)

**Pattern (Dante's Trial):** Game Boy Advance ROM implements a custom VM. Hash function uses FNV-1a variant with uninitialized memory (stays all zeros). Meet-in-the-middle attack splits the search space.

**Hash function structure:**
```python
# FNV-1a variant with XOR/multiply
P = 0x100000001b3        # FNV prime
CUP = 0x9e3779b185ebca87  # Golden ratio constant
MASK64 = (1 << 64) - 1

def fmix64(h):
    """Finalization mixer."""
    h ^= h >> 33; h = (h * 0xff51afd7ed558ccd) & MASK64
    h ^= h >> 33; h = (h * 0xc4ceb9fe1a85ec53) & MASK64
    h ^= h >> 33
    return h

def hash_input(chars, seed_lo=0x84222325, seed_hi=0xcbf29ce4):
    hlo, hhi, ptr = seed_lo, seed_hi, 0
    for c in chars:
        # tri_mix(c, mem[ptr]) — mem is always 0
        delta = ((ord(c) * CUP) ^ (0 * P)) & MASK64
        hlo = ((hlo ^ (delta & 0xFFFFFFFF)) * (P & 0xFFFFFFFF)) & 0xFFFFFFFF
        hhi = ((hhi ^ (delta >> 32)) * (P >> 32)) & 0xFFFFFFFF
        ptr = (ptr + 1) & 0xFF
    combined = ((hhi << 32) | (hlo ^ ptr)) & MASK64
    return fmix64((combined * P) & MASK64)
```

**Meet-in-the-middle attack:**
```python
import string

TARGET = 0x73f3ebcbd9b4cd93
LENGTH = 6
SPLIT = 3
charset = [c for c in string.printable if 32 <= ord(c) < 127]

# Forward pass: enumerate first 3 characters from seed state
forward = {}
for c1 in charset:
    for c2 in charset:
        for c3 in charset:
            state = hash_forward(seed, [c1, c2, c3])
            forward[state] = c1 + c2 + c3

# Backward pass: invert fmix64 and final multiply, enumerate last 3 chars
inv_target = invert_fmix64(TARGET)
for c4 in charset:
    for c5 in charset:
        for c6 in charset:
            state = hash_backward(inv_target, [c4, c5, c6])
            if state in forward:
                print(f"Found: {forward[state]}{c4}{c5}{c6}")
```

**Key insight:** Meet-in-the-middle reduces search from `95^6 ≈ 7.4×10^11` to `2×95^3 ≈ 1.7×10^6` — a factor of ~430,000x speedup. Critical when the hash function is invertible from the output side (i.e., `fmix64` and the final multiply can be undone). Also: uninitialized VM memory that stays zero simplifies the hash function by removing a variable.

---

## Sprague-Grundy Game Theory Binary (DiceCTF 2026)

**Pattern (Bedtime):** Stripped Rust binary plays N rounds of bounded Nim. Each round has piles and max-move parameter k. Binary uses a PRNG for moves when in a losing position; user must respond optimally so the PRNG eventually generates an invalid move (returns 1). Sum of return values must equal a target.

**Game theory identification:**
- Bounded Nim: remove 1 to k items from any pile per turn
- **Grundy value** per pile: `pile_value % (k+1)`
- **XOR** of all Grundy values: non-zero = winning (N-position), zero = losing (P-position)
- N-positions: computer wins automatically (returns 0)
- P-positions: computer uses PRNG, may make invalid move (returns 1)

**PRNG state tracking through user feedback:**
```python
MASK64 = (1 << 64) - 1

def prng_step(state, pile_count, k):
    """Computer's PRNG move. Returns (pile_idx, amount, new_state)."""
    r12 = state[2] ^ 0x28027f28b04ccfa7
    rax = (state[1] + r12) & MASK64
    s0_new = ROL64((state[0] ** 2 + rax) & MASK64, 32)
    r12_upd = (r12 + rax) & MASK64
    s0_final = ROL64((s0_new ** 2 + r12_upd) & MASK64, 32)

    pile_idx = rax % pile_count
    amount = (r12_upd % k) + 1
    return pile_idx, amount, [s0_final, r12_upd, state[2]]

# Critical: state[2] updated ONLY by user moves (XOR of pile_idx, amount, new_value)
# PRNG moves do NOT affect state[2] — creates feedback loop
```

**Solving approach:**
1. Dump game data from GDB (all entries with pile values and parameters)
2. Classify: count P-positions (return 1) vs N-positions (return 0)
3. Simulate each P-position: PRNG moves → user responds optimally → track state[2]
4. Encode user moves as input format (4-digit decimal pairs, reversed order)

**Key insight:** When a game binary's PRNG state depends on user input, you must simulate the full feedback loop — not just solve the game theory. Use GDB hardware watchpoints to discover which state variables are affected by user vs computer moves.

---

## Kernel Module Maze Solving (DiceCTF 2026)

**Pattern (Explorer):** Rust kernel module implements a 3D maze via `/dev/challenge` ioctls. Navigate the maze, avoid decoy exits (status=2), find the real exit (status=1), read the flag.

**Ioctl enumeration:**
| Command | Description |
|---------|-------------|
| `0x80046481-83` | Get maze dimensions (3 axes, 8-16 each) |
| `0x80046485` | Get status: 0=playing, 1=WIN, 2=decoy |
| `0x80046486` | Get wall bitfield (6 directions) |
| `0x80406487` | Get flag (64 bytes, only when status=1) |
| `0x40046488` | Move in direction (0-5) |
| `0x6489` | Reset position |

**DFS solver with decoy avoidance:**
```c
// Minimal static binary using raw syscalls (no libc) for small upload size
// gcc -nostdlib -static -Os -fno-builtin -o solve solve.c -Wl,--gc-sections && strip solve

int visited[16][16][16];
int bad[16][16][16];   // decoy positions across resets

void dfs(int fd, int x, int y, int z) {
    if (visited[x][y][z] || bad[x][y][z]) return;
    visited[x][y][z] = 1;

    int status = ioctl_get_status(fd);
    if (status == 1) { read_flag(fd); exit(0); }
    if (status == 2) { bad[x][y][z] = 1; return; }  // decoy — mark bad

    int walls = ioctl_get_walls(fd);
    int dx[] = {1,-1,0,0,0,0}, dy[] = {0,0,1,-1,0,0}, dz[] = {0,0,0,0,1,-1};
    int opp[] = {2,3,0,1,5,4};  // opposite directions for backtracking

    for (int dir = 0; dir < 6; dir++) {
        if (!(walls & (1 << dir))) continue;  // wall present
        ioctl_move(fd, dir);
        dfs(fd, x+dx[dir], y+dy[dir], z+dz[dir]);
        ioctl_move(fd, opp[dir]);  // backtrack
    }
}
// After decoy hit: reset via ioctl 0x6489, clear visited, re-run DFS
```

**Remote deployment:** Upload binary via base64 chunks over netcat shell, decode, execute.

**Key insight:** For kernel module challenges, injecting test binaries into initramfs and probing ioctls dynamically is faster than static RE of stripped kernel modules. Keep solver binary minimal (raw syscalls, no libc) for fast upload.

---

## Multi-Threaded VM with Channel Synchronization (DiceCTF 2026)

**Pattern (locked-in):** Custom stack-based VM runs 16 concurrent threads verifying a 30-char flag. Threads communicate via futex-based channels. Pipeline: input → XOR scramble → transformation → base-4 state machine → final check.

**Analysis approach:**
1. **Identify thread roles** by tracing channel read/write patterns in GDB
2. **Extract constants** (XOR scramble values, lookup tables) via breakpoints on specific opcodes
3. **Watch for inverted logic:** validity check returns 0 for valid, non-zero for blocked (opposite of intuition)
4. **Detect futex quirks:** `unlock_pi` on unowned mutex returns EPERM=1, which can change all computations

**BFS state space search for constrained state machines:**
```python
from collections import deque

def solve_flag(scramble_vals, lookup_table, initial_state, target_state):
    """BFS through state machine to find valid flag bytes."""
    flag = [None] * 30
    # Known prefix/suffix from flag format
    flag[0:5] = list(b'dice{')
    flag[29] = ord('}')

    # For each unknown position, try all printable ASCII
    states = {initial_state}
    for pos in range(28, 4, -1):  # processed in reverse
        next_states = {}
        for state in states:
            for ch in range(32, 127):
                transformed = transform(ch, scramble_vals[pos])
                digits = to_base4(transformed)
                new_state = apply_digits(state, digits, lookup_table)
                if new_state is not None:  # valid path exists
                    next_states.setdefault(new_state, []).append((state, ch))
        states = set(next_states.keys())

    # Trace back from target_state to recover flag
```

**Key insight:** Multi-threaded VMs require tracing data flow across thread boundaries. Channel-based communication creates a pipeline — identify each thread's role (input, transform, validate, output) by watching which channels it reads/writes. Constants that affect computation may come from unexpected sources (futex return values, thread IDs).

---

## Backdoored Shared Library Detection via String Diffing (Hack.lu CTF 2012)

**Pattern (Zombie Lockbox):** A setuid binary uses `strcmp` for password validation. The expected password is visible via `strings` and works under GDB (which drops suid), but fails when run normally. The binary links against a non-standard libc that patches function behavior based on suid status.

**Detection steps:**
1. Check for non-standard library paths with `ldd`:
```bash
ldd ./binary
# Suspicious: libc.so.6 => /lib/libc/libc.so.6  (non-standard path)
# Normal:    libc.so.6 => /lib32/libc.so.6
```

2. Diff strings between the suspicious and system libc:
```bash
strings /lib/libc/libc.so.6 > suspicious_strings
strings /lib32/libc-2.15.so > normal_strings
diff suspicious_strings normal_strings
```

3. Disassemble the patched function (e.g., `puts`) to find injected code:
```bash
gdb /lib/libc/libc.so.6
(gdb) disas puts
# Look for unexpected calls or branches
# Injected code may check suid status (getuid/geteuid syscalls)
# and swap the expected password at runtime
```

**Key insight:** When a binary behaves differently under GDB vs. normal execution, check `ldd` for non-standard library paths. Suid binaries drop privileges under debuggers, so a backdoored libc can detect this via `getuid`/`geteuid` syscalls and change program behavior accordingly. The `strings | diff` approach quickly reveals injected data without full disassembly.

---

---

## Custom binfmt Kernel Module with RC4 Flat Binaries (BSidesSF 2026)

**Pattern (Private Binary):** A custom Linux kernel module (`.ko`) registers a `binfmt` handler for non-standard binary formats. When a file with a specific magic number is executed, the kernel module intercepts it, decrypts the contents in memory, and jumps to the entry point.

**Reverse engineering approach:**
1. **Analyze the `.ko`:** Look for `register_binfmt()` call — it registers a `struct linux_binfmt` with a `load_binary` callback
2. **Find the magic number:** The `load_binary` function checks the file's first bytes against a specific magic number to identify its format
3. **Extract the encryption key:** Look for `movabs` instructions loading 8-byte constants — these are often RC4 key bytes
4. **Identify the encryption scheme:** Common choices are RC4, XOR, or AES-ECB. RC4 is identifiable by the S-box initialization loop (256-byte array, swap pattern)
5. **Decrypt the flat binary:** Apply the recovered key to the encrypted file contents, skipping any header

```python
from Crypto.Cipher import ARC4

# Extract RC4 key from kernel module (found via movabs instructions)
key = bytes([0x41, 0x42, 0x43, ...])  # Key bytes from .ko disassembly

with open('encrypted.bin', 'rb') as f:
    header = f.read(HEADER_SIZE)  # Skip binfmt header
    encrypted = f.read()

cipher = ARC4.new(key)
decrypted = cipher.decrypt(encrypted)

# The decrypted output is a flat binary (no ELF headers)
# Load at the fixed virtual address specified in the kernel module
# Disassemble with: objdump -b binary -m i386:x86-64 -D decrypted.bin
# Or in Ghidra: import as "Raw Binary", set base address from .ko
```

**Detection in kernel module:**
- `register_binfmt` / `unregister_binfmt` calls
- `vm_mmap()` or `vm_brk()` for memory allocation at fixed addresses
- Direct jump to mapped memory (entry point execution)
- S-box initialization pattern (RC4): loop 0-255, swap `S[i]` with `S[j]`

**Key insight:** The flat binary has no ELF headers, so standard tools won't recognize it. You must extract the load address from the kernel module (look for the `vm_mmap` call's address argument) and import the decrypted blob at that address in your disassembler. RC4 keys in kernel modules are often stored as immediate values in `mov` or `movabs` instructions rather than in data sections.

**References:** BSidesSF 2026 "Private Binary"

---

## Hash-Resolved Imports / No-Import Ransomware (BSidesSF 2026)

**Pattern (Ran Somewhere):** Malware binary has zero visible imports — all API calls are resolved at runtime by hashing symbol names and comparing against pre-computed hash values. The binary uses `dlopen` + a custom hash table to find libc and libcrypto functions.

**Identification:**
- `readelf -d` shows no dynamic symbols or very few (just `dlopen`/`dlsym`)
- Strings reveal no standard API names
- Disassembly shows hash computation loops followed by indirect calls
- RC4-encrypted embedded strings (RSA public key, file paths, passphrases)

**Analysis shortcut — LD_PRELOAD key extraction:**

Rather than reversing the full hash resolution and key derivation, hook the crypto functions that the malware ultimately calls:

```c
// hook_crypto.c — captures AES key used by the ransomware
#define _GNU_SOURCE
#include <dlfcn.h>
#include <openssl/evp.h>
#include <stdio.h>

int EVP_CipherInit_ex(EVP_CIPHER_CTX *ctx, const EVP_CIPHER *type,
                       ENGINE *impl, const unsigned char *key,
                       const unsigned char *iv) {
    if (key) {
        FILE *f = fopen("/tmp/aes_key.bin", "wb");
        fwrite(key, 1, 32, f);  // AES-256
        fclose(f);
        fprintf(stderr, "[HOOK] AES key captured\n");
    }
    typedef int (*orig_t)(EVP_CIPHER_CTX*, const EVP_CIPHER*, ENGINE*,
                          const unsigned char*, const unsigned char*);
    orig_t orig = (orig_t)dlsym(RTLD_NEXT, "EVP_CipherInit_ex");
    return orig(ctx, type, impl, key, iv);
}
```

```bash
# Compile and run
gcc -shared -fPIC -o hook.so hook_crypto.c -ldl
# Run in Docker container (ransomware may be destructive!)
docker run --rm -v $(pwd):/work -w /work ubuntu:22.04 \
  bash -c "LD_PRELOAD=./hook.so ./ransomware; xxd /tmp/aes_key.bin"
```

**Hash resolution patterns:**
- **SipHash variant:** Two 64-bit seeds, iterative mixing with symbol name bytes
- **DJB2/FNV variants:** Simpler hash functions with recognizable constants (`5381`, `0xcbf29ce484222325`)
- **ROR13-based:** Windows malware favorite: `hash = (hash >> 13) | (hash << 19); hash += c`

**Decryption after key capture:**
```python
from Crypto.Cipher import AES

key = open('/tmp/aes_key.bin', 'rb').read()
iv = open('/tmp/aes_iv.bin', 'rb').read()  # Also hookable
cipher = AES.new(key, AES.MODE_CBC, iv)

with open('flag.txt.enc', 'rb') as f:
    ct = f.read()
pt = cipher.decrypt(ct)
# Remove PKCS7 padding
pt = pt[:-pt[-1]]
print(pt.decode())
```

**Key insight:** When a binary resolves all imports via hashing, don't waste time reversing the hash function and building a rainbow table. Instead, let the malware resolve everything itself by running it in a sandboxed environment with `LD_PRELOAD` hooks on the functions you care about (OpenSSL crypto functions, file I/O, network calls). The AES key is deterministic across runs — if it works once, it works always.

**Safety:** Always run suspected ransomware in a Docker container or VM. Mount only copies of the encrypted files, never originals.

**References:** BSidesSF 2026 "Ran Somewhere"

---

## ELF Section Header Corruption for Anti-Analysis (BSidesSF 2026)

**Pattern (stubborn-elf):** An ELF binary has deliberately corrupted section header table entries, causing standard analysis tools (`readelf`, `objdump`, IDA, Ghidra) to crash or produce errors. However, the **program headers** (which the OS loader uses) are intact, so the binary executes normally. The flag is appended after the corrupted sections, marked with magic bytes.

```python
import sys

# Standard tools fail on corrupted section headers
# Manual parsing bypasses section headers entirely

with open("stubborn_elf", "rb") as f:
    data = f.read()

# Search for magic marker appended after ELF sections
magic = b"\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE"
idx = data.find(magic)
if idx >= 0:
    # Data after magic is XOR-encrypted
    encrypted = data[idx + len(magic):]
    decrypted = bytes(b ^ 0x42 for b in encrypted)
    print(decrypted.decode(errors='ignore'))
```

**Key insight:** ELF execution requires **program headers** (PT_LOAD segments), NOT section headers. Section headers are metadata for debuggers and analysis tools — they're optional at runtime. Corrupting `e_shoff`, `e_shnum`, or `e_shstrndx` in the ELF header breaks tools but not execution. When tools fail, parse the binary manually or patch the ELF header to zero out section header references before loading in a disassembler.

**Recovery approach:**
```bash
# Patch section header offset to 0 (removes section table)
printf '\x00\x00\x00\x00\x00\x00\x00\x00' | dd of=binary bs=1 seek=40 conv=notrunc
# Now Ghidra/IDA can load it using program headers only

# Or use readelf -l (program headers only, ignores sections)
readelf -l stubborn_elf
```

**When to recognize:** `readelf -S` crashes or shows garbage. `file` command identifies it as ELF. `readelf -l` (lowercase L, program headers) works fine. The binary runs normally despite tool failures.

**References:** BSidesSF 2026 "stubborn-elf"

---

## VM Trace Diffing Instead of Full Disassembly (CONFidence CTF 2019 Teaser)

**Pattern (Go Machine):** Go binary runs a 15-handler stack-VM (`0123456789OEQLCI` dispatch string) whose opcode meanings are rotated by an LCG-driven `shuffle` handler after every tick. Rewriting the interpreter faithfully is painful; the VM actually computes a simple 32-bit hash over 4-character input groups.

Instead, attach a debugger-driven tracer to the dispatch routine and dump `(opcode, stack)` per step — then compare traces for two nearly identical inputs:

```python
# Pseudo-code for an IDAPython / gdb conditional-breakpoint tracer
def on_dispatch():
    op  = read_byte(bytecode + pc)
    top = stack[:sp+1]
    print(f"{decode(op)}\t({'|'.join(hex(x) for x in top)})")

# Replay the dumped trace in plain Python; no bytecode parsing, no shuffle logic:
elif line.startswith('save at (0x51)'):
    return stack[top] == expected_hash   # calculated hash lands at mem[0x51]

# Diff trace("abcd") vs trace("dcba") -> the same mul/mod sequence shows up,
# revealing the real algorithm:
def calc_hash(x, mod):
    for _ in range(8):
        x = x * x % mod
    return x * x_original % mod
```

Dump the per-group moduli (`[0x88ca6b51, 0x8405b751, 0xbfa08c87, 0x82013f23, 0x4666751b, 0x5271083f]`) and expected hashes from the trace, then brute-force 4-character permutations of `string.printable` against `calc_hash`.

**Key insight:** Custom VMs with self-modifying dispatch (shuffle, rotor, LCG-keyed opcode table) are designed to punish naive reimplementation. Recording the executed instruction stream bypasses the trick entirely — the trace is deterministic for a given input, and diffing two traces with a single-bit difference localises the "real" algorithm hiding under the VM overhead.

**References:** CONFidence CTF 2019 Teaser — Go Machine, writeup 13947

---

See also: [patterns-ctf.md](#part-2) for Part 2 (multi-layer self-decrypting binary, embedded ZIP+XOR license, stack string deobfuscation, prefix hash brute-force, CVP/LLL lattice, decision tree obfuscation, GF(2^8) Gaussian elimination), [patterns-ctf.md](#part-3) for Part 3 (Z3 boolean circuit, sliding window popcount, keyboard LED Morse code, C++ destructor-hidden validation, VM sequential key-chain brute-force, BWT inversion, OpenType font ligature exploitation, GLSL shader VM with self-modifying code).

---

# Part 2

## Table of Contents
- [Multi-Layer Self-Decrypting Binary (DiceCTF 2026)](#multi-layer-self-decrypting-binary-dicectf-2026)
- [Embedded ZIP + XOR License Decryption (MetaCTF 2026)](#embedded-zip--xor-license-decryption-metactf-2026)
- [Stack String Deobfuscation from .rodata XOR Blob (Nullcon 2026)](#stack-string-deobfuscation-from-rodata-xor-blob-nullcon-2026)
- [Prefix Hash Brute-Force (Nullcon 2026)](#prefix-hash-brute-force-nullcon-2026)
- [CVP/LLL Lattice for Constrained Integer Validation (HTB ShadowLabyrinth)](#cvplll-lattice-for-constrained-integer-validation-htb-shadowlabyrinth)
- [Decision Tree Function Obfuscation (HTB WonderSMS)](#decision-tree-function-obfuscation-htb-wondersms)
- [GF(2^8) Gaussian Elimination for Flag Recovery (ApoorvCTF 2026)](#gf28-gaussian-elimination-for-flag-recovery-apoorvctf-2026)
- [ROP Chain Obfuscation in Modified Binary (PlaidCTF 2016)](#rop-chain-obfuscation-in-modified-binary-plaidctf-2016)

---

## Multi-Layer Self-Decrypting Binary (DiceCTF 2026)

**Pattern (another-onion):** Binary with N layers (e.g., 256), each reading 2 key bytes, deriving keystream via SHA-256 NI instructions, XOR-decrypting the next layer, then jumping to it. Must solve within a time limit (e.g., 30 minutes).

**Oracle for correct key:** Wrong key bytes produce garbage code. Correct key bytes produce code with exactly 2 `call read@plt` instructions (next layer's reads). Brute-force all 65536 candidates per layer using this oracle.

**JIT execution approach (fastest):**
```c
// Map binary's memory at original virtual addresses into solver process
// Compile solver at non-overlapping address: -Wl,-Ttext-segment=0x10000000
void *text = mmap((void*)0x400000, text_size, PROT_RWX, MAP_FIXED|MAP_PRIVATE, fd, 0);
void *bss = mmap((void*)bss_addr, bss_size, PROT_RW, MAP_FIXED|MAP_SHARED, shm_fd, 0);

// Patch read@plt to inject candidate bytes instead of reading stdin
// Patch tail jmp/call to next layer with ret/NOP to return from layer

// Fork-per-candidate: COW gives isolated memory without memcpy
for (int candidate = 0; candidate < 65536; candidate++) {
    pid_t pid = fork();
    if (pid == 0) {
        // Child: remap BSS as MAP_PRIVATE (COW from shared file)
        mmap(bss_addr, bss_size, PROT_RW, MAP_FIXED|MAP_PRIVATE, shm_fd, 0);
        inject_key(candidate >> 8, candidate & 0xff);
        ((void(*)())layer_addr)();  // Execute layer as function call
        // Check: does decrypted code contain exactly 2 call read@plt?
        if (count_read_calls(next_layer_addr) == 2) signal_found(candidate);
        _exit(0);
    }
}
```

**Performance tiers:**
| Approach | Speed | 256-layer estimate |
|----------|-------|--------------------|
| Python subprocess | ~2/s | days |
| Ptrace fork injection | ~119/s | 6+ hours |
| JIT + fork-per-candidate | ~1000/s | 140 min |
| JIT + shared BSS + 32 workers | ~3500/s | **~17 min** |

**Shared BSS optimization:** BSS (16MB+) stored in `/dev/shm` as `MAP_SHARED` in parent. Children remap as `MAP_PRIVATE` for COW. Reduces fork overhead from 16MB page-table setup to ~4KB.

**Key insight:** Multi-layer decryption challenges are fundamentally about building fast brute-force engines. JIT execution (mapping binary memory into solver, running code directly as function calls) is orders of magnitude faster than ptrace. Fork-based COW provides free memory isolation per candidate.

**Gotchas:**
- Real binary may use `call` (0xe8) instead of `jmp` (0xe9) for layer transitions — adjust tail patching
- BSS may extend beyond ELF MemSiz via kernel brk mapping — map extra space
- SHA-NI instructions work even when not advertised in `/proc/cpuinfo`

---

## Embedded ZIP + XOR License Decryption (MetaCTF 2026)

**Pattern (License To Rev):** Binary requires a license file as argument. Contains an embedded ZIP archive with the expected license, and an XOR-encrypted flag.

**Recognition:**
- `strings` reveals `EMBEDDED_ZIP` and `ENCRYPTED_MESSAGE` symbols
- Binary is not stripped — `nm` or `readelf -s` shows data symbols in `.rodata`
- `file` shows PIE executable, source file named `licensed.c`

**Analysis workflow:**
1. **Find data symbols:**
```bash
readelf -s binary | grep -E "EMBEDDED|ENCRYPTED|LICENSE"
# EMBEDDED_ZIP at offset 0x2220, 384 bytes
# ENCRYPTED_MESSAGE at offset 0x21e0, 35 bytes
```

2. **Extract embedded ZIP:**
```python
import struct
with open('binary', 'rb') as f:
    data = f.read()
# Find PK\x03\x04 magic in .rodata
zip_start = data.find(b'PK\x03\x04')
# Extract ZIP (size from symbol table or until next symbol)
open('embedded.zip', 'wb').write(data[zip_start:zip_start+384])
```

3. **Extract license from ZIP:**
```bash
unzip embedded.zip  # Contains license.txt
```

4. **XOR decrypt the flag:**
```python
license = open('license.txt', 'rb').read()
enc_msg = open('encrypted_msg.bin', 'rb').read()  # Extract from .rodata
flag = bytes(a ^ b for a, b in zip(enc_msg, license))
print(flag.decode())
```

**Key insight:** No need to run the binary or bypass the expiry date check. The embedded ZIP and encrypted message are both in `.rodata` — extract and XOR offline.

**Disassembly confirms:**
- `memcmp(user_license, decompressed_embedded_zip, size)` — license validation
- Date parsing with `sscanf("%d-%d-%d")` on `EXPIRY_DATE=` field
- XOR loop: `ENCRYPTED_MESSAGE[i] ^ license[i]` → `putc()` per byte

**Lesson:** When a binary has named symbols (`EMBEDDED_*`, `ENCRYPTED_*`), extract data directly from the binary without execution. XOR with known plaintext (the license) is trivially reversible.

---

## Stack String Deobfuscation from .rodata XOR Blob (Nullcon 2026)

**Pattern (stack_strings_1/2):** Binary mmaps a blob from `.rodata`, XOR-deobfuscates it, then uses the blob to validate input. Flag is recovered by reimplementing the verification loop.

**Recognition:**
- `mmap()` call followed by XOR loop over `.rodata` data
- Verification loop with running state (`eax`, `ebx`, `r9`) updated with constants like `0x9E3779B9`, `0x85EBCA6B`, `0xA97288ED`
- `rol32()` operations with position-dependent shifts
- Expected bytes stored in deobfuscated buffer

**Approach:**
1. Extract `.rodata` blob with pyelftools:
   ```python
   from elftools.elf.elffile import ELFFile
   with open(binary, "rb") as f:
       elf = ELFFile(f)
       ro = elf.get_section_by_name(".rodata")
       blob = ro.data()[offset:offset+size]
   ```
2. Recover embedded constants (length, magic values) by XOR with known keys from disassembly
3. Reimplement the byte-by-byte verification loop:
   - Each iteration: compute two hash-like values from running state
   - XOR them together and with expected byte to recover input byte
   - Update running state with constant additions

**Variant (stack_strings_2):** Adds position permutation + state dependency on previous character:
- Position permutation: byte `i` may go to position `pos[i]` in the output
- State dependency: `need = (expected - rol8(prev_char, 1)) & 0xFF`
- Must track `state` variable that updates to current character each iteration

**Key constants to look for:**
- `0x9E3779B9` (golden ratio fractional, common in hash functions)
- `0x85EBCA6B` (MurmurHash3 finalizer constant)
- `0xA97288ED` (related hash constant)
- `rol32()` with shift `i & 7`

---

## Prefix Hash Brute-Force (Nullcon 2026)

**Pattern (Hashinator):** Binary hashes every prefix of the input independently and outputs one digest per prefix. Given N output digests, the flag has N-1 characters.

**Attack:** Recover input one character at a time:
```python
for pos in range(1, len(target_hashes)):
    for ch in charset:
        candidate = known_prefix + ch + padding
        hashes = run_binary(candidate)
        if hashes[pos] == target_hashes[pos]:
            known_prefix += ch
            break
```

**Key insight:** If each prefix hash is independent (no chaining/HMAC), the problem decomposes into `N` x `|charset|` binary executions. This is the hash equivalent of byte-at-a-time block cipher attacks.

**Detection:** Binary outputs multiple hash lines. Changing last character only changes last hash. Different input lengths produce different numbers of output lines.

---

## CVP/LLL Lattice for Constrained Integer Validation (HTB ShadowLabyrinth)

**Pattern:** Binary validates flag via matrix multiplication where grouped input characters are multiplied by coefficient matrices and checked against expected 64-bit results. Standard algebra fails because solutions must be printable ASCII (32-126). Lattice-based CVP (Closest Vector Problem) with LLL reduction solves this efficiently.

**Identification:**
1. Binary groups input characters (e.g., 4 at a time)
2. Each group is multiplied by a coefficient matrix
3. Results compared against hardcoded 64-bit values
4. Need integer solutions in a constrained range (printable ASCII)

**SageMath CVP solver:**
```python
from sage.all import *

def solve_constrained_matrix(coefficients, targets, char_range=(32, 126)):
    """
    coefficients: list of coefficient rows (e.g., 4 values per group)
    targets: expected output values
    char_range: valid character range (printable ASCII)
    """
    n = len(coefficients[0])  # characters per group
    mid = (char_range[0] + char_range[1]) // 2

    # Build lattice: [coeff_matrix | I*scale]
    # The target vector includes adjusted targets
    M = matrix(ZZ, n + len(targets), n + len(targets))
    scale = 1000  # Weight to constrain character range

    for i, row in enumerate(coefficients):
        for j, c in enumerate(row):
            M[j, i] = c
        M[n + i, i] = 1  # padding

    for j in range(n):
        M[j, len(targets) + j] = scale

    target_vec = vector(ZZ, [t - sum(c * mid for c in row)
                              for row, t in zip(coefficients, targets)]
                        + [0] * n)

    # LLL + CVP
    L = M.LLL()
    closest = L * L.solve_left(target_vec)  # or use Babai
    solution = [closest[len(targets) + j] // scale + mid for j in range(n)]
    return bytes(solution)
```

**Two-phase validation pattern:**
1. **Phase 1 (matrix math):** Solve via CVP/LLL → recovers first N characters
2. First N characters become AES key → decrypt `file.bin` (XOR last 16 bytes + AES-256-CBC + zlib decompress)
3. **Phase 2 (custom VM):** Decrypted bytecode runs in custom VM, validates remaining characters via another linear system (mod 2^32)

**Modular linear system solving (Phase 2 — VM validation):**
```python
import numpy as np
from sympy import Matrix

# M * x = v (mod 2^32)
M_mod = Matrix(coefficients) % (2**32)
v_mod = Matrix(targets) % (2**32)
# Gaussian elimination in Z/(2^32)
solution = M_mod.solve(v_mod)  # Returns flag characters
```

**Key insight:** When a binary validates input through linear combinations with large coefficients and the solution must be in a small range (printable ASCII), this is a lattice problem in disguise. LLL reduction + CVP finds the nearest lattice point, recovering the constrained solution. Cross-reference: invoke `/crypto` for LLL/CVP fundamentals (advanced-math.md in ctf-crypto).

**Detection:** Binary performs matrix-like operations on grouped input, compares against 64-bit constants, and a brute-force search space is too large (e.g., 256^4 per group × 12 groups).

---

## Decision Tree Function Obfuscation (HTB WonderSMS)

**Pattern:** Binary routes input through ~200+ auto-generated functions, each computing a polynomial expression from input positions, comparing against a constant, and branching left/right. The tree makes static analysis impractical without scripted extraction.

**Identification:**
1. Large number of similar functions with random-looking names (e.g., `f315732804`)
2. Each function computes arithmetic on specific input positions
3. Functions call other tree functions or a final validation function
4. Decompiled code shows `if (expr cmp constant) call_left() else call_right()`

**Ghidra headless scripting for mass extraction:**
```python
# Extract comparison constants from all tree functions
# Run via: analyzeHeadless project/ tmp -import binary -postScript extract_tree.py
from ghidra.program.model.listing import *
from ghidra.program.model.symbol import *

fm = currentProgram.getFunctionManager()
results = []
for func in fm.getFunctions(True):
    name = func.getName()
    if name.startswith('f') and name[1:].isdigit():
        # Find CMP instruction and extract immediate constant
        inst_iter = currentProgram.getListing().getInstructions(func.getBody(), True)
        for inst in inst_iter:
            if inst.getMnemonicString() == 'CMP':
                operand = inst.getOpObjects(1)
                if operand:
                    results.append((name, int(operand[0].getValue())))
```

**Constraint propagation from known output format:**
1. Start from known output bytes (e.g., `http://HTB{...}`) → fix several input positions
2. Fixed positions cascade through arithmetic constraints → determine dependent positions
3. Tree root equation pins down remaining free variables
4. Recognize English words in partial flag to disambiguate multiple solutions

**Key insight:** Auto-generated decision trees look overwhelming but are repetitive by construction. Script the extraction (Ghidra, Binary Ninja, radare2) rather than reversing each function manually. The tree is just a dispatcher — the real logic is in the leaf function and its constraints.

**Detection:** Binary with hundreds of similarly-structured functions, 3-5 input position references per function, branching to two other functions or a common leaf.

---

## GF(2^8) Gaussian Elimination for Flag Recovery (ApoorvCTF 2026)

**Pattern (Forge):** Stripped binary performs Gaussian elimination over GF(2^8) (Galois Field with 256 elements, using the AES polynomial). A matrix and augmentation vector are embedded in `.rodata`. The solution vector is the flag.

**GF(2^8) arithmetic with AES polynomial (x^8+x^4+x^3+x+1 = 0x11b):**
```python
def gf_mul(a, b):
    """Multiply in GF(2^8) with AES reduction polynomial."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xff
        if hi:
            a ^= 0x1b  # Reduction: x^8 = x^4+x^3+x+1
        b >>= 1
    return p

def gf_inv(a):
    """Brute-force multiplicative inverse (fine for 256 elements)."""
    if a == 0: return 0
    for x in range(1, 256):
        if gf_mul(a, x) == 1:
            return x
    return 0
```

**Solving the linear system:**
```python
# Extract N×N matrix + N-byte augmentation from binary .rodata
N = 56  # Flag length
# Build augmented matrix: N rows × (N+1) cols

for col in range(N):
    # Find non-zero pivot
    pivot = next((r for r in range(col, N) if aug[r][col] != 0), -1)
    if pivot != col:
        aug[col], aug[pivot] = aug[pivot], aug[col]
    # Scale pivot row by inverse
    inv = gf_inv(aug[col][col])
    aug[col] = [gf_mul(v, inv) for v in aug[col]]
    # Eliminate column in all other rows
    for row in range(N):
        if row == col: continue
        factor = aug[row][col]
        if factor == 0: continue
        aug[row] = [v ^ gf_mul(factor, aug[col][j]) for j, v in enumerate(aug[row])]

flag = bytes(aug[i][N] for i in range(N))
```

**Key insight:** GF(2^8) is NOT regular integer arithmetic — addition is XOR, multiplication uses polynomial reduction. The AES polynomial (0x11b) is the most common; look for the constant `0x1b` in disassembly. The binary may encrypt the result with AES-GCM afterward, but the raw solution vector (pre-encryption) is the flag.

**Detection:** Binary with a large matrix in `.rodata` (N² bytes), XOR-based row operations, constants `0x1b` or `0x11b`, and flag length matching sqrt of matrix size.

---

## ROP Chain Obfuscation in Modified Binary (PlaidCTF 2016)

**Pattern (quite quixotic quest):** Modified `curl` binary with a custom `--pctfkey KEY` option. Key validation replaces `esp` with a buffer address and returns into a ~250KB ROP chain stored in a `magic_buf` symbol. The ROP chain validates the key through XOR, MD5, and constant comparisons.

**Analysis approach:**

1. **Detect the ROP dispatch:** Look for `mov esp, eax; ret` or similar stack pivot — this redirects execution into the ROP chain
2. **Dump the ROP chain:** Script GDB to disassemble instructions after each return address in the chain:
```python
# GDB script to trace ROP gadgets
import gdb

magic_buf = 0x080b0000  # symbol address
buf_size = 0x40000       # quarter megabyte
offset = 0

while offset < buf_size:
    addr = int.from_bytes(gdb.selected_inferior().read_memory(magic_buf + offset, 4), 'little')
    gdb.execute(f'x/3i {addr}')
    # Advance past the gadget (typically 4 bytes per return address)
    offset += 4
```

3. **Identify patterns in the chain:** Look for unrolled loops (repeated gadget sequences), `pop` instructions that skip data, and `ret imm16` that skip large blocks
4. **Reconstruct the algorithm:** The chain typically performs:
   - Key length check (compare with constant)
   - Character-level operations (sum ASCII values, XOR with constants)
   - Hash computation (MD5 of derived value)
   - Hash prefix comparison
   - XOR of input with hash as keystream
   - Comparison with embedded constants

5. **Extract and solve:** Dump the embedded constants, brute-force any intermediate values (e.g., character sum → MD5 with matching prefix), then XOR to recover the key:
```python
import hashlib

# Brute-force the sum that produces correct MD5 prefix
target_prefix = 0xc0050bdd  # extracted from ROP chain
for s in range(128 * 0x35):  # max sum of printable chars * key_length
    h = hashlib.md5(str(s ^ xor_constant).encode()).hexdigest()
    if int(h[:8], 16) == target_prefix:
        md5_key = bytes.fromhex(h)
        break

# XOR embedded values with MD5 keystream to get flag
flag = bytes(v ^ md5_key[i % 16] for i, v in enumerate(embedded_values))
```

**Key insight:** ROP chain obfuscation ("ROPfuscation") hides algorithms in chains of return-oriented gadgets. The chain looks incomprehensible as raw addresses but becomes analyzable when you: (a) dump each gadget's disassembly, (b) filter repetitions and skip regions, (c) annotate register effects. The chain is functionally equivalent to normal code — it just uses `ret` instead of sequential execution. Large chains (100K+ gadgets) often contain unrolled loops that compress to ~1000 lines of pseudocode.

See also: [patterns-ctf.md](patterns-ctf.md) for Part 1 (hidden emulator opcodes, SPN static extraction, image XOR smoothness, byte-at-a-time cipher, mathematical convergence bitmap, Windows PE XOR bitmap OCR, two-stage RC4+VM loaders, GBA ROM meet-in-the-middle, Sprague-Grundy game theory, kernel module maze solving, multi-threaded VM channels). [patterns-ctf.md](#part-3) for Part 3 (Z3 single-line Python circuit, sliding window popcount, keyboard LED Morse code, C++ destructor-hidden validation, syscall side-effect memory corruption, MFC dialog event handlers, VM sequential key-chain brute-force, Burrows-Wheeler transform inversion, OpenType font ligature exploitation, GLSL shader VM with self-modifying code, instruction counter as cryptographic state).

---

# Part 3

## Table of Contents
- [Z3 for Single-Line Python Boolean Circuit (BearCatCTF 2026)](#z3-for-single-line-python-boolean-circuit-bearcatctf-2026)
- [Sliding Window Popcount Differential Propagation (BearCatCTF 2026)](#sliding-window-popcount-differential-propagation-bearcatctf-2026)
- [Morse Code from Keyboard LEDs via ioctl (PlaidCTF 2013)](#morse-code-from-keyboard-leds-via-ioctl-plaidctf-2013)
- [C++ Destructor-Hidden Validation (Defcamp 2015)](#c-destructor-hidden-validation-defcamp-2015)
- [Syscall Side-Effect Memory Corruption (Hack.lu 2015)](#syscall-side-effect-memory-corruption-hacklu-2015)
- [MFC Dialog Event Handler Location (WhiteHat 2015)](#mfc-dialog-event-handler-location-whitehat-2015)
- [VM Sequential Key-Chain Brute-Force (Midnight Flag 2026)](#vm-sequential-key-chain-brute-force-midnight-flag-2026)
- [Burrows-Wheeler Transform Inversion without Terminator (ASIS CTF Finals 2016)](#burrows-wheeler-transform-inversion-without-terminator-asis-ctf-finals-2016)
- [OpenType Font Ligature Exploitation for Hidden Messages (Hack The Vote 2016)](#opentype-font-ligature-exploitation-for-hidden-messages-hack-the-vote-2016)
- [GLSL Shader VM with Self-Modifying Code (ApoorvCTF 2026)](#glsl-shader-vm-with-self-modifying-code-apoorvctf-2026)
- [Instruction Counter as Cryptographic State (MetaCTF Flash 2026)](#instruction-counter-as-cryptographic-state-metactf-flash-2026)
- [Thread Race Condition with Signed Integer Overflow (Codegate 2017)](#thread-race-condition-with-signed-integer-overflow-codegate-2017)
- [ESP32/Xtensa Firmware Reversing with ROM Symbol Map (Insomni'hack 2017)](#esp32xtensa-firmware-reversing-with-rom-symbol-map-insomnihack-2017)
- [Batch Crackme Automation via objdump Pattern Extraction (DEF CON 2017)](#batch-crackme-automation-via-objdump-pattern-extraction-def-con-2017)
- [Fork + Pipe + Dead Branch Anti-Analysis (RCTF 2017)](#fork--pipe--dead-branch-anti-analysis-rctf-2017)
- [Time-Locked Binary with Date-Based Key (Hack.lu 2017)](#time-locked-binary-with-date-based-key-hacklu-2017)
- [ARM Code in Image Pixels via UnicornJS (Hack.lu 2017)](#arm-code-in-image-pixels-via-unicornjs-hacklu-2017)
- [x86 16-bit MBR psadbw Constraint Solving (CSAW 2017)](#x86-16-bit-mbr-psadbw-constraint-solving-csaw-2017)
- [TensorFlow DNN Inversion by Inverting Sigmoid Layers (N1CTF 2018)](#tensorflow-dnn-inversion-by-inverting-sigmoid-layers-n1ctf-2018)
- [BPF Filter Analysis via JIT Compilation to x64 Assembly (Midnight Sun CTF 2018)](#bpf-filter-analysis-via-jit-compilation-to-x64-assembly-midnight-sun-ctf-2018)
- [Single-Byte XOR ROM Deobfuscation Sweep (X-MAS CTF 2018)](#single-byte-xor-rom-deobfuscation-sweep-x-mas-ctf-2018)
- [WebKit Array.slice OOB CVE-2016-4622 (Codegate 2019)](#webkit-arrayslice-oob-cve-2016-4622-codegate-2019)
- [Multi-Modulus CRT Keygen with Matrix Lookup Password (Pragyan CTF 2019)](#multi-modulus-crt-keygen-with-matrix-lookup-password-pragyan-ctf-2019)

---

## Z3 for Single-Line Python Boolean Circuit (BearCatCTF 2026)

**Pattern (Captain Morgan):** Single-line Python (2000+ semicolons) validates flag via walrus operator chains decomposing input as a big-endian integer, with bitwise operations producing a boolean circuit.

**Identification:**
- Single-line Python with semicolons separating statements
- Walrus operator `:=` chains: `(x := expr)`
- Obfuscated XOR: `(x | i) & ~(x & i)` instead of `x ^ i`
- Input treated as a single large integer, decomposed via bit-shifting

**Z3 solution:**
```python
from z3 import *

n_bytes = 29  # Flag length
ari = BitVec('ari', n_bytes * 8)

# Parse semicolon-separated statements
# Model walrus chains as LShR(ari, shift_amount)
# Evaluate boolean expressions symbolically
# Final assertion: result_var == 0

s = Solver()
s.add(bfu == 0)  # Final validation variable
if s.check() == sat:
    m = s.model()
    val = m[ari].as_long()
    flag = val.to_bytes(n_bytes, 'big').decode('ascii')
```

**Key insight:** Single-line Python obfuscation creates a boolean circuit over input bits. The walrus operator chains are just variable assignments — split on semicolons and translate each to Z3 symbolically. Obfuscated XOR `(a | b) & ~(a & b)` is just `a ^ b`. Z3 solves these circuits in under a second. Look for `__builtins__` access or `ord()`/`chr()` calls to identify the input→integer conversion.

**Detection:** Single-line Python with 1000+ semicolons, walrus operators, bitwise operations, and a final comparison to 0 or True.

---

## Sliding Window Popcount Differential Propagation (BearCatCTF 2026)

**Pattern (Treasure Hunt 4):** Binary validates input via expected popcount (number of set bits) for each position of a 16-bit sliding window over the input bits.

**Differential propagation:**
When the window slides by 1 bit:
```text
popcount(window[i+1]) - popcount(window[i]) = bit[i+16] - bit[i]
```
So: `bit[i+16] = bit[i] + (data[i+1] - data[i])`

```python
expected = [...]  # 337 expected popcount values
total_bits = 337 + 15  # = 352

# Brute-force the initial 16-bit window (must have popcount = expected[0])
for start_val in range(0x10000):
    if bin(start_val).count('1') != expected[0]:
        continue

    bits = [0] * total_bits
    for j in range(16):
        bits[j] = (start_val >> (15 - j)) & 1

    valid = True
    for i in range(len(expected) - 1):
        new_bit = bits[i] + (expected[i + 1] - expected[i])
        if new_bit not in (0, 1):
            valid = False
            break
        bits[i + 16] = new_bit

    if valid:
        # Convert bits to bytes
        flag_bytes = bytes(int(''.join(map(str, bits[i:i+8])), 2)
                          for i in range(0, total_bits, 8))
        if b'BCCTF' in flag_bytes or flag_bytes[:5].isascii():
            print(flag_bytes.decode(errors='replace'))
            break
```

**Key insight:** Sliding window popcount differences create a recurrence relation: each new bit is determined by the bit 16 positions back plus the popcount delta. Only the first 16 bits are free (constrained by initial popcount). Brute-force the ~4000-8000 valid initial windows — for each, the entire bit sequence is deterministic. Runs in under a second.

**Detection:** Binary computing popcount/hamming weight on fixed-size windows. Expected value array with length ≈ input_bits - window_size + 1. Values in array are small integers (0 to window_size).

---

---

## Morse Code from Keyboard LEDs via ioctl (PlaidCTF 2013)

**Pattern:** Binary uses `ioctl(fd, KDSETLED, value)` to blink keyboard LEDs (Num/Caps/Scroll Lock). Timing patterns encode Morse code.

```bash
# Step 1: Bypass ptrace anti-debug
# Patch ptrace call at offset with NOP (0x90)
python3 -c "
data = open('binary','rb').read()
data = data[:0x72b] + b'\x90'*5 + data[0x730:]  # NOP the ptrace call
open('patched','wb').write(data)
"

# Step 2: Run under strace, capture ioctl calls
strace -e ioctl ./patched 2>&1 | grep KDSETLED > leds.txt

# Step 3: Decode timing patterns
# Short blink (250ms) = dit (.), long blink (750ms) = dah (-)
# Inter-character pause = 3x, inter-word pause = 7x
```

```python
# Parse strace output to extract Morse
import re
morse_map = {'.-':'A', '-...':'B', '-.-.':'C', '-..':'D', '.':'E',
             '..-.':'F', '--.':'G', '....':'H', '..':'I', '.---':'J',
             '-.-':'K', '.-..':'L', '--':'M', '-.':'N', '---':'O',
             '.--.':'P', '--.-':'Q', '.-.':'R', '...':'S', '-':'T',
             '..-':'U', '...-':'V', '.--':'W', '-..-':'X', '-.--':'Y',
             '--..':'Z', '-----':'0', '.----':'1'}
# Map LED on-durations to dots/dashes, group by pauses
```

**Key insight:** `KDSETLED` controls physical keyboard LEDs on Linux (`/dev/console`). The binary must run with console access. Use `strace -e ioctl` to capture all LED state changes without needing physical observation. Timing between calls determines dot vs dash.

---

## C++ Destructor-Hidden Validation (Defcamp 2015)

Validation logic may hide in C++ destructors that execute after `main()` returns. The `__cxa_atexit` mechanism registers destructor callbacks:

1. **Locate destructors:** Search for `__cxa_atexit` calls in `.init_array`/constructor sections
2. **Static analysis:** Identify global objects whose destructors perform flag checking
3. **Dynamic verification:** Set breakpoints on `__cxa_finalize` to trace post-main execution

```asm
# In IDA/Ghidra: look for atexit registrations
__cxa_atexit(destructor_func, object_ptr, dso_handle);

# Destructor contains actual validation:
# - Regex pattern matching on 4-byte blocks (8 sequential checks)
# - Arithmetic: v2 += -3 * s[i] + 36 + (s[i] ^ 0x2FCFBA)
# - Modular verification of accumulated sum
```

**Key insight:** When `main()` appears trivial or incomplete, check destructors of global/static C++ objects. The `.fini_array` section and `__cxa_atexit` registrations reveal hidden post-main logic.

---

## Syscall Side-Effect Memory Corruption (Hack.lu 2015)

The `rt_sigprocmask` syscall writes a `sigset_t` structure to its output pointer. When input parsing passes a pointer near a security-critical variable:

1. Certain input characters (e.g., `:` to `@` range, values 0x3A-0x40) trigger `rt_sigprocmask` as a side effect
2. The syscall zeros out bytes at the output address, which may overlap adjacent variables
3. In little-endian layout, zeroing the MSB of an adjacent integer variable effectively sets it to a small value

```c
// Memory layout (no ASLR):
// 0x603390: input_buffer[4]
// 0x603394: security_check_var

// Input ':' triggers: rt_sigprocmask(SIG_BLOCK, NULL, (sigset_t*)0x603397, ...)
// This zeros bytes at 0x603397+, corrupting security_check_var's high bytes
```

**Key insight:** Audit how input validation functions interact with syscalls. Character-to-syscall mappings in hex conversion routines can produce unintended memory writes via kernel-space operations.

---

## MFC Dialog Event Handler Location (WhiteHat 2015)

To find event handlers in MFC (Microsoft Foundation Class) applications:

1. **Break on SendMessageW:** Set breakpoint on `user32!SendMessageW` to intercept dialog messages
2. **Filter for WM_COMMAND:** Message ID 0x111 indicates button clicks and control events
3. **Trace message map:** Follow the MFC message dispatch from `CWnd::OnWndMsg` → `CCmdTarget::OnCmdMsg` → handler function
4. **OnInitDialog:** Often contains decryption or validation setup; triggered by WM_INITDIALOG (0x110)

```asm
# WinDbg/x64dbg:
bp user32!SendMessageW ".if (poi(@esp+8)==0x111) {} .else {gc}"
# Or in IDA: find cross-references to AFX_MSGMAP_ENTRY structures
```

**Key insight:** MFC applications route messages through dispatch tables. Identify the `AFX_MSGMAP` structure to enumerate all handled messages without runtime analysis.

---

## VM Sequential Key-Chain Brute-Force (Midnight Flag 2026)

**Pattern (67):** Custom VM validates input in N-byte blocks. Each block's output key feeds as input to the next block, preventing parallel solving. Per-block search space is small enough to brute-force (2^24 for 3-byte blocks).

**Recognition signs:**
- Bytecode with XOR-obfuscated opcodes (all bytes XOR'd with a constant, producing ASCII-looking bytecode)
- Iterative transformation loop (xorshift + multiply, repeated 1000+ times) making algebraic inversion impractical
- CHECK opcodes comparing accumulated state against embedded constants
- Large `.data` section with repetitive bytecode patterns

**Solving approach:**
1. Parse bytecode to extract CHECK values (expected key after each block)
2. For each block sequentially, brute-force the input bytes that produce the expected key
3. Use the CHECK value as the key for the next block

```c
// OpenMP-parallelized per-block brute-force
uint32_t process(uint32_t val) {
    for (int i = 0; i < 1000; i++) {
        val ^= (val << 13);
        val ^= (val >> 17);
        val ^= (val << 5);
        val *= 0x2545f491;
    }
    return val;
}

int solve_block(uint32_t old_key, uint32_t expected_key, unsigned char *out) {
    int found = 0;
    #pragma omp parallel for shared(found)
    for (int v = 0; v < 0x1000000; v++) {
        if (found) continue;
        uint32_t input_val = ((v >> 16) << 16) | (v & 0xFF) | ((v >> 8 & 0xFF) << 8);
        uint32_t saved = input_val ^ old_key;
        uint32_t final_val = process(saved);
        if ((final_val ^ saved) == expected_key) {
            #pragma omp critical
            { if (!found) { out[0]=v>>16; out[1]=(v>>8)&0xFF; out[2]=v&0xFF; found=1; } }
        }
    }
    return found;
}
// Compile: gcc -O3 -march=native -fopenmp -o solve solve.c
```

**Key insight:** When a transformation is intentionally non-invertible (iterated hash-like function), brute-force is the intended solution. OpenMP parallelization is critical — 287 blocks x 16.7M candidates each takes minutes parallelized vs hours single-threaded. The sequential key dependency means blocks must be solved in order, but each individual block search is embarrassingly parallel.

---

## Burrows-Wheeler Transform Inversion without Terminator (ASIS CTF Finals 2016)

BWT applied to binary representation without a standard terminating character. Requires brute-force inversion by trying all possible original strings.

```python
def bwt_inverse_bruteforce(bwt_string):
    """Invert BWT when no terminating character is present.
    Standard BWT inverse needs the terminator position.
    Without it, try all n possible rotations."""
    n = len(bwt_string)

    # Standard BWT inverse produces a table
    table = [''] * n
    for _ in range(n):
        table = sorted([bwt_string[i] + table[i] for i in range(n)])

    # Without terminator, all n rows are valid candidates
    # Filter by known constraints (e.g., starts with '1' for binary, matches XOR pattern)
    candidates = []
    for row in table:
        # Apply challenge-specific validation
        if is_valid_plaintext(row):
            candidates.append(row)

    return candidates

def bwt_with_xor_rounds(encrypted_hex, num_rounds):
    """Multi-round BWT with XOR key derived from round index"""
    data = bytes.fromhex(encrypted_hex)
    for round_idx in range(num_rounds - 1, -1, -1):
        # Each round: BWT on binary representation, then XOR with round-based key
        binary_str = ''.join(format(b, '08b') for b in data)
        candidates = bwt_inverse_bruteforce(binary_str)
        # Select candidate matching constraints (leading '1', trailing bit rule)
        data = select_valid_candidate(candidates, round_idx)
    return data
```

**Key insight:** Standard BWT uses a terminating character (like '$') to mark the original string's position. Without it, BWT inversion produces n candidates (one per rotation). Use domain-specific constraints (binary format, XOR round structure, flag prefix) to identify the correct candidate.

---

## OpenType Font Ligature Exploitation for Hidden Messages (Hack The Vote 2016)

Font files with custom OpenType ligatures map visible characters to hidden glyphs. The GSUB (Glyph Substitution) table defines these mappings.

```python
from fontTools.ttLib import TTFont

def decode_font_ligatures(font_path, encoded_text):
    """Extract ligature substitution table and decode message"""
    font = TTFont(font_path)

    # Extract GSUB table for ligature substitutions
    gsub = font['GSUB']

    # Navigate to ligature lookup
    ligature_map = {}
    for lookup in gsub.table.LookupList.Lookup:
        for subtable in lookup.SubTable:
            if hasattr(subtable, 'ligatures'):
                for glyph_name, ligatures in subtable.ligatures.items():
                    for lig in ligatures:
                        # Map: input sequence -> output glyph
                        input_seq = [glyph_name] + lig.Component
                        output = lig.LigGlyph
                        ligature_map[tuple(input_seq)] = output

    print("Ligature mappings found:")
    for inp, out in ligature_map.items():
        print(f"  {inp} -> {out}")

    # Alternative: convert TTF to XML for manual analysis
    # font.saveXML('font_dump.xml')
    # Search for <LigatureSubst> entries

# Command-line approach:
# pip install fonttools
# ttx font.otf  # converts to XML
# grep -A5 'LigatureSubst' font.ttx
```

**Key insight:** Custom fonts with GSUB ligature tables create a cipher where displayed characters differ from their glyph mappings. The `fonttools` library's `ttx` command dumps the font to XML, making ligature substitution tables easily readable. Each ligature maps an input character sequence to a different output glyph.

---

## GLSL Shader VM with Self-Modifying Code (ApoorvCTF 2026)

**Pattern (Draw Me):** A WebGL2 fragment shader implements a Turing-complete VM on a 256x256 RGBA texture. The texture is both program memory and display output.

**Texture layout:**
- **Row 0:** Registers (pixel 0 = instruction pointer, pixels 1-32 = general purpose)
- **Rows 1-127:** Program memory (RGBA = opcode, arg1, arg2, arg3)
- **Rows 128-255:** VRAM (display output)

**Opcodes:** NOP(0), SET(1), ADD(2), SUB(3), XOR(4), JMP(5), JNZ(6), VRAM-write(7), STORE(8), LOAD(9). 16 steps per frame.

**Self-modifying code:** Phase 1 (decryption) uses STORE opcode to XOR-patch program memory that Phase 2 (drawing) then executes. The decryption overwrites SET instructions with correct pixel color values before the drawing code runs.

**Why GPU rendering fails:** The GPU runs all pixels in parallel per frame, but the shader tracks only ONE write target per pixel per frame. With multiple VRAM writes per frame, only the last survives — losing 75%+ of pixels. Similarly, STORE patches conflict during parallel decryption.

**Solve via sequential emulation:**
```python
from PIL import Image
import numpy as np

img = Image.open('program.png').convert('RGBA')
state = np.array(img, dtype=np.int32).copy()
regs = [0] * 33

# Phase 1: Trace decryption — apply all STORE patches sequentially
x, y = start_x, start_y
while True:
    r, g, b, a = state[y][x]
    opcode = int(r)
    if opcode == 1: regs[g] = b & 255           # SET
    elif opcode == 4: regs[g] = regs[b] ^ regs[a]  # XOR
    elif opcode == 8:                              # STORE — patches program memory
        tx, ty = regs[g], regs[b]
        state[ty][tx] = [regs[a], regs[a+1], regs[a+2], regs[a+3]]
    elif opcode == 5: break                        # JMP to drawing phase
    x += 1
    if x > 255: x, y = 0, y + 1

# Phase 2: Execute drawing code — all VRAM writes preserved
vram = np.zeros((128, 256), dtype=np.uint8)
# ... trace with opcode 7 writing to vram[ty][tx] = color
Image.fromarray(vram, mode='L').save('output.png')
```

**Key insight:** GLSL shaders are Turing-complete but GPU parallelism causes write conflicts. Self-modifying code (STORE patches) compounds the problem — patches from parallel executions overwrite each other. Sequential emulation in Python recovers the full output. The program.png file IS the bytecode.

**Detection:** WebGL/shader challenge with a PNG "program" file, challenge says "nothing renders" or output is garbled. Look for custom opcode tables in GLSL source.

---

## Instruction Counter as Cryptographic State (MetaCTF Flash 2026)

**Pattern (Who's Counting?):** Hand-written assembly binary uses a dedicated register (e.g., `r12`) as an instruction counter that increments after nearly every instruction. The counter value feeds into XOR, ROL, and multiply transformations on each input byte, making the entire transformation path-dependent on the number of instructions executed before reaching each byte.

**Identification:**
- Hand-written assembly (no compiler patterns, unusual register usage)
- A register that only increments (`inc r12` or `add r12, 1`) appearing after most instructions
- Transformations that reference this counter register (`xor rax, r12`, `rol al, cl` where `cl` derives from counter)
- Sequential byte processing loop where state carries forward

**Solving approach:**
```python
# Byte-by-byte brute force with emulation
# Since each byte's transformation depends on the counter (which depends
# on all prior instructions), state is path-dependent.

from unicorn import *
from unicorn.x86_const import *

def try_byte(known_prefix, candidate_byte):
    """Emulate binary with known prefix + candidate, check output."""
    uc = Uc(UC_ARCH_X86, UC_MODE_64)
    # Map code, stack, data segments
    uc.mem_map(CODE_BASE, 0x10000)
    uc.mem_write(CODE_BASE, binary_code)
    uc.mem_map(STACK_BASE, 0x10000)
    uc.mem_map(DATA_BASE, 0x10000)

    # Write input: known_prefix + candidate
    test_input = known_prefix + bytes([candidate_byte])
    uc.mem_write(DATA_BASE, test_input + b'\x00' * (64 - len(test_input)))

    # Set up registers (rsp, rdi pointing to input, r12 = 0)
    uc.reg_write(UC_X86_REG_RSP, STACK_BASE + 0x8000)
    uc.reg_write(UC_X86_REG_R12, 0)  # instruction counter starts at 0

    try:
        uc.emu_start(CODE_BASE + ENTRY_OFFSET, CODE_BASE + EXIT_OFFSET)
        # Read transformed output, compare against expected
        output = uc.mem_read(OUTPUT_ADDR, len(test_input))
        return output[:len(test_input)] == expected[:len(test_input)]
    except:
        return False

# Recover flag byte by byte
flag = b''
for pos in range(FLAG_LEN):
    for b in range(256):
        if try_byte(flag, b):
            flag += bytes([b])
            print(f"Position {pos}: {chr(b)} -> {flag}")
            break
```

**Key insight:** When a register acts as an instruction counter feeding into byte transformations, the transformation of byte N depends on the exact number of instructions executed while processing bytes 0 through N-1. This makes analytical inversion impractical because the counter value at each byte position depends on the execution path through all prior bytes. Byte-by-byte brute force with full emulation (Unicorn or GDB scripting) is the most reliable approach -- try all 256 values for each position, keeping the state from the correct prefix.

**When to recognize:** Binary has no standard library calls, uses unusual registers consistently, and shows a register that only increments. The transformation per byte involves operations (XOR, rotate, multiply) that reference this counter. Challenge name hints at "counting" or "instructions".

**Alternative approaches:**
- GDB scripting: set breakpoint after each byte's transformation, compare output
- Static analysis: count instructions manually to compute counter values, then invert transforms algebraically (error-prone due to counter accumulation)

**References:** MetaCTF Flash CTF 2026 "Who's Counting?"

---

## Thread Race Condition with Signed Integer Overflow (Codegate 2017)

**Pattern (Hunting):** A game binary uses thread-unsafe skill selection. The attack thread checks `skill_id <= 4` using signed comparison, then sleeps briefly before applying damage. During the sleep, switch to a different skill. The fireball skill uses `cdqe` (sign-extend EAX to RAX), converting `0xFFFFFFFF` (icesword damage) to `-1` as a signed 64-bit value. Subtracting `-1` from the boss's HP (`0x7FFFFFFFFFFFFFFF`) causes signed overflow to a negative value, killing the boss.

```python
# Race condition exploit:
# Thread A: select fireball (skill_id=2, passes <= 4 check)
# Thread A: sleeps for animation
# Main: switch to icesword (skill_id=5, damage=0xFFFFFFFF)
# Thread A: wakes, reads damage from icesword slot
# cdqe: 0xFFFFFFFF -> 0xFFFFFFFFFFFFFFFF (-1 signed)
# boss_hp -= (-1) -> boss_hp = 0x7FFFFFFFFFFFFFFF + 1 = negative -> dead

import time, threading
def race():
    select_skill(2)  # fireball - passes bounds check
    time.sleep(0.001)
    select_skill(5)  # icesword - race into damage calculation
```

**Key insight:** `cdqe` (Convert Doubleword to Quadword Extension) sign-extends 32-bit EAX into 64-bit RAX. When the attack code reads a 32-bit damage value and sign-extends it, `0xFFFFFFFF` becomes `-1`. Subtracting a negative number adds to HP, but if HP is already at `INT64_MAX`, the addition overflows to negative, killing the target.

---

## ESP32/Xtensa Firmware Reversing with ROM Symbol Map (Insomni'hack 2017)

**Pattern (Internet of Fail):** ESP32 firmware (Xtensa architecture) with no native IDA support. Use radare2 with the ESP32 ROM linker script (`esp32.rom.ld`) to map function addresses to names. Cross-reference with public ESP32 HTTP server source code to identify the password-checking logic, composed of ~20 conditional XOR functions operating on a global state variable.

```bash
# Load ESP32 firmware in radare2
r2 -a xtensa -b 32 firmware.bin

# Apply ROM symbol map from ESP-IDF
# esp32.rom.ld maps addresses like:
# 0x40000000 = ets_printf
# 0x400013A0 = cache_Read_Enable
# Load as flags: . esp32.rom.ld.r2

# Identify HTTP request handler by cross-referencing
# with esp-idf/examples/protocols/http_server
# Look for URI handler registration patterns
```

**Key insight:** ESP32's Xtensa architecture lacks mainstream RE tool support, but the ESP-IDF SDK provides ROM linker scripts mapping every ROM function address to its name. Loading these as symbols in radare2 immediately resolves hundreds of function calls. Cross-referencing with public ESP-IDF example code identifies application-level patterns (HTTP handlers, WiFi callbacks) even in stripped firmware.

---

## Batch Crackme Automation via objdump Pattern Extraction (DEF CON 2017)

Solve hundreds of identical-structure crackmes by scripting `objdump` to extract comparison values and arithmetic operations, computing keys without execution.

```bash
# Simple variant: extract CMP immediates directly
objdump -M intel -d $binary | grep -P "cmp\s+rdi" | \
    grep -oP "0x\w{1,2}" | xxd -r -p

# Complex variant: parse add/sub/cmp chains and reverse-compute
# Each binary: series of add/sub rdi,N then cmp rdi,target
# Reverse: start from target, undo operations in reverse order
python3 <<'EOF'
import subprocess, re, glob
for binary in sorted(glob.glob("crackmes/*")):
    asm = subprocess.check_output(["objdump", "-M", "intel", "-d", binary]).decode()
    ops = re.findall(r'(add|sub)\s+rdi,(0x\w+)', asm)
    target = int(re.search(r'cmp\s+rdi,(0x\w+)', asm).group(1), 16)
    # Reverse operations
    for op, val in reversed(ops):
        val = int(val, 16)
        target = (target - val) if op == 'add' else (target + val)
    print(chr(target & 0xff), end='')
EOF
```

**Key insight:** Mass crackme challenges (100s-1000s of binaries) have identical structure with per-binary constants. Script `objdump` disassembly parsing to extract immediates and arithmetic sequences, then reverse-compute the key algebraically. No execution or emulation needed.

---

## Fork + Pipe + Dead Branch Anti-Analysis (RCTF 2017)

Binary uses fork/pipe IPC where the parent writes data and exits, child reads from pipe and continues. Key validation is in a dead branch (always-false comparison) that requires binary patching to reach.

```bash
# Detection: fork() + pipe() + read()/write() in main
# The child process reads from pipe, needs to know its own PID

# Dead branch pattern:
# cmp DWORD PTR [ebp-0xc], 0x1  ; compares 0 with 1, always false
# je  real_flag_computation      ; never taken

# Patch: change comparison value from 0x1 to 0x0
# Find: 83 7d f4 01 → change to: 83 7d f4 00
python3 -c "
data = open('binary','rb').read()
data = data.replace(b'\x83\x7d\xf4\x01', b'\x83\x7d\xf4\x00')
open('binary_patched','wb').write(data)
"
```

**Key insight:** Fork+pipe creates a parent-child relationship where the parent provides data and exits. Dead branches (comparisons that always evaluate to false) hide the real validation logic. `strace` reveals the fork/pipe/read pattern; patching the comparison constant reaches the hidden code path.

---

---

## Time-Locked Binary with Date-Based Key (Hack.lu 2017)

Binary reads the system date and only executes correctly on a specific date (e.g., December 21, 2012). The date constant appears in the binary as a Unix timestamp or structured date comparison.

**Detection:** Look for comparisons against large integer constants that fall in a recognizable date range (Unix timestamps: 2012 = ~1.35B, 2017 = ~1.5B). Cultural significance helps: apocalypse dates, CTF release dates, historical events.

```bash
# Set system clock to the required date
sudo date -s "2012-12-21 00:00:00"
./binary

# Or use faketime to avoid system-wide change
LD_PRELOAD=/usr/lib/faketime/libfaketime.so.1 FAKETIME="2012-12-21 00:00:00" ./binary

# Restore system time afterward
sudo ntpdate pool.ntp.org
```

**In IDA/Ghidra:** Search for `time()` or `localtime()` calls. The struct `tm` fields to watch: `tm_year` (years since 1900), `tm_mon` (0-based), `tm_mday`.

**Key insight:** Time-based keys use culturally significant dates. Always check for date comparisons in reversed code and try setting the system clock or using faketime before attempting deeper analysis.

**References:** Hack.lu CTF 2017

---

## ARM Code in Image Pixels via UnicornJS (Hack.lu 2017)

JavaScript challenge embeds ARM bytecode in image pixel data. The image is base64-encoded in the HTML/JS source. Pixel RGBA values encode ARM instructions. A bundled UnicornJS library (ARM CPU emulator in JavaScript) extracts and executes the bytecode.

**Identification flow:**
1. Find base64 blob in JS source → decode → PNG/BMP file
2. Identify UnicornJS import (`unicorn.js`, `uc.js`, or similar) → confirms ARM emulation
3. Pixel extraction loop: RGBA bytes concatenated in raster order form the ARM instruction stream
4. Feed the extracted bytes to an ARM disassembler

```python
from PIL import Image
import capstone

img = Image.open('decoded.png').convert('RGBA')
pixels = list(img.getdata())

# Extract ARM bytecode from pixel data (4 bytes per pixel: R, G, B, A)
arm_code = bytes([channel for pixel in pixels for channel in pixel])

# Disassemble as ARM Thumb or ARM32
md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
for insn in md.disasm(arm_code, 0x0):
    print(f"0x{insn.address:04x}: {insn.mnemonic} {insn.op_str}")
```

**Key insight:** Multi-layer obfuscation: ARM code in image pixels, base64 encoded, emulated via UnicornJS at runtime. Identify the emulator library first to know which ISA to reverse — the library name reveals the architecture.

**References:** Hack.lu CTF 2017

---

## x86 16-bit MBR psadbw Constraint Solving (CSAW 2017)

Bootable MBR uses SSE2 `psadbw` (Packed Sum of Absolute Differences of Bytes) on xmm registers to validate the flag. Each iteration masks 2 input bytes, computes `psadbw` against known constants, and compares the sum to an expected value.

**`psadbw` semantics:**
```asm
psadbw xmm0, xmm1
; For each of 8 byte pairs: sum += |xmm0[i] - xmm1[i]|
; Result stored as 16-bit integer in low qword of xmm0
```

This generates sum-of-absolute-differences equations:
```text
|a[0] - k[0]| + |a[1] - k[1]| + ... + |a[7] - k[7]| = C
```

**Solution approach:**
```python
import numpy as np
from itertools import product

# For each 2-byte masked group, extract the constants and expected sum
# Equations are not purely linear (absolute value), but printable ASCII
# constrains each byte to [0x20, 0x7e], limiting brute-force space

def solve_psadbw_group(known_constants, expected_sum, printable_range=(0x20, 0x7e)):
    """Brute-force 2 unknown bytes given sum-of-abs-diff constraint."""
    solutions = []
    for a, b in product(range(*printable_range), repeat=2):
        pair = [a, b]
        sad = sum(abs(pair[i] - known_constants[i]) for i in range(len(pair)))
        if sad == expected_sum:
            solutions.append(bytes([a, b]))
    return solutions

# For ambiguous cases with multiple solutions: apply additional constraints
# (flag format prefix, character frequency, subsequent iterations)
```

**Key insight:** `psadbw` creates sum-of-absolute-difference equations — not purely linear but solvable with constrained brute-force when bytes are limited to printable ASCII. Each 2-byte group is independent, keeping the search space to 95^2 = ~9000 candidates per group.

**References:** CSAW CTF 2017

---

## TensorFlow DNN Inversion by Inverting Sigmoid Layers (N1CTF 2018)

**Pattern:** Binary implements a 5-layer deep neural network with sigmoid activation. The input (flag characters) is transformed as `1.0/char_value` before feeding into the network. Extract weights and biases from the binary, then compute the inverse layer-by-layer: apply inverse-sigmoid, subtract bias, multiply by weight matrix inverse.

```python
import numpy as np

def sigmoid_inv(x):
    return -np.log(1.0/x - 1.0)

# Invert layer by layer from output to input
v = target_output
for i in range(num_layers - 1, -1, -1):
    v = np.dot(sigmoid_inv(v) - biases[i], np.linalg.inv(weights[i]))

# Input was 1.0/char, so flag chars are the multiplicative inverse
flag = ''.join(chr(int(round(1.0 / v[j]))) for j in range(len(v)))
```

**Key insight:** Neural networks with invertible activation functions (sigmoid, tanh) and square weight matrices can be mathematically inverted layer-by-layer. Apply inverse-sigmoid, subtract bias, multiply by weight inverse. Watch for input transformations (e.g., 1/x) that must also be inverted.

**Detection:** Binary with TensorFlow or custom DNN implementation. Look for sigmoid/tanh calls, matrix multiplications, and hardcoded float arrays (weights/biases) in `.rodata`. Square weight matrices (N x N) indicate the network is invertible.

**References:** N1CTF 2018

---

## BPF Filter Analysis via JIT Compilation to x64 Assembly (Midnight Sun CTF 2018)

**Pattern:** Binary creates a raw socket with a BPF (Berkeley Packet Filter) attached. When standard BPF disassemblers fail to produce readable output, enable the kernel's BPF JIT compiler to convert BPF bytecode to native x64 assembly, then read the compiled code from dmesg.

```bash
# Enable BPF JIT compilation
echo 1 > /proc/sys/net/core/bpf_jit_enable

# Run the binary, then read JIT-compiled BPF from kernel log
dmesg | grep -A 100 "flen="

# Analysis revealed: expects DNS TXT query on UDP port 3333
dig @target -p 3333 'M4d!bKn3~l' TXT
```

**Key insight:** Linux can JIT-compile BPF filters to native x64 machine code. When standard BPF disassemblers fail or produce unreadable output, enable `bpf_jit_enable` and read the compiled assembly from dmesg. The native code is often easier to understand than BPF bytecode.

**Detection:** Binary using `setsockopt` with `SO_ATTACH_FILTER`, raw socket creation (`socket(AF_PACKET, ...)`), or embedded `struct sock_fprog` structures. BPF programs appear as arrays of `struct sock_filter` (8 bytes each: opcode, jt, jf, k).

**References:** Midnight Sun CTF 2018

---

## Single-Byte XOR ROM Deobfuscation Sweep (X-MAS CTF 2018)

**Pattern:** A large opaque blob (GBA ROM, firmware, game binary) refuses `binwalk`/`file` identification. Sweep all 256 single-byte XOR keys and re-run `file` + `strings` over the outputs; the correct key reveals a recognisable magic/signature.

```bash
for i in $(seq 0 255); do
  python3 -c "
import sys
k = $i
d = open('blob.bin','rb').read()
open(f'xor_{k}','wb').write(bytes(b^k for b in d))" 
  file "xor_$i" | grep -v data
done
strings "xor_0x42" | grep -i "POKEMON\|ELF\|MZ"
```

**Key insight:** Brute-forcing 256 XOR keys costs seconds and defeats any single-byte XOR packer. Always run this sweep before assuming a custom algorithm; look for format magics (`ELF`, `PK`, `MZ`, `PDF-`, ROM name strings) in the output.

**References:** X-MAS CTF 2018 — Unown Gift, writeup 12665

---

## WebKit Array.slice OOB CVE-2016-4622 (Codegate 2019)

**Pattern:** Challenge ships a WebKit binary with the `isJSArray(thisObj) && length == toLength(...)` bounds check commented out inside `ArrayPrototype.cpp`. `Array.prototype.slice` then reads beyond the backing store, giving OOB into adjacent JS objects. Chain Saelo's `addrof` / `fakeobj` primitives to obtain arbitrary read/write in the JS heap, then pivot to native code via a fake `StructureID`.

```javascript
let oob = new Array(8);
let victim = {a: 1};
let leak = oob.slice(-1, oob.length + 16)[0];  // reads past backing store
```

**Key insight:** Any JIT/engine challenge that patches out a safety check almost always exposes a classic browser-CVE primitive. Diff the vendored source against upstream `ArrayPrototype.cpp`, `JSArray.cpp`, and `JITOperations.cpp` for removed `if`/`assert` statements — that's the bug.

**References:** Codegate CTF 2019 — Butterfree, writeup 12902

---

## Multi-Modulus CRT Keygen with Matrix Lookup Password (Pragyan CTF 2019)

**Pattern (Super Secure Vault):** `main` asks for a numeric `key` (<= 30 digits) and checks it against five independent modular equations derived from slicing a hardcoded big number `N = "27644437104591489104652716127"` into `[27644437, 10459, 1489, 1046527, 16127]`:

```
key mod 27644437 == 213
key mod 10459    == 229
key mod 1489     == 25
key mod 1046527  == 83
key mod 16127    == 135
```

The five moduli are pairwise coprime, so the Chinese Remainder Theorem yields the smallest valid `key = 3087629750608333480917556`. After `scanf`, `func2(password, key, N)` concatenates `key + N + "80"` into `v12`, then validates each password byte against a 10000-byte lookup table:

```python
# Round 1: index = 100*(10*d0 + d1) + 10*d_mid + d_mid+1
# Round 2: index = 100*((10*d0+d1)**2 % 97) + ((10*d_mid+d_mid+1)**2 % 97)
password = b""
v8, v10 = 0, len(v12) // 2
while v8 < len(v12) // 2:
    idx = 100 * (10*v12[v8] + v12[v8+1]) + 10*v12[v10] + v12[v10+1]
    password += bytes([matrix[idx]]); v8 += 2; v10 += 2
v9, v11 = 0, len(v12) // 2
while v9 < len(v12) // 2:
    a = 10*v12[v9] + v12[v9+1]; b = 10*v12[v11] + v12[v11+1]
    password += bytes([matrix[100*(a*a % 97) + b*b % 97]])
    v9 += 2; v11 += 2
```

CRT (via `sympy.ntheory.modular.crt` or a manual `mul_inv` routine) plus `matrix` dumped from the binary reproduces the flag `pctf{R3v3rS1Ng_#s_h311_L0t_Of_Fun}`.

**Key insight:** Five coprime moduli pin the key down uniquely modulo their product (~4.7e19), which fits in a 30-digit input — pick the smallest representative instead of brute-forcing, otherwise you waste hours on 100k+ equally valid but ugly keys. The second stage looks complicated but is really a pair of fixed index-generator functions over a static table; dump the table once and both rounds become direct array reads.

**References:** Pragyan CTF 2019 — Super Secure Vault, writeup 13760

---

See also: [patterns-ctf.md](patterns-ctf.md) for Part 1, [patterns-ctf.md](#part-2) for Part 2 (multi-layer self-decrypting binary, embedded ZIP+XOR license, stack string deobfuscation, prefix hash brute-force, CVP/LLL lattice, decision tree obfuscation, GF(2^8) Gaussian elimination).
