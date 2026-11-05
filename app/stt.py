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

    def _find(self, spec):
        """spec: ""/"default" = Windows 기본 마이크, 정수(또는 숫자 문자열) = 장치 번호,
        그 외 문자열 = 장치 이름의 일부. 못 찾으면 기본 마이크로 대체한다."""
        sd = self.sd
        devs = sd.query_devices()
        spec = spec.strip() if isinstance(spec, str) else spec
        if spec not in ("", None, "default"):
            if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
                i = int(spec)
                if 0 <= i < len(devs) and devs[i]["max_input_channels"] > 0:
                    return i, devs[i]
                log.warning("장치 번호 %s는 입력 장치가 아니라 기본 입력 장치를 사용", spec)
            else:
                cands = [(i, d) for i, d in enumerate(devs)
                         if d["max_input_channels"] > 0 and spec.lower() in d["name"].lower()]
                # 호스트 API 우선순위: MME(자동 리샘플링) > DirectSound > WASAPI > 나머지(WDM-KS 등).
                # 같은 우선순위에서는 이름에 마이크/mic 이 있는 장치를 먼저 (라인·SPDIF 입력 회피).
                prio = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}

                def rank(c):
                    i, d = c
                    api = sd.query_hostapis(d["hostapi"])["name"]
                    mic = any(w in d["name"].lower() for w in ("마이크", "mic"))
                    return (prio.get(api, 3), 0 if mic else 1, i)

                if cands:
                    return min(cands, key=rank)
                log.warning("'%s' 장치를 찾지 못해 기본 입력 장치를 사용", spec)
        i = sd.default.device[0]
        if i is None or i < 0 or devs[i]["max_input_channels"] <= 0:
            inputs = [k for k, d in enumerate(devs) if d["max_input_channels"] > 0]
            if not inputs:
                raise RuntimeError("사용 가능한 마이크(입력 장치)가 없습니다")
            i = inputs[0]
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


def list_input_devices():
    """입력 장치 목록 출력. 같은 장치가 호스트 API별로 중복되므로 MME 항목만 보인다."""
    import sounddevice as sd
    default = sd.default.device[0]
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "MME" in sd.query_hostapis(d["hostapi"])["name"]:
            mark = "  <- Windows 기본" if i == default else ""
            print(f"[{i}] {d['name']}  ({d['default_samplerate']:.0f}Hz){mark}")
    print("\nconfig.toml의 [audio] device 에 이름 일부(예: \"USB\") 또는 위 번호를 적으세요. "
          "비워 두면 Windows 기본 마이크를 씁니다.")


class Hotkey:
    """'f13', 'ctrl+win', 'right ctrl' 같은 키 조합. 모든 키가 눌려 있는 동안 활성."""

    ALIAS = {"win": "windows", "control": "ctrl", "return": "enter", "escape": "esc",
             "scrolllock": "scroll lock", "caps": "caps lock"}
    PRINTING = {"space", "enter", "tab"}

    def __init__(self, spec):
        self.spec = spec
        toks = [t.strip().lower() for t in spec.split("+") if t.strip()]
        self.tokens = frozenset(self.ALIAS.get(t, t) for t in toks)
        # 글자를 입력하는 키가 들어 있으면 그 글자가 활성 창에도 그대로 입력된다.
        self.types_text = any(len(t) == 1 or t in self.PRINTING for t in self.tokens)


