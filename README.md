# UlanzeAgent

Ulanzi AU05 Vibe Key의 Voice Key(F13)를 누르고 있는 동안 말하면, 로컬 GPU에서 Whisper(`large-v3-turbo`, faster-whisper)가 인식한 한국어 텍스트를 현재 커서 위치에 붙여넣는 Windows용 음성 입력 도구입니다. Windows 내장 음성 입력(Win+H)보다 인식률을 높이는 것이 목적입니다.

```
Voice Key(F13) 누름 -> AU05 마이크 녹음 -> faster-whisper(CUDA) -> 용어 치환 -> Ctrl+V 붙여넣기
```

## 구성

| 폴더 | 내용 |
|---|---|
| [`app/`](app/) | 트레이 상주 앱 (`stt.py`), 설정(`config.toml`), 자동 실행 등록(`autostart.ps1`) |
| [`bench/`](bench/) | 내 마이크로 모델별 한국어 인식률과 지연 시간을 비교하는 벤치마크 |

설치와 사용법, 운영 사양, 제한 사항은 **[`app/INSTALL.md`](app/INSTALL.md)** 를 보세요.

## 요약

- Windows 11, NVIDIA GPU(CUDA) 필요. Python 3.11~3.12 (uv가 자동 설치)
- 실측(RTX 3090): 인식 지연 평균 약 270ms, VRAM 약 2.4GB
- 벤치마크 결과 `large-v3-turbo`가 `large-v3`와 정확도는 같고 약 2.7배 빠름. 용어 힌트(`initial_prompt`)로 CER 12.2% -> 6.2% (10문장, 단일 화자 기준이므로 참고용)

```powershell
cd app
uv sync
uv run stt.py
```

## 참고

이 프로젝트는 Ulanzi와 무관한 개인 프로젝트입니다. Whisper 모델과 faster-whisper는 각 프로젝트의 라이선스를 따릅니다.
