"""NVIDIA GPU 가속 켜기 — 번들은 용량(설치파일 ~330MB) 때문에 CPU 전용 torch 를 담는다. NVIDIA
GPU 가 있는 사용자가 원하면 CUDA 판 torch(~2.5GB)를 한 번 내려받아 교체한다. 분리는 매번 새
서브프로세스로 torch 를 임포트하므로, 교체 후 앱 재시작 없이 '다음 분리부터' GPU 를 쓴다.

정직 고지(하드 규칙 7): 개발 머신에 NVIDIA GPU 가 없어(2026-07-12) 실제 CUDA 설치·가속은 이
코드에서 미검증이다. 감지·거부·진행 배선은 검증됨 — NVIDIA PC 실측으로 최종 확인이 필요하다.
"""
import asyncio
import subprocess

from app import config, system

# CUDA 12.1 판 휠 인덱스(PyTorch 공식). NVIDIA 드라이버가 있으면 이 판이 GPU 를 잡는다.
CUDA_INDEX = "https://download.pytorch.org/whl/cu121"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_state = {"running": False, "pct": 0, "message": "", "done": False, "ok": False}
_task = None


def state() -> dict:
    return dict(_state)


async def _run_install():
    global _state
    _state.update(running=True, pct=5, done=False, ok=False,
                  message="GPU용 라이브러리를 내려받는 중이에요 (약 2.5GB — 몇 분 걸려요)")
    try:
        # 1) 기존 CPU 판을 먼저 제거한다. `pip install --upgrade` 만으로는 이미 깔린 torch(+cpu)를
        #    같은 버전 번호로 보고 CUDA 판(+cu121)으로 안 바꾼다(실측: '설치는 됐지만 GPU 못 잡음'의
        #    원인) → 명시적으로 uninstall 후 CUDA 인덱스에서 재설치해 확실히 교체.
        _state.update(pct=8, message="기존 라이브러리를 정리하는 중이에요…")
        un = await asyncio.create_subprocess_exec(
            config.PYTHON, "-m", "pip", "uninstall", "-y", "torch", "torchaudio",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=_NO_WINDOW)
        await un.wait()  # 없어도(실패해도) 다음 설치가 새로 깐다
        # 2) CUDA 판 torch 설치(cuda 런타임 포함 — 시스템 CUDA 불필요, NVIDIA 드라이버만 있으면 됨)
        _state.update(pct=12, message="GPU용 라이브러리를 내려받는 중이에요 (약 2.5GB — 몇 분 걸려요)")
        proc = await asyncio.create_subprocess_exec(
            config.PYTHON, "-m", "pip", "install", "--no-warn-script-location",
            "torch", "torchaudio", "--index-url", CUDA_INDEX,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=_NO_WINDOW)
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            t = line.decode(errors="replace")
            # pip 진행률은 정밀 파싱이 어려워 대략적 단계로만 표시(정직: 추정치)
            if "Downloading" in t:
                _state.update(pct=min(_state["pct"] + 5, 82),
                              message="GPU용 torch 를 받는 중이에요…")
            elif "Installing collected" in t:
                _state.update(pct=90, message="설치하는 중이에요… 거의 다 됐어요")
        code = await proc.wait()
        if code == 0:
            system.invalidate()
            dev = await system.device()  # 교체 후 재판독
            ok = dev == "gpu"
            _state.update(running=False, pct=100, done=True, ok=ok,
                          message="GPU 가속을 켰어요! 다음 곡 분리부터 빨라져요" if ok
                          else "설치는 됐지만 GPU 를 아직 못 잡았어요 — 그래픽 드라이버(NVIDIA)를 최신으로 업데이트해 주세요")
        else:
            _state.update(running=False, done=True, ok=False, pct=0,
                          message="설치에 실패했어요 — 인터넷 연결을 확인하고 다시 시도해 주세요")
    except Exception as e:  # noqa: BLE001
        _state.update(running=False, done=True, ok=False, pct=0,
                      message="설치 중 문제가 있었어요 — 잠시 뒤 다시 시도해 주세요")
        print(f"[gpu] enable 실패: {e}", flush=True)


def start() -> bool:
    """CUDA torch 설치를 백그라운드로 시작. 이미 실행 중이면 False."""
    global _task
    if _state["running"]:
        return False
    _state.update(running=True, pct=0, done=False, ok=False, message="시작하는 중…")
    _task = asyncio.ensure_future(_run_install())
    return True
