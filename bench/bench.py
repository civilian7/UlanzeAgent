"""AU05 마이크 기반 Whisper 한국어 벤치마크.

  uv run bench.py devices                    # 입력 장치 목록
  uv run bench.py record [--device AU05]     # sentences.txt를 읽으며 녹음
  uv run bench.py run [--models large-v3-turbo large-v3 medium]
"""
import argparse, os, sys, time, wave, glob, re, site
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
SAMPLES = ROOT / "samples"
SR = 16000


def add_cuda_dlls():
    """pip로 설치한 cuBLAS/cuDNN DLL을 ctranslate2가 찾도록 등록."""
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        for b in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            os.add_dll_directory(b)
            os.environ["PATH"] = b + os.pathsep + os.environ["PATH"]


def find_device(sd, name):
    cands = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
            cands.append((i, d))
    if not cands:
        sys.exit(f"'{name}' 입력 장치를 찾을 수 없음. `devices`로 확인하세요.")
    # MME(리샘플링 지원)를 우선
    for i, d in cands:
        if "MME" in sd.query_hostapis(d["hostapi"])["name"]:
            return i, d
    return cands[0]


def cmd_devices(_):
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            print(f"[{i}] {d['name']}  ({api}, {d['default_samplerate']:.0f}Hz)")


def save_wav(path, audio):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def record_once(sd, idx, info):
    rate = SR
    try:
        sd.check_input_settings(device=idx, samplerate=SR, channels=1)
    except Exception:
        rate = int(info["default_samplerate"])
    chunks = []
    cb = lambda data, f, t, s: chunks.append(data[:, 0].copy())
    with sd.InputStream(device=idx, samplerate=rate, channels=1, callback=cb):
        input("  [Enter]로 녹음 종료 ")
    a = np.concatenate(chunks) if chunks else np.zeros(1, np.float32)
    if rate != SR:
        n = int(len(a) * SR / rate)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return a.astype(np.float32)


def cmd_record(args):
    import sounddevice as sd
    idx, info = find_device(sd, args.device)
    print(f"장치: [{idx}] {info['name']}\n문장을 평소 말투로 읽으세요. 건너뛰려면 s, 종료는 q.\n")
    SAMPLES.mkdir(exist_ok=True)
    lines = [l.strip() for l in (ROOT / "sentences.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    for n, text in enumerate(lines, 1):
        wav = SAMPLES / f"{n:02d}.wav"
        if wav.exists() and not args.overwrite:
            continue
        print(f"({n}/{len(lines)}) {text}")
        cmd = input("  [Enter]로 녹음 시작 (s=건너뜀, q=종료) ").strip().lower()
        if cmd == "q": break
        if cmd == "s": continue
        a = record_once(sd, idx, info)
        save_wav(wav, a)
        (SAMPLES / f"{n:02d}.txt").write_text(text, encoding="utf-8")
        print(f"  저장: {wav.name} ({len(a)/SR:.1f}s, peak={np.abs(a).max():.2f})\n")


def norm(s):
    return re.sub(r"[\s\.,!?\"'~·…\-]", "", s).lower()


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cmd_run(args):
    add_cuda_dlls()
    from faster_whisper import WhisperModel
    files = sorted(SAMPLES.glob("*.wav"))
    if not files:
        sys.exit("samples/가 비어 있음. 먼저 `record` 실행.")
    data = [(f, load_wav(f), (f.with_suffix(".txt")).read_text(encoding="utf-8")) for f in files]
    total_audio = sum(len(a) for _, a, _ in data) / SR
    rows = []
    for name in args.models:
        print(f"\n=== {name} ({args.compute_type}) ===")
        t0 = time.perf_counter()
        model = WhisperModel(name, device="cuda", compute_type=args.compute_type)
        load_s = time.perf_counter() - t0
        model.transcribe(data[0][1], language="ko")  # 워밍업(제외)
        errs = chars = 0
        lat = []
        for f, audio, ref in data:
            t = time.perf_counter()
            segs, _ = model.transcribe(
                audio, language="ko", beam_size=args.beam, vad_filter=True,
                initial_prompt=args.prompt or None, condition_on_previous_text=False)
            hyp = "".join(s.text for s in segs).strip()
            dt = time.perf_counter() - t
            lat.append(dt)
            r, h = norm(ref), norm(hyp)
            e = edit_distance(r, h)
            errs += e; chars += len(r)
            print(f"  {f.stem}  {dt*1000:5.0f}ms  CER {e/max(len(r),1):5.1%}\n    정답: {ref}\n    결과: {hyp}")
        rows.append((name, load_s, errs / chars, np.mean(lat), np.max(lat), sum(lat) / total_audio))
        del model
    print("\n모델                  로드(s)   CER     평균지연   최대지연   RTF")
    for n, l, c, m, x, rtf in rows:
        print(f"{n:<20} {l:7.1f}  {c:6.1%}  {m*1000:7.0f}ms {x*1000:7.0f}ms  {rtf:.3f}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices").set_defaults(fn=cmd_devices)
    r = sub.add_parser("record"); r.set_defaults(fn=cmd_record)
    r.add_argument("--device", default="AU05"); r.add_argument("--overwrite", action="store_true")
    b = sub.add_parser("run"); b.set_defaults(fn=cmd_run)
    b.add_argument("--models", nargs="+", default=["large-v3-turbo", "large-v3", "medium"])
    b.add_argument("--compute-type", default="float16")
    b.add_argument("--beam", type=int, default=5)
    b.add_argument("--prompt", default="")
    a = p.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
