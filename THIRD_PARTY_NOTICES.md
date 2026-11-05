# Third-party notices

UlanzeAgent itself is MIT licensed (see [LICENSE](LICENSE)). This repository does not contain or
redistribute any third-party code or models: they are installed by the user (`uv sync`) and
downloaded on first run. The licenses below are the ones declared by the installed packages' own
metadata (and, for models, by their Hugging Face model cards) when this file was written; check the
packages themselves for the authoritative text.

## Python packages

| Package | License |
|---|---|
| faster-whisper | MIT |
| ctranslate2 | MIT |
| sounddevice | MIT |
| keyboard | MIT |
| onnxruntime | MIT |
| numpy | BSD-3-Clause (and others, see the package) |
| pyperclip | BSD |
| av | BSD-3-Clause |
| pillow | MIT-CMU |
| tokenizers, huggingface-hub | Apache-2.0 |
| **pystray** | **LGPL-3.0** |
| nvidia-cublas-cu12, nvidia-cudnn-cu12 | NVIDIA proprietary (installed from PyPI under NVIDIA's terms) |

### Note on pystray (LGPL-3.0)
pystray is used unmodified as a separately installed library (imported at run time). If you
distribute a bundled executable that includes it (for example with PyInstaller), the LGPL requires
that users can replace the library with their own version; consider that before bundling.

### Note on the NVIDIA packages
`nvidia-cublas-cu12` and `nvidia-cudnn-cu12` are proprietary NVIDIA software. They are fetched by the
user from PyPI and must not be committed to or redistributed from this repository.

## Model

`large-v3-turbo` in the CTranslate2 format is downloaded by faster-whisper from
https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo, whose model card states the
MIT license (derived from OpenAI Whisper).
