# -*- coding: utf-8 -*-
"""가사 받아쓰기 워커 — 분리된 보컬 스템에 faster-whisper(small·int8·CPU).
서브프로세스로 실행(서버와 격리): python -m app.lyrics_worker <vocals.wav> <out.json>

SP-5 실측(2026-07-10) 근거:
- 보컬 스템 입력이 결정적(원본 믹스는 소절 탈락) — 우리는 스템을 이미 갖고 있다.
- small(int8) RTF 0.09 — 412초 곡 36초. medium 은 3배 느린데 품질 우위 없음.
- 간주(무가사) 구간에서 수십 초짜리 환각 세그먼트 발생 → 길이·무음확률 필터 필수.
- PyAV(FFmpeg 동봉) 디코딩을 피하려고 soundfile 로 직접 읽어 numpy 로 넣는다(라이선스 게이트 메모).
모델 가중치(MIT)는 첫 실행에 HF 에서 다운로드(동봉 금지 정책 부합)."""
import json
import sys

import numpy as np
import soundfile as sf

MAX_SEG_SEC = 20.0   # 이보다 긴 세그먼트 = 간주 환각(실증 65초)
MIN_CHARS = 1


def load_16k_mono(path):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr == 16000:
        return x
    idx = np.linspace(0, len(x) - 1, int(len(x) * 16000 / sr)).astype(np.float64)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


SPLIT_SEC = 8.0      # 이보다 긴 세그먼트는 단어 경계로 쪼갠다(레가토 절을 whisper 가 뭉치는 것 교정)
SPLIT_TARGET = 5.5   # 쪼갤 때 한 조각 목표 길이(캐시 골격이 ~5s 라 그에 맞춤 — 한 줄 단위)


def _split_long(seg):
    """긴 세그먼트를 단어 시각 기준 ~SPLIT_TARGET 초 조각으로 쪼갠다. words 없으면 원본 그대로.
    (whisper 가 런마다 절을 5s 로 쪼개기도·13s 로 뭉치기도 해 골격 밀도가 들쭉날쭉 → 여기서 균일화.
     골격이 촘촘해야 공식 가사가 줄마다 정확히 얹히고, 절 틈에 애드립 ♪ 가 새지 않는다.)"""
    w = seg.get("words") or []
    if len(w) < 4:
        return [seg]
    pieces, cur = [], []
    for wd in w:
        if cur and (wd["e"] - cur[0]["s"] > SPLIT_TARGET) and len(cur) >= 2:
            pieces.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        if len(cur) == 1 and pieces:  # 마지막 1단어는 앞 조각에 붙임
            pieces[-1].append(cur[0])
        else:
            pieces.append(cur)
    return [{"s": round(p[0]["s"], 2), "e": round(p[-1]["e"], 2),
             "text": " ".join(x["w"] for x in p), "words": p} for p in pieces]


def clean_segments(segments):
    """환각·잡음 세그먼트 필터 — 무음일 확률 높은 것, 빈 텍스트. 초장문은 버리되(단어시각 없으면 환각),
    단어시각이 있으면 쪼개서 살린다. 단어별 시각(words)도 보존 — 화면이 가사를 '실제 박자 위치'의
    마디에 배치(사용자 지시 2026-07-10)."""
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if len(text) < MIN_CHARS:
            continue
        if getattr(s, "no_speech_prob", 0.0) > 0.8:
            continue
        words = [{"w": (w.word or "").strip(), "s": round(float(w.start), 2),
                  "e": round(float(w.end), 2)}
                 for w in (s.words or []) if (w.word or "").strip()]
        dur = s.end - s.start
        if dur > MAX_SEG_SEC and len(words) < 4:
            continue  # 단어시각 없는 초장문 = 간주 환각 → 버림
        seg = {"s": round(float(s.start), 2), "e": round(float(s.end), 2),
               "text": text, "words": words}
        out.extend(_split_long(seg) if dur > SPLIT_SEC else [seg])
    return out


