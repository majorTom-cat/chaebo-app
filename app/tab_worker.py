"""타브 초안 파이프라인 — 서버와 격리된 서브프로세스로 실행 (CREPE CPU 수 분).

사용: python -m app.tab_worker <bass_wav> <drums_wav> <out_json>
stdout 에 "PROG <0-100>" 라인으로 진행률 보고, 결과는 out_json 에 저장.

파라미터는 SP-4 실측 검증값(fmin>=32.7Hz 마스킹 함정 — spike-results.md).
결과 노트: {start, dur, midi, conf, string, fret} — start/dur 는 16분 그리드 정량화(초).
"""
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

# 4현 베이스 표준 튜닝 개방현 MIDI (E1 A1 D2 G2)
OPEN_STRINGS = [28, 33, 38, 43]
MAX_FRET = 15
HOP_SEC = 0.01
PERIODICITY_TH = 0.6

# 검출 감도(사용자 요청 2026-07-10: 벧엘 류 밀집 믹스 — 분리 블리드·필인까지 받아적어 타브 과밀).
# simple = 문턱 강화: basic-pitch 공식 파라미터(onset/frame/minimum_note_length) + 무음 게이트 강화
SENS = {"mode": "normal", "bp_onset": 0.4, "bp_frame": 0.3, "bp_minlen": 70, "gate_db": 30.0}


def apply_sensitivity(mode):
    if mode == "simple":
        SENS.update(mode="simple", bp_onset=0.65, bp_frame=0.5, bp_minlen=120, gate_db=22.0)
PERIODICITY_LOW = 0.35  # 음량 강한 프레임의 완화 문턱 — 실증: 곡6 C# 8분음(가장 큰 음량)이 per 0.4 대로 전멸
GAP_FRAMES = 8
PITCH_JUMP = 0.6
MIN_NOTE_FRAMES = 6


def prog(p):
    print(f"PROG {int(p)}", flush=True)


def load_mono(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x, sr


def track_pitch(x, sr):
    import torch
    import torchcrepe

    hop = int(sr * HOP_SEC)
    # GPU 있으면 GPU 로 — torchcrepe 가 입력을 내부에서 device 로 옮긴다. (분리는 이미 GPU 적응형.)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # 모델 크기: 'full'(22M 파라미터)은 CPU 에서 너무 느림(실측 30초 베이스에 68초 = 5.5분 곡 ~13분+,
    # 사용자 지적 2026-07-14: Pretender 채보 너무 오래·하드웨어 과다). 베이스는 저음·단선율이라 CREPE
    # 'tiny'(0.5M)로도 품질 거의 동일 — 실측 A/B(곡6 베이스 30초): tiny 가 full 대비 음정 반음이내 94.5%·
    # 중앙값 0.14반음, 속도 19.9배(3.4s). 프렛(반음 간격)·16분 그리드 초안엔 충분. 기본 tiny, 필요시
    # CHAEBO_CREPE_MODEL=full 로 되돌림.
    model = os.environ.get("CHAEBO_CREPE_MODEL", "tiny")
    f0, per = torchcrepe.predict(
        torch.from_numpy(x).unsqueeze(0), sr, hop_length=hop,
        fmin=35.0, fmax=400.0, model=model, batch_size=512,
        device=dev, return_periodicity=True,
    )
    # GPU 텐서는 .numpy() 직접 불가 → .cpu() 먼저(안 하면 GPU 에서 크래시).
    return f0.squeeze(0).cpu().numpy(), per.squeeze(0).cpu().numpy()


def frame_rms_db(x, sr, n_frames):
    """CREPE 프레임과 같은 격자의 RMS(dB) — 주기성-음량 증거 융합용."""
    hop = int(sr * HOP_SEC)
    need = n_frames * hop
    xx = np.pad(x, (0, max(0, need - len(x))))[:need]
    r = np.sqrt((xx.reshape(n_frames, hop) ** 2).mean(axis=1))
    return 20 * np.log10(np.maximum(r, 1e-9))


def segment_notes(f0, per, frame_db=None):
    """SP-4 실곡 검증 로직 — 유성 연속 + 피치 점프 분할. 반환: (notes, tune_cents).
    유성 판정: 주기성 단독이 아니라 음량과 융합 — CREPE 는 저음 스타카토에서 음이 맞아도 주기성을
    낮게 줄 때가 있다(실증: 곡6 C#2 8분음 전멸, per 0.4·RMS 최강). 강한 프레임은 문턱 완화."""
    midi_track = 69 + 12 * np.log2(np.maximum(f0, 1e-6) / 440.0)
    voiced = per > PERIODICITY_TH
    if frame_db is not None and voiced.any():
        strong = frame_db > (float(np.median(frame_db[voiced])) - 12.0)
        voiced = voiced | ((per > PERIODICITY_LOW) & strong)
    # 전역 튜닝 오프셋 — 유튜브 업로드가 표준 피치에서 벗어난 경우(실증: 곡4 -29센트, 저작권 회피성
    # 피치 시프트 추정) 반음 반올림이 경계에서 뒤집힌다. 유성 프레임 소수부 중앙값을 빼고 반올림.
    tune = 0.0
    if voiced.any():
        frac = midi_track[voiced] - np.round(midi_track[voiced])
        tune = float(np.median(frac))
    notes = []
    i, n = 0, len(f0)
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        j, gap, seg = i, 0, [midi_track[i]]
        while j + 1 < n:
            j += 1
            if voiced[j]:
                med = np.median(seg[-20:])
                if abs(midi_track[j] - med) > PITCH_JUMP + 0.4:
                    break
                seg.append(midi_track[j])
                gap = 0
            else:
                gap += 1
                if gap >= GAP_FRAMES:
                    break
        end = j - gap
        if end - i >= MIN_NOTE_FRAMES:
            vseg = midi_track[i:end][voiced[i:end]]
            notes.append({
                "start": i * HOP_SEC,
                "dur": (end - i) * HOP_SEC,
                "midi": int(round(float(np.median(vseg)) - tune)),
                "conf": round(float(per[i:end][voiced[i:end]].mean()), 2),
            })
        i = max(j, i + 1)
    return notes, round(tune * 100, 1)


def gate_quiet(notes, x, sr):
    """무음 위 유령 검출 제거 — CREPE 주기성은 음량과 무관해 노이즈 플로어에서도 확신 0.8이 나온다
    (실증: 곡4 t=0.05s 의 -111dBFS 'F3' — 사용자가 잡음으로 지목, 1박 기준까지 오염).
    노트 구간 RMS 가 전체 활동 중앙값보다 30dB 낮으면 삭제."""
    if not notes:
        return notes

    def note_db(nt):
        seg = x[int(nt["start"] * sr):int((nt["start"] + nt["dur"]) * sr)]
        if not len(seg):
            return -120.0
        return 20 * float(np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9)))

    dbs = [note_db(n) for n in notes]
    gate = float(np.median(dbs)) - SENS["gate_db"]
    return [n for n, d in zip(notes, dbs) if d >= gate]


def detect_onsets(x, sr):
    """진폭 어택 시각 — CREPE 와 독립인 재타건 신호(백트래킹으로 어택 시작점)."""
    import librosa

    y = librosa.resample(x, orig_sr=sr, target_sr=22050) if sr != 22050 else x
    return librosa.onset.onset_detect(y=y, sr=22050, backtrack=True, units="time")


def align_to_onsets(notes, onsets, tol=0.08):
    """CREPE 유성 시작은 실제 어택보다 수십 ms 늦다(저음은 주기 안정까지 시간 소요 — 실증: 곡6
    8분음 반복의 30% 누락·홀수 칸 유령). ①노트 시작을 ±tol 내 가장 가까운 진폭 어택에 스냅
    ②노트 내부 어택 = 같은 음 재타건 → 분할."""
    onsets = np.asarray(onsets, dtype=float)
    if not len(onsets) or not notes:
        return notes
    out = []
    for nt in notes:
        s = nt["start"]
        e = s + nt["dur"]
        near = onsets[(onsets >= s - tol) & (onsets <= s + tol)]
        if len(near):
            s = float(near[np.argmin(np.abs(near - s))])
        inner = onsets[(onsets > s + 0.09) & (onsets < e - 0.05)]
        bounds = [s] + [float(t) for t in inner] + [e]
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b - a >= 0.05:
                out.append({**nt, "start": round(a, 3), "dur": round(b - a, 3)})
    return out


# 파형(진폭 envelope) 기준 정제 문턱 — env 로 조절 가능(사용자 귀 검증 후 튜닝).
REFINE_RISE = float(os.environ.get("CHAEBO_REFINE_RISE", "0.10"))   # 재타건 판정: envelope 상승폭
REFINE_LVL = float(os.environ.get("CHAEBO_REFINE_LVL", "0.06"))     # 그 구간 최소 음량(정규화 0~1)
REFINE_MINKEEP = float(os.environ.get("CHAEBO_REFINE_MINKEEP", "0.12"))  # 어택 없이도 살릴 최소 길이(초)
REFINE_SNAP = float(os.environ.get("CHAEBO_REFINE_SNAP", "0.05"))  # 음 시작을 파형 어택에 스냅 허용(초). 0=끔
# 같은음 연속 병합 판정(둥둥 과분절 완화, 2026-07-15) — 경계에서 진짜 다시 뜯음이면 유지, 지속이면 병합.
# ★핵심: 다시 뜯으려면 이전 음이 감쇠해 envelope 이 골(valley)로 꺼져야 한다. 지속음(둥~)은 골 없이
#   계속 높게 유지된다. 그래서 '경계 골이 두 음 peak 의 REFINE_VALLEY 미만으로 꺼졌나'로 판정하면,
#   빠른 진짜 반복(감쇠 골 있음)은 보존하고 안 꺼진 지속만 병합한다(실음 손실 방지 — 실증 우선).
#   옛 has_attack(단순 상승>문턱)은 align 이 지속음 안에 찍은 유령 어택에도 걸려 병합을 놓쳤다.
REFINE_VALLEY = float(os.environ.get("CHAEBO_REFINE_VALLEY", "0.8"))  # 경계 골/peak 비. ★높일수록 병합 보수적
# (진짜 반복음 보존↑) — 0.5 는 실음까지 삼켜(실증 곡14: B1/F1/G1 진짜 음 병합 소실, 사용자 지적 2026-07-15)
# 0.8 로 상향: 골이 peak 의 80% 밑으로 꺼진(=명백히 감쇠) 경우만 다시 뜯음 유지, 그 이상만 지속 병합.
REFINE_MERGE = os.environ.get("CHAEBO_REFINE_MERGE", "1") == "1"  # 0=옛 병합(has_attack)으로 되돌림


def _bass_envelope(x, sr, hop_ms=8):
    """8ms RMS envelope(정규화 0~1) — 표시 파형과 같은 진폭 근거. 반환:(env, 프레임초)."""
    hop = max(1, int(sr * hop_ms / 1000))
    n = len(x) // hop
    if n < 2:
        return np.array([0.0]), 1.0
    e = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    return e / (float(e.max()) or 1.0), hop / sr


def _attack_times(e, fd):
    """envelope 급상승 시작 = 어택(재타건) 시각들. 표시 파형과 같은 근거라 음을 여기 스냅하면
    음·그리드·파형이 정렬된다(사용자 지적 2026-07-14: 파형 어택과 타브가 진행바와 안 맞음)."""
    if len(e) < 3:
        return np.array([])
    d = np.diff(e)
    idx = np.where((d > REFINE_RISE) & (e[:-1] > REFINE_LVL))[0]
    out, prev = [], -10
    for fi in idx:
        if fi - prev > 3:   # 24ms 이상 떨어진 새 어택만(연속 상승 프레임은 첫 것만)
            out.append(float(fi) * fd)
        prev = fi
    return np.array(out)


