# -*- coding: utf-8 -*-
"""첫 실행 준비물 점검·다운로드 — 서버 시작 전에 run.py 가 호출.

정책(하드 규칙 5): 분리 엔진·AI 가중치·ffmpeg 는 저장소에 동봉하지 않는다 —
없으면 각 원출처에서 자동으로 내려받는다(진행 표시는 쉬운 한국어, G4).
BS-Roformer SW 는 라이선스 불명이라 자동 다운로드 대상이 아님(licenses.md 경고).

원출처(전부 licenses.md 감사표 ✅):
- 분리 엔진 MSST: ZFTurbo/Music-Source-Separation-Training (MIT) — 커밋 고정 zip
- 가중치 htdemucs_6s.th: Meta demucs 공식 배포 서버 (MIT) — 크기 검증
- ffmpeg: BtbN 공식 자동빌드 LGPL판 (동봉 아님 — 사용자 PC 로 직접 다운로드)
"""
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from app import config

# 이 커밋으로 고정 — 이 PC에서 실측 검증된 엔진 버전(무단 업스트림 변경 차단)
MSST_COMMIT = "b24341972c72f6f2d90e72cfa262772e41dd9418"
MSST_ZIP_URL = ("https://codeload.github.com/ZFTurbo/"
                f"Music-Source-Separation-Training/zip/{MSST_COMMIT}")
CKPT_URL = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/5c90dfd2-34c22ccb.th"
CKPT_SIZE = 54996327  # 실측 — 내려받은 파일 크기가 다르면 손상으로 판정
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-lgpl.zip")
FFMPEG_DIR = config.BASE_DIR / "vendor-tools" / "ffmpeg"