def _vocal_gap_spans(segs, audio, sr=16000, min_gap=2.5):
    """보컬 스템 에너지로 '노래는 있는데 받아쓰기 세그먼트가 없는' 구간 (start,end,onsets) 목록을 낸다.
    애드립 채우기(초안·placeholder) 공통 토대. onsets=구간 안 에너지 상승 시각(placeholder 위치)."""
    if len(audio) < sr:
        return []
    hop = int(0.05 * sr)
    n = len(audio) // hop
    if n < 8:
        return []
    e = np.sqrt((audio[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    e = e / (float(e.max()) or 1.0)
    fd = hop / sr
    thr = 0.10
    covered = np.zeros(n, dtype=bool)
    for s in segs:  # 기존 세그먼트 범위 ±0.4s 는 커버로 표시
        a = max(0, int(s["s"] / fd) - 8)
        b = min(n, int((s.get("e", s["s"]) + 0.4) / fd) + 8)
        covered[a:b] = True
    spans, i = [], 0
    while i < n:
        if e[i] > thr and not covered[i]:
            j = i
            while j < n and e[j] > thr * 0.6 and not covered[j]:
                j += 1
            if (j - i) * fd >= min_gap:  # min_gap 이상 '보컬 있는데 미커버' → 애드립 구간
                onsets, last = [], -10.0
                for k in range(i, j):
                    t = k * fd
                    if e[k] > thr and e[max(0, k - 1)] <= thr and t - last >= 2.0:
                        onsets.append(round(t, 2))
                        last = t
                if not onsets:
                    onsets = [round(i * fd, 2)]
                spans.append((i * fd, j * fd, onsets))
            i = j
        else:
            i += 1
    return spans


def merge_adlib(base_segs, extra_segs, audio):
    """VAD-ON 골격(절 타이밍 정확)의 빈 애드립 구간에만 VAD-OFF 초안 텍스트를 끼워 넣는다.
    (사용자 지적 2026-07-17: '원리적 한계 아님 — VAD가 조용한 애드립을 버린 것'. 교차검증으로 확인:
     VAD 끄면 애드립이 초안으로 잡히나 절은 병합/누락 → 절은 VAD-ON, 애드립만 VAD-OFF 로.)
    extra 세그먼트 중 ①골격과 안 겹치고 ②에너지 공백 구간 안인 것만 초안(improv 후보)으로 삽입."""
    spans = _vocal_gap_spans(base_segs, audio)
    if not extra_segs or not spans:
        return base_segs
    covered = [(s["s"], s.get("e", s["s"])) for s in base_segs]
    add = []
    for ex in extra_segs:
        es, ee = ex["s"], ex.get("e", ex["s"])
        if any(es < ce + 1.0 and ee > cs - 1.0 for cs, ce in covered):
            continue  # 골격(절)과 겹침 → 골격 신뢰
        if not any(gs - 0.5 <= es <= ge + 0.5 for gs, ge, _ in spans):
            continue  # 에너지 공백 밖 → 환각 위험, 버림
        add.append({"s": ex["s"], "e": ex.get("e", ex["s"]),
                    "text": ex["text"], "words": ex.get("words", []), "draft": True})
    return sorted(base_segs + add, key=lambda s: s["s"]) if add else base_segs


def fill_vocal_gaps(segs, audio, sr=16000):
    """받아쓰기(초안 포함)가 여전히 못 채운 '보컬 있는 구간'에 placeholder(♪) 삽입 — 골격이 애드립까지
    덮게 한다. 텍스트는 사용자가 들으며 직접 입력(overlay 에선 공식에 안 맞아 즉흥으로 남고, ♪ 로 보인다).
    (긴 세그먼트는 clean_segments 가 이미 단어 경계로 쪼개 골격이 촘촘하므로, 절 틈에 ♪ 가 새지 않는다.)"""
    spans = _vocal_gap_spans(segs, audio, sr=sr)
    if not spans:
        return segs
    added = []
    for gs, ge, onsets in spans:
        for t in onsets:
            added.append({"s": round(t, 2), "e": round(min(t + 2.5, ge), 2),
                          "text": "♪", "placeholder": True, "words": []})
    return sorted(segs + added, key=lambda s: s["s"]) if added else segs


def main():
    vocals, out_json = sys.argv[1], sys.argv[2]
    print("PROG 5", flush=True)
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    print("PROG 20", flush=True)
    audio = load_16k_mono(vocals)
    segments, info = model.transcribe(audio, vad_filter=True, beam_size=5,
                                      word_timestamps=True)  # 절 골격 — VAD 로 환각 억제, 타이밍 정확
    segs = clean_segments(segments)
    # 애드립 등 골격이 빈 '보컬 있는' 구간이 있으면, VAD 꺼 초안 텍스트를 뽑아 그 구간만 채운다(교차검증).
    if _vocal_gap_spans(segs, audio):
        print("PROG 60", flush=True)
        seg2, _ = model.transcribe(audio, vad_filter=False, beam_size=5, word_timestamps=True)
        segs = merge_adlib(segs, clean_segments(seg2), audio)
    segs = fill_vocal_gaps(segs, audio)  # 그래도 남은 공백엔 에너지 기반 ♪(직접 입력 안내)
    print("PROG 95", flush=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"language": info.language, "segments": segs}, f, ensure_ascii=False)
    print("PROG 100", flush=True)


if __name__ == "__main__":
    main()
