
import glob
import json
import os

import acoustid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(SCRIPT_DIR, "inputwavs")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "fingerprints.json")

DEFAULTS = {
    "mintimebettweenclipsshouldbedurationofsoundatleast": 10,
    "maxtimetomergemultiplesounds": 10,
    "beforecliptime": 10,
    "aftercliptime": 4,
}


def main():
    paths = sorted(glob.glob(os.path.join(INPUT_DIR, "*.wav")))
    if not paths:
        print(f"no .wav files in {INPUT_DIR}/")
        return

    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as f:
            data = json.load(f)
    else:
        data = {}

    for i, path in enumerate(paths, 1):
        filename = os.path.basename(path)
        name = filename[:-4] if filename.lower().endswith(".wav") else filename
        print(f"[{i}/{len(paths)}] fingerprinting {filename}...", flush=True)
        duration, fingerprint = acoustid.fingerprint_file(path)
        fp_str = fingerprint.decode("ascii") if isinstance(fingerprint, bytes) else fingerprint

        entry = {**DEFAULTS, **data.get(name, {})}
        entry["duration"] = duration
        entry["fingerprint"] = fp_str
        data[name] = entry

        print(f"    duration={duration:.2f}s  fp_len={len(fp_str)}")

    with open(OUTPUT_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    print(f"wrote {OUTPUT_PATH} ({len(data)} entries)")


if __name__ == "__main__":
    main()
