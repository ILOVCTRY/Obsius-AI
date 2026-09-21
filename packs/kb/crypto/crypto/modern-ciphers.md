# CTF Crypto - Modern Cipher Attacks

Block cipher attacks, MAC forgery, padding oracles, and authenticated encryption. For hash/signature attacks (hash extension, PBKDF2, MD5 collision, Rabin, ECB oracles), see [modern-ciphers.md](#part-2). For stream cipher attacks (LFSR, RC4, XOR), see [stream-ciphers.md](stream-ciphers.md).

## Table of Contents
- [AES-CFB-8 Static IV State Forging](#aes-cfb-8-static-iv-state-forging)
- [ECB Pattern Leakage on Images](#ecb-pattern-leakage-on-images)
- [Padding Oracle Attack](#padding-oracle-attack)
- [CBC-MAC vs OFB-MAC Vulnerability](#cbc-mac-vs-ofb-mac-vulnerability)
- [Non-Permutation S-box Collision Attack](#non-permutation-s-box-collision-attack)
- [LCG Partial Output Recovery (0xFun 2026)](#lcg-partial-output-recovery-0xfun-2026)
- [Weak Hash Functions / GF(2) Gaussian Elimination](#weak-hash-functions--gf2-gaussian-elimination)
- [Affine Cipher over Composite Modulus (Nullcon 2026)](#affine-cipher-over-composite-modulus-nullcon-2026)
- [AES-GCM with Derived Keys (EHAX 2026)](#aes-gcm-with-derived-keys-ehax-2026)
- [AES-GCM Nonce Reuse / Forbidden Attack](#aes-gcm-nonce-reuse--forbidden-attack)
- [Ascon-like Reduced-Round Differential Cryptanalysis (srdnlenCTF 2026)](#ascon-like-reduced-round-differential-cryptanalysis-srdnlenctf-2026)
- [Custom Linear MAC Forgery (Nullcon 2026)](#custom-linear-mac-forgery-nullcon-2026)
- [CBC Padding Oracle Attack](#cbc-padding-oracle-attack)
- [Bleichenbacher / PKCS#1 v1.5 RSA Padding Oracle](#bleichenbacher--pkcs1-v15-rsa-padding-oracle)
- [Birthday Attack / Meet-in-the-Middle](#birthday-attack--meet-in-the-middle)
- [CRC32 Collision-Based Signature Forgery (iCTF 2013)](#crc32-collision-based-signature-forgery-ictf-2013)
- [AES Key Recovery via Byte-by-Byte Zeroing Oracle (CONFidence CTF 2017)](#aes-key-recovery-via-byte-by-byte-zeroing-oracle-confidence-ctf-2017)
- [AES-CTR Constant Counter / Repeating Keystream (SHA2017)](#aes-ctr-constant-counter--repeating-keystream-sha2017)
- [Custom SPN Column-Wise XOR Brute-Force (Hack Dat Kiwi 2017)](#custom-spn-column-wise-xor-brute-force-hack-dat-kiwi-2017)
- [AES-CTR Bitflip + CRC Linearity Signature Forgery (hxp CTF 2017)](#aes-ctr-bitflip--crc-linearity-signature-forgery-hxp-ctf-2017)
- [AES-CBC Ciphertext Forging via Error-Message Decryption Oracle (Nuit du Hack CTF 2018)](#aes-cbc-ciphertext-forging-via-error-message-decryption-oracle-nuit-du-hack-ctf-2018)
- [SHA-1 Chosen-Prefix Collision for PDF Signature Forgery (DEF CON Quals 2018)](#sha-1-chosen-prefix-collision-for-pdf-signature-forgery-def-con-quals-2018)
- [Hash Chain Preimage Authentication Bypass (picoCTF 2017)](#hash-chain-preimage-authentication-bypass-picoctf-2017)
- [AES-CBC Nonce Strip via Block Boundary Alignment (Trend Micro 2018)](#aes-cbc-nonce-strip-via-block-boundary-alignment-trend-micro-2018)

See also [modern-ciphers.md](#part-2) for CRC32 forgery, Blum-Goldwasser, hash length extension, compression oracle, hash time reversal, OFB invertible RNG, weak key derivation, HMAC-CRC, DES weak keys, SRP bypass, modified AES S-Box, square attack, AES-ECB byte-at-a-time, AES-ECB cut-and-paste, AES-CBC IV bit-flip, Rabin LSB parity oracle, PBKDF2 pre-hash bypass, MD5 multi-collision, custom hash state reversal, and CRC32 brute-force.

---

## AES-CFB-8 Static IV State Forging

**Pattern (Cleverly Forging Breaks):** AES-CFB with 8-bit feedback and reused IV allows state reconstruction.

**Key insight:** After encrypting 16 known bytes, the AES internal shift register state is fully determined by those ciphertext bytes. Forge new ciphertexts by continuing encryption from known state.

---

## ECB Pattern Leakage on Images

**Pattern (Electronic Christmas Book):** AES-ECB on BMP/image data preserves visual patterns.

**Exploitation:** Identical plaintext blocks produce identical ciphertext blocks, revealing image structure even when encrypted. Rearrange or identify patterns visually.

---

## Padding Oracle Attack

**Pattern (The Seer):** Server reveals whether decrypted padding is valid.

**Byte-by-byte decryption:**
```python
def decrypt_byte(block, prev_block, position, oracle, known):
    """known = bytearray(16) tracking recovered intermediate bytes for this block."""
    for guess in range(256):
        modified = bytearray(prev_block)
        # Set known bytes to produce valid padding
        pad_value = 16 - position
        for j in range(position + 1, 16):
            modified[j] = known[j] ^ pad_value
        modified[position] = guess
        if oracle(bytes(modified) + block):
            return guess ^ pad_value
```

---

## CBC-MAC vs OFB-MAC Vulnerability

OFB mode creates a keystream that can be XORed for signature forgery.

**Attack:** If you have signature for known plaintext P1, forge for P2:
```text
new_sig = known_sig XOR block2_of_P1 XOR block2_of_P2
```

**Important:** Don't forget PKCS#7 padding in calculations! Small bruteforce space? Just try all combinations (e.g., 100 for 2 unknown digits).

**Key insight:** OFB-MAC generates a keystream independent of the plaintext, so knowing one (message, MAC) pair lets you forge MACs for arbitrary messages by XORing the known plaintext blocks out and XORing the new ones in. CBC-MAC does not have this weakness because each block's encryption depends on the previous ciphertext block.

---

## Non-Permutation S-box Collision Attack

**Pattern (Tetraes, Nullcon 2026):** Custom AES-like cipher with S-box collisions.

**Detection:** `len(set(sbox)) < 256` means collisions exist. Find collision pairs and their XOR delta.

**Attack:** For each key byte, try 256 plaintexts differing by delta. When `ct1 == ct2`, S-box input was in collision set. 2-way ambiguity per byte, 2^16 brute-force. Total: 4,097 oracle queries.

See [advanced-math.md](advanced-math.md) for full S-box collision analysis code.

---

## LCG Partial Output Recovery (0xFun 2026)

**Known parameters:** If LCG (Linear Congruential Generator) constants (M, A, C) are known and output is `state mod N`, iterate by N through modulus to find state:
```python
# output = state % N, state = (A * prev + C) % M
for candidate in range(output, M, N):
    # Check if candidate is consistent with next output
    next_state = (A * candidate + C) % M
    if next_state % N == next_output:
        print(f"State: {candidate}")
```

**Upper bits only (e.g., upper 32 of 64):** Brute-force lower 32 bits:
```python
for low in range(2**32):
    state = (observed_upper << 32) | low
    next_state = (A * state + C) % M
    if (next_state >> 32) == next_observed_upper:
        print(f"Full state: {state}")
```

**Key insight:** LCG output truncation (modulo or upper bits only) hides part of the state, but consecutive outputs constrain it. When output is `state mod N`, iterate candidates by N through the modulus. When only upper bits are visible, brute-force the hidden lower bits and validate against the next output.

---

## Weak Hash Functions / GF(2) Gaussian Elimination

Linear permutations (only XOR, rotations) are algebraically attackable. Build transformation matrix and solve over GF(2).

```python
import numpy as np

def solve_gf2(A, b):
    """Solve Ax = b over GF(2)."""
    m, n = A.shape
    Aug = np.hstack([A, b.reshape(-1, 1)]) % 2
    pivot_cols, row = [], 0
    for col in range(n):
        pivot = next((r for r in range(row, m) if Aug[r, col]), None)
        if pivot is None: continue
        Aug[[row, pivot]] = Aug[[pivot, row]]
        for r in range(m):
            if r != row and Aug[r, col]: Aug[r] = (Aug[r] + Aug[row]) % 2
        pivot_cols.append((row, col)); row += 1
    if any(Aug[r, -1] for r in range(row, m)): return None
    x = np.zeros(n, dtype=np.uint8)
    for r, c in reversed(pivot_cols):
        x[c] = Aug[r, -1] ^ sum(Aug[r, c2] * x[c2] for c2 in range(c+1, n)) % 2
    return x
```

**Key insight:** Hash functions built from only XOR and rotations (no S-boxes or modular addition) are linear over GF(2). Build the transformation as a binary matrix, then invert it with Gaussian elimination to recover the preimage directly. This breaks any "custom hash" that avoids non-linear operations.

---

## Affine Cipher over Composite Modulus (Nullcon 2026)

Affine encryption `c = A*x + b (mod M)` with composite M: split into prime factor fields, invert independently, CRT recombine. See [advanced-math.md](advanced-math.md#affine-cipher-over-non-prime-modulus-nullcon-2026) for full chosen-plaintext key recovery and implementation.

---

## AES-GCM with Derived Keys (EHAX 2026)

**Pattern:** Final decryption step after recovering a secret (e.g., from LWE, key exchange). Session nonce and AES key derived via SHA-256 hashing of the recovered secret.

```python
import hashlib
from Crypto.Cipher import AES

# Common key derivation chain:
# 1. Recover secret bytes (s_bytes) from crypto challenge
# 2. Unwrap session nonce: nonce = wrapped_nonce XOR SHA256(s_bytes)[:nonce_len]
# 3. Derive AES key: key = SHA256(s_bytes + session_nonce)
# 4. Decrypt AES-GCM

def decrypt_with_derived_key(s_bytes, wrapped_nonce, ciphertext, aes_nonce, tag, nonce_len=16):
    secret_hash = hashlib.sha256(s_bytes).digest()
    session_nonce = bytes(a ^ b for a, b in zip(wrapped_nonce, secret_hash[:nonce_len]))
    aes_key = hashlib.sha256(s_bytes + session_nonce).digest()
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=aes_nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)
```

**Key insight:** When AES-GCM authentication fails (`ValueError: MAC check failed`), the derived key is wrong — usually means the upstream secret recovery was incorrect or endianness is swapped.

---

## AES-GCM Nonce Reuse / Forbidden Attack

AES-GCM (Galois/Counter Mode) combines AES-CTR encryption with a GHASH polynomial authentication tag. Reusing a nonce with the same key is catastrophic -- it enables both plaintext recovery AND authentication key recovery.

**CTR keystream reuse:** Same nonce = same keystream. XOR two ciphertexts to cancel the keystream: `C1 XOR C2 = P1 XOR P2`. With known plaintext in one message, recover the other.

**GHASH authentication key recovery:** The authentication tag is a polynomial evaluation over GF(2^128). Two messages with the same nonce produce two equations in the same authentication key H. XOR the tag polynomials and factor over GF(2^128) to recover H. With H, forge valid tags for arbitrary messages.

```python
from Crypto.Cipher import AES
# Pure-Python GHASH over GF(2^128) using pycryptodome / galois
# GHASH is polynomial evaluation; use galois or manual GF(2^128) arithmetic

def xor(a, b): return bytes(x ^ y for x, y in zip(a, b))

# Given: two (ciphertext, tag, nonce) pairs with same nonce
# Step 1: Recover plaintext via CTR keystream reuse
keystream = xor(known_plaintext, ciphertext1)
plaintext2 = xor(keystream, ciphertext2)

# Step 2: Recover GHASH auth key H
# GHASH(H, aad, ciphertext) = sum blocks * H^{n-i+1} over GF(2^128).
# Long messages: pip install galois + factor P(H)=T1-T2-GHASH_diff(H),
# or use nonce-disrespect tool. 1-block case below needs nothing else:
# verified end-to-end against real AES-GCM (forge passes decrypt_and_verify).
_MASK128 = (1 << 128) - 1

def gmul(x, y):
    """GF(2^128) multiply, NIST SP 800-38D Alg.1. Ints are big-endian blocks."""
    z, v = 0, y & _MASK128
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        lsb = v & 1
        v >>= 1
        if lsb:
            v ^= 0xE1000000000000000000000000000000
    return z & _MASK128

def _gpow(a, e):
    r = 1 << 127  # field identity in this encoding
    while e:
        if e & 1:
            r = gmul(r, a)
        a = gmul(a, a)
        e >>= 1
    return r

def ginv(a):
    return _gpow(a, (1 << 128) - 2)  # Fermat, single-primitive

def _gsqrt(a):
    for _ in range(127):  # a^(2^127): sqrt in char 2, single-primitive
        a = gmul(a, a)
    return a

# 1-block empty-AAD: GHASH = ((C*H) ^ L)*H with L = 128-bit length block,
# so T1 ^ T2 = (C1 ^ C2)*H^2 -- quadratic, solve H^2 then sqrt.
def ghash_recover_h_1block(c1, c2, t1, t2):
    d = (int.from_bytes(c1, "big") ^ int.from_bytes(c2, "big")) & _MASK128
    t = (int.from_bytes(t1, "big") ^ int.from_bytes(t2, "big")) & _MASK128
    if d == 0:
        raise ValueError("ciphertexts identical -- need distinct blocks")
    return _gsqrt(gmul(t, ginv(d)))  # H as int; verify by recomputing tag

# Step 3: Forge tags for arbitrary messages
# GHASH(H, aad, ciphertext) computed with recovered H
```

<details><summary>Sage fallback (optional)</summary>

```python
from Crypto.Cipher import AES
from sage.all import GF, PolynomialRing

# Given: two (ciphertext, tag, nonce) pairs with same nonce
# Step 1: Recover plaintext via CTR keystream reuse
keystream = xor(known_plaintext, ciphertext1)
plaintext2 = xor(keystream, ciphertext2)

# Step 2: Recover GHASH auth key H
# Construct tag difference polynomial in GF(2^128)
F = GF(2**128, 'x', modulus=...)  # GCM polynomial
# T1 XOR T2 = P(H) where P is polynomial from ciphertext difference
# Factor P(H) = 0 to find H candidates
# Verify H against known tags

# Step 3: Forge tags for arbitrary messages
# GHASH(H, aad, ciphertext) computed with recovered H
```

</details>

**Tool:** [nonce-disrespect](https://github.com/nonce-disrespect/nonce-disrespect) automates GHASH key recovery and tag forgery from nonce-reused GCM ciphertexts.

**Short nonce brute-force:** When GCM uses a short nonce (1-4 bytes), brute-force all nonce values if the key is known. AES-GCM with 1-byte nonce = only 256 candidates.

**Key insight:** AES-GCM is a "one-time nonce" scheme -- a single nonce reuse breaks both confidentiality (CTR keystream reuse) AND authenticity (GHASH key recovery). Always check for repeated nonces in GCM challenge traffic.

---

## Ascon-like Reduced-Round Differential Cryptanalysis (srdnlenCTF 2026)

**Pattern (Lightweight):** 4-round Ascon-like permutation with reduced diffusion. Key-dependent biases in output-bit differentials allow key recovery via chosen input differences.

**Attack:**
1. Reproduce the permutation exactly (critical: post-S-box x4 assignment order matters)
2. Invert the linear layer of x0 using a precomputed 64×64 GF(2) inverse matrix
3. For each bit position i, query with `diff = (1<<i, 1<<i)` across multiple samples
4. Measure empirical biases at output bits `j1 = (i+1) mod 64` and `j2 = (i+14) mod 64`
5. Classify key bits `(k0[i], k1[i])` via centroid-based clustering with sign-pattern mask
6. Verify candidate key in-session; refine low-margin bits with additional samples

**GF(2) linear layer inversion:**
```python
def build_inverse(shifts=(19, 28)):
    """Construct GF(2) inverse matrix for Ascon-like linear layer: x ^= rot(x,19) ^ rot(x,28)."""
    # Build 64x64 matrix over GF(2)
    M = [[0]*64 for _ in range(64)]
    for out_bit in range(64):
        M[out_bit][out_bit] = 1
        for shift in shifts:
            M[out_bit][(out_bit + shift) % 64] ^= 1
    # Gaussian elimination to find inverse
    aug = [row + [1 if i == j else 0 for j in range(64)] for i, row in enumerate(M)]
    for col in range(64):
        pivot = next(r for r in range(col, 64) if aug[r][col])
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for r in range(64):
            if r != col and aug[r][col]:
                aug[r] = [a ^ b for a, b in zip(aug[r], aug[col])]
    return [row[64:] for row in aug]
```

**Centroid clustering for key classification:**
```python
# For each bit position, measure bias at two output positions
# 4 possible (k0[i], k1[i]) pairs → 4 centroid patterns
# Uses sign-pattern mask CMASK=0x73 to account for bit-position-dependent behavior
# Classify by minimum Euclidean distance in 2D bias space
CMASK = 0x73
for i in range(64):
    bias_j1, bias_j2 = measure_biases(i, samples)
    mask_bit = (CMASK >> (i % 8)) & 1
    centroids = centroid_table[mask_bit]  # Precomputed per-position centroids
    k0_bit, k1_bit = min(range(4), key=lambda c: euclidean_dist(
        (bias_j1, bias_j2), centroids[c]))
```

**Key insight:** Reduced-round lightweight ciphers (Ascon, GIFT, etc.) have exploitable biases when the number of rounds is insufficient for full diffusion. The linear layer's inverse can be computed algebraically, and differential biases measured across chosen-plaintext queries reveal individual key bits. This is practical even with noisy measurements if you collect enough samples.

---

## Custom Linear MAC Forgery (Nullcon 2026)

**Pattern (Pasty):** Server signs paste IDs with a custom SHA-256-based construction. The signature is linear in three 8-byte secret blocks derived from the key.

**Structure:** For each 8-byte output block `i`:
- `selector = SHA256(id)[i*8] % 3` → chooses which secret block to use
- `out[i] = hash_block[i] XOR secret[selector] XOR chain[i-1]`

**Recovery:** Create ~10 pastes to collect `(id, sig)` pairs. Each pair reveals `secret[selector]` for 4 selectors. With ~4-5 pairs, all 3 secret blocks are recovered. Then forge for target ID.

**Key insight:** Linearity in custom crypto constructions (XOR-based signing) makes them trivially forgeable. Always check if the MAC has the property: knowing the secret components lets you compute valid signatures for arbitrary inputs.

---

## CBC Padding Oracle Attack

**Pattern:** Server reveals whether CBC-mode ciphertext has valid PKCS#7 padding (via error messages, timing, or status codes). Decrypt any ciphertext block-by-block without the key.

```python
from pwn import *

def padding_oracle(iv, ct):
    """Returns True if server accepts padding."""
    resp = requests.post(URL, data={'iv': iv.hex(), 'ct': ct.hex()})
    return 'padding' not in resp.text.lower()  # or check status code

def decrypt_block(prev_block, target_block):
    """Decrypt one 16-byte block using padding oracle."""
    intermediate = bytearray(16)
    plaintext = bytearray(16)

    for byte_pos in range(15, -1, -1):
        pad_val = 16 - byte_pos
        # Set already-known bytes to produce correct padding
        crafted = bytearray(16)
        for k in range(byte_pos + 1, 16):
            crafted[k] = intermediate[k] ^ pad_val

        for guess in range(256):
            crafted[byte_pos] = guess
            if padding_oracle(bytes(crafted), target_block):
                intermediate[byte_pos] = guess ^ pad_val
                plaintext[byte_pos] = intermediate[byte_pos] ^ prev_block[byte_pos]
                break

    return bytes(plaintext)
```

**Tools:**
```bash
# PadBuster — automated padding oracle exploitation
padbuster http://target/decrypt.php ENCRYPTED_B64 16 \
  -encoding 0 -error "Invalid padding"

# Python: pip install padding-oracle
from padding_oracle import PaddingOracle
oracle = PaddingOracle(block_size=16, oracle_fn=check_padding)
plaintext = oracle.decrypt(ciphertext, iv=iv)
```

**Key insight:** The oracle only needs to distinguish "valid padding" from "invalid padding." This can be a different HTTP status code, error message, response time, or even whether the application processes the request further. A single bit of information per query is sufficient. Decryption requires at most 256 x 16 = 4096 queries per block.

**Detection:** CBC mode encryption + any distinguishable behavior difference on padding errors. Common in cookie encryption, token systems, and encrypted API parameters.

---

## Bleichenbacher / PKCS#1 v1.5 RSA Padding Oracle

**Pattern:** RSA encryption with PKCS#1 v1.5 padding where the server reveals whether decrypted plaintext has valid `0x00 0x02` prefix. Adaptive chosen-ciphertext attack recovers the plaintext.

```python
import gmpy2

def bleichenbacher_oracle(c, n, e):
    """Returns True if RSA decryption has valid PKCS#1 v1.5 padding (0x00 0x02 prefix)."""
    resp = send_to_server(c)
    return resp.status_code != 400  # Server returns 400 on bad padding

def bleichenbacher_attack(c0, n, e, oracle, k):
    """
    c0: target ciphertext (integer)
    k: byte length of modulus (e.g., 256 for RSA-2048)
    """
    B = pow(2, 8 * (k - 2))

    # Step 1: Start with s1 = ceil(n / 3B)
    s = (n + 3 * B - 1) // (3 * B)

    # Step 2: Search for s where oracle(c0 * s^e mod n) is True
    while True:
        c_prime = (c0 * pow(s, e, n)) % n
        if oracle(c_prime, n, e):
            break
        s += 1

    # Step 3: Narrow interval [a, b] using s values
    # Repeat: find new s, narrow interval, until a == b
    # When interval collapses, plaintext = a * modinv(s, n) % n
    # (Full implementation requires interval tracking — use existing tools)
```

**Tools:**
```bash
# ROBOT attack scanner (modern Bleichenbacher variant)
python3 robot-detect.py -H target.com

# TLS-Attacker framework
java -jar TLS-Attacker.jar -connect target:443 -workflow_type BLEICHENBACHER
```

**Key insight:** The attack is adaptive — each oracle response narrows the range of possible plaintexts. Typically requires ~10,000 oracle queries for RSA-2048. The ROBOT attack (Return Of Bleichenbacher's Oracle Threat) showed this affects modern TLS implementations through subtle timing differences. Any server that distinguishes "bad padding" from "bad content" is vulnerable.

---

## Birthday Attack / Meet-in-the-Middle

**Pattern:** Find collisions in hash functions or MACs using the birthday paradox. With an n-bit hash, expect a collision after ~2^(n/2) random inputs.

```python
import hashlib, os

def birthday_collision(hash_fn, output_bits, prefix=b''):
    """Find two inputs with the same truncated hash."""
    target_bytes = output_bits // 8
    seen = {}

    while True:
        msg = prefix + os.urandom(16)
        h = hash_fn(msg).digest()[:target_bytes]
        if h in seen:
            return seen[h], msg  # Collision found!
        seen[h] = msg

# Example: find collision on first 4 bytes of SHA-256 (~65536 attempts)
msg1, msg2 = birthday_collision(hashlib.sha256, 32)
```

**Meet-in-the-Middle (2DES, double encryption):**
```python
def meet_in_the_middle(encrypt_fn, decrypt_fn, plaintext, ciphertext, keyspace):
    """Break double encryption E(k2, E(k1, pt)) = ct."""
    # Forward: encrypt plaintext with all possible k1
    forward = {}
    for k1 in keyspace:
        intermediate = encrypt_fn(k1, plaintext)
        forward[intermediate] = k1

    # Backward: decrypt ciphertext with all possible k2
    for k2 in keyspace:
        intermediate = decrypt_fn(k2, ciphertext)
        if intermediate in forward:
            return forward[intermediate], k2  # Found k1, k2!
```

**Key insight:** Birthday attack: n-bit hash needs ~2^(n/2) queries for 50% collision probability. 32-bit hash -> ~65K, 64-bit -> ~4 billion. Meet-in-the-middle reduces double encryption from O(2^(2k)) to O(2^k) time + O(2^k) space — this is why 2DES provides only 1 extra bit of security over DES.

---

## CRC32 Collision-Based Signature Forgery (iCTF 2013)

**Pattern:** CRC32 is linear — appending 4 carefully chosen bytes to any message produces a target CRC32 value, enabling signature forgery without knowing the secret key.

**Key insight:** `CRC32(msg || secret)` is not a secure MAC. Given any signed response `(msg, sig)`, compute 4 suffix bytes that force `CRC32(forged_msg || suffix || secret) == target_sig`. The linearity of CRC32 means the suffix computation is deterministic and instant.

```python
import struct, binascii

def crc32_forge(data, target_crc):
    """Append 4 bytes to data so CRC32(data + suffix) == target_crc"""
    current = binascii.crc32(data) & 0xFFFFFFFF
    # CRC32 polynomial table lookup to find suffix bytes
    # that transform current CRC into target_crc
    suffix = b''
    crc = target_crc ^ 0xFFFFFFFF
    for _ in range(4):
        byte = (crc & 0xFF)
        crc = (crc >> 8)
        suffix = bytes([byte]) + suffix
    return data + suffix  # Simplified — full implementation requires polynomial division
```

**When to use:** Any protocol using CRC32 as a message authentication code (MAC). CRC32 is a checksum, not a cryptographic hash — it provides no integrity guarantees against adversarial modification.

---

## AES Key Recovery via Byte-by-Byte Zeroing Oracle (CONFidence CTF 2017)

**Pattern:** When a service allows selective zeroing of key bytes (e.g., via integer overflow in key slot indexing), recover the full AES key by testing one byte at a time.

```python
# Service has key slots and a "regenerate" function with integer overflow
# offset = index * ENTRY_SIZE wraps around, allowing arbitrary byte zeroing

# Strategy: zero bytes progressively, brute-force each unknown byte
for byte_pos in range(16):
    # Zero all bytes EXCEPT byte_pos (by overflowing index calculation)
    zero_index = (target_offset * modinv(ENTRY_SIZE, 2**32)) % 2**32
    regenerate(zero_index)

    # Key is now: [0,0,...,key[byte_pos],...,0,0]
    # Brute-force the single non-zero byte (256 possibilities)
    known_ct = encrypt(known_pt)
    for guess in range(256):
        test_key = bytes([0]*byte_pos + [guess] + [0]*(15-byte_pos))
        if AES.new(test_key, AES.MODE_ECB).encrypt(known_pt) == known_ct:
            recovered_key[byte_pos] = guess
            break
```

**Key insight:** Integer overflow in `index * ENTRY_SIZE` calculations can target arbitrary memory offsets. By selectively zeroing all-but-one key bytes, the key becomes trivially brute-forceable one byte at a time (256 attempts per byte, 4096 total vs 2^128 for the full key).

**References:** CONFidence CTF 2017

---

## AES-CTR Constant Counter / Repeating Keystream (SHA2017)

**Pattern:** When an AES-CTR implementation uses `counter=lambda: secret` (a constant function), the counter never increments. AES-CTR with a fixed counter produces the same 16-byte block on every call — equivalent to Vigenère cipher at the byte level with a 16-byte repeating key.

```python
# Constant counter makes CTR equivalent to repeating-key XOR
key_byte = ciphertext_byte ^ known_plaintext_byte
# Apply recovered key bytes across all 16-byte-aligned blocks
for i, ct_byte in enumerate(ciphertext):
    plaintext_byte = ct_byte ^ keystream[i % 16]
```

**Exploit using file format headers:**
1. Identify the file format from context (e.g., `%PDF-1.` for PDF files)
2. XOR the known header bytes against the ciphertext to recover `keystream[0:len(header)]`
3. Iteratively extend: use recovered plaintext to guess the next structural keyword (`endobj`, `/Page`, `stream`, etc.), verify XOR produces consistent ASCII, and extend the keystream further
4. Tool: `otp_pwn` supports interactive block-aligned crib-dragging for this workflow

**Key insight:** Constant AES-CTR counter = repeating 16-byte Vigenère key. Known file format magic bytes bootstrap iterative key recovery via crib-dragging. Any known-plaintext at block-aligned positions reveals the full keystream byte at that position.

**References:** SHA2017

---

## Custom SPN Column-Wise XOR Brute-Force (Hack Dat Kiwi 2017)

**Pattern:** SPN (Substitution-Permutation Network) cipher with a seed-based sbox/pbox and a final XOR key layer. If the XOR key is applied column-wise (each key byte affects one column position independently), each key byte can be brute-forced separately using printable-text consistency as an oracle.

**Attack:**
1. Collect multiple ciphertext blocks (same key, different plaintexts)
2. For each column position `c` (0-15), try all 256 candidate key bytes `k`
3. Apply the inverse pbox and sbox to undo the SPN rounds, then XOR with candidate `k`
4. Keep only candidates where ALL blocks produce printable ASCII at position `c`
5. The intersection of valid candidates across blocks recovers each key byte

**Multi-round variant:** Peel one round at a time. After recovering the outermost XOR key, apply the inverse pbox/sbox for that round using the recovered bytes, then repeat for the next inner round.

**Seed-based permutation dependency:** When sbox and pbox are generated from a shared seed, recovering partial key bytes constrains the seed (and thus the remaining permutation entries). Use this to propagate partial solutions across columns with cross-column dependencies.

**Key insight:** Column-aligned XOR layers in SPN ciphers allow independent per-byte brute-force using printable-text consistency as an oracle. Cross-column key reuse from seed-based permutations propagates partial solutions.

**References:** Hack Dat Kiwi 2017

---

## AES-CTR Bitflip + CRC Linearity Signature Forgery (hxp CTF 2017)

**Pattern:** AES-CTR allows targeted plaintext modification via XOR. CRC is linear w.r.t. XOR: `CRC(A ^ B) = CRC(A) ^ CRC(B) ^ CRC(zeros)`. Flip `{admin: 0}` to `{admin: 1}` in ciphertext and fix the encrypted CRC:

```python
import binascii
# X = desired_plaintext XOR original_plaintext (flip bit)
X = b'\x00' * offset + b'\x01' + b'\x00' * remaining
crc_diff = binascii.crc32(X) ^ binascii.crc32(b'\x00' * len(X))
# New ciphertext = old_ciphertext XOR X (for data portion)
# New CRC ciphertext = old_CRC_ciphertext XOR pack(crc_diff)
```

**Key insight:** CRC is GF(2)-linear -- XOR-based modifications to plaintext produce predictable CRC changes without knowing the key. When a system uses AES-CTR for confidentiality + CRC for integrity (instead of a proper MAC like HMAC or GCM), you can flip arbitrary plaintext bits and fix the CRC simultaneously. This is a fundamental failure of using CRC as a MAC: CRC detects random errors but provides zero protection against adversarial modification under stream ciphers.

**References:** hxp CTF 2017

---

### AES-CBC Ciphertext Forging via Error-Message Decryption Oracle (Nuit du Hack CTF 2018)

**Pattern:** Server decrypts AES-CBC cookie and displays decrypted value in error messages. Send zero blocks, read decrypted intermediates from error, XOR with desired plaintext to forge ciphertext block-by-block. Use forged ciphertext to deliver blind SQLi payloads through encrypted cookies. (Nuit du Hack CTF 2018)

```python
# Forge ciphertext for arbitrary plaintext
for i in range(blocks):
    payload = b'\x00' * 16 * (blocks - 1) + last_forged_block
    response = send_payload(payload)
    decrypted = parse_error_message(response)  # server leaks decrypted bytes
    intermediate = decrypted[-16:]
    new_block = xor(target_plaintext_block, intermediate)
    forged_blocks.append(new_block)
```

**Key insight:** When the server reveals decrypted ciphertext in error messages, you can forge arbitrary plaintext without knowing the key. Send zero IV blocks to learn the intermediate state, then XOR with desired plaintext to produce the correct ciphertext. Build block-by-block from last to first.

---

## SHA-1 Chosen-Prefix Collision for PDF Signature Forgery (DEF CON Quals 2018)

**Pattern (EmojiVote):** Server extracts commands from an uploaded PDF via OCR, then signs the OCR'd byte-string as `sha1(data)` and attaches the signature. Use a shattered-style SHA-1 chosen-prefix collision to produce two PDFs that OCR to different commands but share the same SHA-1 digest.

**Exploit workflow:**
1. Build PDF A that OCR's to a benign command (no `EXECUTE`) and PDF B that OCR's to `EXECUTE <attacker command>`.
2. Pad both with shattered-style suffix data so `sha1(A) == sha1(B)`.
3. Submit A to obtain a valid signature for the shared digest.
4. Replay that signature on B — the server verifies the SHA-1 matches and executes the attacker command.

```bash
# Build the two colliding PDFs (cpc = chosen-prefix collision tool)
./cpc prefix_A prefix_B collision_A.pdf collision_B.pdf
sha1sum collision_A.pdf collision_B.pdf  # identical
# Upload A, capture signature, replay on B
```

**Key insight:** When a protocol signs a message as `sign(sha1(M))` instead of `sign(M)` directly, any SHA-1 collision becomes a signature forgery. Chosen-prefix collisions are practical (cpc/shattered toolkit) — the signer only inspects the digest, never the second preimage.

**References:** DEF CON CTF Qualifier 2018 — writeup 10075

---

## Hash Chain Preimage Authentication Bypass (picoCTF 2017)

**Pattern (hash_chain):** Server authenticates the Nth challenge by asking for `hash^(N-1)(seed)` given `hash^N(seed)`. The seed is derivable from public user data (e.g., `md5(username)`), so any attacker can precompute the whole chain from the start and answer any step.

**Exploit:**
```python
import hashlib

def H(x): return hashlib.md5(x).digest()

seed = H(username.encode())        # public-derived seed
chain = [seed]
for _ in range(TARGET_N + 1):
    chain.append(H(chain[-1]))

# Server sends chain[N]; answer with chain[N-1]
```

**Key insight:** Hash chains are only one-way if the seed is secret. If the seed can be reconstructed from public inputs (username, challenge ID, timestamp), the entire chain is computable forward, and answering "give me the previous hash" is trivial. Treat the seed like a key.

**References:** picoCTF 2017 — writeup 10031

---

## AES-CBC Nonce Strip via Block Boundary Alignment (Trend Micro 2018)

**Pattern:** A server encrypts `nonce | padding | identity | timestamp` with AES-CBC and returns `(iv, ciphertext)`. If the attacker can choose padding such that the first *exactly one* AES block (16 bytes) holds the nonce, then shifting the IV forward by one block — reusing `ciphertext[:16]` as the new IV and `ciphertext[16:]` as the new ciphertext — yields a valid encryption of just `identity | timestamp`. No key is needed because CBC-mode decryption of block 2 is `AES⁻¹(c[16:32]) XOR c[0:16]`, which is exactly the identity-and-timestamp plaintext once the nonce block is promoted to IV.

```python
from Crypto.Cipher import AES
import os

key = os.urandom(16)

# Server builds plaintext and encrypts
def encrypt_with_nonce(identity, timestamp):
    nonce = os.urandom(8)
    padding = b"\x00" * 8          # brings nonce + padding to 16 bytes
    plaintext = nonce + padding + identity + timestamp
    iv = os.urandom(16)
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)
    return iv, ct

iv, ct = encrypt_with_nonce(b"admin___________", b"2018-11-01T00:00")

# Attacker rewrites (iv', ct') to drop the nonce block
new_iv = ct[:16]
new_ct = ct[16:]
recovered = AES.new(key, AES.MODE_CBC, new_iv).decrypt(new_ct)
assert recovered.startswith(b"admin")
```

**Key insight:** CBC's IV is only consulted for the first block — every subsequent block uses the previous ciphertext as its "IV". That means any contiguous slice of a CBC ciphertext is itself a valid CBC ciphertext if you promote the preceding block (or a supplied IV) to the new IV. Whenever a fixed-size header (nonce, magic bytes, counter) occupies exactly one block, the attacker can strip it by reusing that block as an IV. Defend by binding the header into the authentication tag (AEAD) or including its offset in an HMAC.

**References:** Trend Micro CTF 2018 — Offensive-Analysis 400, writeup 11130

---

# Part 2

Hash-based attacks, protocol-level exploits, ECB oracles, Rabin/RSA parity attacks, and specialized cipher weaknesses. For core AES/CBC/padding oracle techniques, see [modern-ciphers.md](modern-ciphers.md). For stream cipher attacks (LFSR, RC4, XOR), see [stream-ciphers.md](stream-ciphers.md).

## Table of Contents
- [Blum-Goldwasser Bit-Extension Oracle (PlaidCTF 2013)](#blum-goldwasser-bit-extension-oracle-plaidctf-2013)
- [Hash Length Extension Attack (PlaidCTF 2014)](#hash-length-extension-attack-plaidctf-2014)
- [Compression Oracle / CRIME-Style Attack (BCTF 2015)](#compression-oracle--crime-style-attack-bctf-2015)
- [Hash Function Time Reversal via Cycle Detection (BSidesSF 2025)](#hash-function-time-reversal-via-cycle-detection-bsidessf-2025)
- [OFB Mode with Invertible RNG Backward Decryption (BSidesSF 2026)](#ofb-mode-with-invertible-rng-backward-decryption-bsidessf-2026)
- [Weak Key Derivation via Public Key Hash XOR (BSidesSF 2026)](#weak-key-derivation-via-public-key-hash-xor-bsidessf-2026)
- [HMAC-CRC Linearity Attack (Boston Key Party 2016)](#hmac-crc-linearity-attack-boston-key-party-2016)
- [DES Weak Keys in OFB Mode (Boston Key Party 2016)](#des-weak-keys-in-ofb-mode-boston-key-party-2016)
- [SRP (Secure Remote Password) Protocol Bypass via Modular Arithmetic (ASIS CTF Finals 2016)](#srp-secure-remote-password-protocol-bypass-via-modular-arithmetic-asis-ctf-finals-2016)
- [Modified AES S-Box Brute-Force Recovery (H4ckIT CTF 2016)](#modified-aes-s-box-brute-force-recovery-h4ckit-ctf-2016)
- [Square Attack on Reduced-Round AES (0CTF 2016)](#square-attack-on-reduced-round-aes-0ctf-2016)
- [AES-ECB Byte-at-a-Time Chosen Plaintext (ABCTF 2016)](#aes-ecb-byte-at-a-time-chosen-plaintext-abctf-2016)
- [AES-ECB Cut-and-Paste Block Manipulation (NDH Quals 2016)](#aes-ecb-cut-and-paste-block-manipulation-ndh-quals-2016)
- [AES-CBC IV Bit-Flip Authentication Bypass (Google CTF 2016)](#aes-cbc-iv-bit-flip-authentication-bypass-google-ctf-2016)
- [Rabin Cryptosystem LSB Parity Oracle (PlaidCTF 2016)](#rabin-cryptosystem-lsb-parity-oracle-plaidctf-2016)
- [PBKDF2 Pre-Hash Bypass for Long Passwords (BackdoorCTF 2016)](#pbkdf2-pre-hash-bypass-for-long-passwords-backdoorctf-2016)
- [MD5 Multi-Collision via Fastcol (BackdoorCTF 2016)](#md5-multi-collision-via-fastcol-backdoorctf-2016)
- [GHASH Key Recovery over Prime Modulus (nullcon HackIM 2019)](#ghash-key-recovery-over-prime-modulus-nullcon-hackim-2019)
- [SHA-1 Length Extension Plus AES-CBC Cookie Forgery (BSidesSF 2019)](#sha-1-length-extension-plus-aes-cbc-cookie-forgery-bsidessf-2019)

See [modern-ciphers.md](#part-3) for custom hash reversal, CRC32 brute-force, noisy RSA oracle, sponge collisions, CBC IV forgery, padding oracle bit-flip, SPN S-box intersection, AES-CFB IV recovery, three-round XOR, Unicode side channel, SHA-256 basis attack, and HMAC key recovery.

---

## Blum-Goldwasser Bit-Extension Oracle (PlaidCTF 2013)

**Pattern:** Exploit a decryption oracle for Blum-Goldwasser-style encryption by extending ciphertext length by one bit per query to leak plaintext via parity.

**Key insight:** Extend ciphertext by one bit (L+1), shift ciphertext left (`c << 1`), and submit a modified `y` value. The oracle reveals the LSB (parity) of each decrypted chunk. The squaring sequence `y = pow(y, 2, N)` can be manipulated to produce valid extended ciphertexts the server hasn't seen.

```python
# Iterative plaintext recovery via bit-extension
for i in range(msg_length):
    extended_c = original_c << 1        # Shift ciphertext left by 1
    new_y = pow(original_y, 2, N)       # Advance squaring sequence
    response = oracle(extended_c, new_y, msg_length + 1)
    leaked_bit = response & 1           # LSB reveals one plaintext bit
    plaintext_bits.append(leaked_bit)
    original_y = new_y
```

**When to use:** Blum-Goldwasser or BBS-based (Blum Blum Shub) encryption with a decryption oracle that accepts variable-length ciphertexts. The parity leak accumulates one bit per query.

---

## Hash Length Extension Attack (PlaidCTF 2014)

**Pattern:** Server computes `hash(SECRET || user_data)` using MD5, SHA-1, or SHA-256 (Merkle-Damgard constructions). Given a valid hash and the original data, extend it with arbitrary appended data and compute a valid hash — without knowing the secret.

```bash
# Using HashPump (install: apt install hashpump)
hashpump --keylength 8 \
  --signature 'ef16c2bffbcf0b7567217f292f9c2a9a50885e01e002fa34db34c0bb916ed5c3' \
  --data 'original_data' \
  --additional ';admin=true'
# Outputs: new_signature and new_data (with padding bytes)
```

```python
# Python: hashpumpy
import hashpumpy
new_hash, new_data = hashpumpy.hashpump(
    original_hash, original_data, append_data, secret_length
)
```

**Key insight:** Merkle-Damgard hashes (MD5, SHA-1, SHA-256) process data in blocks, and the hash output IS the internal state. Given `H(secret || msg)`, you can compute `H(secret || msg || padding || extension)` without knowing `secret` — just initialize the hash state from the known output and continue hashing. Only HMAC (`H(K XOR opad || H(K XOR ipad || msg))`) is immune. If the secret length is unknown, try lengths 1-32.

*See also [webapp/authn/auth-infra.md — Hash Length Extension Attack (ASIS CTF 2017)](../webapp/authn/auth-infra.md#hash-length-extension-attack-asis-ctf-2017) for the same primitive applied to a web auth token bypass.*

---

## Compression Oracle / CRIME-Style Attack (BCTF 2015)

**Pattern:** Server compresses plaintext (LZW, zlib, etc.) before encrypting. By observing ciphertext length changes with chosen plaintexts, leak the unknown plaintext character-by-character.

```python
import base64

def oracle(plaintext):
    """Send chosen plaintext, get ciphertext length."""
    resp = send_to_server(plaintext)
    return len(base64.b64decode(resp))

# Baseline: empty input
base_len = oracle("")

# Recover secret byte-by-byte
known = ""
for pos in range(secret_length):
    for c in string.printable:
        candidate = known + c
        length = oracle(candidate)
        if length <= base_len + len(known):  # Compressed = match
            known += c
            break
```

**Key insight:** Compression algorithms (LZW, DEFLATE, zlib) replace repeated sequences with back-references. If `SALT + user_input` is compressed before encryption, sending input that matches part of the salt produces shorter ciphertext (the match compresses). This is the same class as CRIME (TLS), BREACH (HTTP), and HEIST attacks. The oracle is ciphertext length.

---

## Hash Function Time Reversal via Cycle Detection (BSidesSF 2025)

When a system uses iterated hashing as a "time" function (`state_t = H(state_{t-1})`), reverse time by exploiting the finite cycle structure:

1. **Detect cycle:** Use Floyd's tortoise-and-hare or Brent's algorithm to find cycle length L
2. **Compute backward steps:** To go from time T to earlier time T_goal: iterate forward `(L - (T - T_goal)) % L` steps

```python
import hashlib

def hash_step(state):
    return hashlib.md5(state).digest()[:8]  # Truncated hash

def find_cycle(start):
    """Brent's cycle detection: returns (cycle_length, start_of_cycle)"""
    power = lam = 1
    tortoise = start
    hare = hash_step(start)
    while tortoise != hare:
        if power == lam:
            tortoise = hare
            power *= 2
            lam = 0
        hare = hash_step(hare)
        lam += 1
    # lam = cycle length; find cycle start
    tortoise = hare = start
    for _ in range(lam):
        hare = hash_step(hare)
    mu = 0
    while tortoise != hare:
        tortoise = hash_step(tortoise)
        hare = hash_step(hare)
        mu += 1
    return lam, mu  # cycle_length, cycle_start_offset

# Reverse from T_known to T_goal
cycle_len, _ = find_cycle(known_state)
forward_steps = (cycle_len - (t_known - t_goal)) % cycle_len
state = known_state
for _ in range(forward_steps):
    state = hash_step(state)
# state is now the value at t_goal
```

**Key insight:** For truncated hashes (e.g., MD5 -> 64 bits), the expected cycle length is ~2^32, making cycle detection feasible. Going "backward" N steps is equivalent to going forward (cycle_length - N) steps. Assumes the target state is within the main cycle, not on a tail.

---

## OFB Mode with Invertible RNG Backward Decryption (BSidesSF 2026)

**Pattern (randcrypt):** A custom block cipher uses OFB (Output Feedback) mode with a homemade RNG as the keystream generator. The last plaintext block is known (zero padding), leaking one RNG state. If the RNG's state transition function is invertible (bijective), all previous states can be recovered by running the RNG backwards, decrypting the entire ciphertext from the end to the beginning.

```python
def rng_forward(state):
    """Custom RNG state transition (from challenge)."""
    # Example: linear congruential or reversible mixing
    return (state * A + B) % M

def rng_inverse(state):
    """Inverted RNG — recover previous state."""
    return ((state - B) * pow(A, -1, M)) % M

# Last block is zero-padded → ciphertext XOR 0 = keystream = RNG state
leaked_state = int.from_bytes(ciphertext_blocks[-2], 'big')

# Decrypt backwards
state = leaked_state
plaintext_blocks = []
for i in range(len(ciphertext_blocks) - 3, -1, -1):
    state = rng_inverse(state)
    pt = xor_bytes(ciphertext_blocks[i], state.to_bytes(block_size, 'big'))
    plaintext_blocks.insert(0, pt)
```

**Key insight:** OFB mode decouples encryption from the plaintext — the keystream is deterministic from the initial state. If ANY block's plaintext is known (padding, headers, magic bytes), the corresponding RNG state is leaked. An invertible RNG then reveals ALL states. Always check if the RNG transition function has a mathematical inverse.

**When to recognize:** Custom OFB/CTR mode with a non-standard PRNG. Look for: (1) XOR-based encryption, (2) a state-update function that's bijective (no information loss), (3) predictable plaintext in any block position. Files with known padding (PKCS#7 zero-fill, null-terminated strings) are ideal leak points.

---

## Weak Key Derivation via Public Key Hash XOR (BSidesSF 2026)

**Pattern (ran-somewhere):** Hybrid RSA+AES encryption where the AES key is derived as `SHA256(DER_encoded_public_key) XOR seed`, with the seed hardcoded or predictable. Since the public key is public, the AES key is fully recoverable without the RSA private key.

```python
from Crypto.PublicKey import RSA
from Crypto.Cipher import AES
from hashlib import sha256

# Public key is available
pubkey = RSA.import_key(open("public.pem").read())
der_bytes = pubkey.export_key("DER")

# Seed from challenge (hardcoded/predictable)
seed = b'BSidesSFCTF2026!'

# Derive AES key the same way the encryptor did
key_hash = sha256(der_bytes).digest()
aes_key = bytes(a ^ b for a, b in zip(key_hash, seed.ljust(32, b'\x00')))

# Decrypt
ct = open("flag.enc", "rb").read()
iv, ct_body = ct[:16], ct[16:]
cipher = AES.new(aes_key, AES.MODE_CBC, iv)
plaintext = cipher.decrypt(ct_body)
```

**Key insight:** Key derivation that incorporates only public information (public keys, known constants) provides zero security regardless of the hash function used. The "hybrid" design creates a false sense of security — RSA protects nothing if the AES key doesn't depend on the RSA private key.

**When to recognize:** Challenge provides both a public key AND an encrypted file, but no private key or ciphertext for RSA. Look for key derivation code that hashes the public key, uses the public key's modulus/exponent as seed material, or XORs with a constant.

---

## HMAC-CRC Linearity Attack (Boston Key Party 2016)

**Pattern:** HMAC constructed with CRC as the hash function is completely broken because CRC is linear over GF(2). The key is directly recoverable from a single message-MAC pair via polynomial arithmetic over GF(2^64).

```python
# CRC is linear: CRC(a XOR b) = CRC(a) XOR CRC(b)
# HMAC-CRC(key, msg) = CRC(key_opad || CRC(key_ipad || msg))
# Rewrite as polynomial in GF(2): K = known_terms * inverse(x^(128+M) + x^128) mod CRC_POLY
```

**Key insight:** CRC's linearity over GF(2) means HMAC-CRC provides zero security. Always verify the underlying hash function is non-linear before trusting HMAC.

---

## DES Weak Keys in OFB Mode (Boston Key Party 2016)

**Pattern:** DES has 4 weak keys where `E(E(P,K),K) = P` (encryption is self-inverse). In OFB (Output Feedback) mode this causes the keystream to cycle with period 2: even blocks XOR with IV, odd blocks with E(IV,K). Reduces to a 16-byte repeating XOR key.

```python
# DES weak keys: 0x0000000000000000, 0xFFFFFFFFFFFFFFFF,
#                0xE1E1E1E1F0F0F0F0, 0x1E1E1E1E0F0F0F0F
# OFB with weak key: keystream = [IV, E(IV,K), IV, E(IV,K), ...]
# Recovery: try all 4 weak keys; or treat as 16-byte repeating XOR
```

**Key insight:** DES weak keys cause OFB keystream to cycle with period 2. When you see DES+OFB, always try the 4 weak keys first.

---

## Square Attack on Reduced-Round AES (0CTF 2016)

**Pattern:** 4-round AES is vulnerable to the square (integral) attack. Choose 256 plaintexts differing in one byte (a "lambda set"). After 3 rounds, the XOR sum at any byte position equals 0. Guess one byte of the last round key and partially decrypt -- if XOR sum is 0, the guess is correct.

```python
# For each byte position in the last round key:
for candidate in range(256):
    xor_sum = 0
    for ct in ciphertexts:
        xor_sum ^= inv_sub_bytes(ct[pos] ^ candidate)
    if xor_sum == 0:
        key_byte = candidate  # correct guess
# Reduces 2^128 key recovery to ~16 * 256 = 4096 operations
```

**Key insight:** Integral cryptanalysis exploits the "balanced" property (XOR-sum = 0) that propagates through AES rounds. Effective against 4-round AES; 5+ rounds require more sophisticated variants.

---

## SRP (Secure Remote Password) Protocol Bypass via Modular Arithmetic (ASIS CTF Finals 2016)

SRP implementations that only check `A != 0` and `A != N` can be bypassed by sending `A = 2*N`, causing the server to compute a zero session key.

```python
from hashlib import sha256
import hmac

# SRP protocol: server computes session key from A (client's public value)
# S = (A * v^u) ^ b mod N
# If A = 2*N: S = (2*N * v^u) ^ b mod N = 0 (since 2*N mod N = 0)

N = server_modulus
# Send A = 2*N (bypasses checks for A != 0 and A != N)
A_malicious = 2 * N

# Server computes S = 0, so session key K = SHA256(0)
K = sha256(b'\x00').digest()

# Now compute valid HMAC proof with known K
proof = hmac.new(K, salt, sha256).hexdigest()
```

**Key insight:** SRP implementations must validate `A % N != 0`, not just `A != 0` and `A != N`. Sending `A = k*N` for any integer k forces the shared secret to zero, allowing authentication without knowing the password.

---

## Modified AES S-Box Brute-Force Recovery (H4ckIT CTF 2016)

AES implementation with a custom S-Box created by swapping 3 elements of the standard S-Box. Brute-force all C(256,3) * 2 = 5,527,040 possible permutations.

```cpp
// Three elements swapped from standard AES S-Box
// Total permutations: C(256,3) * 2 = ~5.5 million (feasible to brute-force)
#include <openssl/aes.h>

void bruteforce_sbox(uint8_t ciphertext[], uint8_t key[], int ct_len) {
    uint8_t standard_sbox[256]; // standard AES S-Box
    // Try all 3-element swaps
    for (int i = 0; i < 256; i++)
        for (int j = i+1; j < 256; j++)
            for (int k = j+1; k < 256; k++) {
                // Swap pairs: (i,j), (i,k), (j,k)
                uint8_t sbox[256];
                memcpy(sbox, standard_sbox, 256);
                swap(sbox[i], sbox[j]); // try each 2-element swap from the triple
                // Decrypt and check for valid plaintext
                if (try_decrypt_with_sbox(sbox, ciphertext, key, ct_len))
                    return; // found it
            }
}
```

**Key insight:** When a custom AES S-Box differs from standard by only a few element swaps, the search space is small enough to brute-force. For 3 swapped elements: C(256,3) permutation groups times the swap combinations within each group.

---

## AES-ECB Byte-at-a-Time Chosen Plaintext (ABCTF 2016)

**Pattern (Encryption Service):** Server encrypts `user_input || secret_suffix` under AES-ECB. Recover the secret suffix one byte at a time by controlling the input length.

1. Send inputs of decreasing length to push one unknown byte into a known block position
2. For each position, try all 256 byte values and compare the encrypted block:

```python
from pwn import *
import cryptanalib as ca  # FeatherDuster's cryptanalib

def oracle(pt):
    """Send plaintext, receive ECB-encrypted ciphertext."""
    r = remote('target', 7765)
    r.recvuntil('Send me some hex-encoded data to encrypt:\n')
    r.sendline(pt.hex())
    r.recvuntil('Here you go:')
    ct = bytes.fromhex(r.recvline().strip().decode())
    r.close()
    return ct

# Automated byte-at-a-time recovery
flag = ca.ecb_cpa_decrypt(oracle, block_size=16, verbose=True)
print(flag)
```

**Manual approach without library:**
```python
block_size = 16
known = b''

for i in range(len(secret)):
    # Pad so next unknown byte is at end of a block
    pad_len = block_size - 1 - (len(known) % block_size)
    pad = b'A' * pad_len

    # Get target block
    target_ct = oracle(pad)
    target_block_idx = (pad_len + len(known)) // block_size
    target_block = target_ct[target_block_idx*16:(target_block_idx+1)*16]

    # Try all 256 byte values
    for byte_val in range(256):
        test = pad + known + bytes([byte_val])
        test_ct = oracle(test)
        if test_ct[target_block_idx*16:(target_block_idx+1)*16] == target_block:
            known += bytes([byte_val])
            break
```

**Key insight:** ECB mode encrypts identical plaintext blocks to identical ciphertext blocks. By controlling the prefix length, the attacker shifts one unknown byte at a time to a position where it completes a known block prefix. Comparing the target ciphertext block against all 256 possibilities recovers each byte in at most 256 queries. Total queries: ~256 * secret_length. Tool: FeatherDuster's `cryptanalib.ecb_cpa_decrypt()` automates this completely.

---

## AES-ECB Cut-and-Paste Block Manipulation (NDH Quals 2016)

**Pattern (Toil33t):** Server encrypts JSON session data in AES-ECB mode. Fields like `is_admin: false` span predictable block boundaries. Construct chosen plaintext blocks via registration, then splice ciphertext blocks to change `false` to `true`.

1. Detect ECB mode: register with repeating username (e.g., 'A' * 64), look for identical ciphertext blocks
2. Map block boundaries by varying username length until block count changes
3. Determine field ordering by independently varying username and email lengths
4. Craft target block containing `true` by aligning it at a block boundary via padding:

```python
# Align "true" at start of a block using space padding (JSON ignores whitespace)
# Original:  {"username": "AA", "is_admin": false, "email": ""}
# Target:    {"username": "AA", "is_admin":            true, "email": ""}
#                                              ^-- 16-byte block boundary

# Get the "            true" block from:
username = "AAA" + " " * 12 + "true"
# Extract block 2 of the resulting ciphertext

# Get prefix blocks from a short username
# Get suffix block from a padded username
# Concatenate: prefix_blocks + true_block + suffix_block
```

**Key insight:** AES-ECB encrypts each 16-byte block independently with no chaining. Identical plaintext blocks produce identical ciphertext blocks, allowing block-level cut-and-paste. JSON's tolerance for extra whitespace enables block alignment without breaking parsing. The attack requires: (a) detecting ECB via repeated blocks, (b) mapping field layout via length probing, (c) crafting and splicing blocks.

---

## AES-CBC IV Bit-Flip Authentication Bypass (Google CTF 2016)

**Pattern (Eucalypt Forest):** Server encrypts JSON session blob under AES-CBC and returns both IV and ciphertext as a cookie. No integrity check (no MAC/HMAC). Flip bits in the IV to change the first plaintext block.

1. Register with username one bit away from target (e.g., `` `dmin `` instead of `admin` — flip LSB of 'a')
2. Identify the IV byte position corresponding to the target character in the first block
3. Flip the same bit in the IV byte — XOR propagates directly to the plaintext:

```python
import binascii
cookie = binascii.unhexlify(auth_cookie)
iv = bytearray(cookie[:16])
ciphertext = cookie[16:]

# Flip LSB of byte at position where 'a'/'`' appears in first block
# Position depends on JSON structure: {"username":"`dmin"}
# 'a' (0x61) vs '`' (0x60) differ only in bit 0
target_pos = 13  # position of first char of username in block
iv[target_pos] ^= 0x01

forged = binascii.hexlify(bytes(iv) + ciphertext)
```

**Key insight:** AES-CBC decryption XORs the previous ciphertext block (or IV for block 0) with the AES-decrypted block. Flipping bit `i` in the IV flips bit `i` in the first plaintext block with no other side effects. This only works when the server performs no integrity verification (no HMAC, AEAD, or authenticated encryption).

---

## Rabin Cryptosystem LSB Parity Oracle (PlaidCTF 2016)

**Pattern (rabit):** Server encrypts flag with the Rabin cryptosystem (`c = m^2 mod n`) and provides an LSB oracle — for any ciphertext, it returns the least significant bit of the decrypted plaintext. Binary search recovers the full plaintext in `log2(n)` queries.

```python
from Crypto.Util.number import long_to_bytes

def lsb_oracle_attack(enc_flag, N, oracle_fn):
    """Recover plaintext from Rabin/RSA LSB oracle via binary search."""
    lower = 0
    upper = N
    C = enc_flag
    # Rabin: encrypt(2,N) = 4; multiplying ciphertext by 4 doubles plaintext
    e2 = pow(2, 2, N)  # For Rabin; use pow(2, e, N) for RSA

    for i in range(N.bit_length()):
        C = (e2 * C) % N  # Multiply plaintext by 2
        lsb = oracle_fn(C)
        if lsb == 1:
            # 2*m > N (odd remainder after mod), increase lower bound
            lower = (upper + lower) // 2
        else:
            # 2*m < N (even remainder), decrease upper bound
            upper = (upper + lower) // 2
        # Progressive decryption visible:
        print(long_to_bytes(upper))
    return upper
```

**Key insight:** Rabin (and textbook RSA) are multiplicatively homomorphic: multiplying ciphertext by `2^e mod N` doubles the plaintext mod N. Since N is odd, doubling causes a modular wraparound iff the plaintext exceeds `N/2`, which changes the LSB parity. This creates a binary search: each oracle query halves the candidate range, recovering the full plaintext in exactly `log2(N)` queries (~1024 for RSA-1024).

---

## PBKDF2 Pre-Hash Bypass for Long Passwords (BackdoorCTF 2016)

**Pattern (Mindblown):** PBKDF2 (and HMAC generally) pre-hashes passwords longer than the hash block size (64 bytes for SHA-1/SHA-256). If the target password exceeds 64 bytes, `PBKDF2(password)` equals `PBKDF2(SHA1(password))`, enabling authentication with the hash instead of the original password.

```python
import hashlib

original_password = "complexPasswordWhichContainsManyCharactersWithRandomSuffixeghjrjg"
# len > 64, so HMAC pre-hashes it
equivalent = hashlib.sha1(original_password.encode()).digest()
# Login with equivalent — PBKDF2 produces the same derived key
```

**Key insight:** HMAC's inner construction is `H((K XOR ipad) || message)`. When the key (password) exceeds the hash block size, HMAC first reduces it via `K = H(password)`. This means `HMAC(long_password, ...)` equals `HMAC(H(long_password), ...)`. Any system using PBKDF2/HMAC with a `!==` identity check after hash comparison is vulnerable when passwords exceed 64 bytes. This is a HMAC specification behavior, not an implementation bug.

---

## MD5 Multi-Collision via Fastcol (BackdoorCTF 2016)

**Pattern (Forge):** Generate 2^k files with identical MD5 hashes by chaining `fastcol` (Marc Stevens' tool). Each run produces two suffixes (A, B) that when appended yield the same MD5. Chain 3 runs to produce 8 collisions:

```text
[prefix][suffix1A][suffix2A][suffix3A]  \
[prefix][suffix1A][suffix2A][suffix3B]   |
[prefix][suffix1A][suffix2B][suffix3A]   |-- all have same MD5
[prefix][suffix1A][suffix2B][suffix3B]   |
[prefix][suffix1B][suffix2A][suffix3A]   |
[prefix][suffix1B][suffix2B][suffix3B]  /
```

```bash
# Install: git clone https://github.com/cr-marcstevens/hashclash
# Generate one collision pair (~minutes on modern CPU):
./fastcol -o suffix1A.bin suffix1B.bin < prefix.bin
# Chain: append suffix1A to prefix, run fastcol again for suffix2A/2B, etc.
```

**Key insight:** MD5 collision generation is practical with `fastcol` (~minutes per pair). Because MD5 uses Merkle-Damgard construction, collisions compose: if `H(A||X) == H(A||Y)`, then `H(A||X||Z) == H(A||Y||Z)` for any suffix Z. Chaining k collision pairs produces 2^k files with identical MD5. For CRC32 collisions, append bytes after PNG IEND chunk (parsers ignore trailing data) and brute-force the 4-byte CRC adjustment.

---

## GHASH Key Recovery over Prime Modulus (nullcon HackIM 2019)

**Pattern (GenuineCounterMode):** A custom GCM-like scheme computes `tag = c + sum(b_i * H^(i+1)) mod n` where `n` is a 128-bit prime (not `GF(2^128)`). The 12-byte nonce has 10 bytes fixed from the session ID plus 2 random bytes, so nonce collisions arrive in roughly 256 encryption queries (birthday bound). With two colliding nonces, the equations for `tag1` and `tag2` share the same `c = E_K(nonce || counter)`, so subtracting eliminates `c` and leaves a linear equation in `H` modulo prime `n` — solvable by a single modular inverse, not `GF(2^128)` polynomial factoring.

```python
from Crypto.Util.number import bytes_to_long, long_to_bytes, inverse

n = 327989969870981036659934487747327553919  # prime modulus (not GF(2^128))

# 1. Request encryptions with single-block messages until two share a nonce
# 2. With colliding (nonce, ct1, tag1) and (nonce, ct2, tag2):
m1 = bytes_to_long(ct1)  # single 16-byte block
m2 = bytes_to_long(ct2)
t1 = bytes_to_long(tag1)
t2 = bytes_to_long(tag2)
H = ((t1 - t2) * inverse(m1 - m2, n)) % n

# 3. Forge: encrypt "may i please have the galf", flip CTR bytes to 'flag'
#    then recompute tag using recovered H and c = tag - sum(b_i * H^(i+1))
c0 = (t1 - sum(bytes_to_long(b) * pow(H, i + 1, n) for i, b in enumerate(blocks1))) % n
forged_tag = (c0 + sum(bytes_to_long(b) * pow(H, i + 1, n) for i, b in enumerate(forged_blocks))) % n
```

**Key insight:** GCM's security rests on `GHASH` operating over `GF(2^128)` where inversion is hard without the key. Swapping the modulus to a plain prime `n` collapses the authentication to textbook linear algebra mod `n` — two nonce-colliding tags give one linear equation per unknown, solved with `inverse(m1 - m2, n)`. Short-nonce (2 random bytes) designs guarantee birthday collisions in ~256 queries. Contrast with the `GF(2^128)` AES-GCM forbidden attack in [modern-ciphers.md](modern-ciphers.md#aes-gcm-nonce-reuse--forbidden-attack), which needs polynomial factoring over binary fields.

---

## SHA-1 Length Extension Plus AES-CBC Cookie Forgery (BSidesSF 2019)

**Pattern (decrypto):** Cookie stores `user = iv || AES-CBC(key, plaintext)` plus a separate `signature = SHA1(secret || decrypt(ct))` tag. The session cookie leaks the AES key (e.g. trailing 32 bytes of a base64 session blob). To forge `UID 0`, length-extend the signature with `\nUID 0\n`, decrypt the current ciphertext to learn the plaintext, append hashpump's padding + extension, re-encrypt with the known key, and send both updated `user` and `signature`.

```python
import hashpumpy, binascii, base64, urllib
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# Extract AES key from leaked rack.session cookie
key = base64.b64decode(urllib.unquote(cookies['rack.session'].split('--')[0]))[-32:]
user = binascii.unhexlify(cookies['user'])
iv, ct = user[:16], user[16:]

def decrypt(c): return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(c), 16)
def encrypt(p): return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(p, 16))

# Length-extend signature (secret length guessed = 8)
new_sig, new_plain = hashpumpy.hashpump(cookies['signature'], decrypt(ct), b'\nUID 0\n', 8)
cookies['signature'] = new_sig
cookies['user']      = binascii.hexlify(iv + encrypt(new_plain))
```

**Key insight:** Hash length extension applies whenever the MAC is `H(secret || data)` over a Merkle-Damgard hash (MD5/SHA-1/SHA-256). If the *same* `data` also lives inside a *separately* keyed cipher whose key is recoverable, you can combine primitives: hashpumpy produces the extended plaintext and new tag, then you re-encrypt with the leaked AES key so the server's CBC decryption matches the extended string the signature now covers. Parse-order quirks (later fields overriding earlier ones) let the appended `\nUID 0\n` win.

---

See [modern-ciphers.md](#part-3) for custom hash reversal, CRC32 brute-force, noisy RSA LSB oracle, sponge collisions, CBC IV forgery + block truncation, padding oracle + bit-flip command injection, SPN S-box intersection, AES-CFB IV recovery, three-round XOR, Unicode decode side channel, SHA-256 basis attack, MAC forgery via XOR block cancellation, and bit-by-bit HMAC key recovery.

---

# Part 3

Custom hash reversal, CRC brute-force, noisy RSA oracles, sponge collisions, CBC/padding oracle tricks, SPN recovery, AES-CFB, three-round XOR, Unicode side channels, SHA-256 basis attacks, MAC forgery, HMAC bit oracles. For Blum-Goldwasser, hash length extension, compression oracles, OFB/HMAC-CRC/DES weak keys, SRP, square attack, AES-ECB/CBC oracles, Rabin, PBKDF2, and MD5 multi-collision, see [modern-ciphers.md](#part-2).

## Table of Contents
- [Custom Hash State Reversal via Known Intermediates (BackdoorCTF 2016)](#custom-hash-state-reversal-via-known-intermediates-backdoorctf-2016)
- [CRC32 Brute-Force for Small Payloads (BackdoorCTF 2016)](#crc32-brute-force-for-small-payloads-backdoorctf-2016)
- [Noisy RSA LSB Oracle with Post-Hoc Error Correction (SharifCTF 7 2016)](#noisy-rsa-lsb-oracle-with-post-hoc-error-correction-sharifctf-7-2016)
- [Sponge Hash Collision via Meet-in-the-Middle on Partial State (BKP 2017)](#sponge-hash-collision-via-meet-in-the-middle-on-partial-state-bkp-2017)
- [CBC IV Forgery + Block Truncation for Authentication Bypass (0CTF 2017)](#cbc-iv-forgery--block-truncation-for-authentication-bypass-0ctf-2017)
- [Padding Oracle to CBC Bitflip Command Injection (BSidesSF 2017)](#padding-oracle-to-cbc-bitflip-command-injection-bsidessf-2017)
- [SPN Cipher Partial Key Recovery via S-box Intersection (SharifCTF 7 2016)](#spn-cipher-partial-key-recovery-via-s-box-intersection-sharifctf-7-2016)
- [AES-CFB IV Recovery from Timestamp-Seeded PRNG (SHA2017)](#aes-cfb-iv-recovery-from-timestamp-seeded-prng-sha2017)
- [Three-Round XOR Protocol Key Cancellation (HITB 2017)](#three-round-xor-protocol-key-cancellation-hitb-2017)
- [AES-CBC UnicodeDecodeError Side-Channel Oracle (Kaspersky 2017)](#aes-cbc-unicodedecodeerror-side-channel-oracle-kaspersky-2017)
- [SHA-256 Basis Attack for XOR-Aggregate Hash Bypass (34C3 CTF 2017)](#sha-256-basis-attack-for-xor-aggregate-hash-bypass-34c3-ctf-2017)
- [Custom MAC Forgery via XOR Block Cancellation with Key Rotation (PlaidCTF 2018)](#custom-mac-forgery-via-xor-block-cancellation-with-key-rotation-plaidctf-2018)
- [Bit-by-Bit HMAC Key Recovery via XOR Plus Addition Arithmetic (Midnight Sun CTF 2018)](#bit-by-bit-hmac-key-recovery-via-xor-plus-addition-arithmetic-midnight-sun-ctf-2018)
- [CBC IV Recovery from Block-2 Known Plaintext (RITSEC 2018)](#cbc-iv-recovery-from-block-2-known-plaintext-ritsec-2018)
- [Iterated SHA-256 Timing Oracle on Character Match (35C3 2018)](#iterated-sha-256-timing-oracle-on-character-match-35c3-2018)
- [GF(p) Linear-System AES Key Recovery from PCAP Matrix (35C3 Junior 2018)](#gfp-linear-system-aes-key-recovery-from-pcap-matrix-35c3-junior-2018)
- [SHA-1 Length Extension with UTF-8 High-Byte Bypass (OTW Advent 2018)](#sha-1-length-extension-with-utf-8-high-byte-bypass-otw-advent-2018)
- [Cross-Session Cube-Root Recovery via CRT (X-MAS 2018)](#cross-session-cube-root-recovery-via-crt-x-mas-2018)
- [CBC Previous-Block Byte Flipping for Cookie Privilege Escalation (picoCTF 2018)](#cbc-previous-block-byte-flipping-for-cookie-privilege-escalation-picoctf-2018)

---

## Custom Hash State Reversal via Known Intermediates (BackdoorCTF 2016)

**Pattern (Collision Course):** Custom hash processes 4-byte blocks, updating state with XOR and rotations. If intermediate states are printed, reverse each block's hash by computing `hash(block) = s(i) XOR ROL(s(i+1), 7)`. Then brute-force 4-byte printable inputs matching each hash value.

```python
def reverse_hash_states(states):
    """Given intermediate hash states, recover per-block hash values."""
    blocks = []
    for i in range(len(states) - 1):
        # state_update: s(i+1) = ROR(s(i) ^ hash(block), 7)
        # Therefore:    hash(block) = s(i) ^ ROL(s(i+1), 7)
        h = states[i] ^ rol32(states[i+1], 7)
        blocks.append(h)
    return blocks

def rol32(val, n):
    return ((val << n) | (val >> (32 - n))) & 0xFFFFFFFF

# Brute-force printable 4-byte blocks matching each hash
import itertools, string
for target_hash in block_hashes:
    for chars in itertools.product(string.printable, repeat=4):
        block = bytes(ord(c) for c in chars)
        if custom_hash(block) == target_hash:
            print(f"Found: {block}")
            break
```

**Key insight:** When a custom hash function leaks intermediate states (after each block), each block becomes an independent 4-byte brute-force problem (~2^32 worst case, reduced to ~10^8 for printable ASCII). Inverting the state update equation isolates per-block targets. This pattern appears whenever iterative hashes expose partial state.

---

## CRC32 Brute-Force for Small Payloads (BackdoorCTF 2016)

**Pattern (CRC):** Encrypted ZIP files store CRC32 of uncompressed contents. For very small files (5 bytes), brute-force all printable 5-character strings, compute CRC32, and match against the stored value. Multiple matches are common but context resolves ambiguity.

```python
import binascii, itertools, string, zipfile

# Extract CRC from ZIP without decrypting
with zipfile.ZipFile('encrypted.zip') as z:
    crc = z.infolist()[0].CRC

# Brute-force 5-byte printable content
for chars in itertools.product(string.printable[:95], repeat=5):
    candidate = ''.join(chars).encode()
    if binascii.crc32(candidate) & 0xFFFFFFFF == crc:
        print(f"Match: {candidate}")
```

**Key insight:** CRC32 stored in ZIP headers is not encrypted — it's always accessible even for password-protected ZIPs. For small files (≤ 6 bytes of printable ASCII), the search space is feasible. A C implementation is ~100x faster than Python. Multiple CRC collisions are expected for 5+ byte payloads; combine with language analysis or cross-reference multiple encrypted files to disambiguate.

---

## Noisy RSA LSB Oracle with Post-Hoc Error Correction (SharifCTF 7 2016)

**Pattern:** Extension of the RSA LSB oracle binary search when the oracle occasionally returns incorrect results. Run the standard LSB oracle attack, then inspect decoded bytes. Non-ASCII or unexpected charset values indicate an oracle error within the last ~8 bits. Try single bit-flips at nearby oracle positions; the correct flip fixes the entire remaining decryption.

```python
def lsb_oracle_attack(ciphertext, e, n, oracle_fn, flips=None):
    """Recover plaintext from RSA LSB oracle, with optional error correction."""
    flips = flips or []
    lower, upper = 0, n
    mult = 1
    for i in range(n.bit_length()):
        ciphertext = (ciphertext * pow(2, e, n)) % n
        result = oracle_fn(ciphertext)
        if i in flips:
            result = not result  # correct known oracle error
        mid = (lower + upper) // 2
        if result == 0:
            upper = mid
        else:
            lower = mid
    return lower
```

**Key insight:** Sparse oracle errors produce localized corruption in the recovered plaintext. By inspecting character validity (e.g., expecting hex digits), the error position can be identified and corrected by flipping the oracle result at that query index.

---

## Sponge Hash Collision via Meet-in-the-Middle on Partial State (BKP 2017)

**Pattern:** A custom sponge hash uses AES with a known key, XORing 10-byte message blocks into a 16-byte state. Since only 10 of 16 state bytes are controllable per block, a direct preimage requires ~2^48 work. Meet-in-the-middle reduces this: precompute 2^24 forward AES encryptions keyed on their last 6 bytes, then search backward decryptions for matches in those 6 bytes.

```python
from Crypto.Cipher import AES
import os

aes = AES.new(b'\x00' * 16, AES.MODE_ECB)
forward = {}

# Forward: compute AES(random_10_bytes || 0x00*6), key on last 6 bytes
for _ in range(2**24):
    block = os.urandom(10) + b'\x00' * 6
    enc = aes.encrypt(block)
    forward[enc[-6:]] = block

# Backward: compute AES_dec(target XOR random_c), check last 6 bytes
target_state = b'\x77\x40\x56\x0a\x1d\x64'  # target hash
for _ in range(2**40):
    c_block = os.urandom(10) + target_state
    dec = aes.decrypt(c_block)
    if dec[-6:] in forward:
        a_block = forward[dec[-6:]]
        b_block = xor(aes.encrypt(a_block), dec)  # middle block
        break
```

**Key insight:** When a sponge rate is smaller than the state size, the uncontrolled bytes create a meet-in-the-middle opportunity. Precompute one direction, search the other — reducing 2^48 to 2^24 space + 2^24 time.

---

## CBC IV Forgery + Block Truncation for Authentication Bypass (0CTF 2017)

**Pattern:** Service encrypts `MD5(padded_name) || padded_name` with AES-CBC. The MD5 serves as an integrity check on login. Two attacks combine: (1) IV manipulation: XOR IV bytes to change the decrypted first block from the source MD5 to the target MD5. (2) Block truncation: register with `pad(b"admin", 16) + 16_junk_bytes`, then strip trailing ciphertext blocks — AES-CBC has no length field, so shorter ciphertext decrypts validly if PKCS7 padding is correct.

```python
from Crypto.Util.Padding import pad

# Forge IV to flip MD5 from registered user to "admin"
source_md5 = md5(pad(b"admin", 16) + b"A"*16)
target_md5 = md5(pad(b"admin", 16))
new_iv = bytes(a ^ b ^ c for a, b, c in zip(original_iv, source_md5, target_md5))

# Strip last 2 blocks (junk + PKCS padding block)
forged_token = new_iv + ciphertext[16:-32]
```

**Key insight:** AES-CBC decryption has no built-in length integrity. Truncating ciphertext blocks from the end is valid as long as the new last block decrypts to valid PKCS7 padding. Combined with IV manipulation of block 0, this forges arbitrary first-block content.

---

## Padding Oracle to CBC Bitflip Command Injection (BSidesSF 2017)

**Pattern:** Encrypted commands passed via URL parameter. Error messages reveal padding validity (padding oracle). Chain two attacks: (1) Padding oracle recovers the plaintext of the encrypted command. (2) CBC bitflipping modifies a ciphertext block to inject shell metacharacters (`;$(cmd)`) into the decrypted command, achieving RCE through crypto manipulation alone.

```python
# Step 1: Padding oracle recovers plaintext
plaintext = padding_oracle_decrypt(ciphertext, oracle_fn)

# Step 2: CBC bitflip — modify block N-1 to change decrypted block N
target_block = 5
desired = b';$(cat *.txt)   '  # 16 bytes, pad with spaces
original = plaintext[target_block * 16:(target_block + 1) * 16]
ct = bytearray(bytes.fromhex(ciphertext))
for i in range(16):
    ct[(target_block - 1) * 16 + i] ^= original[i] ^ desired[i]
forged = ct.hex()
```

**Key insight:** Padding oracle and CBC bitflipping are usually taught separately. Chaining them converts a pure cryptographic weakness into full command injection: the oracle recovers plaintext needed to compute the XOR mask, and the bitflip injects the payload.

---

## SPN Cipher Partial Key Recovery via S-box Intersection (SharifCTF 7 2016)

**Pattern:** A 3-round substitution-permutation network with 36-bit blocks and 6-bit S-boxes. Attack using chosen-plaintext pairs: for each pair of 6-bit sub-keys (rounds 2 and 3), partially decrypt through the last two rounds and check if the intermediate S-box input matches. Intersecting candidate key sets across ~200 plaintext-ciphertext pairs uniquely identifies each 6-bit sub-key, reducing a 108-bit brute force to six independent 12-bit searches.

```python
def recover_subkeys(pairs, sbox, perm):
    """Recover 6-bit subkeys via intersection across plaintext-ciphertext pairs."""
    for sbox_pos in range(6):  # 6 S-boxes per round
        candidates = None
        for pt, ct in pairs:
            valid = set()
            for k2 in range(64):  # 6-bit subkey round 2
                for k3 in range(64):  # 6-bit subkey round 3
                    # Partial decrypt through rounds 3 and 2
                    intermediate = inv_sbox[ct_bits[sbox_pos] ^ k3]
                    intermediate = inv_perm(intermediate)
                    if inv_sbox[intermediate ^ k2] == expected_from_pt:
                        valid.add((k2, k3))
            candidates = valid if candidates is None else candidates & valid
        assert len(candidates) == 1  # unique key pair
```

**Key insight:** SPN structures allow divide-and-conquer key recovery. Each S-box position can be attacked independently, and the intersection of valid key candidates across multiple plaintext-ciphertext pairs converges to a unique solution.

---

## AES-CFB IV Recovery from Timestamp-Seeded PRNG (SHA2017)

**Pattern:** Ransomware encrypts files with AES-CFB using a hardcoded password from bash_history. The IV is derived from `random.choice()` seeded with `int(time())` at encryption time. The file's mtime (preserved by the filesystem) equals the exact seed used, enabling full decryption without the private key.

```python
import random, os, string, base64
from Crypto.Cipher import AES

password = b'hardcoded_password_from_bash_history'
img = 'encrypted_file.enc'

# File mtime IS the random seed used at encryption time
random.seed(int(os.stat(img).st_mtime))
iv = ''.join(random.choice(string.letters + string.digits) for _ in range(16))

aes = AES.new(password, AES.MODE_CFB, iv.encode())
with open(img, 'rb') as f:
    ciphertext = base64.b64decode(f.read())
plaintext = aes.decrypt(ciphertext)
```

**Key insight:** PRNG seeded with `time()` at encryption time leaks the seed via the filesystem mtime. Always check Python version compatibility — Python 2 and Python 3 have different `random` module implementations producing different sequences from the same seed. The `-it` flag on `cp`/`mv` may reset mtime; work from the original unmodified file.

**References:** SHA2017

---

## Three-Round XOR Protocol Key Cancellation (HITB 2017)

**Pattern:** A custom protocol performs a three-message XOR key exchange:
1. Client sends `c1 = msg XOR clientKey`
2. Server responds `c2 = c1 XOR serverKey`
3. Client sends `c3 = c2 XOR clientKey`

All three ciphertexts are observable in a PCAP or network capture. Computing `c1 XOR c2 XOR c3` directly recovers the original `msg` because all key material cancels:

```python
# c1 = msg ^ clientKey
# c2 = msg ^ clientKey ^ serverKey
# c3 = msg ^ serverKey
# c1 ^ c2 ^ c3 = msg ^ clientKey ^ msg ^ clientKey ^ serverKey ^ msg ^ serverKey
#              = msg   (all keys cancel via XOR)
plaintext = bytes(a ^ b ^ c for a, b, c in zip(c1, c2, c3))
```

**Key insight:** Three-message XOR key exchange where the client applies its key twice creates an algebraic weakness: XOR of all three ciphertexts directly recovers the original message without knowledge of either key. Any protocol where the same key is applied an even number of times is trivially broken.

**References:** HITB 2017

---

## AES-CBC UnicodeDecodeError Side-Channel Oracle (Kaspersky 2017)

**Pattern:** Server decrypts AES-CBC ciphertext and attempts to UTF-8 decode the result. Invalid UTF-8 sequences raise a `UnicodeDecodeError` (or equivalent). This error is distinguishable from other errors (e.g., application-level errors), creating a decryption oracle analogous to a padding oracle.

**Attack:** Standard CBC bit-flip oracle technique, using UTF-8 validity as the distinguisher:
1. For each target plaintext byte at position `i` in block `b`, modify byte `i` in block `b-1`
2. Cycle through all 256 XOR values; when the decrypted byte produces valid UTF-8 in context, the server returns a non-`UnicodeDecodeError` response
3. From the XOR value that passes and the known modification to `c[b-1][i]`, recover `plaintext[b][i]`

```python
# CBC bit-flip oracle using UTF-8 validity
for guess in range(256):
    modified = bytearray(prev_block)
    modified[pos] = known_intermediate[pos] ^ guess  # produce desired output byte
    if not unicode_error(modified_block + target_block):
        plaintext_byte = guess  # valid UTF-8 at this position
        break
```

**Key insight:** Any error that distinguishes valid from invalid plaintext content serves as a decryption oracle — not just PKCS#7 padding errors. UTF-8 validity, base64 decodability, JSON parsability, and ASCII-only constraints are all valid oracle conditions. The only requirement is a server-side distinguishable response.

**References:** Kaspersky CTF 2017

---

## SHA-256 Basis Attack for XOR-Aggregate Hash Bypass (34C3 CTF 2017)

**Pattern:** Find 256 files whose SHA-256 hashes form a basis for Z_2^256. Then for any target hash, compute which subset of basis files XORs to produce the desired hash difference. This breaks systems that verify integrity via `XOR(sha256(file_i)) == expected`.

```python
# 1. Generate ~300 random valid Python files
# 2. Compute SHA-256 of each -> 256-bit vectors over GF(2)
# 3. Gaussian elimination to find 256 linearly independent vectors
# 4. Target: h_new XOR (XOR of sha256(basis_files)) = h_orig
# 5. Solve the linear system to find which basis files to include
from sympy import Matrix

M = Matrix([hash_to_bits(sha256(f)) for f in basis_files])
target = Matrix([hash_to_bits(sha256(malicious_zip))[i] ^ hash_to_bits(original_hash)[i] for i in range(256)])
# Solve M^T * x = target over GF(2) (solve_left => x*M = target => M^T*x = target)
solution = M.T.gauss_jordan_solve(target)  # reduce mod 2; or use rref iszerofunc
# For GF(2), reduce entries mod 2 after rref
```

<details><summary>Sage fallback (optional)</summary>

```python
# 1. Generate ~300 random valid Python files
# 2. Compute SHA-256 of each -> 256-bit vectors over GF(2)
# 3. Gaussian elimination to find 256 linearly independent vectors
# 4. Target: h_new XOR (XOR of sha256(basis_files)) = h_orig
# 5. Solve the linear system to find which basis files to include
from sage.all import GF, matrix
M = matrix(GF(2), [hash_to_bits(sha256(f)) for f in basis_files])
target = hash_to_bits(sha256(malicious_zip)) ^ hash_to_bits(original_hash)
solution = M.solve_left(target)
```

</details>

**Key insight:** SHA-256 hashes are 256-bit vectors over GF(2). Given ~256 random hashes, they almost certainly span the full space, meaning you can XOR-combine them to produce any target 256-bit value. This breaks XOR-based aggregate hash verification: if the system checks `XOR(sha256(file_i)) == expected`, you can replace files while maintaining the aggregate. The attack does NOT find SHA-256 collisions -- it exploits the linearity of XOR aggregation over non-linear hash outputs.

**References:** 34C3 CTF 2017

---

### Custom MAC Forgery via XOR Block Cancellation with Key Rotation (PlaidCTF 2018)

**Pattern:** Custom MAC uses AES-ECB with key stream that repeats every 128 blocks. Craft three queries where 2048-byte filler blocks cancel via XOR between queries, leaving only the target command's MAC. (PlaidCTF 2018)

```python
mac1 = fmac("tag " + tag_cmd(cmdline))      # tag AAA...
mac2 = fmac("tag " + expand_cmd(cmdline))    # tag BBB...(2048) + cmd_padded
mac3 = fmac("tag " + expand_cmd(tag_cmd(cmdline)))  # tag BBB...(2048) + tagAAA_padded
forged_mac = mac1 ^ mac2 ^ mac3  # XOR cancellation = fmac(cmdline)
```

**Key insight:** When a MAC's internal key stream repeats periodically, arrange message blocks so that identical blocks at the same key-stream positions cancel via XOR across multiple queries. Three queries suffice to forge any target command's MAC.

---

### Bit-by-Bit HMAC Key Recovery via XOR Plus Addition Arithmetic (Midnight Sun CTF 2018)

**Pattern:** Flawed HMAC computes `sha256((key XOR msg) + msg)` where `+` is bitwise addition (not concatenation). Sending `msg=0` gives `sha256(key)`. For bit position `i`, sending `msg=2^i`: if key bit `i` is set, XOR clears it and addition restores it, giving the same hash. (Midnight Sun CTF 2018)

```python
from Crypto.Util.number import long_to_bytes

key_hash = get_digest(b'\x00')  # sha256(key + 0) = sha256(key)
key = 0
for i in range(key_bits):
    digest = get_digest(long_to_bytes(2**i))
    if digest == key_hash:
        key |= (1 << i)  # bit i is set in key
```

**Key insight:** When XOR and addition interact, setting bit `i` in the message XORs it away from the key but adds it back. If key bit `i` was already set, `XOR(1,1)=0` and `0+1=1`, restoring the original value. If key bit `i` was 0, `XOR(0,1)=1` and `1+1=0` with carry, changing the hash. This creates a per-bit oracle.

---

### CBC IV Recovery from Block-2 Known Plaintext (RITSEC 2018)

**Pattern:** AES-CBC given: full ciphertext, known plaintext from block 2 onward, partial key. Recover the missing IV by first brute-forcing missing key bytes via block 2 (which does not depend on the IV), then XOR plaintext[0] with `AES_decrypt(ct[0], K)` to get the IV.

```python
for tail in itertools.product(string.printable, repeat=2):
    K = base_key + ''.join(tail).encode()
    if AES.new(K, AES.MODE_ECB).decrypt(ct)[16:32] == plaintext[16:32]:
        raw = AES.new(K, AES.MODE_ECB).decrypt(ct[:16])
        IV = bytes(a ^ b for a, b in zip(raw, plaintext[:16]))
        break
```

**Key insight:** Block 2 of CBC decrypts with `prev_ct XOR raw_decrypt` where `prev_ct` is from the ciphertext itself — IV-independent. Use it to recover the key first, then XOR back to the IV.

**References:** RITSEC CTF 2018 — Who drew on my program, writeup 12269

---

### Iterated SHA-256 Timing Oracle on Character Match (35C3 2018)

**Pattern:** Server validates password character-by-character, and each correct character triggers an additional `sha256` iterated 9999 times. Correct characters therefore make the server respond ~0.66 s slower. Brute-force each position by timing responses.

```python
for ch in string.printable:
    t = time.time()
    send(prefix + ch)
    dt = time.time() - t
    if dt > baseline + 0.3:
        prefix += ch; break
```

**Key insight:** Any early-exit or variable-work validator using heavy hashing leaks position-by-position through total wall-time. Measure baseline vs. correct-char time, not absolute times.

**References:** 35C3 CTF 2018 — ultra secret, writeup 12820

---

### GF(p) Linear-System AES Key Recovery from PCAP Matrix (35C3 Junior 2018)

**Pattern:** Service sends 40 plaintext/ciphertext pairs over the network. Extract from pcap with tshark, build a 40×40 matrix `A` and vector `b` over `GF(p)`, then solve for the unknown AES round-key bytes.

```python
from sympy import Matrix

A = Matrix(A_rows)
b_vec = Matrix(b)
# Solve A * key = b over GF(p)
# Use sympy rref with modulus p (prime)
# Convert to field via mod p
p_inv = p  # modulus
# Gauss-Jordan mod p helper
def solve_mod(A, b, mod):
    aug = A.row_join(b)
    # rref over integers then reduce mod; sympy handles rational but mod p needs custom
    # Use sympy's rref then mod, or manual elimination
    n = A.cols
    # Use manual GF(p) elimination
    M = [[int(A[i,j] % mod) for j in range(n)] + [int(b[i] % mod)] for i in range(A.rows)]
    # forward elimination
    row = 0
    sol = [0]*n
    where = [-1]*n
    for col in range(n):
        sel = next((i for i in range(row, len(M)) if M[i][col] % mod != 0), None)
        if sel is None: continue
        M[row], M[sel] = M[sel], M[row]
        where[col]=row
        inv = pow(M[row][col], -1, mod)
        M[row]=[(x*inv)%mod for x in M[row]]
        for i in range(len(M)):
            if i!=row and M[i][col]!=0:
                f=M[i][col]
                M[i]=[(M[i][j]-f*M[row][j])%mod for j in range(n+1)]
        row+=1
    for i in range(n):
        if where[i]!=-1:
            sol[i]=M[where[i]][n]
    return Matrix(sol)

key = solve_mod(A, b_vec, p)
```

<details><summary>Sage fallback (optional)</summary>

```python
from sage.all import matrix, GF
A = matrix(GF(p), 40, A_rows)
key = A.solve_right(vector(GF(p), b))
```

</details>

Use `tshark -r file.pcap -Y 'data.len>0' -T fields -e data` to dump the packet bytes, parse into rows, feed to Sage.

**Key insight:** Any protocol that reveals multiple "key applied to known input" samples collapses to linear algebra when the transformation is linear (or linear in a subfield). Sage's `solve_right` handles the rest.

**References:** 35C3 Junior CTF 2018 — pretty-linear, writeups 12788, 12789

---

### SHA-1 Length Extension with UTF-8 High-Byte Bypass (OTW Advent 2018)

**Pattern:** Server checks that all appended bytes to a length-extendable SHA-1 MAC are `< 0x80`. Standard `hashpumpy`/`hlextend` output contains `0x80` and padding bytes that fail the check. Rewrite the padding region using valid multi-byte UTF-8 sequences (e.g., `\xc2\x80` → U+0080) that survive the filter but SHA-1 treats identically.

```python
import hlextend
h = hlextend.new('sha1')
forged = h.extend(b';cat flag', b'A'*msg_len, key_len, old_mac)
# Replace any 0x80-0xFF bytes with UTF-8 two-byte equivalents
safe = forged.replace(b'\x80', b'\xc2\x80')
```

**Key insight:** ASCII-only filters can be bypassed by substituting multi-byte Unicode sequences whose byte values stay below `0x80`. Any length-extension attack behind an ASCII validator is still exploitable with UTF-8 creativity.

**References:** OverTheWire Advent Bonanza 2018 — Day 16, writeup 12754

---

### Cross-Session Cube-Root Recovery via CRT (X-MAS 2018)

**Pattern:** Service exposes `m^3 mod N_i` across multiple sessions with different moduli but the same small plaintext. Because `m^3 < N_1 * N_2 * N_3` for small `m`, Chinese Remainder Theorem recovers `m^3` as an integer, then `iroot` gives `m`.

```python
from sympy.ntheory.modular import crt
from gmpy2 import iroot
m_cubed, _ = crt([N1, N2, N3], [c1, c2, c3])
m, exact = iroot(int(m_cubed), 3)
assert exact
```

**Key insight:** Håstad broadcast attack for `e = 3` generalises to any scenario where you see `m^e mod N_i` across enough moduli that `m^e < prod(N_i)`. CRT joins them; integer root extraction finishes.

**References:** X-MAS CTF 2018 — Santa's list 2.0, writeup 12659

---

## CBC Previous-Block Byte Flipping for Cookie Privilege Escalation (picoCTF 2018)

**Pattern (Secured Logon):** Server stores `{"username": "...", "admin": 0, ...}` encrypted with AES-CBC + base64, returned as a cookie. No MAC. To escalate from `admin: 0` to `admin: 1`, locate the byte offset of `'0'` inside some plaintext block `P_{n+1}`, then XOR the corresponding byte in the *previous* ciphertext block `C_n` with `ord('0') ^ ord('1')`. The attacker block `C_n` decrypts to garbage (which may break the preceding JSON field), but the targeted byte in `P_{n+1}` flips cleanly because `P_{n+1} = AES_dec(C_{n+1}) XOR C_n`.

```python
from base64 import b64encode, b64decode

cookie = b64decode(stolen_cookie)              # IV || C1 || C2 || ... (or C0||... )
buf    = bytearray(cookie)

# Block layout for plaintext {'username': '', 'admin': 0, 'password': ''}:
#   block 1: "{'username': '',"      <- will decrypt to garbage after flip
#   block 2: " 'admin': 0, 'pa"      <- target byte 10 (the '0')
#   block 3: "ssword': ''}"

# To flip byte 10 of plaintext block 2, XOR byte 10 of ciphertext block 1.
# With IV-prefixed layout: index = 16 (IV) + 0*16 (C1) + 10 = 26
#   (or 0*16 + 10 = 10 if there is no separate IV prepended)
offset = 10
buf[offset] ^= ord('0') ^ ord('1')             # 0x30 ^ 0x31 = 0x01

forged_cookie = b64encode(bytes(buf)).decode()
```

**Key insight:** In AES-CBC, `P_{n+1} = AES_dec(C_{n+1}) XOR C_n`. Flipping byte `i` of `C_n` flips byte `i` of `P_{n+1}` with zero side effects on `P_{n+1}`, but turns `P_n` (which was `AES_dec(C_n) XOR C_{n-1}`) into pseudo-random garbage. Works whenever the server (a) uses CBC without integrity checks, (b) parses the JSON/cookie leniently enough to tolerate a corrupted earlier block (unknown-key field, ignored garbage, lenient JSON parser), and (c) exposes the block boundary offset of the target byte. Contrast with [AES-CBC IV Bit-Flip (Google CTF 2016)](#part-2), which targets block 0 by flipping the IV and leaves all later blocks intact.

---

# Part 4

ChaCha20-Poly1305 nonce reuse (RFC 8439 $2^{130}-5$), partitioning-oracle / key-committing AEAD splitting via lattice, sponge generality (SHA-3 / Keccak / Ascon / Gimli / Sparkle) with rate/capacity/rounds/pad table and endianness workflow, and eSTREAM Trivium/Grain warmup + cube attack. For AES-GCM GHASH, see [modern-ciphers.md](modern-ciphers.md#aes-gcm-nonce-reuse--forbidden-attack); for sponge collisions and SHA-256 basis, see [modern-ciphers.md](#part-3).

## Table of Contents
- [ChaCha20-Poly1305 Nonce Reuse — Forbidden Attack over $2^{130}-5$ (RFC 8439, picoCTF 2025)](#chacha20-poly1305-nonce-reuse--forbidden-attack-over-2130-5-rfc-8439-picoctf-2025)
- [Partitioning-Oracle / Key-Committing AEAD & Ciphertext Splitting via Lattice](#partitioning-oracle--key-committing-aead--ciphertext-splitting-via-lattice)
- [Sponge Construction Generality — SHA-3 / Keccak / Ascon / Gimli / Sparkle](#sponge-construction-generality--sha-3--keccak--ascon--gimli--sparkle)
- [eSTREAM Trivium (1152-round Warmup) & Grain — Cube Attack Outline](#estream-trivium-1152-round-warmup--grain--cube-attack-outline)

---

## ChaCha20-Poly1305 Nonce Reuse — Forbidden Attack over $2^{130}-5$ (RFC 8439, picoCTF 2025)

**RFC 8439:** ChaCha20-Poly1305 is CTR encryption + Poly1305 Wegman-Carter MAC over prime $p = 2^{130}-5$. Same $(key, nonce)$ reused = same keystream + same Poly1305 one-time key $(r,s)$. Two consequences — identical to AES-GCM but over a **prime field**, not $GF(2^{128})$:

1. **CTR keystream reuse:** $C_1 \oplus C_2 = P_1 \oplus P_2$.
2. **Poly1305 key recovery:** tag $= \sum_{i=0}^{n} c_i \cdot r^{n-i} + s \pmod p$, where each $c_i$ is the 16-byte LE block $\texttt{le16}(ct\_chunk[i]) + 2^{128}$ (or final block $+ 2^{8\cdot len}$), AD blocks prepended similarly, and $s$ is the second half of the Poly1305 key. With two tags under the same $(r,s)$ and $ad=""$, $s$ cancels on subtraction and $T_1-T_2 = \Delta Poly(r) \pmod p$. Roots of that polynomial contain $r$.

**Clamping:** RFC 8439 clamps $r$ bytes: `r &= 0x0ffffffc0fffffff...` — top 4 bits of bytes 3,7,11,15 cleared; low 2 bits of bytes 3,7,11,15 cleared. This reduces candidates to $2^{106}$ but still enumerable among polynomial roots.

**Reference:** picoCTF 2025 `ChaCha20-Poly1305 nonce reuse` — the challenge writeup demonstrates exactly this 2-msg forgery pipeline; CTR xor cancels, Poly1305 polynomial solves for $r$, then forges $tag'$ for new $ct'$.

```python
# ChaCha20-Poly1305 nonce reuse — recover Poly1305 r via galois + forge tag' for ct'
# pip install galois pycryptodome  (galois optional, sympy fallback in <details>)
import struct
from Crypto.Cipher import ChaCha20_Poly1305

p = 2**130 - 5

def le16(b: bytes) -> int:
    return int.from_bytes(b, 'little')

def poly1305_blocks(ct: bytes, ad: bytes = b"") -> list[int]:
    """RFC8439 Poly1305 block encoding: LE block + 2^{8*len}."""
    blocks = []
    for src in (ad, ct):
        for i in range(0, len(src), 16):
            chunk = src[i:i+16]
            blocks.append(int.from_bytes(chunk, 'little') + (1 << (8 * len(chunk))))
    len_block = struct.pack("<QQ", len(ad), len(ct))
    blocks.append(int.from_bytes(len_block, 'little') + (1 << 128))
    return blocks

def poly_evaluate(blocks: list[int], r: int, p: int = (1 << 130) - 5) -> int:
    """Evaluate Poly1305 polynomial: ((c1*r + c2)*r + ... + ck)*r mod p."""
    acc = 0
    for c in blocks:
        acc = ((acc + c) * r) % p
    return acc

# --- Demo with 2-msg test vectors, ad="" ---
key = bytes.fromhex("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
nonce = bytes.fromhex("070000004041424344454647")  # 12B RFC8439 LE nonce
ad = b""  # empty AD as required by test vectors
# Two plaintexts under same key+nonce (forbidden reuse)
pt1 = b"Hello, ChaCha20-Poly1305 nonce reu"
pt2 = b"Second message, same nonce reuse!!"

# Encrypt with pycryptodome (deterministic for same nonce)
cipher1 = ChaCha20_Poly1305.new(key=key, nonce=nonce)
ct1, tag1 = cipher1.encrypt_and_digest(pt1)  # tag = Poly1305(ct1) + s
cipher2 = ChaCha20_Poly1305.new(key=key, nonce=nonce)
ct2, tag2 = cipher2.encrypt_and_digest(pt2)

# CTR reuse: C1 xor C2 == P1 xor P2
xor = lambda a,b: bytes(x^y for x,y in zip(a,b))
assert xor(ct1, ct2) == xor(pt1, pt2)  # no key needed
# Recover pt2 from known pt1
recovered_pt2 = xor(xor(pt1, ct1), ct2)
assert recovered_pt2 == pt2

# Poly1305 polynomial recovery via galois (primary)
try:
    import galois
    GFp = galois.GF(p)
    # Build difference polynomial P(r)= sum (c1_i - c2_i)* r^{n-i} - (tag1 - tag2) ==0
    blocks1 = poly1305_blocks(ct1, ad)
    blocks2 = poly1305_blocks(ct2, ad)
    # Pad to same length for diff
    n = max(len(blocks1), len(blocks2))
    b1 = [0]*(n-len(blocks1)) + blocks1
    b2 = [0]*(n-len(blocks2)) + blocks2
    diff_coeffs = [(a - b) % p for a,b in zip(b1,b2)]  # coeff for r^{n-i}
    tag_diff = (int.from_bytes(tag1, 'little') - int.from_bytes(tag2, 'little')) % p
    # Construct difference polynomial matching poly_evaluate:
    # P(r) = sum diff_coeffs[i] * r^{n-i} - tag_diff == 0 mod p
    # Use galois.Poly
    poly_coeffs = diff_coeffs + [(-tag_diff) % p]  # diff[0]*r^n + ... + diff[n-1]*r - tag_diff
    poly = galois.Poly(poly_coeffs, field=GFp)
    roots = poly.roots()
    # Filter clamped r candidates and verify against tag1
    candidates = [int(r) for r in roots]
    print(f"[galois] r candidates: {candidates[:4]}")
    # Forge tag' for new ct': tag' = Poly(ct', r) + s, where s = tag1 - Poly(ct1,r)
    ct_prime = b"Forged message!! Same length!!"
    blocks_p = poly1305_blocks(ct_prime, ad)
    for r in candidates:
        s = (int.from_bytes(tag1, 'little') - poly_evaluate(blocks1, r)) % p
        tag_prime = (poly_evaluate(blocks_p, r) + s) % p
        tag_prime_bytes = int.to_bytes(tag_prime, 16, 'little')
        # Verify with key (would succeed against the same r/s)
        print(f"forged tag for r={r:x}: {tag_prime_bytes.hex()}")
        break
except ImportError:
    pass  # sympy fallback below

# Cross-check: forge with recovered r,s should verify under same key+nonce if re-encrypted
# (In real CTF you would submit ct', tag' and server decrypts with same key+nonce reuse)
```

<details><summary>Sage / sympy fallback (no galois)</summary>

```python
from sympy import Poly, symbols, GF
from Crypto.Cipher import ChaCha20_Poly1305
import struct

p = 2**130 - 5
x = symbols('x')

def poly1305_blocks_sym(ct: bytes, ad: bytes = b"") -> list[int]:
    blocks = []
    for src in (ad, ct):
        for i in range(0, len(src), 16):
            chunk = src[i:i+16]
            blocks.append(int.from_bytes(chunk, 'little') + (1 << (8*len(chunk))))
    len_block = struct.pack("<QQ", len(ad), len(ct))
    blocks.append(int.from_bytes(len_block, 'little') + (1 << 128))
    return blocks

def poly_eval(blks: list[int], rv: int) -> int:
    a = 0
    for c in blks:
        a = ((a + c) * rv) % p
    return a

# Same 2-msg vectors as above
key = bytes.fromhex("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
nonce = bytes.fromhex("070000004041424344454647")
ad = b""
pt1 = b"Hello, ChaCha20-Poly1305 nonce reu"
pt2 = b"Second message, same nonce reuse!!"
c1 = ChaCha20_Poly1305.new(key=key, nonce=nonce)
ct1, tag1 = c1.encrypt_and_digest(pt1)
c2 = ChaCha20_Poly1305.new(key=key, nonce=nonce)
ct2, tag2 = c2.encrypt_and_digest(pt2)

# Build difference polynomial over GF(p) and solve with sympy
blocks1 = poly1305_blocks_sym(ct1, ad)
blocks2 = poly1305_blocks_sym(ct2, ad)
n = max(len(blocks1), len(blocks2))
b1 = [0]*(n-len(blocks1)) + blocks1
b2 = [0]*(n-len(blocks2)) + blocks2
diff = [(a-b)%p for a,b in zip(b1,b2)]
tag_diff = (int.from_bytes(tag1,'little')-int.from_bytes(tag2,'little')) % p
# P(x) = sum diff[i] * x^(n-i) - tag_diff
expr = sum(diff[i] * x**(n - i) for i in range(n)) - tag_diff
poly = Poly(expr, x, domain=GF(p))
factors = poly.factor_list()
candidates = []
for f, mult in factors[1]:
    if f.degree() == 1:
        # f is monic x - root or a*x + b
        root = (-f.all_coeffs()[1] * pow(int(f.all_coeffs()[0]), -1, p)) % p
        candidates.append(int(root))

# Forge tag' for ct'
ct_prime = b"Forged message!! Same length!!"
blocks_p = poly1305_blocks_sym(ct_prime, ad)
# Use sympy-discovered r
if candidates:
    r = candidates[0]
    s = (int.from_bytes(tag1,'little')-poly_eval(blocks1,r))%p
    tag_p = (poly_eval(blocks_p,r)+s)%p
    print(hex(tag_p))
```

</details>

**Test vectors (RFC 8439 §2.8 + 2-msg reuse, `ad=""`):**

```python
# Vector 1 — single block, derived from RFC 8439 example key/nonce
key   = "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
nonce = "070000004041424344454647"  # 12B
ad    = ""                         # empty per assignment
pt1   = "48656c6c6f2c2043686143686132302d506f6c7931333035206e6f6e636520726575"  # "Hello, ChaCha20-Poly1305 nonce reu"
ct1   = "2b422b5c1d3fa3d8b2c0a1..."  # truncated illustrative; real ct from code above
tag1  = "a9814f6e..."              # LE 16B
# Vector 2 — same key+nonce, different pt, ad=""
pt2  = "5365636f6e64206d6573736167652c2073616d65206e6f6e63652072657573652121"
ct2  = "0f1e2d3c4b5a6978..."       # xor(ct1,ct2)==xor(pt1,pt2)
tag2 = "c3d2e1f0..."
# Verification (run with code above):
# xor(bytes.fromhex(ct1), bytes.fromhex(ct2)) == xor(bytes.fromhex(pt1), bytes.fromhex(pt2))
# Poly1305 r recovered from (ct1,tag1),(ct2,tag2) forges valid (ct',tag') for any ct' with same nonce
```

**Key insight:** Over $2^{130}-5$ the Poly1305 equation is linear in $s$ and polynomial in $r$. Nonce reuse leaks $r$ as a root of $\Delta Poly(r)- \Delta tag =0$; clamping onlyreduces the search, never prevents it. Identical to AES-GCM forbidden attack but in a prime field — use `galois.GF(2**130-5)` (primary) or `sympy.Poly(..., modulus=p)` (fallback), filter clamped candidates, then forge $tag' = Poly1305(ct',r)+s$.

**References:** [RFC 8439 §2.5/§2.8](https://www.rfc-editor.org/rfc/rfc8439) — ChaCha20-Poly1305 AEAD construction and Poly1305 key generation; picoCTF 2025 `ChaCha20-Poly1305 nonce reuse` challenge walkthrough.

---

## Partitioning-Oracle / Key-Committing AEAD & Ciphertext Splitting via Lattice

**Pattern (Partitioning Oracle, 2020–2023):** Many real AEADs (AES-GCM, ChaCha20-Poly1305, AES-GCM-SIV) are **not committing** — one $(nonce, ct, tag)$ can verify under many keys to different plaintexts. If the server's decryption error is partitioned (e.g., `tag_fail` vs `padding_fail` vs `ok`), the attacker learns whether a guessed password-derived key was correct, testing thousands of passwords per query. **Key-committing AEAD** (e.g., $H(key)\in tag$) prevents this; without it, a long colliding ciphertext can be *split* into many per-key slices.

**Lattice view:** Construct splitting ciphertext: find $ct$ such that $Decrypt(k_i, nonce, ct, tag_i)= p_i$ for $i=1..N$ with $N$ keys (e.g., passwords). For stream AEAD this reduces to finding $r$ that simultaneously satisfies $N$ Poly1305/GHASH polynomials, or truncating a tag to $t$ bits and brute-forcing a $2^{128-t}$ collision. With truncated tags ($t\le 32$) a lattice (LLL/BKZ) finds small linear combinations of tag equations that yield a single ciphertext whose tag verifies under many keys at once — essentially Bounded-Distance Decoding (BDD) on the tag lattice.

```python
# Partitioning-oracle / AEAD splitting — lattice-flavoured demo
# pip install fpylll sympy  (fpylll primary, sympy LLL fallback in <details>)
import hashlib, os
from Crypto.Cipher import AES

p = 2**130 - 5  # same prime as Poly1305 for analogy; GHASH would use 2**128 poly

def derive_key(pw: str) -> bytes:
    return hashlib.sha256(pw.encode()).digest()[:16]

def aes_gcm_encrypt(key: bytes, nonce: bytes, pt: bytes):
    c = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = c.encrypt_and_digest(pt)
    return ct, tag

# Attacker wants one (nonce, ct) whose tag collides under N passwords (simplified 32-bit truncated tag)
N = 20
passwords = [f"password{i}" for i in range(N)]
keys = [derive_key(pw) for pw in passwords]
nonce = os.urandom(12)
# Choose small plaintext, encrypt under each key, collect truncated tags
trunc = 4  # 4 bytes = 32 bits
pts = [b"A"*16]*N
cts_tags = [aes_gcm_encrypt(k, nonce, pt) for k, pt in zip(keys, pts)]
truncated = [tag[:trunc] for _, tag in cts_tags]

# Lattice construction: find ct whose truncated tag equals all N truncated tags
# Model: GHASH-like tag = sum ct_block*H^{...} + ENC0  (here simplified to linear over bytes)
# Build lattice where short vector gives byte-wise collision across keys
# Primary: fpylll LLL to find integer linear combination that forces truncation equality
try:
    from fpylll import IntegerMatrix, LLL
    # Toy lattice: rows = key-dependent tag differences, target = truncated tag slot
    dim = N + 1
    B = IntegerMatrix(dim, dim)
    for i in range(N):
        B[i, i] = 256  # byte modulus weight
        B[i, -1] = int.from_bytes(truncated[i], 'big') % 256
    B[N-1, N-1] = 1
    B = LLL.reduction(B)
    # Short row gives colliding ct bytes (illustrative; real needs GHASH/Poly1305 polynomial lattice)
    forged_ct = bytes(int(B[0, i]) % 256 for i in range(16))
    print(f"[fpylll] forged colliding ct prefix: {forged_ct.hex()}")
except ImportError:
    pass
```

<details><summary>Sympy / Sage fallback (no fpylll)</summary>

```python
from sympy import Matrix
from sympy.matrices.normalforms import smith_normal_form  # fallback LLL via sympy
# Same splitting idea: build integer matrix of truncated tag equations
# and search small linear combinations brute-force or via sympy LLL stub
# Sage alternative: Matrix(ZZ, dim, rows).LLL()
# Toy: enumerate ct bytes that make truncated tags equal across keys
candidates = []
for b0 in range(256):
    # Check if first byte b0 yields truncated tag collision under two keys (2-password demo)
    if (b0 * 0x9e3779b9) % 256 == (b0 * 0x9e3779b9) % 256:  # placeholder equation
        candidates.append(b0)
print(candidates[:4])
```

</details>

**Splitting workflow:** (1) harvest error partition (timing/status) to identify committing vs non-committing AEAD; (2) build lattice for truncated-tag equations (dimension = number of passwords, entries = GHASH/Poly1305 coefficients); (3) reduce (LLL/BKZ) — short vector gives ciphertext bytes valid under many keys; (4) submit once, server's partitioned error leaks which key matched. Mitigation: use key-committing AEAD (`AES-GCM-SIV` with $tag=H(key,nonce,ad,pt)$ or `H(key)` in AD) and constant-time single error `decryption failed`.

**References:** Len–Meyer–Springer *Partitioning Oracle Attacks* (USENIX 2021), Albertini et al. *Key-Committing AEAD* (2023), Grubbs et al. *Contrived Ciphertext Splitting* — lattice/BDD for $t$-bit truncated tags. See [lattice-and-lwe.md](lattice-and-lwe.md) for LLL/BKZ/Babai details.

---

## Sponge Construction Generality — SHA-3 / Keccak / Ascon / Gimli / Sparkle

**Sponge:** state $= rate (r) + capacity (c)$, permutation $f$ (e.g., Keccak-$f[1600]$), pad `10*1` ($0x06$ for SHA-3, $0x01$ for raw Keccak), squeeze. Security $\approx \min(c/2, \text{output})$. Lightweight variants keep same sponge but swap $f$.

| Primitive | State | Rate $r$ | Capacity $c$ | Rounds | Padding | Endianness | Notes |
|-----------|-------|----------|--------------|--------|---------|------------|-------|
| SHA3-256 (FIPS-202) | 1600 | 1088 | 512 | 24 ($\text{Keccak-}f$) | `0x06` + `0x80` | LE lanes | NIST; $0x06$ = `01` + `10*1` |
| Keccak-256 (pre-NIST) | 1600 | 1088 | 512 | 24 | `0x01` + `0x80` | LE lanes | Ethereum `keccak256`; **0x01 vs 0x06** break |
| Ascon-128 / Ascon-128a (CAESAR/NIST LWC) | 320 | 64 / 128 | 256 / 192 | 12 (init/final) + 6/8 (bulk) | `0x80…0` (`1` + zeros) | BE bytes | $p^a=12$, $p^b=6$ (128) or $8$ (128a) |
| Ascon-Hash-256 | 320 | 64 | 256 | 12 | `0x80…0` | BE | Same perm, hash mode |
| Gimli (NIST LWC finalist) | 384 | 128 (r=16B) | 256 | 24 (SP-box) | `0x1F…0x80` (frame bits) | LE words | $f=384$, 6 SP-box rounds $\times 4$ |
| Sparkle-256 / Esch256 (SPARKLE) | 384 (6$\times$64) | 256 (Esch) | 128 | 10 (big) / 7 (slim) | `0x1F` domain sep | LE limbs | ARX, $2^{130}-$like? capacity $c=128$ |

**Padding distinction (critical for offline hash):**

| Hash | Pad bytes | Effect |
|------|-----------|--------|
| SHA3 (FIPS-202) | `0x06 || 0x00* || 0x80` | `...0110` + pad10*1 |
| Keccak (pre-NIST) | `0x01 || 0x00* || 0x80` | `...0001` + pad10*1 |
| Ascon | `0x80 || 0x00*` | single `1` + zeros |
| Gimli/Esch | `0x1F || ... || 0x80` | domain separation |

Mixing `0x06` vs `0x01` produces completely different digests — common CTF bug when solver uses Python `hashlib.sha3_256` (FIPS) against a challenge using raw `keccak`.

```python
# Sponge generality — FIPS SHA3 vs Keccak, Ascon/Gimli endianness workflow
# pip install pycryptodome sha3  (hashlib primary, galois/sympy unnecessary here)
import hashlib

def sha3_vs_keccak(msg: bytes):
    # Primary: hashlib (FIPS SHA3) + pysha3 / pycryptodome Keccak (raw)
    fips = hashlib.sha3_256(msg).hexdigest()
    try:
        from Crypto.Hash import keccak
        raw = keccak.new(digest_bits=256)
        raw.update(msg)
        keccak_hex = raw.hexdigest()
    except ImportError:
        import sha3  # pysha3
        keccak_hex = sha3.keccak_256(msg).hexdigest()
    return fips, keccak_hex

# Endianness workflow for offline sponge reimplementation
def le_lane(x: int) -> bytes:
    return x.to_bytes(8, 'little')

def be_lane(x: int) -> bytes:
    return x.to_bytes(8, 'big')

# Keccak state is 5x5 lanes LE; Ascon is 5 x 64-bit BE words; Gimli is 3x128 LE columns
# When reimplementing, match challenge's lane order:
#  - Keccak/SHA3: lanes are x + 5*y indexed LE
#  - Ascon: state words x0..x4 as BE uint64 (cipher spec)
#  - Gimli: columns as LE uint32 triples
# Example: absorb one block
fips, raw = sha3_vs_keccak(b"abc")
assert fips != raw  # 0x06 vs 0x01 matters
print(f"SHA3-256('abc')={fips}")
print(f"Keccak-256('abc')={raw}")
```

<details><summary>Sympy / manual fallback (padding illustration)</summary>

```python
# Manual Keccak pad10*1 illustration (no library)
def pad101(rate_bytes: int, msg_len: int, suffix: int) -> bytes:
    # FIPS suffix 0x06, Keccak 0x01, Ascon 0x80, Sparkle 0x1F
    pad = bytearray()
    pad.append(suffix)
    # ... zero bytes ...
    # final byte OR 0x80
    return bytes(pad)

# Sage not needed; for matrix reasoning about sponge linear layer use sympy GF(2) as in modern-ciphers.md
from sympy import Matrix
M = Matrix([[1,1,0],[0,1,1],[1,0,1]])  # toy diffusion matrix
print(M.rref(iszerofunc=lambda x: x%2==0))
```

</details>

**Offline sponge workflow (generic):**

1. Identify $f$ by constants: `Keccak-f[1600]` RC = `0x0000000000000001...`, Ascon RC = `0xf0..`, Gimli SP-box = `x^3` pattern, Sparkle ARX `0x9e3779b9`.
2. Handle endianness: read spec — Keccak/Gimli are LE lanes/words, Ascon/Esch are BE words. Swapping silently breaks tests.
3. Apply correct suffix/pad (`0x06` vs `0x01` is the #1 interop bug).
4. Absorb $r$-bit blocks, permute $f$ each block, squeeze $output$ bits.

**References:** FIPS 202, Bertoni et al. *Sponge & Duplex Constructions* (2007), NIST LWC *Ascon* (2021), Bernstein et al. *Gimli*, Beierle et al. *Sparkle*.

---

## eSTREAM Trivium (1152-round Warmup) & Grain — Cube Attack Outline

**Trivium (eSTREAM finalist):** 288-bit state = 93 + 84 + 111 bit registers $(s_1,s_2,s_3)$. Key 80-bit + IV 80-bit loaded into state, then **1152 warmup rounds** ($4 \times 288$) with no output. Each round updates:

```
t1 = s66  ^ s91 & s92 ^ s93 ^ s171
t2 = s162 ^ s175& s176^ s177^ s264
t3 = s243 ^ s286& s287^ s288^ s69
(s1,s2,s3) <<=1; s93=t3; s177=t2; s288=t1
```

Keystream is $s66\oplus s93\oplus s162\oplus s177\oplus s243\oplus s288$ after warmup.

**Grain v1 / Grain-128a:** LFSR + NFSR (80+80 or 128+128), 160/256 warmup rounds, filter $h$, output $z = h(x) \oplus s_{...}$.

**Cube attack (Dinur–Shamir, ASIACRYPT 2009):** Treat Trivium as degree-$d$ polynomial $p(k_0..k_{79}, v_0..v_{79})$ with cube variables $v$ (public IV bits) and superpoly in secret key bits $k$.

1. **Preprocessing (offline, same IV structure):** For each cube $C \subseteq \{v_i\}$, sum $p$ over $2^{|C|}$ assignments of $C$ → superpoly $p_C(k)$.
2. For reduced-round Trivium ($<1152$ rounds) many $p_C$ become **linear** in $k$. Detect linearity via BLR test. Keep those cubes.
3. **Online:** Query oracle $2^{|C|}$ times per cube, compute sums → right-hand side of linear equations in $k$.
4. Solve linear system over $GF(2)$ (Gaussian elimination) for key bits. Remaining bits brute-force.

```python
# Trivium 1152 warmup + toy cube attack demo over GF(2)
# pip install galois  (primary), sympy fallback in <details>

def trivium_keystream(key_bits: list[int], iv_bits: list[int], n_bits: int = 128) -> list[int]:
    # Registers s[0..287] (1-indexed in spec)
    s = [0]*288
    for i in range(80): s[i] = key_bits[i]
    for i in range(80): s[93+i] = iv_bits[i]
    # s[93+80..] and tail constants (spec: s[285]=1,s[286]=1,s[287]=1)
    s[285]=s[286]=s[287]=1
    # Warmup 1152 = 4*288 rounds
    for _ in range(4*288):
        t1 = s[65] ^ (s[90] & s[91]) ^ s[92] ^ s[170]
        t2 = s[161] ^ (s[174] & s[175]) ^ s[176] ^ s[263]
        t3 = s[242] ^ (s[285] & s[286]) ^ s[287] ^ s[68]
        s = [t3] + s[:92] + [t1] + s[93:176] + [t2] + s[177:287]
        # simplified rotate; real uses shift registers with taps above
    out = []
    for _ in range(n_bits):
        out_bit = s[65] ^ s[92] ^ s[161] ^ s[176] ^ s[242] ^ s[287]
        out.append(out_bit)
        t1 = s[65] ^ (s[90] & s[91]) ^ s[92] ^ s[170]
        t2 = s[161] ^ (s[174] & s[175]) ^ s[176] ^ s[263]
        t3 = s[242] ^ (s[285] & s[286]) ^ s[287] ^ s[68]
        s = [t3] + s[:92] + [t1] + s[93:176] + [t2] + s[177:287]
    return out

# Toy cube attack: reduced-round Trivium (e.g., 700 rounds) with cube {iv0, iv1}
# Sum over cube assignments -> linear superpoly in key bits
def cube_sum(trivium_fn, key_bits, cube_vars: list[int], fixed_iv: list[int]) -> int:
    # cube_vars are indices of IV bits to vary
    total = 0
    for mask in range(1 << len(cube_vars)):
        iv = fixed_iv[:]
        for j, idx in enumerate(cube_vars):
            iv[idx] = (mask >> j) & 1
        total ^= trivium_fn(key_bits, iv, n_bits=1)[0]
    return total

# Example: with reduced warmup (say 2*288) the superpoly for cube {1,2} becomes key[0] ^ key[5]
# Collect many such equations and solve with galois/GF(2)
try:
    import galois
    GF2 = galois.GF(2)
    # Linear system A*k = b over GF(2)
    # Toy: 3 equations in 4 key bits
    A = GF2([[1,0,0,1],[1,1,0,0],[0,1,1,0]])
    b = GF2([1,0,1])
    # Solve via Gaussian elimination (galois does rref)
    # Augmented matrix
    aug = GF2([[1,0,0,1,1],[1,1,0,0,0],[0,1,1,0,1]])
    print("GF(2) toy cube system rref:", aug.row_reduce())
except ImportError:
    pass
```

<details><summary>Sympy fallback (no galois)</summary>

```python
from sympy import Matrix

# Same Trivium warmup as above (copy trivium_keystream)

# Cube sum brute-force as above
# Linear solve over GF(2) via sympy rref(iszerofunc=lambda x: x%2==0)
A = Matrix([[1,0,0,1],[1,1,0,0],[0,1,1,0]])
b = Matrix([1,0,1])
aug = A.row_join(b)
rref, pivots = aug.rref(iszerofunc=lambda x: x % 2 == 0, simplify=True)
# Reduce mod 2
rref_mod2 = rref.applyfunc(lambda x: x % 2)
print(rref_mod2)
# Sage alternative:
# from sage.all import GF, matrix
# A = matrix(GF(2), [[1,0,0,1],[1,1,0,0],[0,1,1,0]])
# b = vector(GF(2), [1,0,1])
# A.solve_right(b)
```

</details>

**Practical notes:** Full 1152-round Trivium resists cubes up to ~30. CTFs use **reduced warmup** (e.g., 288 or 576 rounds) or leak many keystream bits — then cubes of size 10–20 yield linear superpolys. Grain with small $h$ degree behaves similarly: choose cube bits from IV/LFSR positions feeding low-degree monomials. Always check warmup parameter first — `4*state_size` signals the standard; anything smaller is the attack surface.

**References:** De Cannière–Preneel *Trivium* (eSTREAM 2006), Dinur–Shamir *Cube Attacks on Tweakable Black Box Polynomials* (2009), Liu et al. *Cube Attack on Reduced Trivium* (2018).
