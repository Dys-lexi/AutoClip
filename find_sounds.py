
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys

import numpy as np

_B64_ALPHABET = re.compile(r"[^A-Za-z0-9_\-]")

OUTPUT_DIR = "output"
FINGERPRINTS_JSON = os.path.join(OUTPUT_DIR, "fingerprints.json")
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus")

THRESHOLD = 0.3
HASH_RATE = 7.8
MERGE_WINDOW_SEC = 1.0

_MAX_NORMAL = 7


def find_fpcalc():
    """Return path to fpcalc(.exe), or None."""
    p = shutil.which("fpcalc") or shutil.which("fpcalc.exe")
    if p:
        return p
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("fpcalc.exe", "fpcalc"):
        cand = os.path.join(script_dir, name)
        if os.path.isfile(cand):
            return cand
    return None


def decode_fingerprint(fp_str):
    """Pure-Python equivalent of chromaprint.decode_fingerprint.

    Returns (list_of_uint32, algorithm).

    Format reference (chromaprint/src/utils/compress.cpp):
      byte 0      : algorithm
      bytes 1..3  : 24-bit big-endian item count
      then        : normal bits  (3 bits each, LSB-first within bytes,
                    one zero terminator per item)
      then        : exception bits (5 bits each), starting at the next byte
                    boundary, one per occurrence of the value 7 in the
                    normal section.
      Items are XOR-delta-encoded against the previous item (0 for item 0);
      within each item, set bit positions are stored as deltas from the last
      set position, base 1.
    """
    raw_str = fp_str
    fp_str = _B64_ALPHABET.sub("", fp_str)
    # Valid base64 lengths satisfy len % 4 in {0, 2, 3}. fpcalc occasionally
    # emits an extra trailing char on long inputs; drop it to recover.
    while len(fp_str) % 4 == 1:
        fp_str = fp_str[:-1]
    try:
        fp_bytes = base64.urlsafe_b64decode(fp_str + "=" * (-len(fp_str) % 4))
    except Exception as e:
        raise ValueError(
            f"base64 decode failed (raw len={len(raw_str)}, clean len={len(fp_str)}): "
            f"head={raw_str[:40]!r} tail={raw_str[-40:]!r}"
        ) from e
    if len(fp_bytes) < 4:
        raise ValueError("fingerprint too short")

    algorithm = fp_bytes[0]
    size = (fp_bytes[1] << 16) | (fp_bytes[2] << 8) | fp_bytes[3]
    data = fp_bytes[4:]
    data_bits = len(data) * 8

    normal_bits = []
    zeros_seen = 0
    bit_pos = 0
    while zeros_seen < size and bit_pos + 3 <= data_bits:
        v = 0
        for i in range(3):
            p = bit_pos + i
            if data[p >> 3] & (1 << (p & 7)):
                v |= (1 << i)
        normal_bits.append(v)
        if v == 0:
            zeros_seen += 1
        bit_pos += 3

    normal_byte_len = (len(normal_bits) * 3 + 7) // 8
    exc_data = data[normal_byte_len:]
    exc_data_bits = len(exc_data) * 8

    num_exc = sum(1 for v in normal_bits if v == _MAX_NORMAL)
    exceptions = []
    exc_bit_pos = 0
    for _ in range(num_exc):
        v = 0
        for i in range(5):
            p = exc_bit_pos + i
            if p + 1 <= exc_data_bits and exc_data[p >> 3] & (1 << (p & 7)):
                v |= (1 << i)
        exceptions.append(v)
        exc_bit_pos += 5

    exc_iter = iter(exceptions)
    bits = []
    for v in normal_bits:
        if v == _MAX_NORMAL:
            v += next(exc_iter)
        bits.append(v)

    output = []
    i = 0
    prev_fp = 0
    while i < len(bits) and len(output) < size:
        fp_val = prev_fp
        last_bit = 0
        while i < len(bits) and bits[i] != 0:
            last_bit += bits[i]
            fp_val ^= (1 << (last_bit - 1)) & 0xFFFFFFFF
            i += 1
        output.append(fp_val & 0xFFFFFFFF)
        prev_fp = fp_val
        i += 1

    return output, algorithm


def decode_fp(fp_str):
    hashes, _algo = decode_fingerprint(fp_str)
    return np.array(hashes, dtype=np.uint32)


