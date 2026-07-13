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
import shutil
import subprocess

from app import config

# pythonw(콘솔 없음)로 실행돼도 자식 콘솔창이 번쩍이지 않게.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_device: str | None = None       # 'gpu' | 'cpu' — torch.cuda 기준(가속이 지금 켜져 있나)
_status: dict | None = None


async def device() -> str:
    global _device
    if _device is None:
        try:
            proc = await asyncio.create_subprocess_exec(
                config.PYTHON, "-c",
                "import torch; print('gpu' if torch.cuda.is_available() else 'cpu')",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=_NO_WINDOW)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            _device = out.decode().strip() or "cpu"
        except Exception:  # noqa: BLE001 — 판독 실패는 보수적으로 CPU 취급
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
    if _status is None:
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
