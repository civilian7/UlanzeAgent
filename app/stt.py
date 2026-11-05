"""AU05 Voice Key(F13) push-to-talk -> faster-whisper -> 활성 창에 붙여넣기.

  uv run stt.py              # 트레이 앱 실행
  uv run stt.py --selftest   # 샘플 wav 인식 + F13 훅 동작 점검
"""
import argparse, collections, glob, logging, os, queue, site, sys, threading, time, tomllib, wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
SR = 16000
log = logging.getLogger("stt")


def add_cuda_dlls():
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        for b in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            os.add_dll_directory(b)
            os.environ["PATH"] = b + os.pathsep + os.environ["PATH"]


_mutex = None


def single_instance():
    """이름 있는 뮤텍스로 중복 실행 방지. 이미 있으면 False."""
    import ctypes
    global _mutex
    k32 = ctypes.windll.kernel32
    _mutex = k32.CreateMutexW(None, False, "Local\\UlanzeSttSingleton")
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def load_config():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


class Recorder:
    """마이크 스트림을 상시 열어 두고 pre-roll 버퍼를 유지한다."""

    BLOCK = 1024  # SR 기준 블록 크기

    def __init__(self, cfg):
        import sounddevice as sd
        self.sd = sd
        a = cfg["audio"]
        self.max_seconds = a["max_seconds"]
        self.preroll = collections.deque(maxlen=max(1, int(a["preroll_ms"] / 1000 * SR / self.BLOCK) + 1))
        self.chunks, self.recording, self.n = [], False, 0
        self.lock = threading.Lock()
        idx, info = self._find(a.get("device", ""))
        self.rate = SR
        try:
            sd.check_input_settings(device=idx, samplerate=SR, channels=1)
        except Exception:
            self.rate = int(info["default_samplerate"])
        self.stream = sd.InputStream(device=idx, samplerate=self.rate, channels=1,
                                     blocksize=int(self.rate * self.BLOCK / SR), callback=self._cb)
        log.info("마이크: [%s] %s @ %dHz", idx, info["name"], self.rate)

    def _find(self, name):
        sd = self.sd
        devs = sd.query_devices()
        if name:
            cands = [(i, d) for i, d in enumerate(devs)
                     if d["max_input_channels"] > 0 and name.lower() in d["name"].lower()]
            for i, d in cands:  # MME 우선 (자동 리샘플링)
                if "MME" in sd.query_hostapis(d["hostapi"])["name"]:
                    return i, d
            if cands:
                return cands[0]
            log.warning("'%s' 장치를 찾지 못해 기본 입력 장치를 사용", name)
        i = sd.default.device[0]
        return i, devs[i]

    def _cb(self, data, frames, t, status):
        x = data[:, 0].copy()
        with self.lock:
            if self.recording:
                if self.n < self.max_seconds * self.rate:
                    self.chunks.append(x)
                    self.n += len(x)
            else:
                self.preroll.append(x)

    def start(self):
        with self.lock:
            self.chunks = list(self.preroll)
            self.n = sum(len(c) for c in self.chunks)
            self.recording = True

    def stop(self):
        with self.lock:
            self.recording = False
            a = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)
            self.chunks = []
        if self.rate != SR and len(a):
            n = int(len(a) * SR / self.rate)
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
        return a.astype(np.float32)


class Transcriber:
    def __init__(self, cfg):
        add_cuda_dlls()
        from faster_whisper import WhisperModel
        self.w, self.cfg = cfg["whisper"], cfg
        t = time.perf_counter()
        kw = dict(device="cuda", compute_type=self.w["compute_type"])
        try:  # 로그인 직후 네트워크가 없어도 뜨도록 캐시 우선
            self.model = WhisperModel(self.w["model"], local_files_only=True, **kw)
        except Exception:
            log.info("캐시에 모델이 없어 다운로드합니다")
            self.model = WhisperModel(self.w["model"], **kw)
        self.model.transcribe(np.zeros(SR, np.float32), language=self.w["language"])  # 워밍업
        log.info("모델 로드 %.1fs (%s)", time.perf_counter() - t, self.w["model"])

    def __call__(self, audio):
        segs, _ = self.model.transcribe(
            audio, language=self.w["language"], beam_size=self.w["beam_size"],
            vad_filter=True, initial_prompt=self.w.get("initial_prompt") or None,
            condition_on_previous_text=False)
        return self.postprocess("".join(s.text for s in segs).strip())

    def postprocess(self, text):
        if text.rstrip(".! ") in self.cfg["hallucinations"]["phrases"]:
            return ""
        for src, dst in self.cfg.get("replacements", {}).items():
            text = text.replace(src, dst)
        return text