def fpcalc_fingerprint_file(path, max_length=86400):
    """Run fpcalc on an audio file, return (duration, fp_str)."""
    fpcalc = find_fpcalc()
    if fpcalc is None:
        raise RuntimeError(
            "fpcalc(.exe) not found — put it on PATH or in this folder. "
            "https://acoustid.org/chromaprint"
        )
    proc = subprocess.run(
        [fpcalc, "-length", str(max_length), path],
        capture_output=True, check=True,
    )
    text = proc.stdout.decode()
    duration = None
    for line in text.splitlines():
        if line.startswith("DURATION="):
            try:
                duration = float(line.split("=", 1)[1].strip())
            except ValueError:
                pass
            break
    idx = text.find("FINGERPRINT=")
    if idx < 0:
        raise RuntimeError("fpcalc gave no fingerprint: " + proc.stderr.decode())
    fp_str = text[idx + len("FINGERPRINT="):]
    return duration, fp_str


def fmt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


_POPCOUNT_TABLE = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount_fallback(x):
    b0 = _POPCOUNT_TABLE[x & 0xFF]
    b1 = _POPCOUNT_TABLE[(x >> 8) & 0xFF]
    b2 = _POPCOUNT_TABLE[(x >> 16) & 0xFF]
    b3 = _POPCOUNT_TABLE[(x >> 24) & 0xFF]
    return (b0 + b1 + b2 + b3).astype(np.uint16)


def slide_match(long_hashes, short_hashes):
    n_long = len(long_hashes)
    n_short = len(short_hashes)
    if n_long < n_short:
        return []
    n_windows = n_long - n_short + 1
    bits_total = 32 * n_short

    hits = []
    chunk = 4096
    for start in range(0, n_windows, chunk):
        end = min(start + chunk, n_windows)
        windowed = np.lib.stride_tricks.sliding_window_view(
            long_hashes[start : end + n_short - 1], n_short
        )
        xor = windowed ^ short_hashes[np.newaxis, :]
        if hasattr(np, "bitwise_count"):
            bits = np.bitwise_count(xor).sum(axis=1)
        else:
            bits = _popcount_fallback(xor).sum(axis=1)
        bers = bits / bits_total
        below = np.where(bers < THRESHOLD)[0]
        for idx in below:
            hits.append((start + int(idx), float(bers[idx])))
    return hits


def cluster(hits, merge_window_hashes):
    if not hits:
        return []
    hits = sorted(hits)
    clusters = [[hits[0]]]
    for h in hits[1:]:
        if h[0] - clusters[-1][-1][0] <= merge_window_hashes:
            clusters[-1].append(h)
        else:
            clusters.append([h])
    return [min(c, key=lambda x: x[1]) for c in clusters]


def main():
    if not os.path.exists(FINGERPRINTS_JSON):
        print(f"missing {FINGERPRINTS_JSON}")
        return
    with open(FINGERPRINTS_JSON) as f:
        refs_raw = json.load(f)

    refs = {}
    for name, entry in refs_raw.items():
        fp = decode_fp(entry["fingerprint"])
        refs[name] = fp
        print(f"  ref {name}: {len(fp)} hashes (~{len(fp)/HASH_RATE:.1f}s)")

    targets = []
    for p in sorted(glob.glob(os.path.join(OUTPUT_DIR, "*"))):
        if p.lower().endswith(AUDIO_EXTS):
            targets.append(p)
    if not targets:
        print(f"no search targets in {OUTPUT_DIR}/")
        return

    merge_window_hashes = max(1, int(MERGE_WINDOW_SEC * HASH_RATE))
    print(f"\nscanning {len(targets)} file(s)...\n")
    for path in targets:
        name = os.path.basename(path)
        print(f"== {name} ==")
        try:
            _duration, fp_str = fpcalc_fingerprint_file(path)
        except Exception as e:
            print(f"  fingerprint failed: {e}")
            continue
        long_hashes = decode_fp(fp_str)
        print(f"  {len(long_hashes)} hashes (~{len(long_hashes)/HASH_RATE:.1f}s)")

        any_hit = False
        for ref_name, short_hashes in refs.items():
            hits = slide_match(long_hashes, short_hashes)
            clustered = cluster(hits, merge_window_hashes)
            for number, (offset, ber) in enumerate(clustered):
                t = offset / HASH_RATE
                print(f"  {number+1}: {ref_name}  in  {name}  at  {fmt_time(t)}  (ber={ber:.3f})")
                any_hit = True
        if not any_hit:
            print("  (no matches)")


if __name__ == "__main__":
    main()