def _is_reattack(ns_start, e, fd):
    """두 같은음 사이가 '진짜 다시 뜯음(둥둥)'인지 '이어진 지속(둥~)'인지 — 경계 부근 envelope 골로 판정.
    다시 뜯으려면 이전 음이 감쇠해 골로 꺼졌다 솟는다(골<peak·REFINE_VALLEY). 지속은 안 꺼지고 높게 유지
    (사용자 지적 2026-07-15: 연음이 둥둥으로 쪼개짐). 안 꺼졌으면 지속으로 병합."""
    ib = int(ns_start / fd); w = max(1, int(0.10 / fd))
    a = e[max(0, ib - w):ib + 1]; b = e[ib:min(len(e), ib + w)]
    if len(a) < 1 or len(b) < 1:
        return True   # 판정 불가 시 보수적으로 분리 유지(음 손실 방지 우선)
    peak = max(float(a.max()), float(b.max()))
    if peak < REFINE_LVL:
        return False  # 둘 다 조용 → 유령 지속으로 병합
    trough = float(e[max(0, ib - 2):ib + 3].min())
    return trough < peak * REFINE_VALLEY   # 골이 깊으면(감쇠) 다시 뜯음 → 분리 유지


def _merge_same_pitch(notes, reattack):
    """같은음 연속을 지속이면 병합, 다시 뜯음이면 유지. reattack(gs, ns_start)->bool(True=분리 유지)."""
    ns = sorted(notes, key=lambda nt: nt["start"])
    merged = []
    for nt in ns:
        if merged and merged[-1]["midi"] == nt["midi"]:
            gs = merged[-1]["start"] + merged[-1]["dur"]
            if not reattack(gs, nt["start"]):   # 사이에 재타건 없음 → 지속으로 연장
                merged[-1]["dur"] = round(nt["start"] + nt["dur"] - merged[-1]["start"], 3)
                continue
        merged.append(dict(nt))
    return merged


def merge_sustained(notes, x, sr):
    """저역 음정 교정 뒤 재병합(사용자 지적 2026-07-15: 한 번 친 걸 둥둥 둥으로 쪼갬) — 교정으로 같은음이
    된 인접 조각(C↔C# 흔들리다 둘 다 C 등)을 refine 과 같은 valley 기준으로 지속(둥~)으로 합친다."""
    if not REFINE_MERGE or not notes:
        return notes
    e, fd = _bass_envelope(x, sr)
    return _merge_same_pitch(notes, lambda gs, ns: _is_reattack(ns, e, fd))


def refine_with_envelope(notes, x, sr):
    """파형 어택(재타건) 기준 정제(사용자 요청 2026-07-14): ①어택 없이 이어지는 같은 음 = 지속(둥~)
    으로 병합(반복 둥둥둥은 사이 어택이 있어 안 병합) ②어택도 없고 짧은 유령음 제거(실제 안 친 미세음).
    파형에 맞춰 — 진폭 envelope 의 상승(diff)>RISE & 음량>LVL 을 '어택'으로 본다."""
    if not notes:
        return notes
    e, fd = _bass_envelope(x, sr)

    def has_attack(t0, t1):
        i0 = max(0, int(t0 / fd)); i1 = min(len(e), int(t1 / fd))
        if i1 - i0 < 2:
            return False
        seg = e[i0:i1]
        return bool(np.max(np.diff(seg)) > REFINE_RISE and seg.max() > REFINE_LVL)

    reattack = ((lambda gs, ns: _is_reattack(ns, e, fd)) if REFINE_MERGE
                else (lambda gs, ns: has_attack(gs - 0.02, ns + 0.03)))
    merged = _merge_same_pitch(notes, reattack)
    # 유령 제거(drop)는 기본 끔(2026-07-14): 실제 음 손실(파형 있는데 타브에 안 그려짐 — 사용자 지적)
    # 방지 우선. 조용한 유령은 gate_quiet 가 이미 처리한다. 정말 과밀하면 CHAEBO_REFINE_DROP=1 로 켠다.
    if os.environ.get("CHAEBO_REFINE_DROP") == "1":
        result = [n for n in merged
                  if n["dur"] >= REFINE_MINKEEP or has_attack(n["start"] - 0.03, n["start"] + 0.05)]
    else:
        result = list(merged)
    # 음 시작을 가장 가까운 파형 어택에 스냅(±SNAP) — 음/그리드/파형/진행바 정렬. 끝은 유지(길이 보정).
    if REFINE_SNAP > 0 and result:
        atk = _attack_times(e, fd)
        if len(atk):
            for nt in result:
                near = float(atk[np.argmin(np.abs(atk - nt["start"]))])
                if abs(near - nt["start"]) <= REFINE_SNAP:
                    end = nt["start"] + nt["dur"]
                    nt["start"] = round(near, 3)
                    nt["dur"] = round(max(0.03, end - near), 3)
    return result


# CREPE fmin=35Hz 아래 저음 사각 — 이 midi 미만은 CREPE 가 못 봐서(주기성 0%) basic-pitch 로 보강한다.
LOW_RECOVER_MIDI = int(os.environ.get("CHAEBO_LOW_RECOVER_MIDI", "45"))  # 베이스 전음역(≤A2). CREPE 가 못 본
# (초저역·짧은 음) bp 검출을 빈 구간에 복구 — 28(E1 아래)만이면 B1/G1/F1 같은 짧은 실음이 CREPE 사각서 소실
# (사용자 지적 2026-07-15: 어택 있는데 안 그려짐). 빈 구간+음량 게이트라 정상곡은 CREPE 가 채워 무영향.


def _autocorr_f0(seg, sr, fmin=28.0, fmax=350.0):
    """FFT 자기상관 기본주파수 + 확신도(정규화 피크). 극저역(30~60Hz)에서 CREPE·basic-pitch 보다
    정확 — 주기 자체를 재므로 배음 혼동이 없다(반옥타브는 같은 음이름이라 무해). 반환 (f0Hz, 0~1)."""
    seg = np.asarray(seg, dtype=float)
    n = len(seg)
    if n < int(sr / fmin) * 2:
        return 0.0, 0.0
    seg = seg - seg.mean()
    if np.sqrt((seg ** 2).mean()) < 1e-4:
        return 0.0, 0.0
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    S = np.fft.rfft(seg, nfft)
    ac = np.fft.irfft(S * np.conj(S), nfft)[:n]
    ac /= (ac[0] + 1e-12)
    lo, hi = int(sr / fmax), min(int(sr / fmin), n - 1)
    if hi <= lo:
        return 0.0, 0.0
    k = lo + int(np.argmax(ac[lo:hi]))
    return sr / k, float(ac[k])


LOWPITCH_MAXMIDI = int(os.environ.get("CHAEBO_LOWPITCH_MAXMIDI", "40"))  # 이 midi 이하만 교정(E2=40 위는 검출기 신뢰)
LOWPITCH_CONF = float(os.environ.get("CHAEBO_LOWPITCH_CONF", "0.6"))     # 자기상관 확신 하한(낮추면 교정↑·오손 위험↑)
LOWPITCH_MAXSHIFT = int(os.environ.get("CHAEBO_LOWPITCH_MAXSHIFT", "2"))  # 반음 교정 최대 폭 — 실패모드는 반음권
LOWPITCH_ON = os.environ.get("CHAEBO_LOWPITCH", "1") == "1"


def refine_low_pitch(notes, x, sr):
    """저역 음정을 자기상관으로 재측정·교정(사용자 지적 2026-07-15: 74마디부터 C를 C#으로 등 반음 오차).
    ★실증(곡14): 곡 전체가 C1(~33Hz) 초저역이라 CREPE 무력·basic-pitch 반음 혼동(C1 33Hz vs C#1 35Hz =
    2Hz 차). 자기상관(주기 기반)은 이 저역서 정확 → midi≤LOWPITCH_MAXMIDI 음을 자기상관 f0 로 교정.
    ★안전장치(정상곡 오손 방지): ①교정 폭 ±LOWPITCH_MAXSHIFT 반음 이내 — 실패모드는 반음권이고, 큰 점프는
    자기상관 옥타브/배음 오검출 의심이라 보류 ②자기상관 확신 하한(약한 피크는 원음 유지). ※검출기 conf 로는
    거르지 않는다 — 곡14 는 CREPE 가 저역을 '확신하며' 틀려(conf 높음) conf 게이트가 오히려 교정을 막았다(실측).
    self-tune: 확신 음들의 센트 편차 중앙값으로 곡 튜닝 보정(반음 경계 뒤집힘 방지)."""
    if not LOWPITCH_ON or not notes:
        return notes
    meas = []
    for i, nt in enumerate(notes):
        if nt["midi"] > LOWPITCH_MAXMIDI:
            continue
        # 분석 창 = 음 길이를 [0.25, 0.4]s 로 클램프 — 짧게 검출된 음도 베이스 울림(다음 음 전까지)에서
        # 피치를 잡게(실증 곡14: 0.05s 로 검출된 C#1 은 창이 짧아 자기상관 0 → 스킵되던 것). 33Hz 는 한 주기
        # 30ms 라 최소 0.25s 면 8주기 확보. 다음 음이 아주 가까우면 창에 섞여 확신이 낮아져 자연히 스킵된다.
        win = max(0.25, min(nt.get("dur", 0.3), 0.4))
        seg = x[int(nt["start"] * sr):int((nt["start"] + win) * sr)]
        f0, conf = _autocorr_f0(seg, sr)
        if f0 > 0 and conf >= LOWPITCH_CONF:
            meas.append((i, 69.0 + 12.0 * np.log2(f0 / 440.0), conf))
    if not meas:
        return notes
    tune = float(np.median([(m - round(m)) for _, m, _ in meas]))  # 반음 단위 튜닝(자체 보정)
    out = [dict(n) for n in notes]
    for i, m, _ in meas:
        target = int(round(m - tune))   # 자기상관이 본 음(옥타브 포함) — 저역이라 검출값보다 옥타브 낮을 수 있다
        old = out[i]["midi"]
        # ★검출 음의 옥타브는 유지하고 '음이름(pitch class)'만 자기상관 값으로 최소 이동(±6 이내).
        #   저역은 진짜 f0 가 옥타브 아래라(C#2 검출 ↔ 실제 C1) 절대 midi 비교는 옥타브차로 막힌다 — 음이름만 본다.
        new = old + ((target - old + 6) % 12) - 6
        if 0 < abs(new - old) <= LOWPITCH_MAXSHIFT:  # 반음권 교정만 — 큰 점프는 보류(오손 방지)
            out[i]["midi"] = new
    return out


def merge_low_notes(primary, bp_notes, x, sr, low_midi=LOW_RECOVER_MIDI):
    """CREPE 가 못 보는 초저음을 basic-pitch 검출로 보강(사용자 지적 2026-07-15: 첫 음 C 누락).
    ★실증(곡14): 첫 음 기본주파수 ~33Hz(C1)을 CREPE(fmin 35Hz)는 주기성 0%로 전멸(full 도 동일),
    bp 는 midi24 로 검출 → assign_frets 가 연주 옥타브로 접어 C2(3번줄 3프렛). 정합점수 근소차(CREPE
    0.745 > bp 0.711)로 CREPE 경로가 채택되면 bp 의 저음이 통째로 버려지던 것. 겹치지 않는 빈 구간만
    메워(CREPE 의 옳은 음은 안 건드림) 초저음을 되살린다. bp 채택 경로면 이미 있어 무효과(멱등).
    조용한 저역 유령은 gate_quiet 와 같은 음량 문턱으로 배제(노이즈 유입 방지)."""
    if not bp_notes:
        return primary

    def note_db(nt):
        seg = x[int(nt["start"] * sr):int((nt["start"] + nt["dur"]) * sr)]
        if not len(seg):
            return -120.0
        return 20 * float(np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9)))

    ref = float(np.median([note_db(n) for n in primary])) if primary else -30.0
    starts = sorted(n["start"] for n in primary)
    added = []
    for n in bp_notes:
        if n["midi"] >= low_midi:
            continue
        if note_db(n) < ref - SENS["gate_db"]:   # gate_quiet 과 동일 문턱 — 조용한 저역 유령 배제
            continue
        s, e = n["start"], n["start"] + n["dur"]
        # primary 에 이 시간대 음이 이미 있으면(겹침) 건너뜀 — CREPE 가 채운 곳은 존중, 빈 구간만 메움
        if any(s - 0.1 <= ps <= e for ps in starts):
            continue
        added.append(dict(n))
    if not added:
        return primary
    return sorted(primary + added, key=lambda nt: nt["start"])


