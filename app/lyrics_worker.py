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


def clean_segments(segments):
    """환각·잡음 세그먼트 필터 — 지나치게 긴 것, 무음일 확률 높은 것, 빈 텍스트.
    단어별 시각(words)도 보존 — 화면이 가사를 '실제 박자 위치'의 마디에 배치(사용자 지시 2026-07-10)."""
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if len(text) < MIN_CHARS:
            continue
        if (s.end - s.start) > MAX_SEG_SEC:
            continue
        if getattr(s, "no_speech_prob", 0.0) > 0.8:
            continue
        words = [{"w": (w.word or "").strip(), "s": round(float(w.start), 2),
                  "e": round(float(w.end), 2)}
                 for w in (s.words or []) if (w.word or "").strip()]
        out.append({"s": round(float(s.start), 2), "e": round(float(s.end), 2),
                    "text": text, "words": words})
    return out


def main():
    vocals, out_json = sys.argv[1], sys.argv[2]
    print("PROG 5", flush=True)
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    print("PROG 20", flush=True)
    audio = load_16k_mono(vocals)
    segments, info = model.transcribe(audio, vad_filter=True, beam_size=5,
                                      word_timestamps=True)  # 언어 자동 감지 + 단어별 시각
    segs = clean_segments(segments)
    print("PROG 95", flush=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"language": info.language, "segments": segs}, f, ensure_ascii=False)
    print("PROG 100", flush=True)


if __name__ == "__main__":
    main()
