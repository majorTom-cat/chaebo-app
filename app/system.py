"""장치 감지(REQ-SEP-002) — torch 임포트가 무거워 서브프로세스로 1회 판독 후 캐시.

정직 UI(하드 규칙 7): 예전엔 '장치 적응형'이라 해놓고 번들이 CPU 전용 torch 라, GPU 가 있어도
언제나 CPU 로만 돌고 화면엔 'NVIDIA GPU 없음'이라 단정했다(사용자 지적 2026-07-12 — "그래픽카드가
있는데도 CPU"). 이를 바로잡기 위해 두 가지를 분리해 정직하게 보고한다:
  - device: torch 가 실제로 CUDA 를 쓸 수 있는가(가속이 지금 켜져 있는가)
  - nvidia: NVIDIA GPU + 드라이버가 물리적으로 있는가(nvidia-smi 로 판독 — torch 무관)
NVIDIA 가 있는데 CPU torch 면(can_enable_gpu) → '가속을 켤 수 있어요'라고 정직하게 안내한다.
(torch 의 CUDA 가속은 NVIDIA 전용 — Intel/AMD 내장·외장은 이 분리 AI 로 가속되지 않는다.)
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from app import config

# pythonw(콘솔 없음)로 실행돼도 자식 콘솔창이 번쩍이지 않게.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 판독은 뒷일 — 낮은 우선순위로(앱 켜자마자 torch 임포트가 화면·분리와 코어를 다투지 않게).
_LOW = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

_device: str | None = None       # 'gpu' | 'cpu' — torch.cuda 기준(가속이 지금 켜져 있나)
_status: dict | None = None
# ★단일 비행(성능 코드검사 2026-07-29): 락이 없으면 startup 예열분과 페이지의 /api/settings 호출들이
#   각자 torch 임포트 프로세스를 띄운다(실측: 동시 3회 = 3개, 1개당 3.4초). 먼저 온 하나만 판독하고
#   나머지는 그 결과를 기다린다. device/status 는 서로 다른 락 — status 안에서 device 를 부르므로.
_dev_lock = asyncio.Lock()
_status_lock = asyncio.Lock()


def _torch_sig() -> str:
    """설치된 torch 를 식별하는 값. 이게 그대로면 판독 결과도 그대로다(GPU 켜기 = torch 교체 → 바뀜)."""
    try:
        p = Path(config.PYTHON).parent.parent / "Lib" / "site-packages" / "torch" / "version.py"
        st = p.stat()
        # 파이썬 경로까지 넣는다 — 데이터 폴더는 설치본끼리 공유라, 경로가 다르면 다른 설치의 판독이다
        return f"{config.PYTHON}|{st.st_size}|{int(st.st_mtime)}"
    except Exception:  # noqa: BLE001 — 못 읽으면 캐시를 안 쓰고 매번 판독(보수적)
        return ""


def _cache_file() -> Path:
    return config.DATA_DIR / "device.json"


def _read_cache(sig: str) -> str | None:
    """앱을 켤 때마다 3.4초짜리 torch 임포트를 되풀이하지 않도록 지난 판독을 재사용."""
    try:
        d = json.loads(_cache_file().read_text(encoding="utf-8"))
        if d.get("sig") == sig and d.get("device") in ("gpu", "cpu"):
            return d["device"]
    except Exception:  # noqa: BLE001 — 없거나 깨졌으면 그냥 다시 판독
        pass
    return None


def _write_cache(sig: str, dev: str) -> None:
    try:
        _cache_file().write_text(json.dumps({"sig": sig, "device": dev}), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 캐시는 있으면 좋은 것(실패해도 동작엔 지장 없음)
        pass


async def device() -> str:
    global _device
    if _device is not None:
        return _device
    async with _dev_lock:
        if _device is not None:  # 기다리는 사이 다른 호출이 끝냈다 — 중복 임포트 금지
            return _device
        sig = _torch_sig()
        cached = _read_cache(sig) if sig else None
        if cached:
            _device = cached
            return _device
        try:
            proc = await asyncio.create_subprocess_exec(
                config.PYTHON, "-c",
                "import torch; print('gpu' if torch.cuda.is_available() else 'cpu')",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=_NO_WINDOW | _LOW)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            _device = out.decode().strip() or "cpu"
            if sig and _device in ("gpu", "cpu"):
                _write_cache(sig, _device)
        except Exception:  # noqa: BLE001 — 판독 실패는 보수적으로 CPU 취급(캐시엔 안 남긴다)
            _device = "cpu"
    return _device


async def _nvidia_present() -> bool:
    """NVIDIA GPU + 드라이버가 실제로 있는지(torch 무관). nvidia-smi 가 GPU 를 나열하면 True.
    torch 의 CUDA 가속은 NVIDIA+드라이버가 있어야 동작하므로, 이게 '가속 가능'의 진짜 신호다."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "-L", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=_NO_WINDOW)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return proc.returncode == 0 and b"GPU" in out
    except Exception:  # noqa: BLE001
        return False


async def status() -> dict:
    """UI 정직 표기용 종합 상태(1회 판독 후 캐시). invalidate() 로 갱신."""
    global _status
    if _status is not None:
        return _status
    async with _status_lock:  # 한 페이지가 /api/settings 를 여러 번 부른다 — 판독은 한 번만
        if _status is not None:
            return _status
        dev = await device()
        nvidia = await _nvidia_present()
        _status = {
            "device": dev,                                # 'gpu'|'cpu' (지금 가속 켜졌나)
            "nvidia": nvidia,                             # NVIDIA+드라이버 물리적 존재
            "can_enable_gpu": nvidia and dev != "gpu",    # NVIDIA 있는데 CPU torch → 켤 수 있음
        }
    return _status


def invalidate():
    """GPU 가속을 켠(torch 를 CUDA 판으로 교체한) 뒤 재판독하도록 캐시를 비운다."""
    global _device, _status
    _device = None
    _status = None
    try:
        _cache_file().unlink()  # 디스크 캐시도 함께 — 안 지우면 교체 전 판독이 되살아난다
    except Exception:  # noqa: BLE001 — 없으면 그만
        pass