def paste(text, restore):
    import keyboard, pyperclip
    old = None
    if restore:
        try:
            old = pyperclip.paste()
        except Exception:
            pass
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    if restore and old is not None:
        time.sleep(0.25)
        pyperclip.copy(old)


def beep(freq, ms):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        pass


class App:
    COLORS = {"idle": (90, 160, 90), "rec": (220, 50, 50), "busy": (230, 160, 40)}
    TITLES = {"idle": "대기", "rec": "녹음 중", "busy": "인식 중"}

    def __init__(self, cfg):
        self.cfg = cfg
        self.rec = Recorder(cfg)
        self.tr = Transcriber(cfg)
        self.q = queue.Queue()
        self.down = False
        self.icon = None

    def on_key(self, e):
        import keyboard
        if e.name != self.cfg["hotkey"]["key"] and e.scan_code != 100:  # 100 = F13 스캔코드
            log.debug("무시된 키: %r scan=%s", e.name, e.scan_code)
            return
        if e.event_type == keyboard.KEY_DOWN:
            if self.down:  # 키 반복 무시
                return
            self.down = True
            self.rec.start()
            self.set_state("rec")
            if self.cfg["output"]["beep"]:
                threading.Thread(target=beep, args=(880, 60), daemon=True).start()
        elif self.down:
            self.down = False
            self.q.put(self.rec.stop())

    def worker(self):
        min_s = self.cfg["hotkey"]["min_seconds"]
        while True:
            audio = self.q.get()
            if len(audio) / SR < min_s:
                log.info("너무 짧아 무시 (%.2fs)", len(audio) / SR)
                self.set_state("idle")
                continue
            self.set_state("busy")
            try:
                t = time.perf_counter()
                text = self.tr(audio)
                log.info("[%.1fs 음성, %.0fms] %s", len(audio) / SR,
                         (time.perf_counter() - t) * 1000, text or "(빈 결과)")
                if text:
                    paste(text, self.cfg["output"]["restore_clipboard"])
            except Exception:
                log.exception("인식 실패")
            self.set_state("idle")

    def make_icon(self, state):
        from PIL import Image, ImageDraw
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((6, 6, 58, 58), fill=self.COLORS[state] + (255,))
        return im

    def set_state(self, s):
        if self.icon:
            self.icon.icon = self.make_icon(s)
            self.icon.title = self.TITLES[s]

    def run(self):
        import keyboard, pystray
        keyboard.hook(self.on_key)
        self.rec.stream.start()
        threading.Thread(target=self.worker, daemon=True).start()
        self.icon = pystray.Icon("ulanze-stt", self.make_icon("idle"), "대기",
                                 pystray.Menu(pystray.MenuItem("종료", lambda i, _: i.stop())))
        log.info("준비 완료. %s 키를 누른 채로 말하세요.", self.cfg["hotkey"]["key"].upper())
        self.icon.run()
        self.rec.stream.stop()


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def selftest(cfg):
    import keyboard
    hits = []
    keyboard.hook(lambda e: hits.append((e.event_type, e.name, e.scan_code)) if e.name == "f13" else None)
    keyboard.press("f13"); time.sleep(0.1); keyboard.release("f13"); time.sleep(0.2)
    print("F13 훅:", hits or "감지 안 됨")
    tr = Transcriber(cfg)
    for f in sorted((ROOT.parent / "bench" / "samples").glob("0[2-5].wav")):
        print(f.name, "->", tr(load_wav(f)))
    r = Recorder(cfg)
    r.stream.start(); time.sleep(1.0)
    r.start(); time.sleep(1.0); a = r.stop(); r.stream.stop()
    print(f"녹음 테스트: {len(a)/SR:.2f}s, peak={np.abs(a).max():.3f}")


if __name__ == "__main__":
    handlers = [logging.FileHandler(ROOT / "stt.log", encoding="utf-8")]
    if sys.stderr:  # pythonw에서는 stderr가 None
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=handlers)
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if not args.selftest and not single_instance():
        log.info("이미 실행 중이라 종료합니다")
        sys.exit(0)
    cfg = load_config()
    selftest(cfg) if args.selftest else App(cfg).run()
