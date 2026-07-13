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

N_BUCKETS = 256000  # 줌 256배에서도 창당 ~1000버킷(0.9ms/버킷@4분) — 클라이언트가 창 단위 축약


def peaks_path(song_id: int):
    return config.STEMS_DIR / str(song_id) / "peaks_v4.json"  # v3=8000버킷(×8 한계) → v4


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

    raw: dict[str, np.ndarray] = {}
    for stem in config.STEMS:
        f = config.STEMS_DIR / str(song_id) / f"{stem}.wav"
        x, _sr = sf.read(f, dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        n = len(x) // N_BUCKETS or 1
        # 픽셀 해상도(0.2초 안팎) 버킷에선 |x|max envelope 가 DAW 표준 — 리프 사이
        # 쉼표·어택이 그대로 보인다. (수 초 버킷에서 max 를 쓰면 천장에 붙음 — 그 실패는
        # 버킷을 줄여 해결하는 게 표준이지 집계를 뭉개는 게 아님)
        if len(x) >= N_BUCKETS:
            buckets = np.abs(x[: n * N_BUCKETS]).reshape(N_BUCKETS, -1)
            raw[stem] = buckets.max(axis=1)
        else:
            raw[stem] = np.abs(x)

    global_max = max(float(v.max()) for v in raw.values()) or 1.0
    data = {stem: [round(float(v) / global_max, 3) for v in arr] for stem, arr in raw.items()}
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data
