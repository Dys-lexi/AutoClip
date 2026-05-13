

import io
import json
import os
import queue
import subprocess
import sys
import time
import datetime
from collections import deque
import functools
import av
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


def _preload_libchromaprint():
    if sys.platform != "win32":
        return
    import ctypes
    search_dirs = [
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(sys.prefix, "Scripts"),
        os.path.join(sys.prefix, "Library", "bin"),
        os.getcwd(),
    ]
    names = ("libchromaprint.dll", "chromaprint.dll", "libchromaprint-1.dll")
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(d)
                print(f"[chromaprint] added DLL search dir: {d}")
            except OSError as e:
                print(f"[chromaprint] add_dll_directory({d}) failed: {e}")
        for name in names:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                try:
                    ctypes.CDLL(path)
                    return path
                except OSError as e:
                    print(f"[chromaprint] found {path} but LoadLibrary failed: {e}")
    return None


_loaded = _preload_libchromaprint()
if _loaded:
    print(f"[chromaprint] preloaded {_loaded}")

import chromaprint  

from find_sounds import (
    HASH_RATE,
    cluster,
    decode_fp,
    fmt_time,
    slide_match,
)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH) as _f:
    _config = json.load(_f)

WATCH_DIR = _config["watch_dir"]
SAVE_PATH = _config["save_path"]
TMP_PATH = "./"
FINGERPRINTS_JSON = _config["fingerprints_json"]

SECONDSBEFOREAFTER = _config["seconds_before_after"]
MERGETIME = _config["merge_time"]
LOCKOUTTIME = _config["lockout_time"]
N_PREVIOUS = _config["n_previous"]
MERGE_WINDOW_SEC = _config["merge_window_sec"]
SAMPLE_RATE = _config["sample_rate"]
CHANNELS = _config["channels"]
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2

CHUNK_PREFIX = _config["chunk_prefix"]
INIT_NAME = _config["init_name"]


def load_refs():
    with open(FINGERPRINTS_JSON) as f:
        raw = json.load(f)
    return { name: {**entry,"fingerprint":decode_fp(entry["fingerprint"])} for name, entry in raw.items()}


def read_init(parent_dir, init_cache):
    if parent_dir in init_cache:
        return init_cache[parent_dir]
    init_path = os.path.join(parent_dir, INIT_NAME)
    for _ in range(2):
        if os.path.exists(init_path):
            with open(init_path, "rb") as f:
                init_cache[parent_dir] = f.read()
            return init_cache[parent_dir]
        time.sleep(0.1)
    return None


