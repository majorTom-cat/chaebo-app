"""공식 가사 붙여넣기 배치 — 순수 로직(웹/잡 공용, 순환 import 방지).

두 경로가 이걸 쓴다:
- main.paste_lyrics: ASR 골격(base)이 이미 있으면 즉시 오버레이.
- jobs._process_lyrics: 골격이 없어 whisper 를 먼저 돌린 뒤, 그 ASR 결과를 base 로 넘겨 오버레이(똑똑한 붙여넣기).

핵심(사용자 실증 2026-07-17): 라이브/워십은 공식 가사에 없는 즉흥(애드립)이 있어 통짜로 붙이면 안 맞는다.
→ ASR 타임라인(전체·반복·즉흥 위치) 위에 각 세그먼트마다 '글자 겹침' 최고 공식 줄을 매칭. 잘 맞으면 정확한
공식 글로 교체, 안 맞으면(=즉흥) 받아쓰기 초안 유지+표시(improv). ♪ placeholder(에너지로 찾은 애드립
자리)는 그대로 보존. base 없으면(부실 ASR·실패) 붙여넣은 줄을 시각 앵커에 보간(정확 텍스트+근사 타이밍)."""


def _kchars(s):
    return {c for c in (s or "") if "가" <= c <= "힣"}  # 한글 음절만(공백·기호·오타영향 줄임)


