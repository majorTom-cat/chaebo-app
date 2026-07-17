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
    # v5: 곡 끝 샘플을 버리던 버그 교정(파형이 늘어나 진행바가 곡 진행에 비례해 밀림 — 사용자 실측
    # −1000ms@2:44, 실측 늘림 1.37%=2.2s@2:44, 2026-07-14). 옛 캐시(v3/v4)는 버그가 박혀 재생성 필요.
    return config.STEMS_DIR / str(song_id) / "peaks_v5.json"  # v3=8000버킷 → v4=끝버림버그 → v5=전구간


def ensure_gz(song_id: int):
    """gzip 사전압축본 경로 보장 — 매 방문 7.6MB 재전송이 최대 낭비였음(실측 2026-07-09).
    파일 응답이라 ETag/Last-Modified 가 붙어 재방문은 304(0바이트)."""
    src = peaks_path(song_id)
    if not src.exists():
        compute(song_id)  # json 캐시 '없을 때만' 생성 — 있으면 7.6MB 재파싱 낭비 제거(코드검사 2026-07-17).
        #                    gz 재생성은 원본 bytes 만 필요(파싱된 dict 불필요)라 여기서 compute 결과를 안 씀.
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
        # 픽셀 해상도(0.2초 안팎) 버킷에선 |x|max envelope 가 DAW 표준 — 리프 사이
        # 쉼표·어택이 그대로 보인다. (수 초 버킷에서 max 를 쓰면 천장에 붙음 — 그 실패는
        # 버킷을 줄여 해결하는 게 표준이지 집계를 뭉개는 게 아님)
        # ★버킷은 곡 [0, len] 전구간을 균등하게 덮어야 한다. 예전엔 x[:n*N_BUCKETS] 로 reshape 해
        # 끝 (len mod N_BUCKETS) 샘플(최대 수 초)을 버렸고, drawWaves 는 이 버킷들이 player.duration()
        # 전체를 덮는다 가정해 그려서 파형이 늘어났다 → 진행바가 곡 진행에 비례해 파형 뒤로 밀림
        # (사용자 실측 −1000ms@2:44, 실측 bass.wav 늘림 1.37%=2.2s@2:44, 2026-07-14). reduceat 로 전구간 덮음.
        if len(x) >= N_BUCKETS:
            ax = np.abs(x)
            # int64 강제 — 윈도우 np.arange 기본 int32 라 (256001 * 1300만)에서 오버플로 위험.
            edges = (np.arange(N_BUCKETS + 1, dtype=np.int64) * len(x)) // N_BUCKETS
            raw[stem] = np.maximum.reduceat(ax, edges[:-1])  # 버킷 i = max(ax[edges[i]:edges[i+1]]), 끝까지
        else:
            raw[stem] = np.abs(x)

    global_max = max(float(v.max()) for v in raw.values()) or 1.0
    data = {stem: [round(float(v) / global_max, 3) for v in arr] for stem, arr in raw.items()}
    cache.write_text(json.dumps(data), encoding="utf-8")  # 인코딩 명시(하드규칙12 — 읽기와 대칭)
    return data