def estimate_tempo(drums_path):
    import librosa

    y, sr = librosa.load(drums_path, sr=22050, mono=True)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    if not np.isfinite(bpm) or bpm <= 0:
        bpm = 100.0  # 무음·비트 검출 실패 폴백 — 초안은 어차피 수동 보정 전제
    # 베이스 연습 상식 범위로 폴딩 (60~200)
    while bpm < 60:
        bpm *= 2
    while bpm > 200:
        bpm /= 2
    beat_times = librosa.frames_to_time(beats, sr=sr)
    return round(bpm, 1), beat_times


def refine_grid(raw_notes, bpm0, beat_times=None):
    """librosa 대략 템포(±수 % 오차)를 원시 어택으로 정제 — 2% 만 틀려도 20초 뒤 그리드가 한 칸 이상
    밀려 어택이 e/a 칸에 무작위로 떨어진다(실증: 곡4 112.3→110.42, 잔차 중앙값 35.5→18ms, ±30ms 정합 42→72%).
    ①bpm ±6% × 위상(원형 평균) 탐색으로 어택-그리드 잔차 중앙값 최소화.
    ②박 위상: 16분 4칸 중 어느 칸이 '박'인지는 드럼 비트 시각과의 정렬로 결정(실증: 곡4 는
      베이스가 박 직전 픽업으로 들어와 첫-어택=박 가정이 한 칸 어긋남 → 어택 최빈이 e/n 으로 보임).
    ③1박 기준 = 첫 어택과 가장 가까운 박 선(첫 음이 픽업이면 직전 박)."""
    if not raw_notes:
        return bpm0, 0.0
    onsets = np.array([n["start"] for n in raw_notes])
    best = None
    for bpm in np.arange(bpm0 * 0.94, bpm0 * 1.06, 0.02):
        grid = 60.0 / bpm / 4
        ph = (onsets % grid) / grid * 2 * np.pi
        mean_ph = float(np.arctan2(np.sin(ph).mean(), np.cos(ph).mean()))
        phase = (mean_ph / (2 * np.pi)) * grid % grid
        residual = np.abs(((onsets - phase + grid / 2) % grid) - grid / 2)
        med = float(np.median(residual))
        if best is None or med < best[0]:
            best = (med, float(bpm), float(phase))
    _, bpm, phase = best
    grid = 60.0 / bpm / 4
    beat_len = grid * 4
    t0 = float(onsets[0])
    if beat_times is not None and len(beat_times):
        bt = np.asarray(beat_times, dtype=float)
        best_k = None
        for kk in range(4):
            d = np.abs(((bt - (phase + kk * grid) + beat_len / 2) % beat_len) - beat_len / 2)
            m = float(np.median(d))
            if best_k is None or m < best_k[0]:
                best_k = (m, kk)
        k = best_k[1]
        # 드럼은 16분 위상까지만 신뢰 — 박/엇박(반박) 은 librosa 가 절반쯤 뒤집는다(실증: 곡6 이
        # 마디 전체가 8분음 하나 늦게 읽힘). 반박 후보 중 ①박 위 어택 질량이 큰 쪽(베이스는 박을
        # 강조하는 게 통례), ②동률(스트레이트 8분 등)이면 첫 어택=박 관례로 판별.
        def _on_beat_ratio(kk):
            d = np.abs(((onsets - (phase + kk * grid) + beat_len / 2) % beat_len) - beat_len / 2)
            return float((d < grid * 0.5).mean())

        def _beat_dist(kk):
            return abs(((t0 - (phase + kk * grid) + beat_len / 2) % beat_len) - beat_len / 2)

        kf = (k + 2) % 4
        mk, mf = _on_beat_ratio(k), _on_beat_ratio(kf)
        if mf > mk + 0.1 or (abs(mf - mk) <= 0.1 and _beat_dist(kf) < _beat_dist(k)):
            k = kf
    else:
        k = round((t0 - phase) / grid) % 4  # 드럼 정보 없음 — 기존 관례(첫 어택=박) 유지
    beat0 = phase + k * grid
    offset = beat0 + round((t0 - beat0) / beat_len) * beat_len
    while offset - t0 > grid / 2 + 1e-9:  # 첫 음이 픽업(박 직전)이면 직전 박을 1박으로
        offset -= beat_len
    while offset < -grid / 2:
        offset += beat_len
    return round(bpm, 2), round(max(offset, 0.0), 4)


def measure_tune_cents(x, sr):
    """전역 디튠(센트) — 유튜브 피치시프트 업로드 대응(실증: 곡4 -29c). 15초 발췌 pyin."""
    import librosa

    seg = x[5 * sr:20 * sr] if len(x) > 20 * sr else x
    y = librosa.resample(seg, orig_sr=sr, target_sr=22050)
    f0, _, _ = librosa.pyin(y, fmin=30, fmax=350, sr=22050)
    f0 = f0[np.isfinite(f0)]
    if len(f0) < 30:
        return 0.0
    midi = 69 + 12 * np.log2(f0 / 440.0)
    return float(np.median((midi - np.round(midi)) * 100))


def mono_reduce(notes, sim=0.08):
    """베이스=단선율 정리(SP-4b): ①동시 시작(80ms) 묶음에서 최저 실음(세기 40% 룰)
    ②양옆과 옥타브 관계인 짧은 유령 접기 ③같은 음 조각 병합."""
    notes = sorted(notes, key=lambda n: n["start"])
    if not notes:
        return notes
    out = []
    i = 0
    while i < len(notes):
        grp = [notes[i]]
        j = i + 1
        while j < len(notes) and notes[j]["start"] - notes[i]["start"] < sim:
            grp.append(notes[j])
            j += 1
        amax = max(g["conf"] for g in grp)
        cand = sorted(grp, key=lambda g: g["midi"])
        out.append(next((g for g in cand if g["conf"] >= 0.4 * amax), cand[0]))
        i = j
    for k in range(len(out)):
        prev_m = out[k - 1]["midi"] if k else None
        next_m = out[k + 1]["midi"] if k + 1 < len(out) else None
        m = out[k]["midi"]
        for ref in (prev_m, next_m):
            if ref and (m - ref) % 12 == 0 and m != ref and out[k]["dur"] < 0.4:
                out[k] = {**out[k], "midi": ref}
                break
    merged = [out[0]]
    for n in out[1:]:
        last = merged[-1]
        if n["midi"] == last["midi"] and n["start"] - (last["start"] + last["dur"]) < 0.06 \
                and n["start"] - last["start"] < 0.2:
            last["dur"] = round(n["start"] + n["dur"] - last["start"], 3)
        else:
            merged.append(n)
    return merged


def detect_notes_bp(x, sr, work_dir):
    """basic-pitch(Apache-2.0) 검출 — CREPE 대비 12~20배 빠르고 리듬 선명한 곡에서 우세
    (SP-4b 실측: 곡6 8분정합 92% vs 73%). 디튠 정규화(리샘플) 후 시각 역보정.
    반환: (raw_notes, tune_cents)."""
    import librosa
    import soundfile as _sf

    tune_c = measure_tune_cents(x, sr)
    f = 1.0
    src = x
    if abs(tune_c) > 8:  # bp 는 반음 정수 반올림이라 디튠에 취약 — 표준 피치로 정규화
        f = 2 ** (-tune_c / 1200)
        src = librosa.resample(x, orig_sr=sr, target_sr=int(round(sr / f)))
    tmp = Path(work_dir) / "_bp_norm.wav"
    _sf.write(str(tmp), src, sr)
    # ONNX 백엔드 명시 — basic-pitch 기본은 tensorflow(설치 시 ~1.1GB)지만, 동봉 onnx 모델 +
    # onnxruntime 으로 동일 결과(실측: 30 notes 동일, tf 차단해도 동작). tensorflow 미설치로 경량화.
    from basic_pitch import build_icassp_2022_model_path, FilenameSuffix
    from basic_pitch.inference import predict
    _onnx_model = build_icassp_2022_model_path(FilenameSuffix.onnx)
    _, _, note_events = predict(
        str(tmp), model_or_model_path=str(_onnx_model),
        onset_threshold=SENS["bp_onset"], frame_threshold=SENS["bp_frame"],
        minimum_note_length=SENS["bp_minlen"],
        minimum_frequency=30.0, maximum_frequency=400.0, melodia_trick=True)
    tmp.unlink(missing_ok=True)
    raw = [{"start": round(float(s) * f, 3), "dur": round(float(e - s) * f, 3),
            "midi": int(round(p)), "conf": round(float(a), 2)}
           for (s, e, p, a, _b) in note_events]
    return mono_reduce(raw), round(tune_c, 1)


def eighth_ratio(raw_notes, beat_times):
    """검출기 품질 점수 = 정량화 후 8분 위치(짝수 칸) 어택 비율 — 같은 곡 안에서의 상대 비교용.
    (잔차 지표는 어택 스냅 후 두 검출기가 같은 값으로 수렴해 판별 불가 — 보정 실측로 기각.
    16분 위주 곡은 둘 다 낮게 나와 상대 비교는 여전히 유효.)"""
    if not raw_notes or beat_times is None or len(beat_times) < 8:
        return 0.0
    ons = np.array([n["start"] for n in raw_notes])
    slots = build_slot_times(beat_times, ons)
    if slots is None:
        return 0.0
    q = quantize_dynamic(raw_notes, slots)
    if len(q) < 16:
        return 0.0
    return float(sum(1 for n in q if n["gi"] % 2 == 0) / len(q))


def detect_meter(beat_times, raw_notes):
    """콤파운드 미터(12/8·셔플) 감지 — 비트 추적기는 이런 곡에서 셋잇단 펄스에 잠금(실증: 곡7
    172bpm=57×3). 펄스가 빠르고(≥140) 어택 강세가 3묶음 주기로 몰리면 12/8. 스파이크 3/3 정답
    (곡4 0.015/0.103·곡6 0.006/0.022 → 4/4, 곡7 0.141/0.037 → 12/8)."""
    if beat_times is None or len(beat_times) < 12 or not raw_notes:
        return "4/4"
    bt = np.asarray(beat_times, dtype=float)
    onsets = np.array([n["start"] for n in raw_notes])
    onsets = onsets[(onsets >= bt[0]) & (onsets <= bt[-1])]
    med = float(np.median(np.diff(bt)))
    if not len(onsets) or med <= 0 or 60.0 / med < 140:
        return "4/4"
    idx = np.clip(np.searchsorted(bt, onsets), 1, len(bt) - 1)
    idx = np.where(np.abs(onsets - bt[idx - 1]) <= np.abs(onsets - bt[idx]), idx - 1, idx)
    ks = idx[np.abs(onsets - bt[idx]) < med * 0.25]
    if len(ks) < 24:
        return "4/4"

    def group_score(G):
        best = -1.0
        for ph in range(G):
            share = np.bincount((ks - ph) % G, minlength=G) / len(ks)
            best = max(best, float(share.max() - 1.0 / G))
        return best

    return "12/8" if group_score(3) > group_score(4) else "4/4"