def _best_run(cands):
    """공식 줄 인덱스 후보 중 '연속된' 최장 구간(한 ASR 세그가 여러 공식 줄을 뭉친 경우 그 줄들). 반복은 단일 처리."""
    if not cands:
        return []
    best, cur = [cands[0]], [cands[0]]
    for i in cands[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [i]
    return cur if len(cur) > len(best) else best


def _split_span_by_lines(s0, e0, lines):
    """뭉친 세그 [s0,e0]를 공식 줄들에 '글자수 비례'로 분배 → 각 줄 (텍스트, 시작, 끝). 오타에 강건(길이 비례)."""
    lens = [max(1, len(_kchars(l))) for l in lines]
    tot = sum(lens) or 1
    span = max(0.4, e0 - s0)
    res, t = [], s0
    for line, ln in zip(lines, lens):
        te = t + span * ln / tot
        res.append((line, t, te))
        t = te
    return res


def has_usable_base(cur):
    """cur(현재 가사 dict)에서 오버레이에 쓸 ASR 골격을 뽑는다 — 없으면 None(→ whisper 먼저 돌려야 함).
    저장된 base 우선, 없으면 현재가 ASR 원본(lrclib/pasted/overlay 아님)이고 ≥5세그면 그걸 base 로."""
    old = cur.get("segments") or []
    base = cur.get("base")
    if not base and cur.get("source") not in ("lrclib", "pasted", "overlay") and len(old) >= 5:
        base = old
    return base if (base and len(base) >= 5) else None


def build_paste_result(lines, old, base, dur):
    """붙여넣은 공식 줄(lines)을 배치한 가사 result dict 반환.
    base(ASR 골격, ≥5세그)가 있으면 그 위에 오버레이(즉흥 보존), 없으면 old 시각 앵커에 보간(폴백)."""
    n = len(lines)
    if base and len(base) >= 5:
        old = base  # 항상 원본 ASR 위에 덧입힘
        off = [(_kchars(l), l) for l in lines]  # (글자집합, 공식줄) — 인덱스=원문 순서
        segs = []
        placed = {}  # 공식줄 인덱스 -> 배치된 시작시각들(누락 복구·중복판단용)

        def _emit(i, s, e):
            segs.append({"s": round(s, 2), "e": round(max(e, s + 0.4), 2),
                         "text": off[i][1][:200], "manual": True})
            placed.setdefault(i, []).append(s)

        for o in old:
            if o.get("placeholder"):  # ♪ 애드립 자리 — 공식에 없는 즉흥, 매칭 대상 아님(그대로 보존)
                s0 = round(float(o.get("s", 0)), 2)
                e0 = round(float(o.get("e") or o.get("s", 0)) or (s0 + 2.0), 2)
                segs.append({"s": s0, "e": e0, "text": "♪", "improv": True, "placeholder": True})
                continue
            s0 = round(float(o.get("s", 0)), 2)
            e0 = round(float(o.get("e") or o.get("s", 0)) or (s0 + 2.0), 2)
            ac = _kchars(o.get("text", ""))
            conts = [(i, (len(ac & oc) / len(oc)) if oc else 0.0) for i, (oc, l) in enumerate(off)]
            cand = [i for i, c in conts if c >= 0.55]
            run = _best_run(cand)
            if len(run) >= 2:
                for (line, ws, we), i in zip(_split_span_by_lines(s0, e0, [off[j][1] for j in run]), run):
                    _emit(i, ws, we)
            elif run:
                _emit(run[0], s0, e0)
            else:  # 즉흥 — 공식에 없음 → 받아쓰기 유지(초안), 표시
                segs.append({"s": s0, "e": e0, "text": (o.get("text", "") or "")[:200], "improv": True})

        # 누락 공식 줄 복구 — '유일하게' 배치된 이웃만 앵커로 국소 위치 잡기(반복 코러스 오배치 방지)
        def _uniq_before(i):
            for j in range(i - 1, -1, -1):
                if len(placed.get(j, [])) == 1:
                    return placed[j][0]
            return None

        def _uniq_after(i):
            for j in range(i + 1, len(off)):
                if len(placed.get(j, [])) == 1:
                    return placed[j][0]
            return None

        for i in range(len(off)):
            if i in placed or not off[i][0]:
                continue
            b, f = _uniq_before(i), _uniq_after(i)
            if b is not None and f is not None and b <= f:
                t = (b + f) / 2
            elif f is not None:
                t = f - 0.5
            elif b is not None:
                t = b + 0.5
            else:
                any_t = [x for v in placed.values() for x in v]
                t = (max(any_t) + 1.0) if any_t else 0.0
            _emit(i, max(0.0, t), max(0.0, t) + 0.5)
        segs.sort(key=lambda s: s["s"])
        src = "overlay"
    else:
        # 받아쓰기 없음/부실 → 붙여넣은 줄마다 고유 시각(보간). 시각 앵커, 부실하면 곡 전체 균등.
        anchors = sorted({round(float(o.get("s", 0)), 2) for o in old}) if old else []
        if old:
            le = round(float(old[-1].get("e") or old[-1].get("s", 0)), 2)
            if not anchors or le > anchors[-1]:
                anchors.append(le)
        use_anchors = len(anchors) >= 3 and (not dur or (anchors[-1] - anchors[0]) >= dur * 0.3)
        if not use_anchors:
            lo = (dur * 0.05) if dur else (anchors[0] if anchors else 0.0)
            hi = (dur * 0.95) if dur else (lo + max(1, n) * 3.0)
            anchors = [round(lo, 2), round(hi, 2)]
        A = len(anchors)

        def _interp(frac):
            x = frac * (A - 1)
            k = min(A - 2, int(x))
            return anchors[k] + (anchors[k + 1] - anchors[k]) * (x - k)

        segs = []
        prev = None
        for i, l in enumerate(lines):
            s = _interp(i / n)
            e = _interp((i + 1) / n)
            if prev is not None and s <= prev + 0.05:
                s = prev + 0.3
            if e <= s:
                e = s + 1.2
            segs.append({"s": round(s, 2), "e": round(e, 2), "text": l[:200], "manual": True})
            prev = s
        src = "pasted"

    result = {"status": "ready", "language": "manual", "source": src, "segments": segs}
    if src == "overlay" and base:  # ASR 골격 보존 — 다시 붙여넣어도 원본 ASR 위에 얹히게(♪·단어시각 포함)
        result["base"] = [{"s": o.get("s"), "e": o.get("e"), "text": o.get("text", ""),
                           "words": o.get("words", []),
                           **({"placeholder": True} if o.get("placeholder") else {})} for o in base]
    return result