def _download(url: str, dest: Path, label: str, on_pct=None):
    """진행률 표시 다운로드 — 실패 시 부분 파일 제거. on_pct(0~100) 는 스플래시 로딩바용."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "chaebo/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            last_log = -10
            last_ui = -2
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct >= last_log + 10:
                        last_log = pct
                        print(f"  {label} 내려받는 중… {pct}%", flush=True)
                    if on_pct and pct >= last_ui + 2:  # UI 는 더 촘촘히(부드러운 바)
                        last_ui = pct
                        on_pct(pct)
        if on_pct:
            on_pct(100)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def ensure_msst(on_pct=None) -> bool:
    if (config.MSST_DIR / "inference.py").exists():
        return False
    print("[준비] 소리 분리 엔진이 없어서 처음 한 번 내려받아요 (약 1~2분)", flush=True)
    zip_path = config.MSST_DIR.parent / "msst_src.zip"
    _download(MSST_ZIP_URL, zip_path, "분리 엔진", on_pct)
    extract_root = config.MSST_DIR.parent
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_root)
    zip_path.unlink(missing_ok=True)
    src = extract_root / f"Music-Source-Separation-Training-{MSST_COMMIT}"
    if config.MSST_DIR.exists():
        shutil.rmtree(config.MSST_DIR)
    src.rename(config.MSST_DIR)
    print("  분리 엔진 준비 완료", flush=True)
    return True


def ensure_ckpt(on_pct=None) -> bool:
    if config.MSST_CKPT.exists() and config.MSST_CKPT.stat().st_size == CKPT_SIZE:
        return False
    print("[준비] 분리 AI 모델(약 52MB)을 처음 한 번 내려받아요", flush=True)
    _download(CKPT_URL, config.MSST_CKPT, "분리 모델", on_pct)
    got = config.MSST_CKPT.stat().st_size
    if got != CKPT_SIZE:
        config.MSST_CKPT.unlink(missing_ok=True)
        raise RuntimeError(f"분리 모델 파일이 손상됐어요(크기 {got}) — 다시 실행해 주세요")
    print("  분리 모델 준비 완료", flush=True)
    return True


def ensure_ffmpeg(on_pct=None) -> bool:
    if shutil.which("ffmpeg"):
        return False
    exe = FFMPEG_DIR / "bin" / "ffmpeg.exe"
    if exe.exists():
        _prepend_path()
        return False
    print("[준비] 오디오 변환 도구(ffmpeg, 약 80MB)를 처음 한 번 내려받아요", flush=True)
    zip_path = FFMPEG_DIR.parent / "ffmpeg.zip"
    _download(FFMPEG_URL, zip_path, "오디오 변환 도구", on_pct)
    FFMPEG_DIR.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.endswith(("ffmpeg.exe", "ffprobe.exe"))]
        for n in names:
            target = FFMPEG_DIR / "bin" / Path(n).name
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
    zip_path.unlink(missing_ok=True)
    _prepend_path()
    print("  오디오 변환 도구 준비 완료", flush=True)
    return True


def _prepend_path():
    bin_dir = str(FFMPEG_DIR / "bin")
    if bin_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


# torch 버전 하한(매니페스트) — 앱이 특정 torch 버전 이상을 요구하게 되면 여기를 올린다. None=검사 안 함.
# 배경(사용자 지적 2026-07-13): 설치기가 업그레이드 때 torch 를 보존(onlyifdoesntexist)해 GPU 재다운로드를
# 막는다. 하지만 '그냥 보존만' 이면 버전 갱신이 안 되므로, 필요할 때 이 하한으로 재설치한다("같으면 보존,
# 필요하면 갱신"). torch 외 라이브러리는 설치기가 매 업그레이드 갱신하므로 이 훅이 필요 없다.
MIN_TORCH = None  # 예: "2.5.0"


def _ver_tuple(v):
    return tuple(int(p) for p in str(v or "0").split(".")[:3] if p.isdigit())


def ensure_torch(on_pct=None) -> bool:
    """torch 버전 하한 검사 — 설치된 게 낮을 때만 재설치(GPU=cu121, 아니면 CPU 인덱스). 실패는 무시
    (옛 torch 가 남아 동작). MIN_TORCH=None 이면 즉시 skip(보존)."""
    if not MIN_TORCH:
        return False
    try:
        import torch
        cur = torch.__version__.split("+")[0]
        gpu = torch.cuda.is_available()
    except Exception:  # noqa: BLE001 — torch 자체 부재는 번들이 보장, 여기선 버전만
        return False
    if _ver_tuple(cur) >= _ver_tuple(MIN_TORCH):
        return False  # 충분 → 보존
    print(f"[준비] 계산 라이브러리(torch)를 {MIN_TORCH} 이상으로 갱신해요… (GPU 판은 다시 받을 수 있어요)",
          flush=True)
    index = ("https://download.pytorch.org/whl/cu121" if gpu
             else "https://download.pytorch.org/whl/cpu")
    import subprocess
    try:
        subprocess.run([config.PYTHON, "-m", "pip", "install", "--no-warn-script-location",
                        "torch", "torchaudio", "--index-url", index],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception as e:  # noqa: BLE001 — 실패해도 옛 torch 로 계속
        print(f"[준비] torch 갱신 실패(옛 버전으로 계속): {e}", flush=True)
        return False


def ensure_all(progress=None):
    """서버 시작 전 일괄 점검 — 실패는 쉬운 한국어로 알리고 중단.

    progress(pct 0~100, msg) 가 있으면 스플래시 로딩바를 갱신한다. 세 준비물에 진행
    구간(band)을 배분: 분리 엔진 4~40, 분리 모델 40~62, 오디오 도구 62~95. 이미 있는
    준비물은 즉시 구간 끝으로 점프(다운로드 없음)."""
    def rep(pct, msg):
        if progress:
            try:
                progress(int(pct), msg)
            except Exception:  # noqa: BLE001 — UI 갱신 실패는 준비를 막지 않음
                pass

    def band(lo, hi, msg):
        return lambda p: rep(lo + (hi - lo) * p / 100.0, msg)

    try:
        rep(3, "준비물을 확인하는 중이에요")
        ensure_torch()  # torch 버전 하한 검사 — 보통 no-op(보존), 하한 올린 릴리스에서만 재설치
        did_msst = ensure_msst(band(4, 40, "소리 분리 엔진을 받는 중이에요"))
        rep(40, "분리 AI 모델을 확인하는 중이에요")
        did_ckpt = ensure_ckpt(band(40, 62, "분리 AI 모델을 받는 중이에요"))
        rep(62, "오디오 변환 도구를 확인하는 중이에요")
        did_ff = ensure_ffmpeg(band(62, 95, "오디오 변환 도구를 받는 중이에요"))
        rep(95, "거의 다 됐어요")
        if did_msst | did_ckpt | did_ff:
            print("[준비] 처음 준비가 끝났어요 — 서버를 시작할게요", flush=True)
    except Exception as e:  # noqa: BLE001
        print("", flush=True)
        print("[문제] 처음 준비 중에 인터넷에서 파일을 받지 못했어요.", flush=True)
        print("       인터넷 연결을 확인하고 프로그램을 다시 실행해 주세요.", flush=True)
        print(f"       (자세한 내용: {e})", flush=True)
        rep(0, "인터넷에서 파일을 받지 못했어요 — 연결을 확인하고 다시 실행해 주세요")
        sys.exit(1)


if __name__ == "__main__":
    ensure_all()