def build_slot_times(beat_times, onsets, sub=4):
    """동적 그리드 — 드럼 비트 시각을 그대로 따라가 실연주의 미세 템포 변화를 추종.
    고정 (bpm, offset) 그리드는 사람 연주에서 구간별로 반 칸씩 어긋난다(실증: 곡6 인트로
    40마디 홀수칸 80%, 41마디부터 6%). sub = 박당 분할 수: 4/4 v2 는 12(16분과 셋잇단의
    최소공배수 — Longview 류 부분 셋잇단 표기), 12/8 은 4.
    반환: slots(gi→절대시각, gi 0 = 1마디 1박) 또는 None(비트 부족 — 균일 그리드 폴백)."""
    if beat_times is None or len(beat_times) < 8 or not len(onsets):
        return None
    bt = np.asarray(beat_times, dtype=float)
    med = float(np.median(np.diff(bt)))
    if not np.isfinite(med) or med <= 0:
        return None
    # 정리: 비정상 간격 제거·긴 공백 보간(비트 트래커의 드문 결손 대비)
    clean = [float(bt[0])]
    for t in bt[1:]:
        gap = t - clean[-1]
        if gap < 0.6 * med:
            continue  # 중복/스퍼리어스
        n_fill = int(round(gap / med))
        if gap > 1.5 * med and n_fill >= 2:
            for j in range(1, n_fill):
                clean.append(clean[-1] + gap / n_fill)
        clean.append(float(t))
    bt = np.asarray(clean)
    # 앞뒤 연장 — 첫 어택·마지막 어택을 덮도록 중앙 간격으로 외삽
    t_lo, t_hi = float(onsets.min()) - med, float(onsets.max()) + 2 * med
    pre = np.arange(bt[0] - med, t_lo, -med)[::-1]
    post = bt[-1] + np.arange(1, max(2, int((t_hi - bt[-1]) / med) + 2)) * med
    bt = np.concatenate([pre, bt, post])
    slots = np.concatenate([bt[i] + (bt[i + 1] - bt[i]) * np.arange(sub) / sub
                            for i in range(len(bt) - 1)] + [bt[-1:]])
    half = sub // 2
    # 박/엇박 판별(드럼 비트가 반 박 뒤집히는 librosa 습성 — 곡6 실증):
    # 어택의 최근접 슬롯 j 를 구해, 박(j%sub==0) vs 반박(==half) 어택 질량 비교. 동률은 첫 어택=박.
    j_near = np.clip(np.searchsorted(slots, onsets), 1, len(slots) - 1)
    j_near = np.where(np.abs(onsets - slots[j_near - 1]) <= np.abs(onsets - slots[j_near]),
                      j_near - 1, j_near)
    m0 = float((j_near % sub == 0).mean())
    m2 = float((j_near % sub == half).mean())
    parity = 0
    j0 = int(j_near[0])
    if m2 > m0 + 0.1 or (abs(m2 - m0) <= 0.1 and j0 % sub == half):
        parity = half
    # 1마디 1박 앵커 = 첫 어택과 가장 가까운 '박' 슬롯. 진짜 픽업(반 칸 이상 앞)만 직전 박으로 —
    # 검출 지터(수십 ms 이른 어택)를 픽업 취급하면 마디가 통째로 밀림(실증: 48그리드 곡6 한 박 밀림)
    jb = parity + round((j0 - parity) / sub) * sub
    while jb - j0 > max(1, sub // 8):
        jb -= sub
    jb = max(jb, parity if parity <= j0 else j0)
    return slots[max(0, jb):]


def quantize_dynamic(notes, slots):
    """build_slot_times 그리드에 정량화 — gi/glen 의미는 균일 그리드와 동일.
    약박 관례: 두 슬롯 정중앙 틈의 음(실증: 곡6 3마디 58ms vs 70ms)은 기계적 최근접이 16분
    약박('a')을 골라 악보가 싱커페이션처럼 보임 — 8분 위치가 1.5배 이내로만 멀면 강박 우선
    (진짜 당겨 친 음은 약박에 바짝 붙어 비율이 커서 그대로 남음)."""
    out = []
    for nt in notes:
        j = int(np.clip(np.searchsorted(slots, nt["start"]), 1, len(slots) - 1))
        gi = j - 1 if abs(nt["start"] - slots[j - 1]) <= abs(nt["start"] - slots[j]) else j
        if gi >= len(slots) - 1:
            continue
        if gi % 2 == 1:
            d_odd = abs(nt["start"] - slots[gi])
            evens = [g for g in (gi - 1, gi + 1) if 0 <= g < len(slots) - 1]
            ge = min(evens, key=lambda g: abs(nt["start"] - slots[g]))
            if abs(nt["start"] - slots[ge]) <= d_odd * 1.5:
                gi = ge
        local = slots[gi + 1] - slots[gi] if gi + 1 < len(slots) else slots[-1] - slots[-2]
        glen = max(1, round(nt["dur"] / local))
        out.append({**nt, "start": round(float(slots[gi]), 3), "gi": int(gi), "glen": int(glen),
                    "dur": round(glen * float(local), 3)})
    seen = {}
    for nt in out:
        if nt["gi"] not in seen:
            seen[nt["gi"]] = nt
    res = sorted(seen.values(), key=lambda n: n["gi"])
    for cur, nxt in zip(res, res[1:]):  # 연음 연장 — quantize() 와 동일 규칙
        ioi = nxt["gi"] - cur["gi"]
        if cur["glen"] > ioi:
            cur["glen"] = ioi
        elif cur["glen"] < ioi and cur["glen"] / ioi >= 0.5:
            cur["glen"] = ioi
        end_i = min(cur["gi"] + cur["glen"], len(slots) - 1)
        cur["dur"] = round(float(slots[end_i] - slots[cur["gi"]]), 3)
    return res


# 4/4 v2(박당 12칸 = 16분·셋잇단 최소공배수) 허용 위치: 16분 {0,3,6,9} + 8분 셋잇단 {4,8}
_ALLOWED_48 = (0, 3, 4, 6, 8, 9)


def sanitize_mixed(notes):
    """편집(이동·추가·박자 시작점) 후 48그리드 정합 복구 — 어긋난 오프셋(예: shift 로 셋잇단이
    7·11 에 감)은 조판기가 도달 못 해 음이 증발(적대 리뷰 확정 결함). 박별 가족을 다수결로 정하되
    편집 의도 존중(셋잇단만 있으면 1개여도 T), 전 노트를 가족 허용 오프셋으로 스냅.
    반환: (notes, families)."""
    if not notes:
        return notes, []
    notes = sorted(notes, key=lambda n: n["gi"])
    nb = notes[-1]["gi"] // 12 + 2
    families = ["S"] * nb
    for b in range(nb):
        offs = [n["gi"] % 12 for n in notes if n["gi"] // 12 == b]
        t = sum(1 for o in offs if o in (1, 2, 4, 5, 7, 8, 10, 11) and min(
            abs(o - 4), abs(o - 8)) <= min(abs(o - 0), abs(o - 3), abs(o - 6), abs(o - 9)))
        s = len(offs) - t
        near_t = sum(1 for o in offs if o in (4, 8))
        # 0(정박)은 양 가족 공통 합법 — S 표로 세면 [정박+셋잇단] 박이 S 로 판정돼
        # 사용자가 추가한 셋잇단(4)이 16분(3)으로 튕겨나감(셋잇단 입력 기능의 전제)
        near_s = sum(1 for o in offs if o in (3, 6, 9))
        if near_t > near_s or (near_t > 0 and near_s == 0):
            families[b] = "T"
        elif near_t == 0 and near_s == 0 and t > s:
            families[b] = "T"
    seen = {}
    for n in notes:
        b, o = n["gi"] // 12, n["gi"] % 12
        legal = (0, 4, 8) if families[b] == "T" else (0, 3, 6, 9)
        if o not in legal:
            n["gi"] = b * 12 + min(legal, key=lambda x: abs(x - o))
        # 길이도 가족 단위(16분=3·셋잇단=4)의 배수로 — 단위 밖 길이(glen 1·5 등)는 조판에서
        # 잔여분이 가짜 마이크로 붙임줄/쉼표로 새어 나옴(사용자 실증 2026-07-09: 지운 음이 남아 보임)
        unit = 4 if families[b] == "T" else 3
        n["glen"] = max(unit, int(round(n["glen"] / unit)) * unit)
        if n["gi"] not in seen:
            seen[n["gi"]] = n
    res = sorted(seen.values(), key=lambda n: n["gi"])
    for cur, nxt in zip(res, res[1:]):
        if cur["glen"] > nxt["gi"] - cur["gi"]:
            cur["glen"] = max(1, nxt["gi"] - cur["gi"])
    return res, families


def derive_families(notes):
    """노트에서 박별 가족(S/T) 재유도 — 편집(셋잇단 위치에 추가 등) 후 재조판이 자동 추종."""
    if not notes:
        return []
    nb = notes[-1]["gi"] // 12 + 2
    fams = ["S"] * nb
    for b in range(nb):
        offs = [n["gi"] % 12 for n in notes if n["gi"] // 12 == b]
        t = sum(1 for o in offs if o in (4, 8))
        s = sum(1 for o in offs if o in (3, 9))
        if t > s and t >= 2:  # quantize_mixed 투표와 동일 문턱(잡음 1개로 T 금지)
            fams[b] = "T"
    return fams


def snap_bar_phase(notes, slots, bar_slots):
    """마디 위상 자동 보정 — 박 '간격'은 맞는데 '어느 박이 1박이냐'(마디 위상)가 통째로 틀린 경우 교정.
    실측(2026-07-15): 서브박 위상(gi%12)은 이미 최적인데 마디 다운비트 커버리지는 곡15 43%뿐 —
    통-박(12·24·36칸)만 시프트하면 92%로 급등(격자가 1박을 엉뚱한 박에 붙였던 것). 당김음(서브박)은
    안 건드리고, 어택이 마디 다운비트에 가장 많이 걸리는 통-박 위상을 1박으로. delta 슬롯을 앞에 외삽
    추가 + gi 이동(음 시각 보존·무손실). 반환 (notes, slots, delta_slots) — delta 0=미변경.
    ★수동 override = '박자 시작점 이동'(자동이 틀리게 골랐을 때 사용자가 되돌림)."""
    if not notes or slots is None or len(slots) < 2:
        return notes, slots, 0
    beat = bar_slots // 4
    gis = [n["gi"] for n in notes]
    nb = len({g // bar_slots for g in gis})
    if nb < 4:  # 너무 짧으면 판단 불가
        return notes, slots, 0

    def cov(phi):  # phi 를 1박으로 봤을 때 다운비트에 어택 있는 마디 비율
        return len({(g - phi) // bar_slots for g in gis if (g - phi) % bar_slots == 0}) / nb
    cands = list(range(0, bar_slots, beat))  # 통-박 위상만(0·12·24·36)
    covs = {p: cov(p) for p in cands}
    phi = max(cands, key=lambda p: covs[p])
    # 게이트: 개선폭 충분(+15%p)하고 결과 쓸만(>=55%)일 때만 — 이미 맞은 곡(예: 곡9)은 안 건드림
    if phi == 0 or covs[phi] < covs[0] + 0.15 or covs[phi] < 0.55:
        return notes, slots, 0
    delta = (bar_slots - phi) % bar_slots  # 앞을 채워 phi->0 (무손실). delta 는 박의 배수
    med = (slots[-1] - slots[0]) / (len(slots) - 1)
    pre = [round(slots[0] - (delta - i) * med, 3) for i in range(delta)]
    new_slots = pre + list(slots)
    for n in notes:
        n["gi"] += delta
        if n["gi"] < len(new_slots):
            n["start"] = round(float(new_slots[n["gi"]]), 3)
    return notes, new_slots, delta


def quantize_mixed(notes, slots):
    """박당 12칸(마디 48) 정량화 — 4/4 곡 속 부분 셋잇단(Longview·Come Together 류) 표기.
    ①허용 위치(16분∪셋잇단)로 스냅 ②약박 관례(정중앙 틈 → 8분 우선, 1.5배 규칙)
    ③박별 가족 투표: 같은 박에 16분·셋잇단이 혼재하면 다수 가족으로 통일(사람 채보 관례)
    ④길이는 가족 단위(16분=3칸·셋잇단=4칸)의 배수 + 연음 연장.
    반환: (notes, families)  families[b] ∈ 'S'(스트레이트)|'T'(셋잇단)."""
    out = []
    for nt in notes:
        j = int(np.clip(np.searchsorted(slots, nt["start"]), 1, len(slots) - 1))
        j = j - 1 if abs(nt["start"] - slots[j - 1]) <= abs(nt["start"] - slots[j]) else j
        beat = j // 12
        best = None
        for b in (beat - 1, beat, beat + 1):
            for o in _ALLOWED_48:
                g = b * 12 + o
                if 0 <= g < len(slots) - 1:
                    d = abs(nt["start"] - slots[g])
                    if best is None or d < best[0]:
                        best = (d, g)
        if best is None:
            continue
        gi = best[1]
        if gi % 6 != 0:  # 약박 관례 — 8분 위치가 1.5배 이내면 강박
            b0 = gi // 12
            eighths = [g for g in (b0 * 12, b0 * 12 + 6, (b0 + 1) * 12) if 0 <= g < len(slots) - 1]
            ge = min(eighths, key=lambda g: abs(nt["start"] - slots[g]))
            if abs(nt["start"] - slots[ge]) <= best[0] * 1.5:
                gi = ge
        out.append({**nt, "gi": int(gi)})
    seen = {}
    for nt in out:
        if nt["gi"] not in seen:
            seen[nt["gi"]] = nt
    res = sorted(seen.values(), key=lambda n: n["gi"])
    if not res:
        return [], []
    # 박별 가족 투표 — 혼재 박은 다수 가족으로 재스냅
    n_beats = res[-1]["gi"] // 12 + 2
    families = ["S"] * n_beats
    for b in range(n_beats):
        offs = [n["gi"] % 12 for n in res if n["gi"] // 12 == b]
        t_cnt = sum(1 for o in offs if o in (4, 8))
        s_cnt = sum(1 for o in offs if o in (3, 9))
        # 어택 1개짜리 잡음이 박을 셋잇단으로 만들지 않게(실증: BJ 스트레이트 곡에 tuplet 106개)
        if t_cnt > s_cnt and t_cnt >= 2:
            families[b] = "T"
    for nt in res:
        b, o = nt["gi"] // 12, nt["gi"] % 12
        fam = families[b]
        legal = (0, 4, 8) if fam == "T" else (0, 3, 6, 9)
        if o not in legal:
            o2 = min(legal, key=lambda x: abs(x - o))
            nt["gi"] = b * 12 + o2
    # 재스냅 후 중복 재정리
    seen = {}
    for nt in res:
        if nt["gi"] not in seen:
            seen[nt["gi"]] = nt
    res = sorted(seen.values(), key=lambda n: n["gi"])
    # 길이: 가족 단위 배수로, 연음 연장(다음 어택까지 ≥50% 울림) 포함
    for i, cur in enumerate(res):
        sub_len = float(slots[min(cur["gi"] + 1, len(slots) - 1)] - slots[cur["gi"]]) or 0.04
        step = 4 if families[cur["gi"] // 12] == "T" else 3
        raw_len = max(step, int(round(cur["dur"] / sub_len / step)) * step)
        gap = (res[i + 1]["gi"] - cur["gi"]) if i + 1 < len(res) else raw_len
        if raw_len >= gap * 0.5 or raw_len > gap:
            cur["glen"] = gap if i + 1 < len(res) else raw_len
        else:
            cur["glen"] = min(raw_len, gap)
        cur["glen"] = max(1, int(cur["glen"]))
        end_i = min(cur["gi"] + cur["glen"], len(slots) - 1)
        cur["start"] = round(float(slots[cur["gi"]]), 3)
        cur["dur"] = round(float(slots[end_i] - slots[cur["gi"]]), 3)
    return res, families


def quantize(notes, bpm, offset):
    """16분 그리드 정량화 (REQ-TAB-004 최소 경로 — 트리플렛·스윙은 후속)."""
    grid = 60.0 / bpm / 4  # 16분음표 길이(초)
    out = []
    for nt in notes:
        gi = round((nt["start"] - offset) / grid)
        glen = max(1, round(nt["dur"] / grid))
        start = offset + gi * grid
        if gi < 0:
            continue
        out.append({**nt, "start": round(start, 3), "dur": round(glen * grid, 3),
                    "gi": gi, "glen": glen})
    # 같은 그리드 슬롯 중복 제거(먼저 온 것 우선)
    seen = {}
    for nt in out:
        if nt["gi"] not in seen:
            seen[nt["gi"]] = nt
    res = sorted(seen.values(), key=lambda n: n["gi"])
    # 연음 연장 — 검출 지속시간은 감쇠에서 일찍 끊겨 짧다(실증: 곡6 8분음이 전부 16분+쉼표로 조판
    # → 사용자 "쉼표가 너무 많다"). 다음 어택까지 절반 이상 울렸으면 이어진 것으로 보고 채움.
    # 절반 미만(진짜 끊어 침)만 쉼표 유지.
    for cur, nxt in zip(res, res[1:]):
        ioi = nxt["gi"] - cur["gi"]
        if cur["glen"] > ioi:
            cur["glen"] = ioi  # 겹침 방지
        elif cur["glen"] < ioi and cur["glen"] / ioi >= 0.5:
            cur["glen"] = ioi
        cur["dur"] = round(cur["glen"] * grid, 3)
    return res


def assign_frets(notes):
    """손 포지션 인식 운지 배치(동적 계획) — 그리디(직전 음만 보고 낮은 프렛 선호)는 포지션
    널뛰기·줄 건너뛰기를 만든다(사용자 실증: '치기 어려워 현실적이지 않다'). 곡 전체에서
    손 이동(검지 위치 변화) + 줄 이동 비용의 최소 경로를 찾는다. 개방현은 어느 포지션에서든
    가능(손 이동 0·포지션 유지)."""
    if not notes:
        return notes
    for nt in notes:
        while nt["midi"] < OPEN_STRINGS[0]:
            nt["midi"] += 12  # 음역 밖 저음(옥타브 오류) 접기
        while nt["midi"] > OPEN_STRINGS[-1] + MAX_FRET:
            nt["midi"] -= 12
    cands = []
    for nt in notes:
        c = [(s, nt["midi"] - o) for s, o in enumerate(OPEN_STRINGS)
             if 0 <= nt["midi"] - o <= MAX_FRET]
        cands.append(c)
    INF = float("inf")
    # 비용 모델(베이시스트 피드백 2026-07-08): 프렛 '거리'가 아니라 ①손 포지션 이동(검지 위치,
    # 4프렛 손폭 안은 이동 0) ②줄 이동(인접 0.2, 두 줄 건너 1.0, 세 줄 2.0 — 초선형: 프렛이
    # 가까워도 줄을 건너면 어렵다) ③개방현은 어느 포지션에서든 무료. 하이프렛 미세 선호만 유지.
    SPAN = 3          # 검지~새끼 4프렛 폭
    STR_COST = [0.0, 0.2, 1.0, 2.0]

    def pos_for(fret, ppos):
        if fret == 0:
            return ppos            # 개방현 — 손 안 움직임
        if ppos <= fret <= ppos + SPAN:
            return ppos            # 손폭 안 — 포지션 유지
        return max(1, fret - SPAN) if fret > ppos + SPAN else fret

    states = [(fret * 0.05, (max(1, fret) if fret > 0 else 1), -1) for s, fret in cands[0]]
    history = [states]
    for i in range(1, len(notes)):
        cur = []
        for s, fret in cands[i]:
            best = (INF, 1, -1)
            for j, (pc, ppos, _) in enumerate(history[-1]):
                npos = pos_for(fret, ppos)
                move = abs(npos - ppos)
                sdiff = min(abs(s - cands[i - 1][j][0]), 3)
                pfret = cands[i - 1][j][1]
                # 손폭 안 손가락 이동도 무료는 아님(지그재그 방지 — 실측: 곡4 큰 이동 77→100 회귀)
                finger = 0.0 if (fret == 0 or pfret == 0) else abs(fret - pfret) * 0.15
                # 시간 여유(쉼·프레이즈 경계 ≥1초)엔 손을 자유로 옮김 — 빠듯한 연속 음만 세게 최적화
                dt = notes[i]["start"] - notes[i - 1]["start"]
                relax = 0.2 if dt > 1.0 else 1.0
                cost = pc + (move + STR_COST[sdiff] + finger) * relax + fret * 0.05
                if cost < best[0]:
                    best = (cost, npos, j)
            cur.append(best)
        history.append(cur)
    # 역추적
    j = min(range(len(history[-1])), key=lambda k: history[-1][k][0])
    for i in range(len(notes) - 1, -1, -1):
        s, fret = cands[i][j]
        notes[i]["string"] = s
        notes[i]["fret"] = fret
        j = history[i][j][2]
    return notes


# ---- 키 추정 (Krumhansl-Schmuckler, 노트 길이 가중) ----
_KRUMHANSL_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KRUMHANSL_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
PC_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PC_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
# 조표에 플랫이 붙는 조성(5도권) — 여기서는 근음을 플랫으로 적는 게 악보 표준(예: Fm 곡의 Bbm, A#m 아님)
_FLAT_TONICS = {"major": {1, 3, 5, 6, 8, 10}, "minor": {0, 2, 3, 5, 7, 10}}


def prefers_flats(key):
    return bool(key) and key["tonic"] in _FLAT_TONICS.get(key["mode"], set())


def _pc_names(key):
    return PC_NAMES_FLAT if prefers_flats(key) else PC_NAMES


def estimate_key(notes):
    """반환: (tonic_pc, 'major'|'minor', 표시명 예 'F#m'). 노트 없으면 None."""
    if not notes:
        return None
    hist = np.zeros(12)
    for nt in notes:
        hist[nt["midi"] % 12] += nt.get("dur", 0.2)
    if hist.sum() == 0:
        return None
    best = None
    for tonic in range(12):
        for mode, profile in (("major", _KRUMHANSL_MAJOR), ("minor", _KRUMHANSL_MINOR)):
            rotated = np.roll(hist, -tonic)
            score = float(np.corrcoef(rotated, profile)[0, 1])
            if best is None or score > best[0]:
                best = (score, tonic, mode)
    _, tonic, mode = best
    names = _pc_names({"tonic": tonic, "mode": mode})
    label = names[tonic] + ("m" if mode == "minor" else "")
    # 표시는 메이저 기준(사용자 지시 2026-07-10) — 마이너면 같은 조표의 상대 장조(F#m→A)
    display = names[tonic] if mode == "major" else names[(tonic + 3) % 12]
    # 괄호 병기용 마이너 표기(사용자 지시 2026-07-10 재확인: 메이저 곡도) — 마이너면 자신,
    # 메이저면 나란한조(상대 단조, 장3도 아래): E→C#m
    minor = label if mode == "minor" else names[(tonic + 9) % 12] + "m"
    return {"tonic": tonic, "mode": mode, "label": label, "display": display, "minor": minor}


_PC_OF_NAME = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5,
               "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11}


def parse_key_label(label):
    """사용자 키 입력('F#m'·'Bb'·'c# minor' 등) → estimate_key 와 같은 형태 + manual=True.
    못 읽으면 None(호출부가 안내). 키 직접 입력(사용자 요청 2026-07-10) — 추정이 틀린 곡의 교정 경로."""
    if not label:
        return None
    m = re.match(r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(m|min|minor|Minor)?\s*$", str(label))
    if not m:
        return None
    acc = {"♯": "#", "♭": "b"}.get(m.group(2), m.group(2))
    tonic = _PC_OF_NAME.get((m.group(1) + acc).upper())
    if tonic is None:
        return None
    mode = "minor" if m.group(3) else "major"
    names = _pc_names({"tonic": tonic, "mode": mode})
    lab = names[tonic] + ("m" if mode == "minor" else "")
    display = names[tonic] if mode == "major" else names[(tonic + 3) % 12]
    minor = lab if mode == "minor" else names[(tonic + 9) % 12] + "m"
    return {"tonic": tonic, "mode": mode, "label": lab, "display": display,
            "minor": minor, "manual": True}


def effective_key(notes, override_label=None):
    """키 override('F#m' 등)가 있으면 그것을, 없으면 추정 — 모든 재계산 경로가 이걸 쓴다."""
    key = parse_key_label(override_label) if override_label else None
    return key or estimate_key(notes)


# 다이어토닉 코드 성질 (장조/자연단조) — 도수 반음 오프셋 → 성질
_DIATONIC_MAJOR = {0: "", 2: "m", 4: "m", 5: "", 7: "", 9: "m", 11: "dim"}
_DIATONIC_MINOR = {0: "m", 2: "dim", 3: "", 5: "m", 7: "m", 8: "", 10: ""}


def estimate_chords(notes, key, bar_slots=16):
    """마디별 코드 초안 — 근음=길이 가중 최빈 음(박머리 2배 가중), 성질=키 다이어토닉.
    반환: [{bar, pos, label}] — pos=마디 내 슬롯(0=첫 박, bar_slots//2=후반 첫 박).
    ①모든 마디 채움(사용자 지시 2026-07-10: 코드 악보만 보고 진행 가능해야) — 감지 없는 마디는
    직전 코드 유지, 첫 감지 전(인트로 쉼)은 첫 코드. ②한 마디 최대 4코드(사용자 지시 2026-07-10
    "n개 코드 곡도 있다"): 구간을 마디→반→박으로 **재귀 반분할** — 각 단계 게이트 = 전·후반 근음 상이
    + 양쪽 무게 각 구간의 1/4↑ + 근음이 그 반의 절반↑ 우세 + **경계 박에 새 근음을 실제로 침**(베이스
    관례). 실측(2026-07-10, 라이브 6곡): 무게 조건만으론 워킹 베이스가 마디 89%를 가짜 분할(곡6
    123/138) → 이 게이트로 3/138, 진짜 체인지 곡은 13~40% 유지. 한계(정직): 경계가 반분할 계층에
    없는 패턴(예: 4박에만 체인지)은 못 잡음 — 수동 수정 경로가 보완. bar_slots: 4/4=16, 12/8·혼합=48."""
    if not notes:
        return []
    beat = bar_slots // 4  # 분할 최소 폭 = 1박(4/4: 4칸 · 48그리드: 12칸)
    bar_items = {}  # bar -> [(off, pc, w)]
    attacks = {}    # bar -> {off: pc} — 그 칸에서 '시작'하는 음(경계 실타격 판정)
    total_bars = 0
    for nt in notes:
        bar = nt["gi"] // bar_slots
        off = nt["gi"] % bar_slots
        w = nt.get("glen", 1) * (2 if off % beat == 0 else 1)  # 박머리 2배 가중
        bar_items.setdefault(bar, []).append((off, nt["midi"] % 12, w))
        attacks.setdefault(bar, {}).setdefault(off, nt["midi"] % 12)
        total_bars = max(total_bars,
                         (nt["gi"] + max(nt.get("glen", 1), 1) - 1) // bar_slots + 1)
    table = _DIATONIC_MAJOR if (key and key["mode"] == "major") else _DIATONIC_MINOR
    tonic = key["tonic"] if key else 0

    def label_of(root):
        quality = table.get((root - tonic) % 12, "")
        if quality == "dim":
            quality = "m"  # 초안 단순화(트라이어드 위주 — REQ-CHORD-001 고지와 일치)
        return _pc_names(key)[root] + quality

    def region_hist(items, lo, hi):
        h = np.zeros(12)
        for off, pc, w in items:
            if lo <= off < hi:
                h[pc] += w
        return h

    def split_region(items, atk, lo, hi):
        """[lo, hi) 구간의 코드 — 게이트 통과 시 반으로 재귀. 반환: [(pos, label)]."""
        h = region_hist(items, lo, hi)
        if float(h.sum()) == 0:
            return []
        if hi - lo > beat:
            mid = lo + (hi - lo) // 2
            h1, h2 = region_hist(items, lo, mid), region_hist(items, mid, hi)
            s1, s2 = float(h1.sum()), float(h2.sum())
            total = s1 + s2
            r1, r2 = int(h1.argmax()), int(h2.argmax())
            if (s1 > 0 and s2 > 0 and r1 != r2
                    and s1 >= total * 0.25 and s2 >= total * 0.25
                    and h1[r1] >= s1 * 0.5 and h2[r2] >= s2 * 0.5  # 근음 우세(워킹 배제)
                    and atk.get(mid) == r2):                       # 새 근음을 경계 박에 실제로 침
                return (split_region(items, atk, lo, mid)
                        + split_region(items, atk, mid, hi))
        return [(lo, label_of(int(h.argmax())))]

    detected = {}  # bar -> [(pos, label), ...] — 인접 동일 라벨은 병합
    for bar in sorted(bar_items):
        parts = split_region(bar_items[bar], attacks.get(bar, {}), 0, bar_slots)
        merged = []
        for pos, label in parts:
            if merged and merged[-1][1] == label:
                continue
            merged.append((pos, label))
        detected[bar] = merged or [(0, label_of(int(region_hist(bar_items[bar], 0, bar_slots).argmax())))]
    if not detected:
        return []
    first_label = detected[min(detected)][0][1]
    chords = []
    prev = None
    for bar in range(total_bars):
        entries = detected.get(bar)
        if entries is None:
            entries = [(0, prev if prev is not None else first_label)]
        for pos, label in entries:
            chords.append({"bar": bar, "pos": pos, "label": label})
        prev = entries[-1][1]
    return chords


# (그리드 길이, 표기, 부점, 셋잇단) — 부점 리듬을 8분+쉼표로 쪼개지 않고 점음표로 조판(가독성)
_DUR_TABLE = [(16, "1", False, False), (12, "2", True, False), (8, "2", False, False),
              (6, "4", True, False), (4, "4", False, False), (3, "8", True, False),
              (2, "8", False, False), (1, "16", False, False)]
# 12/8: 슬롯=펄스(8분)의 1/4 = 32분. 마디 48슬롯
_DUR_TABLE_COMPOUND = [(48, "1", True, False), (24, "2", True, False), (16, "2", False, False),
                       (12, "4", True, False), (8, "4", False, False), (6, "8", True, False),
                       (4, "8", False, False), (3, "16", True, False), (2, "16", False, False),
                       (1, "32", False, False)]
# 4/4 v2(박당 12칸): 스트레이트 가족(16분 계열) / 셋잇단 가족({tu 3})
_DUR48_S = [(48, "1", False, False), (36, "2", True, False), (24, "2", False, False),
            (18, "4", True, False), (12, "4", False, False), (9, "8", True, False),
            (6, "8", False, False), (3, "16", False, False)]
_DUR48_T = [(12, "4", False, False), (8, "4", False, True), (4, "8", False, True)]


def _dur_token(glen, table=_DUR_TABLE):
    """반환: (표기, 부점 여부, 셋잇단 여부, 소비한 그리드 수)."""
    for length, name, dotted, tup in table:
        if glen >= length:
            return name, dotted, tup, length
    length, name, dotted, tup = table[-1]
    return name, dotted, tup, length


def _chord_templates(key):
    """24 트라이어드(장/단) 템플릿 + 라벨(키 철자 반영). 반환 (T[24x12] 정규화, labels)."""
    names = _pc_names(key)
    T, labels = [], []
    for r in range(12):
        maj = np.zeros(12); maj[[r, (r + 4) % 12, (r + 7) % 12]] = 1.0
        T.append(maj); labels.append(names[r])
        mn = np.zeros(12); mn[[r, (r + 3) % 12, (r + 7) % 12]] = 1.0
        T.append(mn); labels.append(names[r] + "m")
    T = np.array(T)
    T = T / np.linalg.norm(T, axis=1, keepdims=True)
    return T, labels


def chroma_chords(stems_dir, notes, slots, bpm, offset, bar_slots, key=None):
    """★코드 초안 — 실제 화성(크로마) 기반(사용자 지적 2026-07-14: 코드 안 맞음). 예전 bass+키다이어토닉
    추정은 근음이 베이스에 종속·비다이어토닉 오류·키 틀리면 전멸(실증: 곡12 전 마디 'C'). 화성 스템
    (드럼 제외)을 합쳐 chroma_cqt → 마디별(필요시 반마디) 12트라이어드 템플릿 매칭. 스템 없으면 None(폴백)."""
    if not notes:
        return []
    try:
        import librosa
    except Exception:  # noqa: BLE001
        return None
    sig = None
    for st in ("guitar", "piano", "other", "vocals"):  # 베이스 제외(2026-07-15 코드청소): 저음 근음이 트라이어드 매칭 편향
        p = Path(stems_dir) / f"{st}.wav"
        if not p.exists():
            continue
        x, s = sf.read(str(p), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        if s != 22050:
            x = librosa.resample(x, orig_sr=s, target_sr=22050)
        sig = x if sig is None else (sig + x if len(sig) == len(x)
                                     else sig[:min(len(sig), len(x))] + x[:min(len(sig), len(x))])
    if sig is None or len(sig) < 22050:
        return None
    hop = 2048
    chroma = librosa.feature.chroma_cqt(y=sig, sr=22050, hop_length=hop)
    tpf = hop / 22050.0
    T, labels = _chord_templates(key)

    def slot_time(g):
        if slots is not None and len(slots) > 1:
            i = max(0, min(len(slots) - 2, int(g)))
            return slots[i] + (slots[i + 1] - slots[i]) * (g - i)
        return offset + g * (60.0 / bpm / (bar_slots / 4.0))

    # 에너지 기준(무음/인트로 마디 배제) — 곡 전체 chroma 에너지 중앙값의 일부 미만이면 무음 취급.
    frame_energy = chroma.sum(axis=0)
    e_th = float(np.median(frame_energy[frame_energy > 0])) * 0.25 if (frame_energy > 0).any() else 0.0

    names = _pc_names(key)  # 슬래시 베이스 표기용 음이름
    # 슬래시 코드(G/B)용 베이스 음 — ★그 구간에서 실제로 '친'(onset) 음 중 가장 긴 것을 베이스로.
    # (앞 마디에서 이어진 held 꼬리를 베이스로 잡던 버그 교정 — 실증 곡14 마디58: C 이어짐+B 침 → G/C 오표기,
    #  실제 친 B 로 G/B 여야. 사용자 지적 2026-07-15.) 구간에 친 음이 없으면(전부 held) 이어지는 음으로.
    _bgis = np.array([nt["gi"] for nt in notes]); _bpcs = np.array([nt["midi"] % 12 for nt in notes])
    _bglens = np.array([max(int(nt.get("glen", 1)), 1) for nt in notes])
    _bord = np.argsort(_bgis, kind="stable"); _bgis = _bgis[_bord]; _bpcs = _bpcs[_bord]; _bglens = _bglens[_bord]

    def bass_pc_at(lo, hi):
        a = int(np.searchsorted(_bgis, lo)); b = int(np.searchsorted(_bgis, hi))
        if b > a:  # 구간에 친 음 있음 → 가장 긴(지배적) 음
            k = a + int(np.argmax(_bglens[a:b])); return int(_bpcs[k]), int(_bglens[k])
        j = a - 1  # 구간에 친 음 없음 → lo 를 덮는 held 음
        if j >= 0 and int(_bgis[j]) + int(_bglens[j]) > lo:
            return int(_bpcs[j]), int(_bglens[j])
        return None, 0

    def chord_at(t0, t1):
        i0 = max(0, int(t0 / tpf)); i1 = min(chroma.shape[1], int(t1 / tpf))
        if i1 - i0 < 1:
            return None
        seg = chroma[:, i0:i1]
        if float(seg.sum(axis=0).mean()) < e_th:   # 무음/인트로 구간 → 코드 없음(직전 유지)
            return None
        c = seg.mean(axis=1)
        n = float(np.linalg.norm(c))
        if n < 1e-6:
            return None
        k = int((T @ (c / n)).argmax())
        return labels[k], k // 2   # (라벨, 근음 pitch class)

    def slashed(lo, hi, label, root_pc):
        # 슬래시(G/B)는 베이스가 근음 아니면서 '한 박 이상 지속'된 경우만 — 걷는(패싱) 베이스의 슬래시
        # 남발 억제(2026-07-15 코드청소). 짧은 패싱음엔 슬래시 안 붙임.
        bpc, bglen = bass_pc_at(lo, hi)
        if bpc is None or bpc == root_pc or bglen < max(1, bar_slots // 4):
            return label
        return f"{label}/{names[bpc]}"

    # 마디를 재귀 반분할 — 전·후반 코드가 다르면 나눠 마디당 여러 코드(사용자 지적 2026-07-15: 한 마디
    # 한 코드로 뭉뚱그려짐). 박(1/4마디) 이하로는 안 나눔·같으면 안 나눔(노이즈 오분할 방지). 최대 4/마디.
    def split_region(lo, hi, depth):
        res = chord_at(slot_time(lo), slot_time(hi))
        if res is None:
            return [(lo, None)]
        if depth <= 0 or (hi - lo) <= max(1, bar_slots // 4):
            return [(lo, slashed(lo, hi, res[0], res[1]))]
        mid = (lo + hi) // 2
        a = chord_at(slot_time(lo), slot_time(mid)); b = chord_at(slot_time(mid), slot_time(hi))
        if a and b and a[0] != b[0]:   # 전·후반 코드 상이 → 분할
            return split_region(lo, mid, depth - 1) + split_region(mid, hi, depth - 1)
        return [(lo, slashed(lo, hi, res[0], res[1]))]

    total_bars = (notes[-1]["gi"] + max(notes[-1].get("glen", 1), 1)) // bar_slots + 1
    depth = int(os.environ.get("CHAEBO_CHORD_DEPTH", "1"))  # 1=반마디(노이즈 적음 — 2026-07-15 코드청소 기본값).
    # 2=박 단위(코드 변화를 실제 박에 찍지만 chroma 노이즈로 과분할). env CHAEBO_CHORD_DEPTH 로 조절.
    chords, prev, first = [], None, None
    for bar in range(total_bars):
        lo0 = bar * bar_slots
        segs = split_region(lo0, lo0 + bar_slots, depth)  # 마디당 여러 코드(사용자 지적)
        resolved = []
        for lo, lab in segs:
            if lab is None:
                lab = prev  # 무음 구간 → 직전 코드 유지
            resolved.append((lo, lab))
            if lab is not None:
                prev = lab
                if first is None:
                    first = lab
        # 매 마디 첫 코드(pos 0)로 채우고(코드만 보고 진행 가능하게 — 2026-07-10), 마디 안에서 바뀌면 추가.
        if resolved:
            chords.append({"bar": bar, "pos": 0, "label": resolved[0][1]})
            for i in range(1, len(resolved)):
                if resolved[i][1] != resolved[i - 1][1]:
                    chords.append({"bar": bar, "pos": resolved[i][0] - lo0, "label": resolved[i][1]})
    # 첫 코드 감지 전(인트로) 마디는 첫 코드로 채움
    for c in chords:
        if c["label"] is None:
            c["label"] = first
    return [c for c in chords if c["label"] is not None]


def to_alphatex(notes, bpm, title, key=None, chords=None, meter="4/4", families=None):
    """alphaTex 생성 — 베이스 표준(낮은음자리표 F4 + 조표 + 마디 코드), 튜닝 표기 G2 D2 A1 E1.
    그리드: 4/4 v1(마디 16칸, 균일 폴백) / 4/4 v2(마디 48칸 + families 로 박별 16분·셋잇단
    가족 — Longview 류 부분 셋잇단을 {tu 3} 괄호로 조판) / 12/8(마디 48칸, 펄스=8분)."""
    compound = meter == "12/8"
    mixed = families is not None and not compound
    bar_len = 48 if (compound or mixed) else 16
    table = _DUR_TABLE_COMPOUND if compound else _DUR_TABLE
    # alphaTex \tempo = 4분음표 기준: 12/8 은 펄스(8분) bpm ÷ 2
    tempo = int(round(bpm / 2)) if compound else int(round(bpm))
    ks = ""
    if key:
        ks = _pc_names(key)[key["tonic"]] + ("minor" if key["mode"] == "minor" else "")
    # 부제에 키·BPM 명기(사용자 요청 2026-07-09) — 표기는 표준 심볼(F#m).
    # '(추정)'은 악보에선 생략(사용자 지시) — '자동 채보 초안' 문구가 초안임을 이미 말한다(정직 원칙 유지)
    bpm_disp = int(round(bpm / 3)) if compound else int(round(bpm))
    sub = "자동 채보 초안 (베이스)"
    if key and key.get("label"):
        disp = key.get("display") or key["label"]  # 메이저 기준 + 마이너 병기(사용자 지시)
        minor = key.get("minor") or (key["label"] if key.get("mode") == "minor" else "")
        if minor:
            disp += f' ({minor})'
        sub += f' · 키 {disp}'
    sub += f' · BPM {bpm_disp}'
    tex = [f'\\title "{title}"',
           f'\\subtitle "{sub}"',
           f'\\tempo {tempo}',
           f'\\defaultSystemsLayout {2 if compound else 4}',  # 곡 수준 메타 — '.' 앞이어야 적용
           '\\chordDiagramsInScore false',
           '.',
           '\\track "Bass"',
           '\\staff {score tabs}']
    if ks:
        tex.append(f'\\ks {ks}')
    tex += ['\\clef F4',
            '\\instrument "Electric Bass Finger"',
            '\\tuning (G2 D2 A1 E1)',
            '\\ts 12 8' if compound else '\\ts 4 4']
    # 조판 코드 표기는 변화 지점 + 줄 첫 마디(barsPerRow 경계)만 — 전 마디 반복은 조판 잡음.
    # 전 마디 코드는 코드 악보(격자)의 몫(사용자 지시 2026-07-10: 악보별 밀도 분리).
    # 반마디 코드(pos>0)는 전역 슬롯 위치에 마크 — 그 슬롯 이후 첫 토큰에 부착
    per_row = 2 if compound else 4
    chord_marks = []  # (전역 슬롯, 라벨) 오름차순
    _prev_label = None
    for c in sorted(chords or [], key=lambda c: (c["bar"], c.get("pos", 0))):
        pos = c.get("pos", 0)
        if c["label"] and (c["label"] != _prev_label
                           or (pos == 0 and c["bar"] % per_row == 0)):
            chord_marks.append((c["bar"] * bar_len + pos, c["label"]))
        _prev_label = c["label"]
    # alphaTex 현 번호: 1=가장 높은 현(G2) → 우리 string 0(E1)=4
    slots = {nt["gi"]: nt for nt in notes}
    end_gi = max(slots) + slots[max(slots)]["glen"] if slots else bar_len
    bars = []
    bar, filled = [], 0
    gi = 0
    chord_pending = None  # 마디의 코드 — 첫 '노트' 토큰에 부착(쉼표엔 불가)
    def fam_of(beat):
        if not mixed:
            return "S"
        return families[beat] if beat < len(families) else "S"

    # 토큰 하나로 못 담는 지속(마디·가족 경계 넘김, 불규칙 길이)은 붙임줄(-.현.길이)로 잇는다 —
    # 예전엔 남는 지속이 쉼표로 떨어져 '앞 음 늘림'이 악보에 안 보였음(사용자 실증 2026-07-09).
    # 문법은 tie 스모크 4케이스 실렌더로 검증(마디 넘김·{tu 3}·{d} 조합).
    sustain = None  # (노트, 남은 칸)
    mark_i = 0  # chord_marks 소비 인덱스 — 토큰 시작 슬롯이 마크에 닿으면 부착
    while gi < end_gi or filled % bar_len != 0:
        remaining_in_bar = bar_len - (filled % bar_len)
        if chord_pending is None and mark_i < len(chord_marks) and chord_marks[mark_i][0] <= gi:
            chord_pending = chord_marks[mark_i][1]
            mark_i += 1
        if mixed:
            # 혼합 그리드: 토큰은 가족 경계를 넘지 않음 — 셋잇단 박은 박 안에서만,
            # 스트레이트는 같은 가족이 이어지는 한(마디 안) 길게(2분·온음 유지)
            beat = gi // 12
            cur_fam = fam_of(beat)
            bar_end = (gi // 48 + 1) * 48
            if cur_fam == "T":
                cap = (beat + 1) * 12
            else:
                e = beat
                while (e + 1) * 12 < bar_end and fam_of(e + 1) == "S":
                    e += 1
                cap = min((e + 1) * 12, bar_end)
            remaining_in_bar = min(remaining_in_bar, cap - gi)
            cur_table = _DUR48_T if cur_fam == "T" else _DUR48_S
        else:
            cur_table = table
        if gi in slots:
            nt = slots[gi]
            token_len = min(nt["glen"], remaining_in_bar)
            name, dotted, tup, used = _dur_token(token_len, cur_table)
            token = f'{nt["fret"]}.{4 - nt["string"]}.{name}'
            effects = []
            if dotted:
                effects.append("d")
            if tup:
                effects.append("tu 3")
            if chord_pending:
                effects.append(f'ch "{chord_pending}"')
                chord_pending = None
            if effects:
                token += "{" + " ".join(effects) + "}"
            bar.append(token)
            sustain = (nt, max(0, nt["glen"] - used)) if nt["glen"] > used else None
        elif sustain and sustain[1] > 0:
            # 붙임줄 연속 — 직전 음의 남은 지속
            s_nt, s_left = sustain
            token_len = min(s_left, remaining_in_bar)
            name, dotted, tup, used = _dur_token(token_len, cur_table)
            token = f'-.{4 - s_nt["string"]}.{name}'
            effects = []
            if dotted:
                effects.append("d")
            if tup:
                effects.append("tu 3")
            if chord_pending:
                effects.append(f'ch "{chord_pending}"')
                chord_pending = None
            if effects:
                token += "{" + " ".join(effects) + "}"
            bar.append(token)
            sustain = (s_nt, s_left - used) if s_left - used > 0 else None
        else:
            # 쉼표 — 다음 노트/마디(가족) 경계까지
            sustain = None
            nxt = min([g for g in slots if g > gi] + [end_gi])
            token_len = min(nxt - gi, remaining_in_bar)
            name, dotted, tup, used = _dur_token(token_len, cur_table)
            reffects = []
            if dotted:
                reffects.append("d")
            if tup:
                reffects.append("tu 3")
            if chord_pending:
                # 코드는 마디 '첫 토큰'에 — 쉼표로 시작하는 마디에서 첫 음표에 붙이면
                # 마디마다 코드 위치가 들쭉날쭉(사용자 실증 2026-07-10). 쉼표 부착은 스모크 검증 완료
                reffects.append(f'ch "{chord_pending}"')
                chord_pending = None
            bar.append(f'r.{name}' + ("{" + " ".join(reffects) + "}" if reffects else ""))
        gi += used
        filled += used
        if filled % bar_len == 0:
            bars.append(" ".join(bar))
            bar = []
        if gi > 10000:  # 안전핀
            break
    if bar:
        bars.append(" ".join(bar))
    tex.append(" | ".join(bars))
    return "\n".join(tex)


def halve_tempo(beat_times, bpm0):
    """2배 템포 오검출 수동 교정(사용자 실증 2026-07-10: 벧엘 129 — 워십 체감 64.5.
    상용 BPM 서비스도 동일 모호성: songbpm 이 Way Maker 를 '136, 하프타임 68'로 병기).
    비트를 한 칸 걸러 취해 박을 절반 속도로 — 8분 펄스로 읽힌 박이 진짜 박이 된다."""
    if len(beat_times) > 2:
        return beat_times[::2], bpm0 / 2.0
    return beat_times, bpm0


def double_tempo(beat_times, bpm0):
    """절반 템포 오검출 교정(halve 의 역, 사용자 요청 2026-07-10 BPM 수동 보정) —
    박 사이 중점을 끼워 박을 2배 촘촘히(2분음표로 읽힌 박이 진짜 박이 된다)."""
    b = np.asarray(beat_times, dtype=float)
    if len(b) > 1:
        mids = (b[:-1] + b[1:]) / 2.0
        out = np.empty(len(b) + len(mids))
        out[0::2] = b
        out[1::2] = mids
        return out, bpm0 * 2.0
    return b, bpm0


def merge_manual_chords(computed, previous):
    """수동 수정 코드 보존(사용자 요청 2026-07-10) — 음 편집으로 코드를 재계산해도
    manual=True 항목은 그대로 유지(자동 추정이 사람 수정을 덮지 않게).
    마디 단위 치환: 수동 항목이 있는 마디는 그 마디의 자동 항목(반마디 포함) 전부를 수동으로 대체."""
    manual_bars = {}
    for c in (previous or []):
        if c.get("manual"):
            manual_bars.setdefault(c["bar"], []).append(c)
    out = [c for c in (computed or []) if c["bar"] not in manual_bars]
    for entries in manual_bars.values():
        out.extend(entries)
    out.sort(key=lambda c: (c["bar"], c.get("pos", 0)))
    return out


def prepend_intro_bars(notes, slots, families, offset, bpm, bar_slots):
    """인트로 포함 곡 전체 악보(사용자 요청 2026-07-10) — 첫 베이스 음 전 구간을 통쉼표 마디로.
    온마디 단위만 앞에 붙여 첫 음이 계속 마디 첫 박에 오게 한다(마디 미만 잔여는 미표기).
    반환: (notes, slots, families, offset, intro_bars)."""
    if offset <= 0.05 or not notes:
        return notes, slots, families, offset, 0
    if slots is not None and len(slots) > 1:
        slot0 = float(slots[1] - slots[0])
        if slot0 <= 0:
            return notes, slots, families, offset, 0
        bar_dur = slot0 * bar_slots
        intro_bars = int(offset // bar_dur)
        if intro_bars <= 0:
            return notes, slots, families, offset, 0
        pre = [float(slots[0]) - slot0 * k for k in range(intro_bars * bar_slots, 0, -1)]
        slots = np.concatenate([np.asarray(pre, dtype=float), np.asarray(slots, dtype=float)])
        new_offset = float(slots[0])
    else:
        bar_dur = 240.0 / bpm  # v1 균일 그리드: 마디 = 4분음표 4개
        intro_bars = int(offset // bar_dur)
        if intro_bars <= 0:
            return notes, slots, families, offset, 0
        new_offset = offset - intro_bars * bar_dur
    for n in notes:
        n["gi"] += intro_bars * bar_slots
    if families is not None:
        families = ["S"] * (intro_bars * 4) + families
    return notes, slots, families, new_offset, intro_bars


def main():
    bass_path, drums_path, out_json = sys.argv[1], sys.argv[2], sys.argv[3]
    title = sys.argv[4] if len(sys.argv) > 4 else "자동 채보"

    # 원시 캐시 재사용: 검출(CREPE ~10분/bp ~30초)은 안 변했는데 그리드·조판 수리 때마다 전체
    # 재실행하던 낭비(실증: 하루 6회) 제거. 검출 파이프라인이 바뀌면 RAW_V 를 올려 무효화.
    # 강제 전체 재분석은 CHAEBO_FRESH=1.
    RAW_V = 6  # v6: 저역 음정 자기상관 교정(74마디 C→C# 등) 추가(2026-07-15)
    apply_sensitivity(os.environ.get("CHAEBO_SENS", "normal"))
    _crepe_mode = os.environ.get("CHAEBO_CREPE_MODEL", "tiny")  # tiny(빠름)|full(정확)
    cache = None
    if os.environ.get("CHAEBO_FRESH") != "1" and Path(out_json).exists():
        try:
            cache = json.loads(Path(out_json).read_text(encoding="utf-8")).get("raw_cache")
            if cache and cache.get("v", 1) != RAW_V:
                cache = None
            # 감도가 바뀌면 검출부터 다시 — 캐시는 검출 결과라 감도 종속
            if cache and cache.get("sens", "normal") != SENS["mode"]:
                cache = None
            # ★음정 모드(tiny/full)가 바뀌면 재검출 — 안 그러면 '정확하게 다시 분석'이 tiny 캐시를 그대로
            #   써서 안 먹혔음(사용자 지적 2026-07-14: 정밀분석 안 먹힘). crepe_mode 를 캐시 키에 포함.
            if cache and cache.get("crepe_mode", "tiny") != _crepe_mode:
                cache = None
        except Exception:  # noqa: BLE001
            cache = None
    BP_ACCEPT = 0.85  # bp 8분정합 수용 하한(SP-4b: 곡6 bp 0.92 즉시 채택, 곡4 bp 0.45→CREPE 0.66 폴백)
    if cache:
        raw_notes = cache["raw_notes"]
        beat_times = np.asarray(cache["beat_times"], dtype=float)
        tune_cents = cache.get("tune_cents", 0.0)
        bpm0 = cache.get("bpm0", 100.0)
        prog(60)
    else:
        prog(2)
        x, sr = load_mono(bass_path)
        prog(5)
        bpm0, beat_times = estimate_tempo(drums_path)
        prog(10)

        _onsets_amp = detect_onsets(x, sr)

        def _prep(raw):  # 공통 정리 + 8분정합 점수
            raw = gate_quiet(raw, x, sr)
            raw = align_to_onsets(raw, _onsets_amp)
            raw = refine_with_envelope(raw, x, sr)  # 파형 어택 기준 지속병합·유령제거
            return raw, eighth_ratio(raw, beat_times)

        # 1차: basic-pitch (~30초, 리듬 선명 곡에서 우세 — SP-4b) → 정합 낮으면 CREPE 도 돌려 우세 채택
        raw_bp, tune_bp = detect_notes_bp(x, sr, Path(out_json).parent)
        raw_bp, score_bp = _prep(raw_bp)
        prog(25)
        if score_bp >= BP_ACCEPT:
            raw_notes, tune_cents = raw_bp, tune_bp
        else:
            f0, per = track_pitch(x, sr)   # 가장 오래 걸리는 단계(CPU 수 분)
            prog(70)
            raw_cr, tune_cr = segment_notes(f0, per, frame_rms_db(x, sr, len(f0)))
            raw_cr, score_cr = _prep(raw_cr)
            if score_cr >= score_bp:
                raw_notes, tune_cents = raw_cr, tune_cr
            else:
                raw_notes, tune_cents = raw_bp, tune_bp
        # CREPE 가 채택돼도 못 보는 초저음(<E1)은 bp 검출로 보강 — 빈 구간만(사용자 지적 2026-07-15:
        # 첫 음 C 누락 = 33Hz 가 CREPE fmin 아래). bp 채택 경로면 멱등(이미 있음).
        raw_notes = merge_low_notes(raw_notes, raw_bp, x, sr)
        # 저역 음정 교정 — CREPE·bp 가 30~60Hz 서 내는 반음 오차를 자기상관으로 바로잡음(사용자 지적
        # 2026-07-15: 74마디부터 C→C# 등). merge 로 보강한 음까지 함께 교정되도록 뒤에 둔다.
        raw_notes = refine_low_pitch(raw_notes, x, sr)
        # 교정으로 같은음이 된 인접 조각(C↔C# 흔들림 등)을 다시 지속으로 병합 — 둥둥 과분절 완화(사용자 지적).
        raw_notes = merge_sustained(raw_notes, x, sr)
        prog(75)
    beat_times_full = list(beat_times)  # 캐시엔 원본 보관(절반/2배 적용을 멱등하게)
    bpm0_orig = bpm0  # 캐시 복원용 — "적용됐을 것"이라고 역산하면 비트 0개 곡(no-op)에서 오염(실증 2026-07-10)
    _tempo_adj = os.environ.get("CHAEBO_TEMPO")
    if _tempo_adj == "half":
        beat_times, bpm0 = halve_tempo(np.asarray(beat_times, dtype=float), bpm0)
    elif _tempo_adj == "double":
        beat_times, bpm0 = double_tempo(np.asarray(beat_times, dtype=float), bpm0)
    bpm, offset = bpm0, float(beat_times[0]) if len(beat_times) else 0.0
    meter = os.environ.get("CHAEBO_METER") or detect_meter(beat_times, raw_notes)  # 수동 고정 우선
    if meter not in ("4/4", "12/8"):
        meter = "4/4"
    sub = 4 if meter == "12/8" else 12  # 4/4 v2: 박당 12(16분·셋잇단 공존 — Longview 류)
    slots = None
    families = None
    if raw_notes:
        onsets = np.array([n["start"] for n in raw_notes])
        # 동적 그리드(실연주 템포 추종) 우선 — 비트 부족 시 균일 16칸 그리드 폴백
        slots = build_slot_times(beat_times, onsets, sub=sub)
        if slots is not None:
            ivals = np.diff(slots)
            bpm = round(float(60.0 / (np.median(ivals) * sub)), 1)
            offset = float(slots[0])
        else:
            bpm, offset = refine_grid(raw_notes, bpm0, beat_times)
    prog(85)
    if slots is not None and meter == "12/8":
        notes = quantize_dynamic(raw_notes, slots)
        grid_v = 2
        bar_slots = 48
    elif slots is not None:
        notes, families = quantize_mixed(raw_notes, slots)
        grid_v = 2
        bar_slots = 48
    else:
        notes = quantize(raw_notes, bpm, offset)
        grid_v = 1
        bar_slots = 16
    notes = assign_frets(notes)
    # 선두 음(프레이즈 시작)이 박에서 1개-16분 이내로 벗어나면 다운비트로 스냅 — 킥과 함께 들어온 첫 음을
    # 비트트래커 첫 박이 살짝 늦게 잡아 'a'로 민 것(측정 2026-07-15: 첫음≈킥 56ms, 둘 다 그리드 박1 직전).
    # ★긴 음만·선두 하나만 — 짧은 당김음(stab)이나 나머지 음은 안 건드린다(당김음은 흔하고 정당 — 검색 확인).
    if (grid_v == 2 and slots is not None and notes
            and os.environ.get("CHAEBO_LEAD_SNAP", "0") == "1"):  # 기본 끔(2026-07-15 결정: 선두 음 하나만
            # 박머리로 옮기는 얕은 보정이고, 첫 음이 진짜 당김음/패싱음이면 잘못 끌어옴 → 기본 OFF, 켜는 곡만 명시).
            # 켜면 선두 음이 박에서 살짝 벗어날 때 가장 가까운 박으로. (근본 해법은 마디 위상 보정 — 별도.)
        beat = bar_slots // 4  # 4/4 v2 = 박당 12슬롯
        li = min(range(len(notes)), key=lambda i: notes[i]["gi"])
        g = notes[li]["gi"]; r = g % beat
        # 선두 음을 '그 음이 속한 박'의 정박(박머리)으로 당김 — 첫 음이 박1 안에 있으면 박1로(사용자
        # 요청 2026-07-15: 첫 음이 정박=1). 뒤 음들은 안 건드림(당김음 보존). 이미 정박이면(r=0) 무동작.
        tgt = g - r if r > 0 else None
        if (tgt is not None and 0 <= tgt < len(slots) - 1
                and notes[li].get("glen", 1) >= beat // 2       # 8분 이상(아주 짧은 당김 stab만 제외)
                and not any(n["gi"] == tgt for n in notes)):    # 그 자리에 다른 음 없을 때만
            end = g + notes[li].get("glen", 1)
            local = float(slots[tgt + 1] - slots[tgt])
            notes[li]["gi"] = tgt
            notes[li]["start"] = round(float(slots[tgt]), 3)
            notes[li]["glen"] = max(1, end - tgt)
            notes[li]["dur"] = round(notes[li]["glen"] * local, 3)
    # 마디 위상 자동 보정 — 박 간격은 맞는데 1박이 엉뚱한 박에 붙은 경우 통-박 이동으로 교정(실측 곡15
    # 다운비트 43%->92%, 2026-07-15). 서브박(당김음) 미변경. 자동이 틀리면 '박자 시작점 이동'으로 수동 override.
    if grid_v == 2 and os.environ.get("CHAEBO_BARPHASE", "1") == "1":
        notes, slots, _dphi = snap_bar_phase(notes, slots, bar_slots)
        if _dphi:
            offset = float(slots[0])
            if families is not None:  # gi 가 통-박(=beat 배수)만큼 밀렸으니 families 도 그만큼 앞에 채움
                families = ["S"] * (_dphi // (bar_slots // 4)) + list(families)
    # 인트로(첫 베이스 음 전) 통쉼표 마디 — 곡 전체가 악보에 그려지게(사용자 요청 2026-07-10)
    notes, slots, families, offset, _intro = prepend_intro_bars(
        notes, slots, families, offset, bpm, bar_slots)
    prog(92)
    key = effective_key(notes, os.environ.get("CHAEBO_KEY"))  # 키 직접 입력은 재분석에도 유지
    # 코드: 화성 크로마 기반(정확) 우선, 실패 시 옛 bass+키 추정 폴백. env 로 끔.
    chords = None
    if os.environ.get("CHAEBO_CHROMA_CHORDS", "1") == "1":
        try:
            chords = chroma_chords(Path(bass_path).parent, notes, slots, bpm, offset, bar_slots, key=key)
        except Exception as e:  # noqa: BLE001 — 실패해도 폴백으로 계속
            print(f"[chroma_chords 실패, 폴백] {e}", flush=True)
            chords = None
    if not chords:
        chords = estimate_chords(notes, key, bar_slots=bar_slots)
    tex = to_alphatex(notes, bpm, title, key=key, chords=chords, meter=meter, families=families)
    max_gi = (notes[-1]["gi"] + notes[-1]["glen"] + 32) if notes else 0
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"bpm": bpm, "offset": offset, "notes": notes, "tex": tex,
                   "key": key, "chords": chords, "tune_cents": tune_cents,
                   "meter": meter, "bar_slots": bar_slots, "grid_v": grid_v,
                   "families": families,
                   "slots": [round(float(s), 3) for s in slots[:max_gi]] if slots is not None else None,
                   "raw_cache": {"v": RAW_V, "sens": SENS["mode"], "crepe_mode": _crepe_mode,
                                 "raw_notes": raw_notes,
                                 "beat_times": [round(float(b), 4) for b in beat_times_full],
                                 "tune_cents": tune_cents,
                                 "bpm0": bpm0_orig}},
                  f, ensure_ascii=False)
    prog(100)


if __name__ == "__main__":
    main()
