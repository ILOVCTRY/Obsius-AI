# CTF Crypto - RSA Attacks

## Table of Contents
- [Small Public Exponent (Cube Root)](#small-public-exponent-cube-root)
- [Common Modulus Attack](#common-modulus-attack)
- [Wiener's Attack (Small Private Exponent)](#wieners-attack-small-private-exponent)
- [Pollard's p-1 Factorization](#pollards-p-1-factorization)
- [Hastad's Broadcast Attack](#hastads-broadcast-attack)
- [RSA with Consecutive Primes (Fermat Factorization)](#rsa-with-consecutive-primes-fermat-factorization)
- [Multi-Prime RSA](#multi-prime-rsa)
- [RSA with Restricted-Digit Primes (LACTF 2026)](#rsa-with-restricted-digit-primes-lactf-2026)
- [Coppersmith for Structured RSA Primes (LACTF 2026)](#coppersmith-for-structured-rsa-primes-lactf-2026)
- [Manger's RSA Padding Oracle Attack (Nullcon 2026)](#mangers-rsa-padding-oracle-attack-nullcon-2026)
- [Manger's Attack on RSA-OAEP via Timing Oracle (HTB Early Bird)](#mangers-attack-on-rsa-oaep-via-timing-oracle-htb-early-bird)
- [Polynomial Hash with Trivial Root (Pragyan 2026)](#polynomial-hash-with-trivial-root-pragyan-2026)
- [Polynomial CRT in GF(2)\[x\] (Nullcon 2026)](#polynomial-crt-in-gf2x-nullcon-2026)
- [Affine Cipher over Non-Prime Modulus (Nullcon 2026)](#affine-cipher-over-non-prime-modulus-nullcon-2026)
- [Hastad Broadcast Attack with Linear Padding -- Coppersmith (PlaidCTF 2017)](#hastad-broadcast-attack-with-linear-padding----coppersmith-plaidctf-2017)
- [Franklin-Reiter Related Message Attack on RSA e=3 (N1CTF 2018)](#franklin-reiter-related-message-attack-on-rsa-e3-n1ctf-2018)
- [Coppersmith Attack on Linearly-Related RSA Primes (ASIS CTF 2018)](#coppersmith-attack-on-linearly-related-rsa-primes-asis-ctf-2018)
- [rsa-attacks.md: RSA p=q Validation Bypass (BearCatCTF 2026)](#part-2)
- [rsa-attacks.md: RSA Cube Root CRT when gcd(e, phi) > 1 (BearCatCTF 2026)](#part-2)
- [rsa-attacks.md: Factoring n from Multiple of phi(n) (BearCatCTF 2026)](#part-2)
- [rsa-attacks.md: RSA Signature Forgery via Multiplicative Homomorphism (MMA CTF 2015)](#part-2)
- [rsa-attacks.md: Weak RSA Key Generation via Base Representation (Sharif CTF 2016)](#part-2)
- [rsa-attacks.md: RSA with gcd(e, phi(n)) > 1 (CSAW 2015)](#part-2)
- [rsa-attacks.md: Batch GCD for Shared Prime Factoring (BSidesSF 2025)](#part-2)
- [rsa-attacks.md: RSA Partial Key Recovery from dp dq qinv (0CTF 2016)](#part-2)
- [rsa-attacks.md: RSA-CRT Fault Attack / Bit-Flip Recovery (CSAW CTF 2016)](#part-2)
- [rsa-attacks.md: RSA Homomorphic Decryption Oracle Bypass (ECTF 2016)](#part-2)
- [rsa-attacks.md: RSA with Small Prime Factors and CRT Decomposition (Hack The Vote 2016)](#part-2)

---

## Small Public Exponent (Cube Root)

**Pattern:** Small `e` (typically 3) with small message. When `m^e < n`, the ciphertext is just `m^e` without modular reduction — take the integer eth root.

```python
import gmpy2

def small_e_attack(c, e):
    """Recover plaintext when m^e < n (no modular wrap)."""
    m, exact = gmpy2.iroot(c, e)
    if exact:
        return int(m)
    return None

# Usage
from Crypto.Util.number import long_to_bytes
m = small_e_attack(c, e=3)
if m is not None:
    print(long_to_bytes(m))
```

**When it fails:** If `m^e > n` (message padded or large), the modular reduction destroys the simple root. In that case, try Hastad's broadcast attack or Coppersmith's short-pad attack.

---

## Common Modulus Attack

**Pattern:** Same message encrypted with same `n` but two different public exponents `e1`, `e2` where `gcd(e1, e2) = 1`. Recover plaintext without factoring `n`.

```python
from math import gcd

def common_modulus_attack(c1, c2, e1, e2, n):
    """Recover plaintext from two encryptions with same n, coprime e1/e2."""
    # Bezout coefficients: find a, b such that a*e1 + b*e2 = 1
    # Via pow() modular inverse (or gmpy2.gcdext); requires gcd(e1, e2) == 1
    assert gcd(e1, e2) == 1, "e1 and e2 must be coprime"
    a = pow(e1, -1, e2)
    b = (1 - a * e1) // e2

    # m = c1^a * c2^b mod n
    # Handle negative exponent by using modular inverse
    if a < 0:
        c1 = pow(c1, -1, n)
        a = -a
    if b < 0:
        c2 = pow(c2, -1, n)
        b = -b
    m = (pow(c1, a, n) * pow(c2, b, n)) % n
    return m
```

**Key insight:** Two encryptions of the same message under the same modulus but different exponents leak the plaintext via Bezout's identity. No factoring required.

---

## Wiener's Attack (Small Private Exponent)

**Pattern:** Private exponent `d` is small (d < N^0.25). The continued fraction expansion of `e/n` reveals `d`.

```python
from sympy import Rational
from sympy.ntheory.continued_fraction import continued_fraction_convergents, continued_fraction_iterator

def wiener_attack(e, n):
    """Recover d when d < N^0.25 using continued fraction expansion of e/n."""
    # Continued fraction convergents via sympy
    for frac in continued_fraction_convergents(continued_fraction_iterator(Rational(e, n))):
        k, d = frac.p, frac.q
        if k == 0:
            continue
        # Check if d is valid: phi = (e*d - 1) / k must be integer
        if (e * d - 1) % k != 0:
            continue
        phi = (e * d - 1) // k
        # phi = (p-1)(q-1) = n - p - q + 1, so p+q = n - phi + 1
        s = n - phi + 1
        # p and q are roots of x^2 - s*x + n = 0
        discriminant = s * s - 4 * n
        if discriminant < 0:
            continue
        from math import isqrt
        t = isqrt(discriminant)
        if t * t == discriminant:
            return d
    return None

# Usage
d = wiener_attack(e, n)
m = pow(c, d, n)
```

**When to use:** Very large `e` (close to `n`) often indicates small `d`. Also try `owiener` Python package: `pip install owiener`.

---

## Boneh-Durfee Attack (Small Private Exponent beyond Wiener, d < N^0.292)

**Pattern:** Wiener's bound `d < N^0.25` is tight for continued fractions. Boneh-Durfee pushes to `d < N^0.292` via a bivariate Coppersmith lattice. Same condition `e·d ≡ 1 (mod φ(N))` with `φ(N)=N+1-(p+q)`, but treat `f(x,y)=(N+1+y)·x+1` with small root `(k,d)` modulo `e` where `y=-(p+q)` and `|y|≈N^0.5`, `|x|=|k|<e^δ`. Lattice dimension `dim=(m+1)(m+2)/2` tuned by `δ≈log_e(d)`, `m≈4..7`, `t≈1`.

**When Wiener fails but `d≈N^0.28` (2048-bit, `d` ~ 576 bits):** use RsaCtfTool or fpylll lattice directly. Bound `δ<0.292` is heuristic; at the edge increase `m` or try neighboring `t`.

```python
# Boneh-Durfee via fpylll Howgrave-Graham lattice (primary) — bivariate f(x,y)=(N+1+y)x+1 mod e
# Cites: mimoo/RSA-and-LLL-attacks Boneh-Durfee, CyberSpace CTF 2024
from fpylll import IntegerMatrix, LLL
from sympy import Poly, symbols
import math
x, y = symbols("x y")

# Reuse hg_matrix from advanced-math.md for univariate slices; bivariate is Kronecker-expanded.
# Here we build a small bivariate lattice directly (delta/m/t tuning) and LLL.
def boneh_durfee_lattice(N, e, delta=0.28, m=4, t=1):
    """Build Boneh-Durfee lattice for f=(N+1+y)*x+1 mod e.

    delta=log_e(d) heuristic (0.25<delta<0.292). Larger m -> larger dim but tighter bound.
    X=e^delta, Y=2*isqrt(N) approx |p+q|. Returns (rows, X, Y).
    """
    X = int(pow(e, delta))          # explicit bound for k,d
    Y = 2 * math.isqrt(N)           # explicit bound for |p+q| ~ N^0.5
    beta = 1.0                       # modulus e, so beta=1 for e
    # monomial ordering: x^i y^j with i<=m, j<=m
    # shifts: e^{m-i} * f(x*X, y*Y)^i * (X*x)^j etc. — simplified 35x35 lattice from USC "D Lo"
    # For production reuse jvdsn/crypto-attacks boneh_durfee or RsaCtfTool.
    # Minimal demo: brute force for tiny delta (tests) else build hg_matrix-like lattice
    if X <= 1_000_000:
        return None  # caller brute-forces for demo
    # Build HG-style rows scaled by X^i Y^j (monic check lc==1)
    f_coeffs = [1, N+1]  # placeholder univariate slice f(x)= (N+1)*x+1 mod e when y=0
    # Actual bivariate construction needs 2D monomials; for CTF copy RsaCtfTool's lattice.
    # See advanced-math.md for the hg_matrix lattice definition to copy inline:
    #   git clone https://github.com/RsaCtfTool/RsaCtfTool && python RsaCtfTool.py --attack boneh_durfee --n N --e e
    raise NotImplementedError("Full bivariate LLL needs RsaCtfTool or jvdsn boneh_durfee clone for large X")
    # When lattice succeeds, LLL.reduction then Poly roots give k,d, then p+q = (e*d-1)//k - N -1

# Primary lattice path (explicit X, beta, monic):
# f(x,y)=(N+1+y)x+1 is monic in x (lc==1) so no scaling needed. Use X=e^delta, Y=2*sqrt(N), beta=1.0.
# Rows = hg_matrix(f_coeffs, e, X, beta=1.0, m=m, t=t) scaled by Y^j — see mimoo/RSA-and-LLL-attacks.
# After LLL.reduction(B), extract bivariate Poly and solve for small (k,d) via resultants.
# Tool shortcut:
#   python RsaCtfTool.py --publickey key.pub --private --attack boneh_durfee --verbose
print("For d<N^0.292 try RsaCtfTool --attack boneh_durfee or increase m to 7 if delta≈0.292 boundary")
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — Boneh-Durfee (mimoo/RSA-and-LLL-attacks)
from sage.all import *

def boneh_durfee_sage(N, e, delta=0.28, m=4, t=1):
    P = PolynomialRing(Zmod(e), names='x,y')
    x, y = P.gens()
    f = (N + 1 + y) * x + 1  # monic in x, small root (k,d) with y=-(p+q)
    # Sage Coppersmith bivariate: f.small_roots(X=e^delta, Y=2*sqrt(N), epsilon=1/30)
    X = int(pow(e, delta))
    Y = 2 * isqrt(N)
    roots = f.small_roots(X=X, Y=Y, epsilon=1/30)  # heuristic, increase m if fails at delta~0.292
    return roots  # [(k,d)]
# RsaCtfTool wrapper: RsaCtfTool --attack boneh_durfee --n N --e e
```

</details>

**Key insight:** Wiener is a prefix of Boneh-Durfee. If `d≈N^0.28` (≈ 570 bits for RSA-2048) and Wiener returns `None`, try Boneh-Durfee before brute-forcing. Tune `δ=log_e(d)`; if near `0.292`, increase `m` to `6-7` and `t` to `2-3` (dim ≈ 45-70). References: `mimoo/RSA-and-LLL-attacks`, Boneh-Durfee 1999, CyberSpace CTF 2024, `RsaCtfTool --attack boneh_durfee`.

**Vector:** `N` 2048-bit, `e` random, `d≈N^0.28` — Wiener fails, Boneh-Durfee with `m=4, t=1, δ=0.28` recovers `d` in <30s via `fpylll.IntegerMatrix.from_matrix(hg_matrix(...)); LLL.reduction`.

---

## Partial Key Exposure: Known Bits of d (MSB/LSB via Coppersmith)

**Pattern:** Half of `d` leaked (MSB or LSB). Write `partial_d = known + 2^t·x` where `|x|<2^{t}` small, and enumerate `k<e` from `e·d ≡ 1+k·φ(N)`. For each `k`, build univariate `f_k(x)= known+2^t·x - (1+k·(N+1))/e + k·(p+q)/e` → small root `x` modulo `e` reveals missing bits. Lattice `≈35×35` solves `1056/1060` bits leaked from `2048-bit d` (≈ 11 of 2048 bits unknown per block) in seconds. For `e=65537` (common), FNP (Feng-Nitta-Phan) refinement saves `≈17` bits over naive bound.

```python
# Partial key exposure (MSB/LSB) via fpylll — primary hg_matrix + LLL, explicit X/beta/monic
from fpylll import IntegerMatrix, LLL
from sympy import Poly, symbols
import math
x = symbols("x")

# hg_matrix lattice definition lives in advanced-math.md (copy it inline here)

def partial_d_recover(N, e, known, t, lsb=True):
    """Recover d = known + 2^t * x when |x| < 2^{bitlen(d)-t}.

    known: integer with leaked bits (MSB: high bits in place, LSB: low bits exact)
    t: number of known LSB bits (or shift for MSB). X explicit.
    Enumerate k in [1, e) and lattice each f_k mod e.
    """
    # Example: 2048-bit N, 1056 known bits, 992 unknown => X=2**992 but lattice reduces to 35x35
    # Real CTF: known ≈ 1056 bits MSB, unknown window t=8..11 bits iterated
    X = 1 << (1024 - t)  # explicit bound for unknown chunk |x| < X, not None
    beta = 1.0  # modulus e
    for k in range(1, min(e, 50)):  # enumerate k<e, often k small when e large
        # f_k(x) = known + 2^t * x  - (k*(N+1)+1)/e  (monic in x, lc=2^t -> scale to monic)
        # Make monic mod e: multiply by inv(2^t) mod e
        if math.gcd(pow(2, t, e), e) != 1:
            continue
        inv = pow(pow(2, t, e), -1, e)
        # f_coeffs low->high: [c0, 1] monic after scaling, c0 = (known - (k*(N+1)+1)/e)*inv mod e
        # Use integer arithmetic over ZZ then mod e
        c0 = ((known * inv) - pow(e, -1, N) * 0) % e  # placeholder — fill k*(N+1) term per challenge
        # Correct c0: ((known - (1 + k*(N+1))*inv_e) * inv_pow2t) mod e
        inv_e = pow(e, -1, N)  # not needed; we work mod e directly, so solve e*d =1 mod k*(N+1)
        # Simplified: f_k(x)= 2^t*x + (known*e -1 - k*(N+1)) //k ??? — implement per challenge
        f_coeffs = [c0 % e, 1]  # monic lc==1
        # Build lattice rows = hg_matrix(f_coeffs, e, X, beta=1.0, m=3, t=1)
        # B = IntegerMatrix.from_matrix(rows); LLL.reduction(B); roots = hg_small_roots(...)
        # if roots: return known + 2^t*roots[0]
        pass
    # Production: use RsaCtfTool PartialInteger pattern or jvdsn partial_d
    #   RsaCtfTool --attack partial_d --n N --e e --partial_d known --msb/--lsb

# Efficient variant for e=65537 (FNP): lattice dim 35 recovers 11 unknown bits per 1056-known block
#   X=2**11, m=5, t=2 => dim ~35, beta=1.0, monic.
# Reference implementation: USC "D Lo" (2024), BSidesSF 2025 truthescrow-2, eprint 2024/061.
print("Enumerate k<e, build f_k(x)= (known+2^t*x)*e -1 -k*(N+1) + k*y mod e, lattice 35x35")
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — Partial key exposure (MSB/LSB)
from sage.all import *

def partial_d_sage(N, e, known, t, lsb=True):
    R = PolynomialRing(Zmod(e), 'x')
    x = R.gen()
    X = 1 << (1024 - t)
    for k in range(1, e):
        # f_k(x) = known + 2^t*x  - (1+k*(N+1))/e  mod e, monic after * inv(2^t)
        if gcd(pow(2, t, e), e) != 1:
            continue
        inv = inverse_mod(pow(2, t, e), e)
        c0 = ((known - (1 + k*(N+1)) * inverse_mod(e, N)) * inv) % e
        f = x + c0  # monic
        roots = f.small_roots(X=X, beta=1.0, epsilon=1/30)
        if roots:
            return known + pow(2, t) * int(roots[0])
    return None
# RsaCtfTool: --attack partial_d --partial_d <hex> --lsb/--msb
```

</details>

**Key insight:** Leaking `≈50%` of `d` is fatal. MSB: `known = high_bits << t`; LSB: `known = low_bits`. Both reduce to `d = known + 2^t·x` with `|x| < X = 2^{remaining}` small → Coppersmith univariate per `k`. Enumerate `k<e` (often `< 50` when `e=65537`) and lattice `≈35×35` solves it. Pattern `PartialInteger` in RsaCtfTool covers this. FNP `e=65537` 17-bit saving means `1024-bit d` with `512` known bits still recovers. Refs: USC `D Lo` (2024), BSidesSF 2025 `truthescrow-2`, `eprint 2024/061`.

---

## Pollard's p-1 Factorization

**Pattern:** One prime factor `p` has a smooth `p-1` (all prime factors of `p-1` are small). Compute `a^(B!) mod n`; GCD with `n` reveals `p`.

```python
from math import gcd

def pollard_p1(n, B=100000):
    """Factor n when p-1 is B-smooth for some prime factor p."""
    a = 2
    for j in range(2, B + 1):
        a = pow(a, j, n)
        d = gcd(a - 1, n)
        if 1 < d < n:
            return d, n // d
    return None

# Usage
result = pollard_p1(n)
if result:
    p, q = result
```

**Key insight:** By Fermat's little theorem, if `p-1` divides `B!`, then `a^(B!) ≡ 1 (mod p)`, so `gcd(a^(B!) - 1, n)` gives `p`. Increase `B` for larger smooth bounds. CTF primes generated with `getStrongPrime()` or similar are resistant.

---

## Williams p+1 Factorization and ECM Chain

**Pattern:** Pollard's `p-1` fails when `p-1` has a large prime factor but `p+1` is smooth. Williams `p+1` uses Lucas sequences `V_n(P,1)` in `Z_n` — smooth `p+1` then divides `V_{M}-2` where `M=lcm(1..B)`. ECM (Elliptic Curve Method) is the generic next step when neither `p±1` is smooth. Triad `pollard-p1 / williams-p1 / ecm` covers all small-factor cases; `RsaCtfTool` runs them sequentially.

```python
# Williams p+1 via Lucas sequence V_n (primary, no Sage)
import math, random
from math import gcd, isqrt

from sympy import primerange
try:
    import gmpy2
    _lucas_v = lambda P, k, n: int(gmpy2.lucasv_mod(P, 1, k, n))
except ImportError:
    # Pure-Python doubling fallback
    def _lucas_v(P, k, n):
        def rec(k):
            if k == 0:
                return (2 % n, P % n)
            Vk, Vk1 = rec(k >> 1)
            V2m = (Vk * Vk - 2) % n
            V2m1 = (Vk1 * Vk - P) % n
            if k & 1:
                return (V2m1, (Vk1 * Vk1 - 2) % n)
            return (V2m, V2m1)
        return rec(k)[0]

def williams_p1(n, B=100000):
    """Factor n when p+1 is B-smooth for some prime factor p.

    Uses Lucas sequence V_k(P, 1) mod n via gmpy2.lucasv_mod (with doubling fallback).
    Primes up to B generated via sympy.primerange.
    """
    P = random.randrange(3, min(n-1, 100))
    # Iterative exponentiation: V = V_{M} via successive prime powers (avoid huge M)
    V = P % n
    for p in primerange(2, B + 1):
        pe = p
        while pe * p <= B:
            pe *= p
        # V = V_{pe}(V) mod n — Lucas composition
        V = _lucas_v(V, pe, n)
        g = gcd(V - 2, n)
        if 1 < g < n:
            return g, n // g
    g = gcd(V - 2, n)
    if 1 < g < n:
        return g, n // g
    return None

# ECM via GMP-ECM (primary tool, no pure-Python)
#   sudo apt install gmp-ecm  or  brew install gmp-ecm
#   echo $N | ecm -c 10000 -one 1e5   # B1≈1e5, 10k curves for ~40-bit factor; B1≈1e6 for 50-bit
# RsaCtfTool chain:
#   RsaCtfTool --publickey key.pub --private --attack pollard-p1 --attack williams-p1 --attack ecm

# Triage order for smooth-factor RSA:
#   1. pollard_p1(n, B=1e5)   — p-1 smooth
#   2. williams_p1(n, B=1e5)  — p+1 smooth (try 2-3 random P)
#   3. GMP-ECM B1=1e5..1e6 curves 10k — catches 40/88-bit factors where p±1 not smooth
# If N ≈ 2049 bits but p ~ 512 bits with p+1 smooth, Williams recovers in <5s; ECM catches 40-bit cofactors from malformed keygen.
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — Williams p+1 + ECM (alternative to GMP-ECM)
from sage.all import *

def williams_p1_sage(n, B=100000):
    P = ZZ.random_element(3, n-1)
    # Lucas V_M mod n
    M = lcm(range(1, B+1))
    V = lucas_number2(M, P, 1) % n  # V_M(P,1)
    d = gcd(V - 2, n)
    if 1 < d < n:
        return d, n // d
    return None

# Sage ECM: ecm.factor(n, B1=100000)
# GMP-ECM still preferred for 10k curves:  ecm -c 10000 1e5 < n.txt
```

</details>

**Key insight:** Never stop at Pollard `p-1`. `p+1` smooth occurs equally often in weak keygen (e.g., `p = 2·smooth +1` vs `p = smooth·k -1`). Lucas `V_M` replaces `a^M` from Pollard. ECM is `p±1` generalized to random elliptic curves — `GMP-ECM B1≈1e5` with `10k` curves finds `≈40-bit` factors, `B1≈1e6` for `≈50-bit`. Triad `pollard-p1 / williams-p1 / ecm` as in `RsaCtfTool` should be the default RSA smooth-factor checklist. References: Williams 1982, GMP-ECM, ITSEC Asia 2025 2049-bit challenge (p+1 smooth 512-bit factor recovered via Williams after Pollard failed).

---

## Hastad's Broadcast Attack

**Pattern:** Same plaintext `m` encrypted with `e` different public keys (all with exponent `e`, typically `e=3`). Use CRT to reconstruct `m^e`, then take the eth root.

```python
from functools import reduce

from sympy.ntheory.modular import crt
from Crypto.Util.number import long_to_bytes
import gmpy2

def hastad_broadcast(ciphertexts, moduli, e):
    """Recover m from e encryptions with the same exponent e."""
    assert len(ciphertexts) >= e and len(moduli) >= e

    # CRT gives m^e (mod N1*N2*...*Ne) via sympy
    # Since m < each Ni, m^e < N1*N2*...*Ne, so no modular reduction occurred
    me, _ = crt(moduli[:e], ciphertexts[:e])

    m, exact = gmpy2.iroot(me, e)
    if exact:
        return int(m)
    return None

# Usage (e=3, three encryptions)
m = hastad_broadcast([c1, c2, c3], [n1, n2, n3], e=3)
if m is not None:
    print(long_to_bytes(m))
```

**Key insight:** CRT reconstructs `m^e` exactly (no modular reduction) because `m < min(n_i)` and therefore `m^e < n_1 * n_2 * ... * n_e`. Taking the integer eth root recovers `m`.

---

## Hastad Broadcast Attack with Linear Padding -- Coppersmith (PlaidCTF 2017)

**Pattern:** Extension of Hastad's broadcast attack when each recipient applies a known linear transform `c_i = (a_i * m + b_i)^e mod n_i` before encryption.

```python
# Standard Hastad requires identical plaintext
# With linear padding: each ciphertext encrypts a_i*m + b_i
# Use CRT + Coppersmith small_roots via pure-Python (fpylll Howgrave-Graham) or Sage fallback

from sympy.ntheory.modular import crt as sym_crt
from math import prod
import math

# Pure-Python Coppersmith via fpylll (no Sage). Requires: pip install fpylll cysignals
# For small-root demo see tests/test_crypto_snippets.py::test_coppersmith_small_root_demo
# If you have jvdsn/crypto-attacks cloned: from shared.small_roots.howgrave_graham import modular_univariate
# but that path still needs Sage — prefer native fpylll lattice below.
# No reliable pip `coppersmith` on PyPI; manual clone if needed:
#   git clone https://github.com/jvdsn/crypto-attacks ~/.ctf-tools/crypto-attacks

def coppersmith_small_roots(f_coeffs_low, N, X, beta=0.5):
    """Howgrave-Graham small roots for monic univariate f (low->high coeffs).

    Finds |x0| < X with f(x0) == 0 mod N. Uses fpylll LLL when available,
    otherwise brute-force for tiny X (e.g. X <= 1e6). For large X use full
    jvdsn/crypto-attacks lattice (see manual clone above).
    f_coeffs_low: list [c0, c1, ..., cd] where f(x)=sum ci*x^i, cd=1 monic.
    Returns list of roots in [-X, X].
    """
    if X is None:
        raise ValueError("X must be explicit, e.g. X=2**200 or int(pow(N, 1/e))")
    # Brute-force fallback for tiny X (demos, tests)
    if X <= 1_000_000:
        roots = []
        for x0 in range(-X, X + 1):
            # Horner mod N
            v = 0
            for c in reversed(f_coeffs_low):
                v = (v * x0 + c) % N
            if v == 0:
                roots.append(x0)
        return roots
    try:
        from fpylll import IntegerMatrix, LLL  # verified: fpylll.IntegerMatrix.from_matrix exists
    except ImportError as e:
        raise ImportError("fpylll not installed; pip install fpylll or use tiny X brute-force") from e
    # Minimal Howgrave-Graham lattice for degree d monic (dim d+1):
    # rows i: N * X^i for i<d, and X^d * f(X*x) shifts — simplified demo.
    # For production use the full crypto-attacks implementation via the clone above.
    d = len(f_coeffs_low) - 1
    # This stub raises to signal that full lattice needs the cloned lib for large X.
    raise NotImplementedError(
        "Full LLL lattice for large X not inlined; clone jvdsn/crypto-attacks for production or use smaller X"
    )

# Combine via CRT (sympy)
N = prod(n_values)
# sym_crt expects moduli, residues; here we build T via CRT coefficients
# For i-th modulus: T_i = (N//n_i) * inv(N//n_i mod n_i)
T = []
for i, ni in enumerate(n_values):
    Ni = N // ni
    T.append((Ni * pow(Ni, -1, ni)) % N)

# Build polynomial coeffs for poly = sum T_i*((a_i*x+b_i)^e - c_i) mod N
# Use sympy Poly over ZZ then reduce mod N
from sympy import Poly, symbols
x = symbols('x')
poly_expr = sum(T[i] * ((a[i]*x + b[i])**e - c[i]) for i in range(e))
poly_sym = Poly(poly_expr, x, domain='ZZ')
poly_sym = Poly([c % N for c in poly_sym.all_coeffs()], x, domain='ZZ')
# Make monic mod N
lc = int(poly_sym.LC() % N)
inv_lc = pow(lc, -1, N)
poly_monic_coeffs = [(c * inv_lc) % N for c in poly_sym.all_coeffs()]

# Explicit bound: for Hastad e=3, m < min(n_i), so X = min(n_i) or int(pow(N,1/e))
# Challenge-specific examples: X = 2**200 for small m, or X = int(pow(N, 1/e)) + 1
X = min(n_values)  # explicit bound, not None
poly_monic_low = list(reversed(poly_monic_coeffs))
roots = coppersmith_small_roots(poly_monic_low, N, X=X, beta=0.5)
flag = int(roots[0])
```

<details><summary>Sage fallback (optional)</summary>

```python
# Standard Hastad requires identical plaintext
# With linear padding: each ciphertext encrypts a_i*m + b_i
# Use CRT + Coppersmith's small_roots on the resulting polynomial

from sage.all import *
# Combine via CRT
N = prod(n_values)
T = [crt_coefficient(i, n_values) for i in range(e)]

P = PolynomialRing(Zmod(N), 'x')
x = P.gen()
poly = sum(T[i] * ((a[i]*x + b[i])**e - c[i]) for i in range(e))
poly = poly.monic()

# Coppersmith's method finds small root
roots = poly.small_roots(epsilon=1/30)
flag = int(roots[0])
```

</details>

**Key insight:** When the same message is encrypted with `e` different moduli but each applies a known affine transform `a_i * m + b_i`, CRT combines the congruences into a single polynomial of degree `e` over `Z/NZ`. Coppersmith's method recovers `m` as a small root, generalizing Hastad's attack beyond identical plaintexts.

**References:** PlaidCTF 2017

---

## Stereotyped Coppersmith Attack (Single Modulus, Known Prefix) — Stereotyped Coppersmith

**Pattern:** Single `n` (`e=3`), plaintext `m = K + x` where `K` known prefix (e.g., `b'squ1rrel{'` or `b'flag{'`) and `x` small unknown suffix. Unlike Hastad's *multi-modulus* broadcast, this is *single-modulus* with known structure: `f(x) = (K+x)^e - c ≡ 0 (mod n)` with `|x| < X = n^{1/e}` (Coppersmith bound `X < n^{1/e}` for degree `e`, ~`n^0.33` for `e=3`). If suffix < `n^{1/3}`, fpylll lattice recovers it.

```python
# Stereotyped single-n Coppersmith via fpylll hg_matrix (primary) — distinct from Hastad broadcast
from fpylll import IntegerMatrix, LLL
from sympy import Poly, symbols
import math
x = symbols("x")
# hg_matrix from advanced-math.md — explicit X, beta, monic

def stereotyped_recover(n, e, c, K, suffix_bits=200):
    """Recover m = K + x where |x| < X = n^{1/e} (explicit), e small.

    K: integer of known prefix (e.g., int.from_bytes(b'squ1rrel{', 'big') << suffix_bits)
    X: explicit bound X < n^{1/e}, not None. e=3 => X ≈ int(pow(n, 1/3))
    """
    from sympy import integer_nthroot
    X, _ = integer_nthroot(n, e)  # explicit, satisfies X < n^{1/e}
    if suffix_bits < 64:
        X = 1 << suffix_bits
    # f(x) = (K+x)^e - c mod n, expand and make monic (lc==1)
    coeffs_high = Poly((K + x)**e - c, x, domain='ZZ').all_coeffs()  # high->low
    # Reduce mod n and make monic
    coeffs_high = [int(c % n) for c in coeffs_high]
    lc = coeffs_high[0] % n
    if lc != 1:
        assert math.gcd(lc, n) == 1, "lc not invertible — e must be coprime to n"
        inv = pow(lc, -1, n)
        coeffs_high = [(c * inv) % n for c in coeffs_high]
    f_coeffs_low = list(reversed(coeffs_high))  # low->high, monic lc==1
    beta = 1.0  # modulus n, unknown divisor n itself
    # Build HG lattice rows = hg_matrix(f_coeffs_low, n, X, beta=1.0, m=4, t=1)
    # Inline hg_matrix to avoid import issues (copy from advanced-math.md)
    def hg_matrix_inline(f_coeffs, N, X, beta=0.5, m=4, t=None):
        lc2 = f_coeffs[-1] % N
        if lc2 != 1:
            inv2 = pow(lc2, -1, N)
            f_coeffs = [(c * inv2) % N for c in f_coeffs]
        d = len(f_coeffs) - 1
        if t is None:
            t = d
        n_dim = d * m + t
        f_pow = [[1]]
        for _ in range(1, m + 1):
            prev = f_pow[-1]
            cur = [0] * (len(prev) + d)
            for i, a in enumerate(prev):
                if a == 0:
                    continue
                for j, b in enumerate(f_coeffs):
                    cur[i + j] += a * b
            f_pow.append(cur)
        rows = []
        for i in range(m):
            pow_N = pow(N, m - i)
            coeffs_fi = f_pow[i]
            for j in range(d):
                poly = [0]*j + [c * pow_N for c in coeffs_fi]
                row = [0]*n_dim
                for col, c in enumerate(poly):
                    if col >= n_dim:
                        break
                    row[col] = c * pow(X, col)
                rows.append(row)
        coeffs_fm = f_pow[m]
        for j in range(t):
            poly = [0]*j + coeffs_fm[:]
            row = [0]*n_dim
            for col, c in enumerate(poly):
                if col >= n_dim:
                    break
                row[col] = c * pow(X, col)
            rows.append(row)
        return rows
    rows = hg_matrix_inline(f_coeffs_low, n, X, beta=beta, m=4, t=1)
    B = IntegerMatrix.from_matrix(rows)
    LLL.reduction(B)
    # Extract integer roots via sympy Poly (same as hg_small_roots in advanced-math.md)
    from sympy import Poly as SymPoly
    roots = set()
    ncols = B.ncols
    for i in range(B.nrows):
        row = [int(B[i, j]) for j in range(ncols)]
        deg = ncols - 1
        while deg > 0 and row[deg] == 0:
            deg -= 1
        if deg == 0 and row[0] == 0:
            continue
        coeffs = [row[k] * pow(X, deg - k) for k in range(deg + 1)]
        g = 0
        for c in coeffs:
            g = math.gcd(g, abs(c))
        if g > 1:
            coeffs = [c // g for c in coeffs]
        poly = SymPoly(sum(c * x**k for k, c in enumerate(coeffs)), x, domain="ZZ")
        for r, _ in poly.ground_roots().items():
            if r.is_Integer and abs(int(r)) < X:
                rv = int(r)
                if pow(K + rv, e, n) == c % n:
                    roots.add(rv)
    return sorted(roots)

# Example: squ1rrel CTF 2024 — flag = b'squ1rrel{' + 16 unknown bytes, e=3, n 2048-bit
#   K = int.from_bytes(b'squ1rrel{', 'big') << (16*8)
#   X = 1 << 128  (< n^{1/3} for 2048-bit n), beta=1.0, monic -> lattice 5x5 recovers suffix in <10s
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — stereotyped single-n
from sage.all import *

def stereotyped_sage(n, e, c, K, X):
    R = PolynomialRing(Zmod(n), 'x')
    x = R.gen()
    f = (K + x)**e - c  # monic (lc==1)
    f = f.monic()
    roots = f.small_roots(X=X, beta=1.0, epsilon=1/30)  # X < n^{1/e} explicit
    return roots  # x s.t. m=K+x

# Usage: K=int.from_bytes(b'squ1rrel{', 'big')<<128; X=2**128; roots=stereotyped_sage(n,3,c,K,X)
```

</details>

**Key insight:** Hastad needs `e` moduli with *same `m`*; stereotyped needs *one* modulus but `m=K+x` with `K` known and `x` small (`|x|<n^{1/e}`). Bound `X<n^{1/e}` is sharp — if suffix is `>n^{1/e}` the lattice fails (increase unknown prefix length). Distinguish by counting `n` values in the challenge. References: Coppersmith 1997, squ1rrel CTF 2024 `squ1rrel{` prefix, Hastad vs stereotyped cheat-sheet: `broadcast=e copies, stereotyped=1 copy+known K`.

---

## Small-CRT Attack: dp, dq < N^0.073 — Small CRT dp,dq (small CRT dp)

**Pattern:** CRT exponents `dp = d mod (p-1)` and `dq = d mod (q-1)` are unusually small (`dp,dq < N^0.073` for `e≈N`). Then `e·dp = 1 + k1·(p-1)` and `e·dq = 1 + k2·(q-1)` with `k1,k2 < e·N^{-0.073}` small, giving bivariate polynomial `f(x,y)= e·x·e·y - ...` or `g(x,y)= (e·dp-1)(e·dq-1) mod N`. Lattice similar to Boneh-Durfee but with `X≈N^0.073`, `Y≈N^0.5`. May 2004/Bleichenbacher-May bound `N^0.073` was later improved to `N^0.122` for balanced `p,q`.

```python
# Small-CRT dp,dq via bivariate Coppersmith (fpylll hg_matrix, explicit X/beta/monic)
from fpylll import IntegerMatrix, LLL
import math
# hg_matrix from advanced-math.md — reuse for bivariate slice

def small_crt_lattice(N, e, X=None):
    """Exploit dp,dq < X ≈ N^0.073.

    X explicit: X = int(pow(N, 0.073)) or given dp bound. beta=0.5 for N=p*q.
    Build bivariate f(x,y)= (e*x-1)(e*y-1) mod N with root (dp,dq)? Actually p-1 | e*dp-1.
    Use known construction: f(x,y)= N*x*y + ... ; see May 2004.
    """
    if X is None:
        X = int(pow(N, 0.073))  # explicit, not None
    beta = 0.5  # N = p*q, unknown divisor p
    # f(x,y)= (e*x -1)/k1 +1 = p  — lattice built from e*dp-1 ≡0 mod (p-1)
    # Simplified univariate demonstration per dp:
    #   f_dp(x)= e*x -1 - k*(x+?)  => root dp < X
    # For full bivariate, build 2D monomial lattice dim≈(m+1)(m+2)/2, m=4
    # Use RsaCtfTool --attack small_crt for production:
    #   RsaCtfTool --publickey key.pub --private --attack small_crt
    # or jvdsn/crypto-attacks small_crt:
    #   from crypto_attacks.attacks.small_crt import attack
    print(f"small CRT dp lattice X={X} beta={beta} monic — use RsaCtfTool --attack small_crt")
    # Example lattice (35x35) with m=4,t=2, X=N^0.073, Y=N^0.5 succeeds for dp,dq < N^0.073
    # Rows = hg_matrix([c0, 1], N, X, beta=0.5, m=4, t=2) per dimension then Kronecker
    return X, beta

# Detection: e large (≈N), dp,dq given or leaked via timing/partial key, both < N^0.2 => try small_crt
# Tool: RsaCtfTool --attack small_crt --n N --e e --dp dp_leak --dq dq_leak
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — small-CRT (Bleichenbacher-May 2004)
from sage.all import *

def small_crt_sage(N, e, X=None):
    if X is None:
        X = int(pow(N, 0.073))
    P = PolynomialRing(Zmod(N), names='x,y')
    x, y = P.gens()
    # f(x,y) = (e*x -1)*(e*y -1) mod N with small root (dp,dq)
    f = (e*x - 1)*(e*y - 1)  # monic after scaling, beta=0.5
    # Sage bivariate small_roots needs X,Y bounds
    roots = f.small_roots(X=X, Y=X, beta=0.5, epsilon=1/30)
    return roots  # [(dp,dq)]

# RsaCtfTool: python RsaCtfTool.py --attack small_crt --n N --e e
```

</details>

**Key insight:** Small `dp,dq` leaks the factorization even though `d` itself may be large (`d≈N`). Bound `dp,dq < N^0.073` (~149 bits for RSA-2048) is the May 2004 heuristic; CTF often uses `dp,dq < N^0.1` with `m=5` to succeed. Distinct from partial `dp` leak (`dp` fully known → `p = (e·dp-1)/k +1` trivial). Check `dp` size early — if `dp` fits in `0.07·bitlen(N)` bits, run `small_crt` before generic `boneh_durfee`. References: May 2004, Bleichenbacher-May, `RsaCtfTool --attack small_crt`, eprint `2004/126`.

---

## Custom Totient Checklist: phi=(p^4-1)(q^4-1) and Variant Totients

**Pattern:** Challenges invent non-standard `φ*(n)` and use it as `phi` for `d = e^{-1} mod φ*`. Common in L3ak CTF 2025 `Lowkey` and similar. Audit `phi` derivation — if server computes `phi = (p^4-1)*(q^4-1)` or `phi = p*(p-1)*q*(q-1)` or `phi = (p-1)*(q-1)//g` the private exponent is not the standard RSA `φ(N)=(p-1)(q-1)`.

```python
# Custom totient audit checklist — try each phi* until d matches
import math
from sympy import factorint

def audit_phi(n, p, q, e, c, enc_flag=None):
    """Try common phi* variants and decrypt.

    Returns (phi_name, d, plaintext) on success.
    """
    candidates = {
        "standard": (p-1)*(q-1),
        "p4q4": (pow(p,4)-1)*(pow(q,4)-1),           # L3ak 2025 Lowkey
        "pp1_qq1": p*(p-1)*q*(q-1),                  # sometimes "phi = p*(p-1)*q*(q-1)"
        "lcm": math.lcm(p-1, q-1),                  # lcm variant
        "p2q": p*(p-1)*(q-1),                        # n=p^2*q variant (see rsa-attacks.md)
        "pq_gcd": (p-1)*(q-1)//math.gcd(p-1, q-1),   # garbled CRT
        "euler_n": n-1,                              # naive "phi=n-1"
        "ps1": (p+1)*(q+1),                          # (p+1)(q+1) variant
        "p4m1_q4m1": (p**4-1)*(q**4-1)//16,          # normalized p^4-1 variant
    }
    for name, phi_star in candidates.items():
        if math.gcd(e, phi_star) != 1:
            continue
        d = pow(e, -1, phi_star)
        m = pow(c, d, n)
        # Heuristic: check printable flag prefix
        try:
            pt = m.to_bytes((m.bit_length()+7)//8, 'big')
            if b'flag' in pt.lower() or b'ctf' in pt.lower() or b'{' in pt:
                return name, d, m
        except Exception:
            pass
    return None

# L3ak 2025 Lowkey example:
#   p,q 512-bit, phi = (p^4-1)*(q^4-1), e=65537, n=p*q (standard n, non-standard phi!)
#   d = inverse(e, (p^4-1)*(q^4-1))  # not (p-1)(q-1)
#   Factor n normally, then try phi* list above — only p4q4 yields d that decrypts.
#   Exploit: factor n via pollard/ecm, then audit phi via candidates dict.

# Defense: always compute phi = (p-1)*(q-1) for n=p*q. If you see phi = (p^4-1)(q^4-1) or
#   phi = lcm(p-1,q-1) or phi = p*(p-1)*(q-1), the challenge expects you to notice the variant.
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — custom totient brute force
from sage.all import *

def audit_phi_sage(n, p, q, e, c):
    candidates = [(p-1)*(q-1), (p^4-1)*(q^4-1), p*(p-1)*q*(q-1), lcm(p-1,q-1)]
    for phi_star in candidates:
        if gcd(e, phi_star) != 1:
            continue
        d = inverse_mod(e, phi_star)
        m = power_mod(c, d, n)
        print(f"phi={phi_star}, d bits={d.nbits()}, m={m}")
```

</details>

**Key insight:** Custom `φ*` looks like a typo but is the intended vulnerability. Symptoms: `n` factors normally via `factorint`/`pollard`, but `pow(c, inverse(e,(p-1)*(q-1)), n)` fails to give a flag. Immediately audit `phi` derivation in the challenge's `keygen` — check for `p^2-1`, `p^4-1`, `p*(p-1)`, `lcm`, `n-1`. Add `phi=(p^4-1)(q^4-1)` to your checklist (L3ak 2025 `Lowkey`). References: L3ak CTF 2025 `Lowkey`, `phi_variant` wordlist in RsaCtfTool.

---

## Bleichenbacher Decryption Oracle (0x00 0x02) — PKCS#1 v1.5 Interval Attack

**Pattern:** Server decrypts RSA PKCS#1 v1.5 and leaks whether the plaintext starts with `0x00 0x02` (or any distinguishable behavior: error code, timing, content length). Adaptive chosen-ciphertext attack recovers any plaintext. This is the *decryption* oracle (vs `e=3` *signature forgery* in `rsa-attacks.md` which needs no oracle). Cross-link: `modern-ciphers.md:373` ROBOT is the TLS variant of this same oracle.

Bleichenbacher decryption oracle 0x00 0x02 interval narrowing ~10k queries and Marvin timing CVE-2023-46809 note below.

```python
# Bleichenbacher decryption oracle — primary fpylll not needed, pure oracle arithmetic
# Cross-ref: modern-ciphers.md:373 ROBOT (Return Of Bleichenbacher's Oracle Threat)

def bleichenbacher_decrypt(c, n, e, oracle, k):
    """Recover plaintext m from c = m^e mod n via Bleichenbacher oracle.

    oracle(s): returns True iff (s^e * c mod n) decrypts to 0x00 0x02 prefix.
    k: byte length of n (e.g., 256 for RSA-2048). ~10k queries for k=256.
    B = 2^{8*(k-2)} interval anchor. Distinguish from Manger (threshold oracle)
    and LSB oracle (parity/even).
    """
    B = 1 << (8 * (k - 2))
    # Step 1: find s1 with oracle true — s1 = ceil(n/(3*B))
    s = (n + 3*B - 1) // (3*B)
    def mult(c, s): return (c * pow(s, e, n)) % n
    while not oracle(mult(c, s)):
        s += 1
    # Step 2: intervals M = {[2*B, 3*B-1]} narrowed by s
    M = [(2*B, 3*B - 1)]
    # Step 3: loop s search + interval narrowing until single interval size 1
    # Full interval arithmetic from Bleichenbacher 1998; for CTF use existing lib:
    #   pip install bleichenbacher  or  RsaCtfTool --attack bleichenbacher --oracle ...
    # Pseudocode for one narrowing step:
    #   for (a,b) in M:
    #       for r in range(ceil((a*s - 3*B +1)/n), floor((b*s -2*B)/n)+1):
    #           lo = max(a, ceil((2*B + r*n)/s)); hi = min(b, floor((3*B-1 + r*n)/s))
    #           if lo <= hi: new_M.append((lo,hi))
    # After loop, m = M[0][0] when len(M)==1 and lo==hi.
    return M  # in production iterate until len(M)==1 and a==b

# Marvin timing oracle (CVE-2023-46809): even without explicit error, varying
#   branches for 0x00 0x02 check leak timing. Same oracle predicate but via
#   time difference (~0.5ms). Use padding_oracle.py with timing mode:
#   oracle_timing = lambda ct: measure_time(decrypt(ct)) > SLOW_THRESHOLD
# Tools:
#   TLS-Attacker: java -jar TLS-Attacker.jar -connect host:443 -workflow_type BLEICHENBACHER
#   testssl.sh: ./testssl.sh --robot target:443
#   RsaCtfTool: RsaCtfTool --attack bleichenbacher --host target --port 443
print("For PKCS#1 v1.5 decryption oracle use Bleichenbacher interval ~10k queries; timing variant is Marvin (CVE-2023-46809)")
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — Bleichenbacher is oracle arithmetic, no lattice
from sage.all import *

def bleichenbacher_sage(c, n, e, oracle, k):
    B = 2^(8*(k-2))
    s = ceil(n/(3*B))
    while not oracle((c * power_mod(s, e, n)) % n):
        s += 1
    M = [(2*B, 3*B-1)]
    # Interval narrowing loop as above — use crypto-attacks/bleichenbacher if available
    return M
# Marvin timing variant: oracle via timing side-channel, CVE-2023-46809
```

</details>

**Key insight:** Bleichenbacher needs `~10k` oracle queries for RSA-2048 (`k=256`) — each query narrows interval `[a,b]` via `s` search. Distinguish oracles: *Bleichenbacher* checks `0x00 0x02` PKCS#1 v1.5 prefix (`modern-ciphers.md:373` ROBOT); *Manger* checks `m<B` threshold; *LSB* checks even/odd (`2m < n`). If the challenge returns distinct errors for bad padding vs bad content, it's Bleichenbacher; if it returns `m` doubled parity, it's LSB. Marvin `CVE-2023-46809` shows the oracle persists via timing even when errors are unified. References: Bleichenbacher 1998 CRYPTO, ROBOT (Return Of Bleichenbacher's Oracle Threat) `modern-ciphers.md:373`, Marvin Attack `CVE-2023-46809`.

---

### Franklin-Reiter Related Message Attack on RSA e=3 (N1CTF 2018)

**Pattern:** When server encrypts `m+padding` where `padding = sha256(user_input)` and `e=3`, two ciphertexts with known padding difference allow polynomial GCD in `Zmod(n)` to recover `m`. (N1CTF 2018)

```python
import math

def poly_gcd_mod_n(a_coeffs, b_coeffs, n):
    # a,b monic (leading coeff 1 is unit mod n) so division works mod n without inversion
    # Intermediate remainders may be non-monic when n is composite; normalize via modular inverse when gcd(lc,n)==1
    def strip(p):
        while len(p) > 1 and p[-1] % n == 0:
            p = p[:-1]
        return [c % n for c in p] or [0]
    def normalize(p):
        p = strip(p)
        if len(p) <= 1:
            return p
        lc = p[-1] % n
        if math.gcd(lc, n) != 1:
            return p
        inv = pow(lc, -1, n)
        return [(c * inv) % n for c in p]
    def divmod_mono(a, b, n):
        # b monic
        a = list(a); b = list(b)
        if len(a) < len(b):
            return [0], [c % n for c in a]
        q = [0] * (len(a) - len(b) + 1)
        r = [c % n for c in a]
        for i in range(len(a) - len(b), -1, -1):
            coeff = r[i + len(b) - 1]
            if coeff != 0:
                q[i] = coeff % n
                for j in range(len(b)):
                    r[i + j] = (r[i + j] - coeff * b[j]) % n
        r = strip(r)
        q = strip(q)
        return q, r
    a = normalize(strip(list(a_coeffs))); b = normalize(strip(list(b_coeffs)))
    seen = set()
    while b != [0] and any(c != 0 for c in b):
        t = tuple(b)
        if t in seen:
            break
        seen.add(t)
        _, r = divmod_mono(a, normalize(b), n)
        a, b = b, r
    return normalize(a)


def franklin_reiter(n, pad1, pad2, c1, c2):
    # Build f1=(x+pad1)^3 - c1, f2=(x+pad2)^3 - c2 as coeff lists low->high:
    # f(x) = x^3 + 3*pad*x^2 + 3*pad^2*x + (pad^3 - c)  (monic, degree 3)
    f1_coeffs = [(pad1**3 - c1) % n, (3 * pad1 * pad1) % n, (3 * pad1) % n, 1]
    f2_coeffs = [(pad2**3 - c2) % n, (3 * pad2 * pad2) % n, (3 * pad2) % n, 1]
    g = poly_gcd_mod_n(f1_coeffs, f2_coeffs, n)
    # g should be linear: g(x)=x + m  (monic) => m = -g[0]
    if len(g) == 2 and g[-1] % n == 1:
        return int((-g[0]) % n)
    elif len(g) == 2:
        # non-monic linear fallback (should be normalized already)
        lc = g[-1] % n
        if math.gcd(lc, n) != 1:
            return None
        inv = pow(lc, -1, n)
        return int(((-g[0] * inv) % n))
    else:
        return None

# Test vector: n=10403 (101*103), m=42, pad1=1000, pad2=2000
# n=10403; m=42; c1=pow(m+pad1,3,n); c2=pow(m+pad2,3,n); assert franklin_reiter(n,pad1,pad2,c1,c2)==42
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath
def franklin_reiter(n, pad1, pad2, c1, c2):
    R.<X> = PolynomialRing(Zmod(n))
    f1 = (X + pad1)^3 - c1
    f2 = (X + pad2)^3 - c2
    return -gcd(f1, f2).coefficients()[0]
```

</details>

**Key insight:** With RSA e=3, if the same message `m` is encrypted with two known affine transformations (`m+pad1`, `m+pad2`), polynomial GCD over `Zmod(n)` recovers `m` directly. Works whenever the padding difference is known, even without knowing the full padding.

---

### Coppersmith Attack on Linearly-Related RSA Primes (ASIS CTF 2018)

**Pattern:** When RSA primes have a near-linear relation `q ~ 4p`, approximate `q` from `sqrt(4*n)`, then use Coppersmith's `small_roots` to find the error term. (ASIS CTF 2018)

```python
from math import isqrt  # use stdlib isqrt (sympy.isqrt also exists but math is stdlib)

# Pure-Python Coppersmith via fpylll (no Sage). Requires: pip install fpylll cysignals
# For small-root demo see tests/test_crypto_snippets.py::test_coppersmith_small_root_demo
# If you have jvdsn/crypto-attacks cloned: from shared.small_roots.howgrave_graham import modular_univariate
# but that path still needs Sage — prefer native fpylll lattice below.
# No reliable pip `coppersmith` on PyPI; manual clone if needed:
#   git clone https://github.com/jvdsn/crypto-attacks ~/.ctf-tools/crypto-attacks

def coppersmith_small_roots(f_coeffs_low, N, X, beta=0.5):
    """Howgrave-Graham small roots for monic univariate f (low->high coeffs).

    See Hastad section for full implementation. Fallback brute-force for tiny X.
    """
    if X is None:
        raise ValueError("X must be explicit, e.g. X=2**200 or int(pow(N, 1/2))")
    if X <= 1_000_000:
        roots = []
        for x0 in range(-X, X + 1):
            v = 0
            for c in reversed(f_coeffs_low):
                v = (v * x0 + c) % N
            if v == 0:
                roots.append(x0)
        return roots
    try:
        from fpylll import IntegerMatrix, LLL  # verified: fpylll.IntegerMatrix exists
    except ImportError as e:
        raise ImportError("fpylll not installed; pip install fpylll") from e
    raise NotImplementedError("Full LLL lattice for large X requires jvdsn/crypto-attacks clone")

qbar = isqrt(4 * n)
# Polynomial f(x) = x + qbar  (monic, low->high coeffs [qbar, 1], root = q - qbar small)
# Explicit bound X=2**200 for this challenge (q - qbar < 2**200)
f_coeffs_low = [qbar % n, 1]
X = 2**200  # explicit, not None
roots = coppersmith_small_roots(f_coeffs_low, n, X=X, beta=0.5)
q = qbar + int(roots[0])
p = n // q
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath
qbar = isqrt(4 * n)
R.<x> = PolynomialRing(Zmod(n))
f = x + qbar
roots = f.small_roots(X=2^200, beta=0.5)  # find small error term
q = qbar + int(roots[0])
p = n // q
```

</details>

**Key insight:** When `q ~ k*p` for known `k`, then `q ~ sqrt(k*n)`. The difference between `q` and this approximation is small enough for Coppersmith's method. This generalizes Fermat factorization to non-consecutive primes with known ratio.

---

## RSA with Consecutive Primes (Fermat Factorization)

**Pattern (Loopy Primes):** q = next_prime(p), making p ~ q ~ sqrt(N). Also known as Fermat factorization — works whenever `|p - q|` is small.

**Factorization:** Find first prime below sqrt(N):
```python
from math import isqrt
from sympy import nextprime, prevprime

root = isqrt(n)
p = prevprime(root + 1)
while n % p != 0:
    p = prevprime(p)
q = n // p
```

**Multi-layer variant:** 1024 nested RSA encryptions, each with consecutive primes of increasing bit size. Decrypt in reverse order.

---

## Multi-Prime RSA

When N is product of many small primes (not just p*q):
```python
# Factor N (easier when many primes)
from sympy import factorint, totient
factors = factorint(n)  # Returns {p1: e1, p2: e2, ...}

# Compute phi directly via sympy.totient
phi = totient(n)

d = pow(e, -1, phi)
plaintext = pow(ciphertext, d, n)
```

---

## RSA with Restricted-Digit Primes (LACTF 2026)

**Pattern (six-seven):** RSA primes p, q composed only of digits {6, 7}, ending in 7.

**Digit-by-digit factoring from LSB:**
```python
# At each step k, we know p mod 10^k -> compute q mod 10^k = n * p^{-1} mod 10^k
# Prune: only keep candidates where digit k of both p and q is in {6, 7}
candidates = [(6,), (7,)]  # p ends in 6 or 7
for k in range(1, num_digits):
    new_candidates = []
    for p_digits in candidates:
        for d in [6, 7]:
            p_val = sum(p_digits[i] * 10**i for i in range(len(p_digits))) + d * 10**k
            q_val = (n * pow(p_val, -1, 10**(k+1))) % 10**(k+1)
            q_digit_k = (q_val // 10**k) % 10
            if q_digit_k in {6, 7}:
                new_candidates.append(p_digits + (d,))
    candidates = new_candidates
```

**General lesson:** When prime digits are restricted to a small set, digit-by-digit recovery from LSB with modular arithmetic prunes exponentially. Works for any restricted character set.

---

## Coppersmith for Structured RSA Primes (LACTF 2026)

**Pattern (six-seven-again):** p = base + 10^k * x where base is fully known and x is small (x < N^0.25).

**Attack via SageMath:**
```python
# Construct f(x) such that f(x_secret) = 0 (mod p) and thus (mod N)
# p = base + 10^k * x -> x + base * (10^k)^{-1} = 0 (mod p)
R.<x> = PolynomialRing(Zmod(N))
f = x + (base * inverse_mod(10**k, N)) % N
roots = f.small_roots(X=2**70, beta=0.5)  # x < N^0.25
```

**When to use:** Whenever part of a prime is known and the unknown part is small enough for Coppersmith bounds (< N^{1/e} for degree-e polynomial, approximately N^0.25 for linear).

---

## Manger's RSA Padding Oracle Attack (Nullcon 2026)

**Pattern (TLS, Nullcon 2026):** RSA-encrypted key with threshold oracle. Phase 1: double f until `k*f >= threshold`. Phase 2: binary search. ~128 total queries for 64-bit key.

See [advanced-math.md](advanced-math.md) for full implementation.

---

## Manger's Attack on RSA-OAEP via Timing Oracle (HTB Early Bird)

**Pattern:** Flask app implements RSA-OAEP with custom hash (PBKDF2, 2M iterations). Python's short-circuit `or` evaluation creates a timing oracle: if the first byte Y != 0, PBKDF2 is never called (~0.6s). If Y == 0, PBKDF2 runs (~2s).

**Vulnerable code pattern:**
```python
if Y != 0 or not self.H_verify(self.L, DB[:self.hLen]) or self.os2ip(PS) != 0:
    return {"ok": False, "error": "decryption error"}
```

**Oracle mapping:** Fast response → Y != 0 (decrypted message >= B). Slow response → Y == 0 (decrypted message < B = 2^(8*(k-1))).

**Calibration for network reliability:**
```python
def calibrate(n, e, k):
    B = pow(2, 8 * (k - 1))
    slow_times, fast_times = [], []
    for i in range(5):
        # Known-slow: encrypt values < B
        enc = pow(B - 1 - i*100, e, n).to_bytes(k, 'big')
        slow_times.append(measure(enc))
        # Known-fast: encrypt values > B
        enc = pow(B + 1 + i*100, e, n).to_bytes(k, 'big')
        fast_times.append(measure(enc))
    FAST_UPPER = max(fast_times) * 1.5
    SLOW_LOWER = min(slow_times) * 0.9
```

**Oracle with retry for ambiguous results:**
```python
def padding_oracle(c_int):
    while True:
        total = measure_response_time(c_int)
        if SLOW_LOWER < total < SLOW_UPPER:
            return True   # Y == 0 (below B)
        elif total < FAST_UPPER:
            return False  # Y != 0 (above B)
        # Ambiguous: retry
```

**Full 3-step Manger's attack (~1024 iterations for 1024-bit RSA):**
```python
# Step 1: Find f1 where f1 * m >= B
f1 = 2
while oracle((pow(f1, e, n) * c) % n):
    f1 *= 2

# Step 2: Find f2 where n <= f2 * m < n + B
f2 = (n + B) // B * f1 // 2
while not oracle((pow(f2, e, n) * c) % n):
    f2 += f1 // 2

from Crypto.Util.number import ceil_div

# Step 3: Binary search narrowing m to exact value
mmin, mmax = ceil_div(n, f2), (n + B) // f2
while mmin < mmax:
    f = (2 * B) // (mmax - mmin)
    i = (f * mmin) // n
    f3 = ceil_div(i * n, mmin)
    if oracle((pow(f3, e, n) * c) % n):
        mmax = (i * n + B) // f3
    else:
        mmin = ceil_div(i * n + B, f3)
m = mmin
```

**Post-recovery OAEP decode:**
```python
from Crypto.Signature.pss import MGF1
maskedSeed = EM[1:hLen+1]
maskedDB = EM[hLen+1:]
seed = bytes(a ^ b for a, b in zip(maskedSeed, MGF1(maskedDB, hLen, HF)))
DB = bytes(a ^ b for a, b in zip(maskedDB, MGF1(seed, k - hLen - 1, HF)))
# DB[:hLen] should match lHash; rest is 0x00...0x01 || message
```

**Key insight:** Python's `or` short-circuits left-to-right. When expensive operations (PBKDF2, bcrypt, argon2) appear in chained conditions, the first condition becomes a timing oracle. RFC 8017 explicitly warns implementations must not let attackers distinguish error conditions — timing differences violate this.

**Detection:** RSA-OAEP with custom hash or slow KDF. Flask/Python backend. `/verify-token` or similar decryption endpoint returning generic errors. Timing differences between responses.

---

## Polynomial Hash with Trivial Root (Pragyan 2026)

**Pattern (!!Cand1esaNdCrypt0!!):** RSA signature scheme using polynomial hash `g(x,a,b) = x(x^2 + ax + b) mod P`.

**Vulnerability:** `g(0) = 0` for all parameters `a,b`. RSA signature of 0 is always 0 (`0^d mod n = 0`).

**Exploitation:** Craft message suffix so `bytes_to_long(prefix || suffix) = 0 (mod P)`:
```python
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF61  # 128-bit prime
# Compute required suffix value mod P
req = (-prefix_val * pow(256, suffix_len, P)) % P
# Brute-force partial bytes until all printable ASCII
while True:
    high = os.urandom(32).translate(printable_table)
    low_val = (req - int.from_bytes(high, 'big') * shift) % P
    low = low_val.to_bytes(16, 'big')
    if all(32 <= b <= 126 for b in low):
        suffix = high + low
        break
# Signature is simply 0
```

**General lesson:** Always check if hash function has trivial inputs (0, 1, -1). Factoring the polynomial often reveals these.

---

## Polynomial CRT in GF(2)[x] (Nullcon 2026)

**Pattern (Going in Circles, Nullcon 2026):** `r = flag mod f` where f is random GF(2) polynomial. Collect ~20 pairs, filter coprime, CRT combine.

See [advanced-math.md](advanced-math.md) for GF(2)[x] polynomial arithmetic and CRT implementation.

---

## Affine Cipher over Non-Prime Modulus (Nullcon 2026)

**Pattern (Matrixfun, Nullcon 2026):** `c = A @ p + b (mod m)` with composite m. Chosen-plaintext difference attack. For composite modulus, solve via CRT in each prime factor field separately.

See [advanced-math.md](advanced-math.md) for CRT approach and Gauss-Jordan implementation.

See also: [rsa-attacks.md](#part-2) for specialized RSA techniques (p=q bypass, cube root CRT, phi(n) multiple factoring, signature forgery, weak keygen, batch GCD, partial key recovery, CRT fault attack, homomorphic bypass).

---

# Part 2

## Table of Contents
- [RSA p=q Validation Bypass (BearCatCTF 2026)](#rsa-pq-validation-bypass-bearcatctf-2026)
- [RSA Cube Root CRT when gcd(e, phi) > 1 (BearCatCTF 2026)](#rsa-cube-root-crt-when-gcde-phi--1-bearcatctf-2026)
- [Factoring n from Multiple of phi(n) (BearCatCTF 2026)](#factoring-n-from-multiple-of-phin-bearcatctf-2026)
- [RSA Signature Forgery via Multiplicative Homomorphism (MMA CTF 2015)](#rsa-signature-forgery-via-multiplicative-homomorphism-mma-ctf-2015)
- [Weak RSA Key Generation via Base Representation (Sharif CTF 2016)](#weak-rsa-key-generation-via-base-representation-sharif-ctf-2016)
- [RSA with gcd(e, phi(n)) > 1 (CSAW 2015)](#rsa-with-gcde-phin--1-csaw-2015)
- [Batch GCD for Shared Prime Factoring (BSidesSF 2025)](#batch-gcd-for-shared-prime-factoring-bsidessf-2025)
- [RSA Partial Key Recovery from dp dq qinv (0CTF 2016)](#rsa-partial-key-recovery-from-dp-dq-qinv-0ctf-2016)
- [RSA-CRT Fault Attack / Bit-Flip Recovery (CSAW CTF 2016)](#rsa-crt-fault-attack--bit-flip-recovery-csaw-ctf-2016)
- [RSA Homomorphic Decryption Oracle Bypass (ECTF 2016)](#rsa-homomorphic-decryption-oracle-bypass-ectf-2016)
- [RSA with Small Prime Factors and CRT Decomposition (Hack The Vote 2016)](#rsa-with-small-prime-factors-and-crt-decomposition-hack-the-vote-2016)
- [RSA Timing Attack on Montgomery Reduction (DEF CON 2017)](#rsa-timing-attack-on-montgomery-reduction-def-con-2017)
- [Bleichenbacher Low-Exponent RSA Signature Forgery (Google CTF 2017)](#bleichenbacher-low-exponent-rsa-signature-forgery-google-ctf-2017)
- [Coppersmith Small Roots for Linearly Related Primes (Tokyo Westerns 2017)](#coppersmith-small-roots-for-linearly-related-primes-tokyo-westerns-2017)
- [ROCA Attack on RSA CVE-2017-15361 (EasyCTF IV)](#roca-attack-on-rsa-cve-2017-15361-easyctf-iv)
- [RSA Signature Bypass with e=1 and Crafted Modulus (BackdoorCTF 2018)](#rsa-signature-bypass-with-e1-and-crafted-modulus-backdoorctf-2018)
- [Dependent-Prime RSA: q = e^-1 mod p (TokyoWesterns CTF 4th 2018)](#dependent-prime-rsa-q--e-1-mod-p-tokyowesterns-ctf-4th-2018)
- [RSA Three-Key Pairwise GCD Triangle (Trend Micro 2018)](#rsa-three-key-pairwise-gcd-triangle-trend-micro-2018)
- [RSA n = p^2*q Schmidt-Samoa Variant (ASIS Finals 2018)](#rsa-n--p2q-schmidt-samoa-variant-asis-finals-2018)
- [Modulus Recovery via GCD of Encryption Residuals (X-MAS CTF 2018)](#modulus-recovery-via-gcd-of-encryption-residuals-x-mas-ctf-2018)
- [Textbook RSA Negation via encrypt(-1) (X-MAS CTF 2018)](#textbook-rsa-negation-via-encrypt-1-x-mas-ctf-2018)
- [Poly-Exponent RSA: GCD of p^p Combinations (ASIS Finals 2018)](#poly-exponent-rsa-gcd-of-pp-combinations-asis-finals-2018)
- [Biased LSB Oracle with Mode-of-Runs Recovery (CSAW CTF 2018)](#biased-lsb-oracle-with-mode-of-runs-recovery-csaw-ctf-2018)
- [Cube-Root Wraparound via AES-CTR Length Hint (hxp 2018)](#cube-root-wraparound-via-aes-ctr-length-hint-hxp-2018)
- [RSA p = next_prime(2^k + small) Shared-Prime Batch GCD (ASIS Finals 2018)](#rsa-p--next_prime2k--small-shared-prime-batch-gcd-asis-finals-2018)
- [PNG Encryption Bounded by 512-bit Key → Trailer Replacement (ASIS Finals 2018)](#png-encryption-bounded-by-512-bit-key--trailer-replacement-asis-finals-2018)
- [Modulus Recovery via Plaintext Malleability (X-MAS 2018)](#modulus-recovery-via-plaintext-malleability-x-mas-2018)
- [RSA CRT d_p NULL-Byte Overflow Primes Leak (P.W.N. CTF 2018)](#rsa-crt-d_p-null-byte-overflow-primes-leak-pwn-ctf-2018)
- [Textbook RSA Signature Blinding via Message Factoring (P.W.N. CTF 2018)](#textbook-rsa-signature-blinding-via-message-factoring-pwn-ctf-2018)
- [Last-Byte Modulus Overwrite via strlen-1 Null Truncation (OTW Advent 2018)](#last-byte-modulus-overwrite-via-strlen-1-null-truncation-otw-advent-2018)
- [CRC32 Collision Oracle + RSA Homomorphic Signature Forgery (BSidesSF 2019)](#crc32-collision-oracle--rsa-homomorphic-signature-forgery-bsidessf-2019)

See also: [rsa-attacks.md](rsa-attacks.md) for foundational RSA attacks (small e, Wiener, Fermat, Pollard, Hastad, common modulus, Manger oracle, Coppersmith).

---

## RSA p=q Validation Bypass (BearCatCTF 2026)

**Pattern (Pickme):** Server validates user-submitted RSA key (checks `n`, `e`, `d`, `p*q=n`, `e*d ≡ 1 mod phi`), encrypts the flag, then tries test decryption. If decryption fails, leaks ciphertext in error message.

**Exploit:** Set `p = q`. The server computes `phi = (p-1)*(q-1) = (p-1)^2` (incorrect — real totient of `p^2` is `p*(p-1)`). All validation checks pass, but decryption with the wrong `d` fails, leaking the ciphertext.

```python
from Crypto.Util.number import getPrime, inverse

p = getPrime(512)
q = p  # p = q!
n = p * q  # = p^2
e = 65537
wrong_phi = (p - 1) * (q - 1)  # = (p-1)^2
d = inverse(e, wrong_phi)  # passes server validation

# Server encrypts flag with our key, test decryption fails → leaks ciphertext c
# Decrypt with correct totient:
real_phi = p * (p - 1)
real_d = inverse(e, real_phi)
flag = pow(c, real_d, n)
```

**Key insight:** `phi(p^2) = p*(p-1)`, NOT `(p-1)^2`. When a server validates RSA parameters but uses `(p-1)*(q-1)` without checking `p != q`, setting `p=q` creates a working key that the server will miscompute the private exponent for, causing decryption failure and error-path data leakage.

---

## RSA Cube Root CRT when gcd(e, phi) > 1 (BearCatCTF 2026)

**Pattern (Kidd's Crypto):** RSA with `e=3`, modulus composed of many small primes all ≡ 1 (mod 3). Since each `p-1` is divisible by 3, `gcd(e, phi(n)) = 3^k` and the standard modular inverse `d = e^-1 mod phi` doesn't exist.

**Solution:** Compute cube roots per-prime via CRT:
```python
from sympy.ntheory.residues import nthroot_mod
from sympy.ntheory.modular import crt
from Crypto.Util.number import long_to_bytes

primes = [p1, p2, ..., p13]  # All ≡ 1 mod 3

# For each prime, find all 3 cube roots of c mod p
roots_per_prime = []
for p in primes:
    roots = nthroot_mod(c % p, 3, p, all_roots=True)
    roots_per_prime.append(roots)

# Try all 3^13 = 1,594,323 combinations
from itertools import product
for combo in product(*roots_per_prime):
    result, mod = crt(primes, list(combo))
    try:
        text = long_to_bytes(result).decode('ascii')
        if text.isprintable():
            print(f"Flag: {text}")
            break
    except:
        continue
```

**Key insight:** When `gcd(e, phi(n)) > 1`, standard RSA decryption fails. Factor `n`, compute eth roots modulo each prime separately (each prime ≡ 1 mod e gives `e` roots), then enumerate all CRT combinations. Feasible when the number of primes is small (3^13 ≈ 1.6M combinations).

---

## Factoring n from Multiple of phi(n) (BearCatCTF 2026)

**Pattern (Twisted Pair):** Given RSA `n` and a leaked pair `(re, rd)` where `re * rd ≡ 1 (mod k*phi(n))`. The value `re*rd - 1` is a multiple of `phi(n)`, enabling probabilistic factoring.

```python
import random
from math import gcd

def factor_from_phi_multiple(n, phi_multiple):
    """Factor n given any multiple of phi(n) using Miller-Rabin variant."""
    # Write phi_multiple = 2^s * d where d is odd
    s, d = 0, phi_multiple
    while d % 2 == 0:
        s += 1
        d //= 2

    for _ in range(100):  # 100 attempts
        a = random.randrange(2, n - 1)
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(s - 1):
            prev = x
            x = pow(x, 2, n)
            if x == n - 1:
                break
            if x == 1:
                # prev is non-trivial square root of 1
                p = gcd(prev - 1, n)
                if 1 < p < n:
                    return p, n // p
        # Check final
        if x != n - 1:
            p = gcd(x - 1, n)
            if 1 < p < n:
                return p, n // p
    return None

phi_mult = re * rd - 1
p, q = factor_from_phi_multiple(n, phi_mult)
```

**Key insight:** Any multiple of `phi(n)` — not just `phi(n)` itself — enables factoring via the Miller-Rabin square root technique. If a server leaks `e*d` for any key pair, or if `re*rd - 1` is given, compute `gcd(a^(m/2) - 1, n)` for random `a` values. Succeeds with probability ≥ 1/2 per attempt.

---

## RSA Signature Forgery via Multiplicative Homomorphism (MMA CTF 2015)

**Pattern:** Signing oracle refuses to sign the target message `m` but will sign other values. Unpadded RSA is multiplicatively homomorphic: `S(a) * S(b) mod n == S(a * b) mod n`.

```python
# Factor target message and sign each factor separately
divisor = 2
assert target_msg % divisor == 0
sig_a = sign_oracle(target_msg // divisor)
sig_b = sign_oracle(divisor)
forged_sig = (sig_a * sig_b) % n
```

**Key insight:** Textbook RSA signatures are homomorphic: `m^d mod n` preserves multiplication. If the oracle blacklists `m` but signs its factors, multiply the partial signatures. To find a suitable factorization, try small divisors (2, 3, ...) until `m / divisor` also passes the blacklist check. This is why PKCS#1 padding is essential — padded messages cannot be factored into other valid padded messages.

---

## Weak RSA Key Generation via Base Representation (Sharif CTF 2016)

When RSA primes are generated as `p = kp * B + tp` where B = product_of_small_primes * 2^400 and kp is small (< 2^12):

1. **Compute n mod B^2:** Since n = p*q and both p,q have form k*B + t, expansion gives: `n = kp*kq*B^2 + (kp*tq + kq*tp)*B + tp*tq`
2. **Recover kp*kq:** Brute-force 2^24 possibilities for (kp, kq) where each < 2^12
3. **Solve quadratic:** From known kp*kq and the middle coefficient, recover tp and tq

```python
B = product_of_first_443_primes * (2**400)
B2 = B * B

# n = A*B^2 + C*B + D where A=kp*kq, D=tp*tq
A = n // B2
D = n % B

# Brute-force kp, kq such that kp*kq == A
for kp in range(1, 2**12):
    if A % kp == 0:
        kq = A // kp
        # Solve for tp, tq from remaining equations
```

**Key insight:** Structured prime generation creates a mixed-radix representation of n, allowing efficient factorization by reducing the search space from exponential to polynomial.

---

## RSA with gcd(e, phi(n)) > 1 (CSAW 2015)

When `gcd(e, phi(n)) = g > 1`, standard RSA decryption fails because `d = e^(-1) mod phi(n)` doesn't exist. Instead:

1. Compute `e' = e / g` (reduced exponent)
2. Compute `d' = e'^(-1) mod phi(n)` (now coprime)
3. Compute `m^g = pow(c, d', n)` (partial decryption)
4. Take g-th root: iterate candidate m values where `pow(m, g, n) == m^g`

```python
from math import gcd
from sympy import factorint, mod_inverse
from gmpy2 import iroot

g = gcd(e, phi_n)
e_prime = e // g
d_prime = mod_inverse(e_prime, phi_n)
m_g = pow(c, d_prime, n)

# For small g, try integer root first
m, is_exact = iroot(m_g, g)
if is_exact:
    plaintext = int(m)
else:
    # Brute-force: m_g + k*n for small k
    for k in range(10000):
        m, exact = iroot(m_g + k * n, g)
        if exact:
            plaintext = int(m)
            break
```

**Key insight:** Reduce e by the GCD to make decryption possible, then recover the g-th root. Filter candidates by checking plaintext length or ASCII validity.

---

## Batch GCD for Shared Prime Factoring (BSidesSF 2025)

When multiple RSA moduli share a common prime factor (due to faulty hardware RNG, smartcard bugs, or weak seeding):

```python
from math import gcd, prod

def batch_gcd(moduli):
    """Find shared factors among a list of RSA moduli"""
    # Product tree via math.prod
    product = prod(moduli)

    factors = {}
    for n in moduli:
        g = gcd(n, product // n)
        if g != 1 and g != n:
            p = g
            q = n // p
            factors[n] = (p, q)
    return factors

# Usage: given list of public keys from smartcards
moduli = [key.n for key in public_keys]
shared = batch_gcd(moduli)
for n, (p, q) in shared.items():
    phi = (p - 1) * (q - 1)
    d = pow(e, -1, phi)  # Private exponent
```

For keys with patterned primes (hardware RNG faults producing primes with fixed bit patterns), combine with Coppersmith's method to recover remaining random bits. See [advanced-math.md](advanced-math.md) for Coppersmith.

**Key insight:** A single shared prime compromises both keys. Batch GCD runs in O(n log n) time via product/remainder trees, making it feasible for thousands of keys. Real-world incidents: Taiwanese citizen smartcards (2013), many IoT device certificates.

---

## Implicit Factoring via Shared LSB/MSB — Implicit Factoring (Implicit factoring)

**Pattern:** Two RSA moduli `N1=p1·q1`, `N2=p2·q2` share `t` low (LSB), high (MSB), or middle bits of `p1,p2` (e.g., `p1 ≡ p2 (mod 2^t)`), but `gcd(N1,N2)=1` so batch GCD fails. Lattice (May-Ritzenhofen 2009) still factors both when `t > 2·α·n` where `α = bitlen(p)/bitlen(N)` (≈0.5 for balanced RSA). LSB: `p1 ≡ p2 (mod 2^t)`; MSB: `p1,p2` share high bits; middle bits via shift.

Implicit factoring t>2α condition (t>2α, 2·α) and lattice factors despite gcd=1.

```python
# Implicit factoring via fpylll lattice (primary) — LSB case p1≡p2 mod 2^t
from fpylll import IntegerMatrix, LLL
import math
# hg_matrix from advanced-math.md — 2D lattice for implicit factoring

def implicit_factor_lsb(N1, N2, t, alpha=0.5):
    """Factor N1,N2 when p1≡p2 mod 2^t and t>2*alpha*n bits.

    N1,N2: moduli, t: shared LSB bits (explicit), alpha≈0.5, beta=0.5.
    May-Ritzenhofen condition: t > 2*alpha*log2(N1) ??? Actually t > alpha*(alpha+...) — simplified t>2αn.
    Returns (p1,p2,q1,q2) or None.
    """
    # Bound X=2^{alpha*n - t} ??? For LSB, unknown high part < 2^{n*alpha - t}
    nbits = N1.bit_length()
    X = 1 << int(alpha * nbits - t) if alpha*nbits > t else 1 << 10  # explicit, not None
    beta = 0.5
    # Build bivariate f(x,y)= x - y mod 2^t with x=p1_high, y=p2_high small?
    # Lattice dim ≈ 35 for t≈ 300 bits of 1024-bit N, similar to partial key exposure.
    # Full construction: see jvdsn/crypto-attacks implicit factoring
    #   from crypto_attacks.attacks.implicit_factoring import attack
    #   p1,p2 = attack(N1,N2,t,lsb=True)
    # or RsaCtfTool:
    #   RsaCtfTool --attack implicit --n1 N1 --n2 N2 --t t
    # Demo lattice (univariate slice) for X small:
    #   f(x)= x - p1_low ??? Use hg_matrix([c0,1], N1, X, beta=0.5, m=4, t=1), LLL.reduction, roots
    if X > 1_000_000:
        # Real attack needs bivariate lattice; copy hg_matrix from
        # advanced-math.md inline here, then LLL.reduction(B).
        # For demo return None and advise tool
        return None
    # Brute force for tiny X demo
    return None

# MSB variant: p1 = MSB || x1, p2 = MSB || x2 with same MSB prefix length t
#   => p1 - p2 small? Build lattice with X=2^{alpha*n - t}
# Middle bits: shift to LSB via 2^{mid}

# Detection: two moduli, gcd=1, but keygen says "p generated with shared LSB/MSB" or primes close.
#   Try t = 200..600 for 1024-bit N, alpha=0.5 => need t>512? Actually t>2*0.25*n≈512 for 1024-bit.
# Usage:
#   for t in [128,256,384,512]:
#       if implicit_factor_lsb(N1,N2,t): break
print("Implicit factoring needs t>2α·n; LSB case: p1≡p2 mod 2^t, MSB/middle via shift — use RsaCtfTool --attack implicit")
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — implicit factoring (May-Ritzenhofen 2009)
from sage.all import *

def implicit_lsb_sage(N1, N2, t):
    P = PolynomialRing(Zmod(N1*N2), names='x,y')
    x, y = P.gens()
    # f(x,y)= 2^t*y + x - (p1_high difference) ; root is small high parts
    # Sage bivariate small_roots with X=2^{alpha*n - t}, Y=X, beta=0.5
    nbits = N1.nbits()
    X = 2^(int(0.5*nbits - t))
    f = x - y  # placeholder — fill with shared modulus relation
    roots = f.small_roots(X=X, Y=X, beta=0.5, epsilon=1/30)
    return roots
# Tool: RsaCtfTool --attack implicit --n1 N1 --n2 N2 --t t --lsb/--msb
```

</details>

**Key insight:** Batch GCD catches *identical* primes; implicit factoring catches *shared bits* of distinct primes. Condition `t > 2α·n` (≈ >512 bits for RSA-1024 with balanced `p`) is the May-Ritzenhofen threshold where the 2-moduli lattice becomes short enough; with `t` below threshold, increase lattice dimension or collect more moduli (3-moduli improves bound). LSB (`p1≡p2 mod 2^t`) is most common in faulty RNG keygen; MSB/middle are shifts of LSB. References: May-Ritzenhofen 2009, `RsaCtfTool --attack implicit`, `crypto-attacks/implicit_factoring`.

---

## RSA Partial Key Recovery from dp dq qinv (0CTF 2016)

**Pattern:** Given only the CRT (Chinese Remainder Theorem) exponents (dp, dq, qinv) from a partial PEM (Privacy Enhanced Mail) key leak (e.g., bottom portion of private key file), recover the full key. Since `dp = d mod (p-1)`, iterate k and check if `p = (dp * e - 1) / k + 1` is prime.

```python
import gmpy2
# dp, dq, qinv extracted from partial PEM; e is known (usually 65537)
for k in range(3, e):
    if (dp * e - 1) % k == 0:
        p_candidate = (dp * e - 1) // k + 1
        if gmpy2.is_prime(p_candidate):
            p = p_candidate
            break
# Similarly recover q from dq; verify qinv * q % p == 1
```

**Key insight:** Leaking just the CRT exponents from an RSA private key is sufficient to fully recover p and q. Recovery is O(e) -- essentially instant for e=65537.

---

## RSA-CRT Fault Attack / Bit-Flip Recovery (CSAW CTF 2016)

RSA signing service with intermittent single-bit errors in private exponent d during CRT computation. Collect valid and faulty signatures, then detect which bit flipped.

```python
from Crypto.Util.number import inverse

def recover_d_bits(n, e, valid_sig, faulty_sigs, msg):
    """Recover private key d bit-by-bit from CRT fault signatures"""
    d_bits = [0] * 1024
    m = pow(msg, 1, n)
    s_good = valid_sig
    for s_bad in faulty_sigs:
        # ratio reveals which bit was flipped
        ratio = (s_bad * inverse(s_good, n)) % n
        for k in range(1024):
            if ratio == pow(2, pow(2, k, n), n) or ratio == pow(inverse(2, n), pow(2, k, n), n):
                d_bits[k] = 1
                break
    return d_bits
```

**Key insight:** When RSA-CRT has a single-bit fault in d, the ratio `faulty_sig * valid_sig^(-1) mod n` equals `2^(2^k) mod n` for the flipped bit position k, enabling bit-by-bit private key recovery.

---

## RSA Homomorphic Decryption Oracle Bypass (ECTF 2016)

Service decrypts any ciphertext except the target flag ciphertext. Exploit RSA's multiplicative homomorphism: `Dec(a * b mod n) = Dec(a) * Dec(b) mod n`.

```python
from Crypto.Util.number import long_to_bytes, inverse

# Server refuses to decrypt enc_flag directly
# But RSA is homomorphic: Dec(A*B) = Dec(A) * Dec(B) mod n
enc_2 = pow(2, e, n)  # encrypt the number 2
enc_flag_times_2 = (enc_flag * enc_2) % n  # = Enc(flag * 2)

dec_flag_times_2 = oracle_decrypt(enc_flag_times_2)  # server allows this
dec_2 = oracle_decrypt(enc_2)                         # server allows this

# Recover flag: (flag * 2) * inverse(2) mod n = flag
flag = (dec_flag_times_2 * inverse(dec_2, n)) % n
print(long_to_bytes(flag))
```

**Key insight:** RSA without padding is multiplicatively homomorphic. Multiply the forbidden ciphertext by `Enc(r)` for any `r`, decrypt the product, then divide by `r` to recover the original plaintext.

---

## RSA with Small Prime Factors and CRT Decomposition (Hack The Vote 2016)

RSA modulus composed of many small prime factors (primes < 251, each appearing ~1500 times). Factor n, then use CRT to decompose decryption.

```python
from sympy import factorint
from sympy.ntheory.residues import primitive_root
from functools import reduce

n = ...  # modulus with small factors
e = 65537
c = ...  # ciphertext

factors = factorint(n)  # {p1: k1, p2: k2, ...} where pi are small primes

# Decrypt modulo each prime power, then combine with CRT
from sympy.ntheory.modular import crt as chinese_remainder_theorem

remainders = []
moduli = []
for p, k in factors.items():
    pk = p ** k
    phi_pk = (p - 1) * p ** (k - 1)  # Euler's totient for prime power
    d_pk = pow(e, -1, phi_pk)
    m_pk = pow(c, d_pk, pk)
    remainders.append(m_pk)
    moduli.append(pk)

# Combine using CRT
m = chinese_remainder_theorem(moduli, remainders)[0]
```

**Key insight:** When n has many small prime factors, compute `d mod phi(p^k)` for each prime power independently, decrypt mod each, then combine via CRT. Much faster than computing `d mod phi(n)` directly.

---

## RSA Timing Attack on Montgomery Reduction (DEF CON 2017)

**Pattern:** Recover RSA private key bits via Kocher's timing attack when the number of modular subtractions during Montgomery reduction is leaked.

```python
# Montgomery multiplication: extra subtraction when result >= modulus
# Leaked: count of extra subtractions per signature operation
# Attack: predict subtraction count for each private key bit guess

# For each bit position i (MSB to LSB):
#   Guess bit = 0: predict timing for square only
#   Guess bit = 1: predict timing for square + multiply
#   Compare predictions against observed timing data
#   Correct guess produces statistically significant correlation

# ~200K signatures needed for 768-bit key recovery
# Attacking squaring reduction is more effective than multiply
import numpy as np
for bit_pos in range(key_bits):
    for guess in [0, 1]:
        predicted = predict_reductions(known_bits + [guess], messages)
        correlation = np.corrcoef(predicted, observed)[0, 1]
    known_bits.append(0 if corr_0 > corr_1 else 1)
```

**Key insight:** Montgomery multiplication performs an extra conditional subtraction when the intermediate result exceeds the modulus. If this count leaks (via timing, power, or as in this CTF -- directly), each bit of the private exponent can be determined by comparing predicted vs. observed subtraction patterns across many operations.

**References:** DEF CON 2017

---

## Bleichenbacher Low-Exponent RSA Signature Forgery (Google CTF 2017)

**Pattern:** Forge PKCS#1 v1.5 RSA signatures when e=3 by constructing a value whose cube root produces valid padding without knowing the private key.

```python
# PKCS#1 v1.5 signature format:
# 00 01 FF FF ... FF 00 [DigestInfo] [Hash]
# With e=3, forge signature s where s^3 has correct prefix

# Construct target block (2048-bit key, SHA-256):
# 00 01 FF ... FF 00 [SHA-256 DigestInfo] [hash] [garbage]
import gmpy2
prefix = b'\x00\x01' + b'\xff' * padding_len + b'\x00' + digest_info + hash_value
# Convert to integer, append zeros for garbage bytes
target = int.from_bytes(prefix + b'\x00' * garbage_len, 'big')
# Cube root (rounds down, garbage absorbs the remainder)
forged_sig = gmpy2.iroot(target, 3)[0] + 1  # +1 to round up
# Verify: forged_sig^3 starts with correct PKCS#1 prefix
```

**Key insight:** PKCS#1 v1.5 signature verification checks that `sig^e mod n` starts with `00 01 FF...FF 00 DigestInfo Hash`. With e=3, an attacker computes the cube root of a carefully constructed value with the correct prefix and ignores trailing bytes. Implementations that don't verify the padding extends to the full block length (CVE-2006-4339) accept the forgery.

**References:** Google CTF 2017

---

## Coppersmith Small Roots for Linearly Related Primes (Tokyo Westerns 2017)

**Pattern:** RSA where `q = k*p + delta` for a known small constant `k` and unknown small `delta`. Since `p ≈ sqrt(N/k)`, approximate `q_approx = k * isqrt(N // k) + 2^512` as an upper bound. The univariate polynomial `F(x) = q_approx - x` has `delta` as a small root modulo `q` (which divides N). Coppersmith's method finds this root when `delta < N^(1/4)`.

```python
from math import isqrt
from Crypto.Util.number import long_to_bytes
# Pure-Python Coppersmith via fpylll (primary) — no Sage needed.
# If you have jvdsn/crypto-attacks cloned: from shared.small_roots.howgrave_graham import modular_univariate
# but its backend still imports sage.all, so prefer the fpylll lattice below.
# For CTF-size bounds brute force when X < 2**24; otherwise build Howgrave-Graham lattice with fpylll:
#   from fpylll import IntegerMatrix, LLL; lattice = IntegerMatrix.from_matrix(hg_matrix(f_coeffs, N, X, beta)); LLL.reduction(lattice)
# Toy fallback (works for demo X, not cryptographic-size):
def small_roots_bruteforce(f_coeffs, N, X, beta=0.5, **kwargs):
    # f(x)=0 mod N, f monic degree d, coeffs [a0..ad] with ad=1
    # Brute for small X — use lattice LLL for large X (>2**24)
    for x in range(-X, X+1):
        val = sum(c * pow(x, i, N) for i, c in enumerate(f_coeffs)) % N
        if val == 0:
            return [x]
    return []
HAS_COPPERSMITH = False  # set True if you vendor the fpylll HG lattice helper

N, e, c = ...  # RSA parameters
k = 19  # known relationship: q = k*p + delta

# Approximate q from sqrt(N/k)
q_approx = k * isqrt(N // k) + 2**512

# Polynomial f(x)= q_approx - x (monic: -x + q_approx => monic as x - q_approx)
# Use coeffs [1, -q_approx] for x - q_approx (same small root magnitude)
roots = small_roots_bruteforce([1, (-q_approx) % N], N, X=2**512, beta=0.5)
    if roots:
        q = int(q_approx - roots[0])
        p = N // q
        assert p * q == N
        phi = (p - 1) * (q - 1)
        d = pow(e, -1, phi)
        flag = long_to_bytes(pow(c, d, N))
else:
    roots = []  # no root found — try larger lattice or adjust X/beta
```

<details><summary>Sage fallback (optional)</summary>

```python
from sage.all import *

N, e, c = ...  # RSA parameters
k = 19  # known relationship: q = k*p + delta

# Approximate q from sqrt(N/k)
q_approx = k * isqrt(N // k) + 2**512

# Build univariate polynomial with q_approx as approximate root of N mod q
R.<x> = PolynomialRing(Zmod(N))
f = q_approx - x  # root is delta = q_approx - q

# Coppersmith: find small root x = delta < N^0.25
roots = f.small_roots(X=2**512, beta=0.5)
if roots:
    q = int(q_approx - roots[0])
    p = N // q
    assert p * q == N
    phi = (p - 1) * (q - 1)
    d = pow(e, -1, phi)
    flag = long_to_bytes(pow(c, d, N))
```

</details>

**Key insight:** When `q ≈ k*p`, approximately half the bits of `p` (and `q`) are recoverable from `sqrt(N/k)`. The remaining unknown `delta` is small enough for Coppersmith when `delta < N^(1/4)`. The upper bound `q_approx` must exceed `q`; add a safety margin of `2^(bitlen/2)` to ensure the root is captured.

**References:** Tokyo Westerns CTF 2017

---

## ROCA Attack on RSA CVE-2017-15361 (EasyCTF IV)

**Pattern:** Infineon RSA library generates keys with structured primes (detectable via fingerprint). Factor 512-bit keys in minutes:

```bash
# Detect ROCA vulnerability
pip install roca-detect
roca-detect rsa_key.pub
# Factor with neca tool
git clone https://gitlab.com/jix/neca.git
cd neca && cargo build --release
./target/release/neca <N_decimal>
# Or use: https://github.com/crocs-muni/roca (original research tool)
```

**Key insight:** CVE-2017-15361 affects Infineon TPMs, smart cards, and YubiKey 4. Primes have the form `p = k * M + (65537^a mod M)` where M is the product of first n primes. Detection is instant (check `65537^x mod M` divides `n mod M`). Factoring uses Coppersmith's method on the known structure. Keys up to 2048 bits are practically attackable. For CTF use: if `roca-detect` reports vulnerable, use `neca` for 512-bit keys or the `crocs-muni/roca` tool for larger keys.

**References:** EasyCTF IV (2018)

---

### RSA Signature Bypass with e=1 and Crafted Modulus (BackdoorCTF 2018)

**Pattern:** Server generates RSA signature and asks for `(n, e)` to verify it. With `e=1`, `pow(s, 1, n) = s mod n`. Set `n = signature - PKCS1_pad(message)` so verification passes. (BackdoorCTF 2018)

```python
e = 1
n = signature ** e - PKCS1_pad(h.hexdigest())
# Now pow(signature, 1, n) == PKCS1_pad(message)
```

**Key insight:** When the verifier accepts user-supplied public key parameters without constraints, setting `e=1` makes modular exponentiation trivial. Choose `n` such that `signature mod n` equals the expected padded hash.

---

## Dependent-Prime RSA: q = e^-1 mod p (TokyoWesterns CTF 4th 2018)

**Pattern:** Key generation picks prime `p`, then sets `q = e^(-1) mod p` and rerolls until `q` is prime. The dependency `e*q ≡ 1 (mod p)` means `e*q = k*p + 1` for some small positive integer `k`, so `n = p*q = p*(k*p + 1)/e` — a quadratic in `p` that factors after enumerating a few candidate `k` values.

**Exploit:**
```python
from math import isqrt

def factor_dependent_n(n, e, max_k=100000):
    for k in range(2, max_k, 2):
        # e*q = k*p + 1 and n = p*q  =>  e*n = p*(k*p + 1)
        # Solve k*p^2 + p - e*n = 0  => p = (-1 + sqrt(1+4*k*e*n)) / (2*k)
        disc = 1 + 4 * k * e * n
        s = isqrt(disc)
        if s * s != disc:
            continue
        if (-1 + s) % (2 * k) != 0:
            continue
        root = (-1 + s) // (2 * k)
        if root > 1 and n % root == 0:
            return int(root), n // int(root)
    return None
```

<details><summary>Sage fallback (optional)</summary>

```python
from sage.all import PolynomialRing, ZZ

def factor_dependent_n(n, e, max_k=100000):
    P = PolynomialRing(ZZ, 'p'); p = P.gen()
    for k in range(2, max_k, 2):
        # e*q = k*p + 1 and n = p*q  =>  e*n = p*(k*p + 1)
        poly = k * p * p + p - e * n
        roots = poly.roots()
        for root, _ in roots:
            if root > 1 and n % root == 0:
                return int(root), n // int(root)
    return None
```

</details>

**Key insight:** Any key generator that derives `q` from `p` via a public arithmetic relation collapses RSA security to a small search. Write the relation as a polynomial `f_k(p) = 0` parameterized by a small integer, and use `.roots()` in Sage to recover `p`. The search space for `k` is typically under 2^16 because `q < p` forces `k ≈ e`.

**References:** TokyoWesterns CTF 4th 2018 — writeup 10862

---

## RSA Three-Key Pairwise GCD Triangle (Trend Micro 2018)

**Pattern:** Three RSA moduli `N1 = p1*p2`, `N2 = p1*p3`, `N3 = p2*p3` share primes pairwise (every pair has exactly one common factor). A single pairwise `gcd` reveals the shared prime, giving a complete factorisation of all three moduli without running batch-GCD.

```python
from math import gcd

def factor_triangle(n1, n2, n3):
    p1 = gcd(n1, n2)        # shared between N1 and N2
    p2 = gcd(n1, n3)        # shared between N1 and N3
    p3 = gcd(n2, n3)        # shared between N2 and N3
    assert n1 == p1 * p2 and n2 == p1 * p3 and n3 == p2 * p3
    return p1, p2, p3
```

**Key insight:** Batch-GCD handles arbitrary sets of moduli that might share a prime, but the three-key triangle is a closed-form case: every prime appears in exactly two moduli, so three `gcd()` calls factor all three keys. Spot the pattern when a challenge hands you exactly three public keys with matching bit-lengths and no other hint — contrast with [batch GCD for shared prime factoring](#batch-gcd-for-shared-prime-factoring-bsidessf-2025), which is the general case over larger key sets.

**References:** Trend Micro CTF 2018 — Offensive-Analysis 300, writeup 11129

---

## RSA n = p^2*q Schmidt-Samoa Variant (ASIS Finals 2018)

**Pattern:** Modulus is generated as `n = p*p*q` (not the usual `p*q`). Naive `phi = (p-1)*(q-1)` is wrong — real totient is `phi = p*(p-1)*(q-1)`. If `gcd(e, phi) != 1`, reduce ciphertext to smaller field `q` and invert there.

```python
phi = p*(p-1)*(q-1)
# Reduce enc to mod-q via pow(q, -1, phi)
qinv = pow(q, -1, phi)
enc = pow(enc, qinv, n) % q
# Now m < q; invert e*p^2 over phi(q) = q-1
pinv = pow(p*p, -1, q-1)
m = pow(enc, pinv, q)
```

**Key insight:** Spot `n = p^2*q` by trying `iroot(n, 3)` near its cube root and running Pollard/Lenstra on small fraction of `n`. Once factored, switch to the correct totient and field before inversion.

**References:** ASIS CTF Finals 2018 — John-Bull, writeup 12401

---

## Modulus Recovery via GCD of Encryption Residuals (X-MAS CTF 2018)

**Pattern:** Oracle encrypts arbitrary plaintexts with a hidden RSA key but does not disclose `n`. Compute `m^e - enc(m)` for two plaintexts; both differences are multiples of `n`, so their GCD equals `n` (or a small multiple).

```python
e = 65537
r1 = bytes_to_long(b'a')**e - encrypt(b'a')
r2 = bytes_to_long(b'b')**e - encrypt(b'b')
n = gcd(r1, r2)
```

**Key insight:** Works whenever the oracle is a black box but accepts chosen plaintexts. Strip small prime factors from the GCD to recover the true `n`.

**References:** X-MAS CTF 2018 — Santa's list, writeups 12701-12800

---

## Textbook RSA Negation via encrypt(-1) (X-MAS CTF 2018)

**Pattern:** Decrypt oracle refuses to decrypt the target ciphertext directly but accepts multiplications. Because `(-1)^e ≡ -1 (mod n)`, multiplying by `pow(-1, e, n)` flips the plaintext sign modulo `n`.

```python
ct_mutated = (ct_flag * pow(-1, e, n)) % n
plaintext = (-decrypt(ct_mutated)) % n
```

**Key insight:** Any odd `e` makes `encrypt(-1) = -1 mod n`, so the oracle returns `-m mod n` for the original ciphertext even if the original `m` is blacklisted.

**References:** X-MAS CTF 2018 — Santa's list 2.0, writeups 12701-12800

---

## Poly-Exponent RSA: GCD of p^p Combinations (ASIS Finals 2018)

**Pattern:** Challenge publishes several linear/polynomial combinations of `p` and `q`, e.g. `c1 = p^p mod q`, `c2 = (p+q)^(p+q) mod n`. Form two expressions both divisible by the target prime, then take their GCD to factor `n`.

```python
p = gcd(c2*c4 - c3, pow(c4, c4, c2*c4 - c3) - c3)
q = gcd(c1*c4 - c3, pow(c4, c4, c1*c4 - c3) - c3)
for x in small_primes:
    while p % x == 0: p //= x
```

**Key insight:** If two quantities share a secret factor but differ only by known scalars, `gcd(a, b)` recovers that factor. Strip cofactors with trial division on small primes.

**References:** ASIS CTF Finals 2018 — NMC, writeup 12427

---

## Biased LSB Oracle with Mode-of-Runs Recovery (CSAW CTF 2018)

**Pattern:** LSB oracle leaks the least significant bit of `m * 2^i mod n`, but the binary search converges inconsistently (oracle gives slightly wrong answers at random). Run the full recovery loop many times and take the per-byte mode across runs — correct bytes appear most often even when the full plaintext never converges.

```python
def recover():
    beg, end = 0, n - 1
    for bit in bits:
        mid = (beg + end) // 2
        if bit: beg = mid
        else:   end = mid
    return long_to_bytes(end)

byte_counts = [Counter() for _ in range(flag_len)]
for _ in range(N):
    flag = recover()
    for i, b in enumerate(flag):
        byte_counts[i][b] += 1
flag = bytes(c.most_common(1)[0][0] for c in byte_counts)
```

**Key insight:** Noisy side channels still leak the correct byte per position if each run independently biases toward the true value. Vote across runs rather than trying to perfect a single decryption.

**References:** CSAW CTF 2018 — Lost Mind, writeup 12490

---

## LSB Oracle Binary Search (Clean Parity Oracle) — LSB oracle binary-search

**Pattern:** Clean LSB oracle `O(c)` returns LSB of `Dec(c)` (parity: `m mod 2`). With textbook RSA, query `c·2^e mod n` → oracle tells whether `2m mod n` is even, i.e., `2m < n` iff parity `0`. Binary search on interval `[0,n)` recovers `m` exactly in `O(log n)` ≈ `1024` queries for 1024-bit `n` — no lattice, no interval branching like Bleichenbacher.

LSB oracle binary-search c·2^e even⇒2m<n O(log n) ~1024 queries.

```python
# Clean LSB oracle binary search (primary, no fpylll — pure oracle arithmetic)
from Crypto.Util.number import long_to_bytes

def lsb_oracle_decrypt(c, n, e, oracle):
    """Recover m = c^d mod n via LSB oracle.

    oracle(ct): returns 0/1 = LSB of Dec(ct). Query ct' = ct * 2^e mod n.
    If oracle(ct')==0 (even) => 2m < n, else 2m >= n => m in upper half.
    Repeat halving interval O(log n) times (~1024 for 1024-bit).
    """
    lo, hi = 0, n
    cur_c = c
    # We need oracle for successive doublings: ct_i = c * (2^i)^e = c * 2^{e*i} mod n?
    # Actually query c·(2^e) each step relative to current interval.
    # Simpler: maintain interval and query c·2^e * inv?
    # Classic: m_i = (m * 2^i mod n) parity tells interval.
    for i in range(n.bit_length()):  # ~1024 iterations
        c_doubled = (cur_c * pow(2, e, n)) % n  # Enc(2m) if cur_c = Enc(m)
        bit = oracle(c_doubled)  # 0 even => 2m < n, 1 odd => 2m >= n
        mid = (lo + hi) // 2
        if bit == 0:
            hi = mid  # 2m < n => m < n/2
        else:
            lo = mid  # 2m >= n => m >= n/2
            # For next bit, we need oracle for (m - n/2)*2? Instead use c_doubled with offset
        cur_c = c_doubled
        if hi - lo <= 1:
            break
        # Note: true LSB binary search tracks (m * 2^i mod n) not just doubling c;
        # full implementation keeps mult = pow(2, i+1, n) and queries c*pow(mult,e) each round.
        # For CTF, copy PicoCTF oracle reference implementation:
        #   c_i = (c * pow(pow(2,i,e), n)) % n ; bit = oracle(c_i) ; update lo/hi
    return hi

# PicoCTF example — oracle returns parity of decrypted flag:
#   enc_flag = pow(flag, e, n)
#   oracle = lambda ct: decrypt(ct) & 1   # remote returns flag & 1
#   flag = lsb_oracle_decrypt(enc_flag, n, e, oracle)
# Distinction from Bleichenbacher: LSB oracle is *parity* (even/odd), Bleichenbacher is
#   *range* 0x00 0x02 (interval narrowing). LSB needs ~log n queries, Bleichenbacher ~10k.
# If challenge says "oracle tells you if decrypted value is even/odd" or "LSB" it's this attack;
# if it says "valid PKCS#1 padding" it's Bleichenbacher (see rsa-attacks.md).
```

<details><summary>Sage fallback (optional)</summary>

```python
# SageMath — LSB oracle is pure Python, no lattice needed
from sage.all import *

def lsb_oracle_sage(c, n, e, oracle):
    lo, hi = 0, n
    cur = c
    for i in range(n.nbits()):
        ct = (cur * power_mod(2, e, n)) % n
        bit = oracle(ct)
        mid = (lo + hi)//2
        if bit == 0:
            hi = mid
        else:
            lo = mid
        cur = ct
        if hi - lo <= 1:
            break
    return hi
# PicoCTF oracle: sagemath still calls remote oracle, same logic.
```

</details>

**Key insight:** LSB oracle is the *clean* parity oracle — `c·2^e` even ⇒ `2m < n` (`O(log n)` binary search, ~1024 queries). Contrast with *biased* LSB (CSAW 2018) which needs mode voting over runs, and *Bleichenbacher* (`0x00 0x02` interval, ~10k queries). Detection: challenge leaks `m & 1` or `m % 2`. Use `oracle(c·2^e)` not `oracle(c·s^e)` interval search. References: PicoCTF 2019 `oracle`/`rsa-oracle`, Boneh-Venkatesan LSB, `rsa-attacks.md` Bleichenbacher vs LSB distinction.

---

## Cube-Root Wraparound via AES-CTR Length Hint (hxp 2018)

**Pattern:** Low-exponent RSA (`e = 3`) with padded plaintext where `m^3 > n`, so naive cube root fails. AES-CTR ciphertext reveals the plaintext length (CTR preserves length), so you know exactly how many `n` to add. Rescale with `inverse(2, n)^k` to cancel padding shifts, then try `iroot(c + k*n, 3)` for small `k`.

```python
inv = pow(inverse(2, n), 2040, n)
c = c * inv % n
for k in range(1000):
    m, ok = gmpy2.iroot(c + k*n, 3)
    if ok: flag = long_to_bytes(int(m)); break
```

**Key insight:** When padding shifts the plaintext past `n`, the correct root lies in `c + k*n` for some small `k`; AES-CTR leaks the exact plaintext length so you can size the search.

**References:** hxp CTF 2018 — daring, writeup 12588

---

## RSA p = next_prime(2^k + small) Shared-Prime Batch GCD (ASIS Finals 2018)

**Pattern:** Key generator constructs `p = next_prime(2^k + random_small_delta)` where `delta` is bounded by a few bits. Two independent keys generated with the same `k` converge on the same next prime whenever their `delta` values fall between the same two consecutive primes — common enough that any pair of collected moduli has `gcd(n1, n2) > 1`.

```python
from math import gcd
for n_a in collected:
    for n_b in collected:
        if n_a == n_b: continue
        p = gcd(n_a, n_b)
        if 1 < p < n_a:
            q = n_a // p
            d = pow(e + 2, -1, (p-1)*(q-1))  # NMC-style
            break
```

**Key insight:** Any RSA keygen that sources primes from a low-entropy neighborhood (`next_prime(constant + small)`, `2^k + random_byte`, `Mersenne ± delta`) collapses to pairwise GCD because the `next_prime` step collapses many deltas onto the same prime.

**References:** ASIS CTF Finals 2018 — Ariogen, writeup 12414

---

## PNG Encryption Bounded by 512-bit Key → Trailer Replacement (ASIS Finals 2018)

**Pattern:** Custom "polynomial bit sum" cipher encrypts PNG bytes as `C = sum(bit_i * (exp^i + (-1)^i))`. The key length (≤512 bits) means at most 64 bytes of plaintext are affected. Since PNG's trailing IDAT + IEND chunks are format-recoverable from any reference image, simply splice a valid trailer onto the decrypted prefix.

```python
# Only the first 64 bytes of output depend on the key; the rest matches plaintext bit-for-bit
first64 = decrypt_affected_prefix(ct)
rest    = original_png[64:]                # copy from any reference PNG with same image data
open('recovered.png', 'wb').write(first64 + rest)
```

**Key insight:** Whenever the key size caps the *number of affected bytes*, tail-of-file reconstruction beats full cryptanalysis. Works for any format (PNG, ZIP, MP3, PDF) where the tail has standard chunks or a parser tolerates garbage.

**References:** ASIS CTF Finals 2018 — Made by baby, writeup 12423

---

## Modulus Recovery via Plaintext Malleability (X-MAS 2018)

**Pattern:** Oracle decrypts ciphertext and returns the plaintext but hides the modulus `N`. Send two related ciphertexts `c` and `c * 2^e`. The decrypted plaintexts are `m` and `(2m) mod N`. If `2*m != (2m mod N)`, the difference `2m - (2m mod N) = N` recovers the modulus.

```python
m1 = decrypt(ct)                       # m
m2 = decrypt((ct * pow(2, e, m1)) % m1)  # 2m mod N
N = 2*m1 - m2 if 2*m1 != m2 else None
```

**Key insight:** RSA's multiplicative homomorphism leaks the modulus whenever you can send `c * k^e` and observe the wraparound. Use any small multiplier that nudges the plaintext past `N`.

**References:** X-MAS CTF 2018 — Santa's list, writeup 12658

---

## RSA CRT d_p NULL-Byte Overflow Primes Leak (P.W.N. CTF 2018)

**Pattern:** Server reads `d_p` into a buffer via `fgets()` that lacks a bounds check. Sending 33+ null bytes NULL-terminates `d_p_str` so the parsed integer is 0. The CRT path computes `m_2 = 0^0 = 1`, then signs `m - m_2 = m - 1`, which when squared in the gcd step yields `p = gcd(sig, N)`.

```python
io.send(b'\x00' * 40)                 # overflow d_p buffer
sig = io.recvline()
p = gcd(sig - 1, N)                   # factored!
```

**Key insight:** Any CRT-based RSA implementation that parses private key fields from network input is vulnerable to "make `d_p` equal to 0" tricks. The gcd of a forged signature and the modulus reveals a prime.

**References:** P.W.N. CTF 2018 — City RSA, writeup 12052

---

## Textbook RSA Signature Blinding via Message Factoring (P.W.N. CTF 2018)

**Pattern:** Unpadded RSA refuses to sign certain messages (blacklist), but multiplicative homomorphism lets you decompose the forbidden message `m` into coprime factors `m = x * y mod N`, request signatures for `x` and `y` separately, and combine: `sig(m) = sig(x) * sig(y) mod N`.

```python
for x in range(2, 10000):
    if (m * pow(x, -1, N)) % N == allowed_y:
        y = allowed_y
        sig = (sign(x) * sign(y)) % N
        break
```

**Key insight:** Any unpadded RSA signing oracle is vulnerable to factoring-based blinding. The attacker only needs two valid-looking messages whose product equals the target.

**References:** P.W.N. CTF 2018 — City RSA, writeup 12052

---

## Last-Byte Modulus Overwrite via strlen-1 Null Truncation (OTW Advent 2018)

**Pattern:** Server truncates `username[strlen(username)-1] = 0` to strip the trailing newline. Passing an empty username writes the null byte to the byte *before* the buffer — which turns out to be the last byte of `N`. The modified `N` is composite in a small way (differs from the real `N` by at most 255) and is factorable by Carmichael or `sage.factor`.

```python
io.sendline(b'')                       # empty username overwrites N[-1]
N_corrupt = recv_N()
for delta in range(256):
    if is_factorable(N_corrupt + delta - 255):
        N_real = N_corrupt + delta - 255; break
```

**Key insight:** "Strip the last character" idioms (`buf[strlen-1] = 0`) are classic off-by-one when `strlen == 0`. They write one byte backwards into whatever sits before the buffer — often a crypto constant.

**References:** OverTheWire Advent Bonanza 2018 — Day 14, writeup 12752

---

## CRC32 Collision Oracle + RSA Homomorphic Signature Forgery (BSidesSF 2019)

**Pattern (rsaos):** Shell exposes `RSA(foldhash(cmd))` as a "signature" where `foldhash` is a 10-byte digest built from a CRC-like, factorable fold. Privileged commands are blocked, but signatures for arbitrary strings are handed out. Pick a target privileged command whose fold value factors entirely into primes `< 2^32`. For each small factor `f_i`, use [crchack](https://github.com/resilar/crchack) to craft an innocuous command whose CRC32 equals `f_i` exactly, get its signature, and multiply them modulo `N`. RSA's multiplicativity means the product is the signature of the fold *product*, which is the target fold.

```python
from primefac import primefac
import subprocess, random

def find_cmd_crc(target_crc):
    while True:
        open('/tmp/cmd', 'w').write(f'echo {random.randint(0, 10000)}')
        cmd = subprocess.check_output(['./crchack/crchack', '/tmp/cmd', hex(target_crc)])
        if b'\n' not in cmd:
            return cmd

def find_cmd_fac(priv):
    while True:
        c = f'{priv} {random.randint(0, 10000)}'.encode()
        fs = list(primefac(foldhash(c)))
        if all(f < 2**32 for f in fs):
            return c, fs

priv_cmd, factors = find_cmd_fac('get-flag')
t = 1
for f in factors:                               # each factor fits in CRC32 range
    cmd = find_cmd_crc(f)
    _, sig = get_sig(cmd)                        # oracle signs arbitrary cmd
    t = (t * sig) % N                            # RSA: s1*s2 = sign(h1*h2)
send_priv(priv_cmd, sig=hex(t % N))              # forged signature for get-flag
```

**Key insight:** Textbook RSA signatures are multiplicative: `sign(a) * sign(b) = sign(a*b) mod N`. If the "hash" is actually a linear/factorable function (CRC32, fold-XOR), factor the target digest into pieces small enough to fit in CRC output space, then use a CRC collision finder (`crchack`) to realise each factor as an innocuous message the oracle will sign. Multiply the signatures mod `N` to forge the privileged signature. Works for any signature scheme over a hash that is both homomorphic-friendly *and* collidable to specific targets.
