#!/usr/bin/env python3
"""Deterministic solver for AliCrackme_3 / com.ctf.crackme3 samples."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from binascii import unhexlify
from pathlib import Path

KNOWN_APK_SHA256 = "598baa992eb1f751740e54bbae27e818efa9c0f0f7fcd7e16db81f794aaf0315"
KNOWN_SO_SHA256 = "49b69ddccdbe76c5304ff5fa414c3f3ca9aef90e65cceddac42d6f21ba769aa8"
PACKAGE_NAME = "com.ctf.crackme3"
NATIVE_LIB = "lib/armeabi/libcrackme.so"
FLAG_BODY_LEN = 15

SBOX_HEX = (
    "F96BE81F2BBB3C604ACC8B32221E3973"
    "367E4C4041388F84374FFA0B8D969AA9"
    "EF3000CB9995AC34275B5405D4C62863"
    "18C24B55897A497FAF4D8726E3479879"
    "D371147DC18C6DDAEDA10A3EF276B256"
    "7CEABF90015DBCDF0EA76E8A676AAEE6"
    "CFB01AFD5066FFC511EE9785DCA4CEEC"
    "C7159E880C94BEAAC0AD532C813F6880"
    "12480283211DB116DD1025D2A0777404"
    "D6D1ABE1D78623515C61E5BDE75FF5D0"
    "582A4262B6249246C84ECA64D965BAB7"
    "9DA5E9DBB8F07B3BB3F4DE7044A66C78"
    "1B9FEB17E09BFE133D520975CD030DF7"
    "3AE4C9D820F3314582F65AD58EC32E6F"
    "C4A25708E22F59F87291B5431C5E35FC"
    "0669A3B9FBA8B4932D339C19070FF129"
)

ENCRYPT_DATA_HEX = (
    "6DC4AD347C649C39171726A7BA1828D2"
    "D5DD27BA862A0E27C4EEC431F7356214"
    "6C4118B877FA5CF0A4202590166C9143"
    "4908A4258E84A147027465B6AFAE45D6"
    "6D2F5393EAF2D488F3DB8738E0FC7E1C"
    "8E73826D6A27E2E864271B970D3205C9"
    "C67EBF9FB8DBCC8536B9ABE5A7F976FB"
    "0B917116404941814CC99247933B528C"
    "A5E1663AB0339086A5209DBFF471962D"
    "AD521D09CC92C1E37306FB92A7F538A7"
    "1AFE09375BABD98996A28A1AF6C92BC6"
    "8E35073387D1A7E34C824B89031A8E7F"
    "7D85E4B5FBF91EF6F7AC79624BB401C4"
    "9967F67D2DF2A8A66B1F67E45915C9EF"
    "2618C42CD654FE33BEF38CCFF85B5F06"
    "2BC218B6801DBA93FF11E92A5DA39597"
)

XOR_KEY = bytes.fromhex("208e1339")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rol8(x: int, n: int) -> int:
    n &= 7
    return ((x << n) | (x >> (8 - n))) & 0xFF if n else x & 0xFF


def ror8(x: int, n: int) -> int:
    n &= 7
    return ((x >> n) | (x << (8 - n))) & 0xFF if n else x & 0xFF


def decrypt_table(encrypted: bytes, sbox_init: bytes, xor_key: bytes) -> bytes:
    if len(encrypted) != 256 or len(sbox_init) != 256:
        raise ValueError("expected 256-byte encrypted table and 256-byte sbox")

    data = bytearray(encrypted)
    for i in range(256):
        data[i] ^= xor_key[i % len(xor_key)]

    sbox = bytearray(sbox_init)
    i = 0
    j = 0
    for idx in range(256):
        c = data[idx]
        c = rol8(c, 8 - (idx % 8))
        c ^= 0x29
        c = ror8(c, 8 - (idx % 8))
        i = (i + 1) & 0xFF
        j = (j + sbox[i]) & 0xFF
        sbox[i], sbox[j] = sbox[j], sbox[i]
        k = sbox[(sbox[i] + sbox[j]) & 0xFF]
        c ^= k
        c = rol8(c, 5)
        data[idx] = c
    return bytes(data)


def derive_flag_body() -> str:
    restored = decrypt_table(unhexlify(ENCRYPT_DATA_HEX), unhexlify(SBOX_HEX), XOR_KEY)
    out = []
    for idx, c in enumerate(restored[:FLAG_BODY_LEN]):
        out.append(rol8(c, idx % 8))
    return bytes(out).decode("latin1")


def inspect_apk(apk_path: Path) -> dict:
    data = apk_path.read_bytes()
    apk_hash = sha256_bytes(data)
    info = {
        "apk_path": str(apk_path),
        "apk_sha256": apk_hash,
        "apk_sha256_matches_known": apk_hash == KNOWN_APK_SHA256,
        "native_lib_path": NATIVE_LIB,
        "native_lib_found": False,
        "native_lib_sha256": None,
        "native_lib_sha256_matches_known": None,
    }
    try:
        with zipfile.ZipFile(apk_path) as zf:
            names = set(zf.namelist())
            info["native_lib_found"] = NATIVE_LIB in names
            if NATIVE_LIB in names:
                so = zf.read(NATIVE_LIB)
                so_hash = sha256_bytes(so)
                info["native_lib_sha256"] = so_hash
                info["native_lib_sha256_matches_known"] = so_hash == KNOWN_SO_SHA256
    except zipfile.BadZipFile:
        info["error"] = "not a valid APK/ZIP"
    return info


def solve(apk_path: Path | None = None) -> dict:
    flag_body = derive_flag_body()
    result = {
        "package": PACKAGE_NAME,
        "native_lib": NATIVE_LIB,
        "flag_body": flag_body,
        "flag": f"flag{{{flag_body}}}",
        "verification": "passed" if flag_body == "L6^a9>z<81Li!*M" else "failed",
        "known_apk_sha256": KNOWN_APK_SHA256,
        "known_so_sha256": KNOWN_SO_SHA256,
    }
    if apk_path is not None:
        result["apk_inspection"] = inspect_apk(apk_path)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("apk", nargs="?", help="optional AliCrackme_3 APK path")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    result = solve(Path(args.apk) if args.apk else None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        insp = result.get("apk_inspection")
        if insp:
            print(f"apk_sha256 = {insp['apk_sha256']}")
            print(f"apk_matches = {insp['apk_sha256_matches_known']}")
            print(f"so_found = {insp['native_lib_found']}")
            print(f"so_sha256 = {insp['native_lib_sha256']}")
            print(f"so_matches = {insp['native_lib_sha256_matches_known']}")
        print(f"flag_body = {result['flag_body']}")
        print(f"flag = {result['flag']}")
        print(f"verification = {result['verification']}")
    return 0 if result["verification"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