class HotkeyTracker:
    """키 이벤트로부터 녹음 시작/종료 시점을 판정한다 (키보드 훅과 분리해 테스트 가능)."""

    def __init__(self, specs):
        if isinstance(specs, str):
            specs = [specs]
        self.hotkeys = [Hotkey(s) for s in specs]
        self.pressed, self.active = set(), None

    @staticmethod
    def _names(name, scan_code):
        names = set()
        if name:
            name = name.lower()
            names.add(name)
            for side in ("left ", "right "):  # 'right ctrl' 은 'ctrl' 로도 취급
                if name.startswith(side):
                    names.add(name[len(side):])
        if scan_code == 100:  # F13 은 키보드 라이브러리가 이름을 못 붙이는 경우가 있음
            names.add("f13")
        return names

    def feed(self, name, scan_code, is_down):
        """'start' / 'stop' / None 을 돌려준다."""
        names = self._names(name, scan_code)
        if not names:
            return None
        if is_down:
            self.pressed |= names
            if self.active is None:
                for hk in self.hotkeys:
                    if hk.tokens and hk.tokens <= self.pressed:
                        self.active = hk
                        return "start"
        else:
            self.pressed -= names
            if self.active is not None and not self.active.tokens <= self.pressed:
                self.active = None
                return "stop"
        return None


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
        self.keys = HotkeyTracker(cfg["hotkey"]["key"])
        for hk in self.keys.hotkeys:
            if hk.types_text:
                log.warning("트리거 '%s'에 글자 키가 있어 누를 때 그 글자도 입력됩니다. "
                            "F키나 Ctrl/Win 같은 조합키를 권장합니다", hk.spec)
        self.icon = None

    def on_key(self, e):
        import keyboard
        action = self.keys.feed(e.name, e.scan_code, e.event_type == keyboard.KEY_DOWN)
        if action == "start":
            self.rec.start()
            self.set_state("rec")
            if self.cfg["output"]["beep"]:
                threading.Thread(target=beep, args=(880, 60), daemon=True).start()
        elif action == "stop":
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
        log.info("준비 완료. %s 를 누른 채로 말하세요.",
                 " 또는 ".join(h.spec.upper() for h in self.keys.hotkeys))
        self.icon.run()
        self.rec.stream.stop()


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def test_hotkeys():
    """키 판정 로직을 가짜 이벤트로 점검한다 (실제 키보드가 필요 없음)."""
    def run(specs, events):
        t, out = HotkeyTracker(specs), []
        for name, scan, down in events:
            out.append(t.feed(name, scan, down))
        return [x for x in out if x]

    D, U = True, False
    cases = [
        ("단일키 f13 (이름)", ["f13"], [("f13", 100, D), ("f13", 100, D), ("f13", 100, U)], ["start", "stop"]),
        ("단일키 f13 (이름 없음, 스캔코드만)", ["f13"], [(None, 100, D), (None, 100, U)], ["start", "stop"]),
        ("조합 ctrl+win", ["ctrl+win"],
         [("ctrl", 29, D), ("left windows", 91, D), ("left windows", 91, U), ("ctrl", 29, U)], ["start", "stop"]),
        ("조합은 일부만 눌러선 시작 안 함", ["ctrl+win"], [("ctrl", 29, D), ("ctrl", 29, U)], []),
        ("right ctrl 지정", ["right ctrl"], [("right ctrl", 285, D), ("right ctrl", 285, U)], ["start", "stop"]),
        ("ctrl 지정은 right ctrl로도 동작", ["ctrl"], [("right ctrl", 285, D), ("right ctrl", 285, U)], ["start", "stop"]),
        ("후보 여러 개 중 하나", ["f13", "scroll lock"], [("scroll lock", 70, D), ("scroll lock", 70, U)], ["start", "stop"]),
        ("관계없는 키는 무시", ["f13"], [("a", 30, D), ("a", 30, U)], []),
    ]
    ok = True
    for title, specs, events, want in cases:
        got = run(specs, events)
        ok &= got == want
        print(f"  {'OK ' if got == want else 'FAIL'} {title}: {got}")
    return ok


def selftest(cfg):
    print("핫키 판정 로직:")
    if not test_hotkeys():
        print("  -> 실패")
    print("설정된 트리거:", cfg["hotkey"]["key"])
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
    ap.add_argument("--devices", action="store_true", help="마이크(입력 장치) 목록을 보고 종료")
    args = ap.parse_args()
    if args.devices:
        list_input_devices()
        sys.exit(0)
    if not args.selftest and not single_instance():
        log.info("이미 실행 중이라 종료합니다")
        sys.exit(0)
    cfg = load_config()
    selftest(cfg) if args.selftest else App(cfg).run()
