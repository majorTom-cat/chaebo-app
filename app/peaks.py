"""스템별 사전계산 peak (REQ-PLAY-007) — 최초 요청 시 계산 후 JSON 캐시.

도메인 표준(DAW lane) = 픽셀 해상도 피크 envelope: 화면 1px ≈ 버킷 1개, 버킷 내 |x|max.
(90버킷 막대는 시안 일러스트 밀도였고 사용자 지적으로 표준 교정 — 2026-07-07)
공유 정규화: 전 스템 공통 최대값 기준(악기 간 강약 비교가 목적).
"""
import gzip
import json

import numpy as np
import soundfile as sf

from app import config

# v6 는 버킷마다 양·음 피크 2값이라 데이터가 2배 — 버킷 수를 절반(128000)으로 낮춰 총량을 v5 수준(~10MB)
# 유지. 줌 256배에서도 창당 ~500버킷(1.8ms/버킷@4분)이라 베이스 실파형 모양엔 충분(저음 <300Hz).
N_BUCKETS = 128000


def peaks_path(song_id: int):
    # v6: 부호 있는 실제 파형 — 버킷마다 양의 피크(hi=max)·음의 피크(lo=min)를 따로 저장(DAW 표준).
    # 예전 v5 는 |x|max 하나라 위아래 대칭 미러였음(부호 없음, 사용자 지적 2026-07-14). 형식도 배열→
    # {hi:[], lo:[]} 로 바뀌어 옛 캐시 재생성 필요. (v3=8000버킷→v4=끝버림버그→v5=전구간|x|→v6=부호)
    return config.STEMS_DIR / str(song_id) / "peaks_v6.json"


def ensure_gz(song_id: int):
    """gzip 사전압축본 경로 보장 — 매 방문 7.6MB 재전송이 최대 낭비였음(실측 2026-07-09).
    파일 응답이라 ETag/Last-Modified 가 붙어 재방문은 304(0바이트)."""
    compute(song_id)  # json 캐시 보장
    src = peaks_path(song_id)
    gz = src.with_suffix(".json.gz")
    if not gz.exists() or gz.stat().st_mtime < src.stat().st_mtime:
        gz.write_bytes(gzip.compress(src.read_bytes(), compresslevel=6))
    return gz


def compute(song_id: int) -> dict:
    cache = peaks_path(song_id)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    hi: dict[str, np.ndarray] = {}
    lo: dict[str, np.ndarray] = {}
    for stem in config.STEMS:
        f = config.STEMS_DIR / str(song_id) / f"{stem}.wav"
        x, _sr = sf.read(f, dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        # 버킷마다 부호 있는 양/음 피크(min·max) — DAW 표준 실파형. 예전엔 |x|max 하나라 대칭이었음.
        # ★버킷은 곡 [0, len] 전구간을 균등 덮음(reduceat) — 끝 버림 버그(v4, 진행바 밀림) 재발 방지.
        if len(x) >= N_BUCKETS:
            # int64 강제 — 윈도우 np.arange 기본 int32 라 (256001 * 1300만)에서 오버플로 위험.
            edges = (np.arange(N_BUCKETS + 1, dtype=np.int64) * len(x)) // N_BUCKETS
            hi[stem] = np.maximum.reduceat(x, edges[:-1])  # 양의 피크(위)
            lo[stem] = np.minimum.reduceat(x, edges[:-1])  # 음의 피크(아래)
        else:
            hi[stem] = np.maximum(x, 0.0)
            lo[stem] = np.minimum(x, 0.0)

    # 공유 정규화(전 스템 공통) — |양/음| 최댓값 기준. 악기 간 강약 비교 목적 유지.
    global_max = max(
        max(float(np.abs(hi[s]).max()) for s in hi),
        max(float(np.abs(lo[s]).max()) for s in lo),
    ) or 1.0
    data = {
        stem: {
            "hi": [round(float(v) / global_max, 3) for v in hi[stem]],
            "lo": [round(float(v) / global_max, 3) for v in lo[stem]],
        }
        for stem in config.STEMS
    }
    cache.write_text(json.dumps(data), encoding="utf-8")  # 인코딩 명시(하드규칙12 — 읽기와 대칭)
    return data
