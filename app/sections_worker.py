# -*- coding: utf-8 -*-
"""곡 구간 경계 감지 워커 — librosa 라플라시안 세그먼테이션(추가 의존성 0, SP-5b 축소안).
서브프로세스: python -m app.sections_worker <원본오디오> <vocals.wav> <out.json>

정직 원칙: 인트로/코러스 같은 '기능 라벨'은 이 환경(allin1 불가 — MSVC·NC 라이선스)에선
붙이지 않는다. 제공하는 것: ①경계 ②반복 그룹(같은 유형 = 같은 색) ③보컬 유무 힌트.
이름은 "구간 N" 기본값 — 사용자가 직접 바꾼다(코러스 등)."""
import json
import sys

import numpy as np

MIN_SEG_SEC = 8.0  # 이보다 짧은 조각은 앞 구간에 병합 — 5초에선 긴 곡 중반이 잘게 조각남(벧엘 16구간 실증)


def detect_sections(y, sr, duration):
    import librosa
    import scipy.linalg
    import scipy.ndimage
    import scipy.sparse.csgraph
    from sklearn.cluster import KMeans

    k = int(min(6, max(3, round(duration / 45))))  # 곡 길이 비례 유형 수(45초당 1)
    C = librosa.amplitude_to_db(np.abs(librosa.cqt(y=y, sr=sr)), ref=np.max)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    if len(beats) < k + 2:  # 비트가 없다시피 한 소재(톤 등) — 통구간 1개
        return [(0.0, duration, 0)]
    Csync = librosa.util.sync(C, beats, aggregate=np.median)
    R = librosa.segment.recurrence_matrix(Csync, width=3, mode="affinity", sym=True)
    df = librosa.segment.timelag_filter(scipy.ndimage.median_filter)
    Rf = df(R, size=(1, 7))
    mfcc = librosa.util.sync(librosa.feature.mfcc(y=y, sr=sr), beats)
    path_dist = np.sum(np.diff(mfcc, axis=1) ** 2, axis=0)
    sigma = np.median(path_dist) or 1.0
    path_sim = np.exp(-path_dist / sigma)
    R_path = np.diag(path_sim, k=1) + np.diag(path_sim, k=-1)
    A = 0.5 * Rf + 0.5 * R_path
    L = scipy.sparse.csgraph.laplacian(A, normed=True)
    evals, evecs = scipy.linalg.eigh(L)
    X = evecs[:, :k] / (np.linalg.norm(evecs[:, :k], axis=1, keepdims=True) + 1e-9)
    labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(X)
    bounds = np.concatenate([[0], 1 + np.flatnonzero(labels[:-1] != labels[1:])])
    beat_times = librosa.frames_to_time(beats, sr=sr)
    out = []
    for i, bb in enumerate(bounds):
        start = float(beat_times[min(bb, len(beat_times) - 1)]) if bb > 0 else 0.0
        end = (float(beat_times[min(bounds[i + 1], len(beat_times) - 1)])
               if i + 1 < len(bounds) else duration)
        out.append((start, end, int(labels[min(bb, len(labels) - 1)])))
    return out


def merge_small(segs):
    """짧은 조각을 앞 구간에 병합 + 인접 동일 그룹 병합 — 스파이크 실증 노이즈 제거."""
    out = []
    for s, e, g in segs:
        if out and (e - s < MIN_SEG_SEC or out[-1][2] == g):
            out[-1] = (out[-1][0], e, out[-1][2])
        else:
            out.append((s, e, g))
    # 첫 조각이 짧으면 다음에 흡수
    if len(out) >= 2 and out[0][1] - out[0][0] < MIN_SEG_SEC:
        out[1] = (out[0][0], out[1][1], out[1][2])
        out.pop(0)
    return out


def vocal_presence(vocals_path, segs):
    """보컬 유무 힌트 — 구간 평균 RMS 가 곡의 활성 보컬 기준의 15% 미만이면 없음(간주 등)."""
    import librosa
    import soundfile as sf
    y, sr = sf.read(vocals_path, dtype="float32", always_2d=True)
    y = y.mean(axis=1)
    hop = 2048
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    ref = float(np.percentile(rms, 90)) or 1e-9
    out = []
    for s, e, g in segs:
        a, b = int(s * sr / hop), max(int(s * sr / hop) + 1, int(e * sr / hop))
        seg_rms = float(np.mean(rms[a:min(b, len(rms))])) if a < len(rms) else 0.0
        out.append((s, e, g, seg_rms > ref * 0.15))
    return out


def main():
    audio_path, vocals_path, out_json = sys.argv[1], sys.argv[2], sys.argv[3]
    import librosa
    print("PROG 5", flush=True)
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    duration = len(y) / sr
    print("PROG 20", flush=True)
    segs = merge_small(detect_sections(y, sr, duration))
    print("PROG 80", flush=True)
    segs4 = vocal_presence(vocals_path, segs)
    sections = [
        {"s": round(s, 2), "e": round(e, 2), "group": g,
         "name": f"구간 {i + 1}", "has_vocal": bool(v)}
        for i, (s, e, g, v) in enumerate(segs4)
    ]
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"sections": sections}, f, ensure_ascii=False)
    print("PROG 100", flush=True)


if __name__ == "__main__":
    main()
