#!/usr/bin/env python3
"""Deterministic solver for the Android CTF wbox / native AES challenge.

This script models the recovered native logic:

    sub_1de0:
        buf = bytes(range(16))
        memcpy(buf, input, strlen(input)); reject if len(input) > 16

    sub_24c8:
        for i in 1..15: buf[i] += i

    sub_14a4 / shellcode:
        block[i] = buf[i] + runtime_add_const[i]
        AES-128-ECB(block, key) == target_ciphertext

It inverts the AES step, removes the add-constant step, removes the index-add step,
and then recovers the user-controlled prefix by recognizing the untouched initialized
suffix 07..0f.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

RUNTIME_ADD_CONST = bytes.fromhex("1fbcdaffe64cbc44f5b813c8eca8cdbd")
AES_KEY = bytes.fromhex("6bcdc67a6b2b7c9d8da459b1ab9d0680")
TARGET_CIPHERTEXT = bytes.fromhex("5cda772fa3c63e39b6f0f3ed515a9986")
EXPECTED_LIB_NAME = "libwbox.so"


@dataclass
class WboxResult:
    apk: str
    apk_sha256: str
    lib_paths: list[str]
    target_ciphertext: str
    aes_key: str
    runtime_add_const: str
    aes_plain_after_decrypt: str
    after_remove_add_const: str
    recovered_buf0: str
    final_input: str
    flag: str
    verification_ciphertext: str
    verification_passed: bool


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_wbox_libs(apk: Path) -> list[str]:
    with zipfile.ZipFile(apk) as zf:
        return [name for name in zf.namelist() if name.endswith("/" + EXPECTED_LIB_NAME) or name == EXPECTED_LIB_NAME]


def openssl_aes_ecb(data: bytes, key: bytes, decrypt: bool) -> bytes:
    mode = "-d" if decrypt else "-e"
    cmd = [
        "openssl",
        "enc",
        mode,
        "-aes-128-ecb",
        "-nopad",
        "-nosalt",
        "-K",
        key.hex(),
    ]
    proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "OpenSSL AES command failed. Install openssl or inspect stderr:\n"
            + proc.stderr.decode("utf-8", "replace")
        )
    return proc.stdout


def recover_input_from_model() -> tuple[bytes, bytes, bytes]:
    aes_plain = openssl_aes_ecb(TARGET_CIPHERTEXT, AES_KEY, decrypt=True)
    after_remove_add = bytes((a - b) & 0xFF for a, b in zip(aes_plain, RUNTIME_ADD_CONST))
    recovered_buf0 = bytearray(after_remove_add)
    for i in range(1, 16):
        recovered_buf0[i] = (recovered_buf0[i] - i) & 0xFF
    return aes_plain, after_remove_add, bytes(recovered_buf0)


def infer_user_input_from_initialized_buffer(buf: bytes) -> bytes:
    if len(buf) != 16:
        raise ValueError("expected 16-byte buffer")

    # The native code initializes buf[i] = i and then copies strlen(input) bytes
    # over the prefix. Therefore the first index k where suffix equals k..15 is
    # the likely input length.
    candidates: list[int] = []
    for k in range(17):
        if buf[k:] == bytes(range(k, 16)):
            candidates.append(k)
    if not candidates:
        raise ValueError(f"cannot infer input length from initialized suffix: {buf.hex()}")

    # Prefer the shortest printable prefix that leaves a native initialized suffix.
    for k in candidates:
        prefix = buf[:k]
        if prefix and all(32 <= b <= 126 for b in prefix):
            return prefix
    return buf[:candidates[0]]


def model_cipher_for_input(inp: bytes) -> bytes:
    if len(inp) > 16:
        raise ValueError("input longer than 16 bytes")
    buf = bytearray(range(16))
    buf[: len(inp)] = inp
    for i in range(1, 16):
        buf[i] = (buf[i] + i) & 0xFF
    block = bytes((a + b) & 0xFF for a, b in zip(buf, RUNTIME_ADD_CONST))
    return openssl_aes_ecb(block, AES_KEY, decrypt=False)


def solve(apk: Path | None = None) -> WboxResult:
    apk_path = Path(apk).resolve() if apk else Path("")
    apk_sha = sha256_file(apk_path) if apk else ""
    lib_paths = list_wbox_libs(apk_path) if apk else []

    aes_plain, after_remove_add, recovered_buf0 = recover_input_from_model()
    final_input = infer_user_input_from_initialized_buffer(recovered_buf0)
    verification_cipher = model_cipher_for_input(final_input)
    passed = verification_cipher == TARGET_CIPHERTEXT

    return WboxResult(
        apk=str(apk_path) if apk else "",
        apk_sha256=apk_sha,
        lib_paths=lib_paths,
        target_ciphertext=TARGET_CIPHERTEXT.hex(),
        aes_key=AES_KEY.hex(),
        runtime_add_const=RUNTIME_ADD_CONST.hex(),
        aes_plain_after_decrypt=aes_plain.hex(),
        after_remove_add_const=after_remove_add.hex(),
        recovered_buf0=recovered_buf0.hex(),
        final_input=final_input.decode("ascii", "replace"),
        flag=f"flag{{{final_input.decode('ascii', 'replace')}}}",
        verification_ciphertext=verification_cipher.hex(),
        verification_passed=passed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Solve the Android CTF wbox native AES challenge")
    parser.add_argument("apk", nargs="?", help="Path to challenge APK")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--write-json", help="Write JSON result to this path")
    args = parser.parse_args(argv)

    apk = Path(args.apk).resolve() if args.apk else None
    if apk and not apk.exists():
        parser.error(f"APK not found: {apk}")

    result = solve(apk)

    if args.write_json:
        Path(args.write_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write_json).write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")

    if args.json:
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    else:
        print(f"target_ciphertext = {result.target_ciphertext}")
        print(f"aes_key           = {result.aes_key}")
        print(f"runtime_add_const = {result.runtime_add_const}")
        print(f"aes_decrypt       = {result.aes_plain_after_decrypt}")
        print(f"remove_add_const  = {result.after_remove_add_const}")
        print(f"recovered_buf0    = {result.recovered_buf0}")
        print(f"input             = {result.final_input}")
        print(f"flag              = {result.flag}")
        print(f"verify_cipher     = {result.verification_ciphertext}")
        print(f"verification      = {'passed' if result.verification_passed else 'failed'}")

    return 0 if result.verification_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