def chunk_to_pcm(init_bytes, chunk_path):
    """init + chunk -> raw s16le mono PCM, entirely in RAM."""
    with open(chunk_path, "rb") as f:
        chunk_bytes = f.read()
    buf = io.BytesIO(init_bytes + chunk_bytes)
    container = av.open(buf)
    try:
        astream = next(s for s in container.streams if s.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        parts = []
        # Don't use bytes(r.planes[0]) — the plane buffer is padded to a
        # SIMD-aligned size, so it includes uninitialized bytes past the
        # actual samples and inserts buzz between frames. to_ndarray()
        # returns exactly samples × bytes_per_sample bytes.
        for frame in container.decode(astream):
            for r in resampler.resample(frame):
                parts.append(r.to_ndarray().tobytes())
        for r in resampler.resample(None):
            parts.append(r.to_ndarray().tobytes())
    finally:
        container.close()
    return b"".join(parts)


def fingerprint_pcm(pcm):
    """PCM -> decoded uint32 hash array via libchromaprint (in-process)."""
    fp = chromaprint.Fingerprinter()
    fp.start(SAMPLE_RATE, CHANNELS)
    fp.feed(pcm)
    fp_str = fp.finish()
    if isinstance(fp_str, bytes):
        fp_str = fp_str.decode("ascii")
    return decode_fp(fp_str)


def is_target(path):
    name = os.path.basename(path)
    return (
        name.startswith(CHUNK_PREFIX)
        and name.endswith(".m4s")
        and not name.endswith(".m4s.tmp")
    )


class SegmentHandler(FileSystemEventHandler):
    def __init__(self, q):
        self.q = q

    def on_created(self, event):
        if not event.is_directory and is_target(event.src_path):
            self.q.put(event.src_path)

    def on_moved(self, event):
        dest = getattr(event, "dest_path", "")
        if not event.is_directory and is_target(dest):
            self.q.put(dest)


def process_segment(path, pcm_cache, histories, init_cache, refs,cliptimings,tosave,timer):
    parent = os.path.dirname(path)
    rel = os.path.relpath(path, WATCH_DIR)
    cliptimings.setdefault(parent,[])
    history = histories.setdefault(parent, []) # remember the names for clips (5 after is a random number, must be increased for longer sounds)

    if path not in pcm_cache:
        init_bytes = read_init(parent, init_cache)
        if init_bytes is None:
            print(f"  skipping {rel}: no {INIT_NAME} in session dir yet")
            return
        try:
            pcm_cache[path] = chunk_to_pcm(init_bytes, path)
        except Exception as e:
            print(f"  decode failed for {rel}: {e}")
            return

    new_pcm = pcm_cache[path]
    prev_pcms = [pcm_cache[p["path"]] for p in history if p["path"] in pcm_cache][-N_PREVIOUS:]
    # print(len(prev_pcms),len(history))
    combined = b"".join(prev_pcms) + new_pcm
    new_start_sec = sum(len(p) for p in prev_pcms) / BYTES_PER_SECOND
    seg_seconds = len(new_pcm) / BYTES_PER_SECOND
    try:
        long_hashes = fingerprint_pcm(combined)
    except Exception as e:
        print(f"  fingerprint failed for {rel}: {e}")
        return
    merge_window = max(1, int(MERGE_WINDOW_SEC * HASH_RATE))
    print(os.path.basename(path))
    any_hit = False
    for ref_name, short in refs.items():
        hits = slide_match(long_hashes, short["fingerprint"])
        for offset, ber in cluster(hits, merge_window):
            t = offset / HASH_RATE
            d = t - new_start_sec
            where = f"in new +{d:.2f}s" if d >= 0 else f"in prev {-d:.2f}s before new"
            # print(
            #     f"  <3 {ref_name} at {fmt_time(t)} "
            #     f"({where}, ber={ber:.3f}) "
            #     f"[{rel} + {len(prev_pcms)} prev, seg={seg_seconds:.2f}s]"
            # )
            # print(t,"t!!!")
            print(f"found a match!!! for {ref_name} at {t:.2f}s, loss={ber:.3f}")
            if len(list(filter(lambda x: abs(x - int(t+timer)) < short["mintimebettweenclipsshouldbedurationofsoundatleast"], list(cliptimings[parent])))):
                print("not clipping - too soon after most recent clip")
                cliptimings[parent].append( t+timer)
                continue
            cliptimings[parent].append( t+timer)
            # print("weeee")
            # durations = list(map(lambda x: dict(list(map(lambda x: [x["path"],x["duration"]],[history,*{"path":path,"duration":seg_seconds}])))[x]["duration"] ,list(pcm_cache.values())))
            # print(durations)
            # durations = dict([history,*{"path":path,"duration":seg_seconds}])
            durations = dict(list(map(lambda x: [x["path"],x["duration"]],[*(list(history)[-N_PREVIOUS:]),{"path":path,"duration":seg_seconds}])))
            power = t
            for i, (clipname,dur) in enumerate(durations.items()):
                power -= dur
                # print("power",power)
                i += 1
                if power < 0:
                    break
            else:
                print ("could not find starter clip - it's not stored in ram for some reason")
                continue
            timestampinclip = dur + power
            print("THE CLIP IT'S IN",timestampinclip,clipname)
            # print(durations)
            tosave.setdefault(parent,[]).append({"anchorname":clipname,"timestampinanchor":timestampinclip,"anchorname2":clipname,"timestampinanchor2":timestampinclip,"fingerprint":ref_name,"timestamp": t+timer})
    #         any_hit = True
    # print(f"{new_start_sec:.2f}")
    # if not any_hit:
    #     print(f"  no match [{rel} + {len(prev_pcms)} prev, seg={seg_seconds:.2f}s]")

    history.append({"path":path,"duration":seg_seconds})

def saveclip(clipdata,history,refs):
    durations = dict(list(map(lambda x: [x["path"],x["duration"]],history)))

    # excesstime = durations[clipdata["anchorname"]] - clipdata["timestampinanchor"] + sum(list(map(lambda x: x["duration"],list(history)[list(durations.keys()).index(clipdata["anchorname"])+1:])))
    # if excesstime < SECONDSBEFOREAFTER["after"]:
    #     print("not enough time",excesstime)
    #     return False
    # at this point, we can clip!
    audioclipsneeded = []
    found = False
    for i,clip in enumerate(list(history)):
        if clip["path"] == clipdata["anchorname"]:
            # print("HEHRHEHRHHE")
            found = True
            audioclipsneeded.append(clip["path"])
        if clip["path"] == clipdata["anchorname2"]:
            found = False
            audioclipsneeded.append(clip["path"])
        elif found:
            audioclipsneeded.append(clip["path"])
    endrelative = refs[clipdata["fingerprint"]]["aftercliptime"]+refs[clipdata["fingerprint"]]["maxtimetomergemultiplesounds"]+5 - durations[clipdata["anchorname2"]] + clipdata["timestampinanchor2"]
    for i,clip in enumerate(list(history)[list(durations.keys()).index(clipdata["anchorname2"])+1:]):
        
        audioclipsneeded.append(clip["path"])
        if endrelative  - clip["duration"]< 0:
            
            break
        endrelative -= clip["duration"]
    else:
        # print("not enough forward clips")
        return False
    beginrelative = refs[clipdata["fingerprint"]]["beforecliptime"] - clipdata["timestampinanchor"]

    # print("len",len((list(history)[:list(durations.keys()).index(clipdata["anchorname"])])))
    for i,clip in enumerate(reversed(list(history)[:list(durations.keys()).index(clipdata["anchorname"])])):
        
        audioclipsneeded.insert(0,clip["path"])
        if beginrelative - clip["duration"]< 0:
            beginrelative = clip["duration"] - beginrelative
            break
        beginrelative -= clip["duration"]
    else:
        print("not enough backward clips - doing a partial clip")
        # return False
    videoclipsneeded = list(map(getvideo,audioclipsneeded))
    endrelative = refs[clipdata["fingerprint"]]["beforecliptime"] + refs[clipdata["fingerprint"]]["aftercliptime"] + beginrelative
    print("saving a clip!!!",len(audioclipsneeded),len(videoclipsneeded),endrelative,beginrelative)
    print(json.dumps(list(map(os.path.basename ,audioclipsneeded)),indent=4))
    actuallysaveclip(audioclipsneeded,videoclipsneeded,clipdata["fingerprint"],endrelative,beginrelative)
    return True

def actuallysaveclip(audioclipsneeded,videoclipsneeded,fingerprint,endrelative,beginrelative):
    os.makedirs(SAVE_PATH, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%y-%m-%d %H-%M-%S")
    outpath = os.path.join(SAVE_PATH, f"{fingerprint} {timestamp}.mp4")

    audio_tmp = os.path.join(TMP_PATH, f".tmp_audio_{timestamp}.mp4")
    video_tmp = os.path.join(TMP_PATH, f".tmp_video_{timestamp}.mp4")

    def init_for(chunk_path):
        # chunk-stream{N}-{seq}.m4s -> init-stream{N}.m4s in same dir
        stream_part = os.path.basename(chunk_path).split("-", 2)[1]
        return os.path.join(os.path.dirname(chunk_path), f"init-{stream_part}.m4s")

    def concat(init_path, chunks, out):
        with open(out, "wb") as o, open(init_path, "rb") as i:
            o.write(i.read())
            for c in chunks:
                with open(c, "rb") as f:
                    o.write(f.read())

    concat(init_for(audioclipsneeded[0]), audioclipsneeded, audio_tmp)
    concat(init_for(videoclipsneeded[0]), videoclipsneeded, video_tmp)

    # Naive init+chunks concat leaves the original session-relative tfdt
    # decode times in every moof, and the seek index from init.m4s describes
    # zero samples, so ffmpeg's input -ss can't find a sample inside the
    # fragments. Decode the whole concat sequentially instead, then use
    # setpts+trim filters to renormalise PTS to 0 and cut the window.
    filter_complex = (
        f"[0:v]setpts=PTS-STARTPTS,"
        f"trim=start={beginrelative}:end={endrelative},"
        f"setpts=PTS-STARTPTS[v];"
        f"[1:a]asetpts=PTS-STARTPTS,"
        f"atrim=start={beginrelative}:end={endrelative},"
        f"asetpts=PTS-STARTPTS[a]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "quiet",
         "-i", video_tmp, "-i", audio_tmp,
         "-filter_complex", filter_complex,
         "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
         outpath],
        check=True,
    )
    os.remove(audio_tmp)
    os.remove(video_tmp)
    print(f"saved {outpath}")

def getvideo(path):
    return path.replace("stream1","stream0")
    
def prune_cache(pcm_cache, histories,parent,timer):
    dict(list(map(lambda x: [x["path"],x["duration"]],histories[parent])))
    histories[parent] = histories[parent][-max(N_PREVIOUS,sum([*list(SECONDSBEFOREAFTER.values()),10])//3+200):]
    timeadded = sum(map(lambda x:dict(list(map(lambda x: [x["path"],x["duration"]],histories[parent])))[x] ,list(filter(lambda x : x not in map(lambda y: y["path"],list(histories[parent][-N_PREVIOUS:])) and x  not in functools.reduce(lambda a,b: [*a,*map(lambda x: x["path"],b[1])] , {**histories , parent: []}.items(),[]), pcm_cache))))
    for key in list(filter(lambda x : x not in map(lambda y: y["path"],list(histories[parent][-N_PREVIOUS:])) and x not in functools.reduce(lambda a,b: [*a,*map(lambda x: x["path"],b[1])] , {**histories , parent: []}.items(),[]), pcm_cache)):
        # print(key)
        del pcm_cache[key]   
    return timer + timeadded


def main():
    os.makedirs(WATCH_DIR, exist_ok=True)
    refs = load_refs()
    print(f"loaded {len(refs)} reference fingerprint(s)")
    print(f"watching {WATCH_DIR}/ recursively (N_PREVIOUS={N_PREVIOUS} per session)")
    # Probe libchromaprint up front so a missing DLL fails loudly here
    # instead of silently per-chunk later.
    try:
        _probe = chromaprint.Fingerprinter()
        _probe.start(SAMPLE_RATE, CHANNELS)
        _probe.feed(b"\x00\x00" * SAMPLE_RATE)
        _probe.finish()
    except Exception as e:
        print(f"WARNING: libchromaprint probe failed: {e}")
        print("  put chromaprint.dll / libchromaprint.dll next to this script or on PATH.")

    q = queue.Queue()
    observer = Observer()
    observer.schedule(SegmentHandler(q), WATCH_DIR, recursive=True)
    observer.start()

    histories = {}
    init_cache = {}
    pcm_cache = {}
    cliptimings = {}
    seen = set()
    tosave = {}
    timer = {}

    try:
        while True:
            try:
                path = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if path in seen:
                continue
            seen.add(path)
            
            # print(f"\n[{os.path.relpath(path, WATCH_DIR)}]")
            parent = os.path.dirname(path)
            timer.setdefault(parent,0)
            process_segment(path, pcm_cache, histories, init_cache, refs,cliptimings,tosave,timer[parent])
            # print(list(map(lambda x:x["timestamp"], tosave.get(parent,[]))))
            for pos,clipdata in enumerate(tosave.get(parent,[])):
                if pos > 0:
                    lastclip = tosave[parent][pos-1]
                    if lastclip and lastclip["timestamp"] + refs[clipdata["fingerprint"]]["maxtimetomergemultiplesounds"] > clipdata["timestamp"]:
                        clipdata["anchorname"] = lastclip["anchorname"]
                        clipdata["timestampinanchor"] = lastclip["timestampinanchor"]
                        tosave[parent][pos-1] = False
                        # print("merged a clip")
                        
                tosave[parent][pos] = (not saveclip(clipdata,histories[parent],refs) and clipdata)
            tosave[parent] = list(filter(lambda x: x, tosave.get(parent,[])))
            

            timer[parent] = prune_cache(pcm_cache, histories,parent,timer[parent])
            # print("timer",timer)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
