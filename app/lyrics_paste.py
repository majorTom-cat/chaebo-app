"""공식 가사 붙여넣기 배치 — 순수 로직(웹/잡 공용, 순환 import 방지).

두 경로가 이걸 쓴다:
- main.paste_lyrics: ASR 골격(base)이 이미 있으면 즉시 오버레이.
- jobs._process_lyrics: 골격이 없어 whisper 를 먼저 돌린 뒤, 그 ASR 결과를 base 로 넘겨 오버레이(똑똑한 붙여넣기).

★배치 = 순서보존 시퀀스 정렬(Needleman-Wunsch, 2026-07-18 재작성). 옛 방식(ASR 세그마다 글자겹침 최고
공식 줄을 독립 매칭)은 후렴이 과도 반복(13회)되고·인트로 애드립과 진짜 줄을 뭉치고·오타 줄을 드롭했다
(사용자 실증). 정렬은 공식 줄 시퀀스와 ASR 세그 시퀀스를 순서를 지키며 최적 대응 → ①모든 공식 줄 보존
(매칭 안 되면 이웃 사이 보간, 절대 드롭 안 함) ②순서 유지 ③반복은 실제 부른 만큼(공식이 반복을 적은
만큼·ASR이 반복한 만큼) ④공식에 없는 ASR = 즉흥(improv 초안) ⑤♪ placeholder 보존. base 없으면(부실
ASR·실패) 붙여넣은 줄을 시각 앵커에 보간(정확 텍스트+근사 타이밍)."""


def _kchars(s):
    return {c for c in (s or "") if "가" <= c <= "힣"}  # 한글 음절만(공백·기호·오타영향 줄임)


def _sim(oc, ac):
    """공식 줄이 ASR 세그에 담긴 비율(containment 0~1) — ASR 오타에 강건(정확 일치 아님)."""
    return (len(oc & ac) / len(oc)) if oc else 0.0


def _best_run(cands):
    """공식 줄 인덱스 후보 중 '연속된' 최장 구간(한 ASR 세그가 여러 공식 줄을 뭉친 경우 그 줄들)."""
    if not cands:
        return []
    best, cur = [cands[0]], [cands[0]]
    for i in sorted(cands)[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [i]
    return cur if len(cur) > len(best) else best


def _split_span(s0, e0, texts):
    """뭉친 세그 [s0,e0]를 여러 줄에 '글자수 비례'로 분배 → 각 (텍스트, 시작, 끝)."""
    lens = [max(1, len(_kchars(t))) for t in texts]
    tot = sum(lens) or 1
    span = max(0.4, e0 - s0)
    out, t = [], s0
    for txt, ln in zip(texts, lens):
        te = t + span * ln / tot
        out.append((txt, round(t, 2), round(te, 2)))
        t = te
    return out


def has_usable_base(cur):
    """cur(현재 가사 dict)에서 오버레이에 쓸 ASR 골격을 뽑는다 — 없으면 None(→ whisper 먼저 돌려야 함).
    저장된 base 우선, 없으면 현재가 ASR 원본(lrclib/pasted/overlay 아님)이고 ≥5세그면 그걸 base 로."""
    old = cur.get("segments") or []
    base = cur.get("base")
    if not base and cur.get("source") not in ("lrclib", "pasted", "overlay") and len(old) >= 5:
        base = old
    return base if (base and len(base) >= 5) else None


# 정렬 파라미터(실측 튜닝, 곡15). MATCH_BIAS = sim 이 이보다 높아야 '매칭'이 '건너뜀'보다 이득.
_MATCH_BIAS = 0.30
_GAP_OFF = 0.45   # 공식 줄 건너뜀(ASR에 없음) 벌점 — 크게: 공식 줄은 웬만하면 매칭/보간해 살린다
_GAP_ASR = 0.12   # ASR 세그 건너뜀(공식에 없는 즉흥·반복) 벌점 — 작게: 즉흥은 흔하다
_REPEAT_CLEAN = 0.55  # 건너뛴 ASR 세그가 이 이상 어떤 공식 줄과 겹치면 그 공식 글로 정리(반복 후렴)


def _word_align(off_line, asr_words, seg_s, seg_e):
    """공식 줄 단어 ↔ ASR 세그 단어(시각) 단조 매칭 → (words_out, adlib).
    words_out = [{w, s}] 모든 공식 단어에 시각(매칭 asr 단어 시각 or 보간) — 박자 정확도↑.
    adlib = [(text, s, e)] 공식 단어에 안 쓰인 연속 asr 단어(인트로/후렴 붙은 즉흥). asr_words 없으면 ([],[])."""
    off_words = [w for w in (off_line or "").split() if w]
    if not off_words or not asr_words:
        return [], []
    aw = [(a.get("w", "") or "", float(a.get("s", 0)),
           float(a.get("e") or a.get("s", 0)) or (float(a.get("s", 0)) + 0.3)) for a in asr_words]
    aw_chars = [_kchars(w) for w, _, _ in aw]
    m = len(aw)
    matched = [None] * len(off_words)
    ptr = 0
    for i, w in enumerate(off_words):
        oc = _kchars(w)
        if not oc:
            continue
        best_k, best = None, 0.34  # 단어 최소 겹침(오타 감안 관대)
        for k in range(ptr, min(m, ptr + 4)):  # 앞 창(window) 안에서만 — 순서 보존
            sim = len(oc & aw_chars[k]) / len(oc)
            if sim > best:
                best, best_k = sim, k
        if best_k is not None:
            matched[i] = best_k
            ptr = best_k + 1
    # 공식 단어 시각(매칭 or 보간)
    times = [None] * len(off_words)
    for i, k in enumerate(matched):
        if k is not None:
            times[i] = aw[k][1]
    known = [(i, times[i]) for i in range(len(off_words)) if times[i] is not None]
    if not known:
        span = max(0.3, seg_e - seg_s)
        for i in range(len(off_words)):
            times[i] = seg_s + span * (i + 0.15) / len(off_words)
    else:
        fi, ft = known[0]
        for i in range(fi):
            times[i] = max(seg_s, ft - (fi - i) * 0.22)
        li, lt = known[-1]
        for i in range(li + 1, len(off_words)):
            times[i] = lt + (i - li) * 0.22
        for a in range(len(known) - 1):
            ia, ta = known[a]
            ib, tb = known[a + 1]
            if ib - ia > 1:
                for i in range(ia + 1, ib):
                    times[i] = ta + (tb - ta) * ((i - ia) / (ib - ia))
    words_out = [{"w": off_words[i], "s": round(float(times[i]), 2)} for i in range(len(off_words))]
    # 안 쓰인 asr 단어 → 연속 그룹(붙은 즉흥/애드립 후보)
    used = {k for k in matched if k is not None}
    groups, cur = [], []
    for k in range(m):
        if k in used:
            if cur:
                groups.append(cur)
                cur = []
        else:
            cur.append(k)
    if cur:
        groups.append(cur)
    adlib = []
    for g in groups:
        txt = " ".join(aw[k][0] for k in g).strip()
        if txt:
            adlib.append((txt, round(aw[g[0]][1], 2), round(aw[g[-1]][2], 2)))
    return words_out, adlib


def _align_overlay(lines, base):
    """공식 줄(lines) ↔ ASR 세그(base) 순서보존 정렬 → 시간순 segments 리스트.
    manual=공식 줄(단어별 시각 부여), improv=공식에 없는 ASR 즉흥(줄에 붙은 것 포함), placeholder=♪."""
    n = len(lines)
    off_chars = [_kchars(l) for l in lines]
    # ASR 세그 정규화(단어 시각 유지 — 단어별 배치·애드립 되살리기용)
    asr = []
    for o in base:
        s0 = round(float(o.get("s", 0)), 2)
        e0 = round(float(o.get("e") or o.get("s", 0)) or (s0 + 2.0), 2)
        ph = bool(o.get("placeholder"))
        asr.append({"s": s0, "e": e0, "text": (o.get("text", "") or ""), "ph": ph,
                    "chars": set() if ph else _kchars(o.get("text", "")), "words": o.get("words") or []})
    m = len(asr)
    if n == 0:
        out = []
        for a in asr:
            if a["ph"]:
                out.append({"s": a["s"], "e": a["e"], "text": "♪", "improv": True, "placeholder": True})
            elif a["text"].strip():
                out.append({"s": a["s"], "e": a["e"], "text": a["text"][:200], "improv": True})
        return out

    # D[i][j] = 공식 앞 i줄 · ASR 앞 j세그 최적 정렬 점수(Needleman-Wunsch, 순서보존)
    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        D[i][0] = D[i - 1][0] - _GAP_OFF
    for j in range(1, m + 1):
        D[0][j] = D[0][j - 1] - _GAP_ASR
    for i in range(1, n + 1):
        oc = off_chars[i - 1]
        row, prow = D[i], D[i - 1]
        for j in range(1, m + 1):
            sim = 0.0 if asr[j - 1]["ph"] else _sim(oc, asr[j - 1]["chars"])
            match = prow[j - 1] + (sim - _MATCH_BIAS)
            row[j] = max(match, prow[j] - _GAP_OFF, row[j - 1] - _GAP_ASR)

    # 백트랙 → 공식 줄별 매칭 세그(off_match_j) + 건너뛴 ASR(잉여)
    off_match_j = [None] * n
    leftover = []
    i, j = n, m
    while i > 0 or j > 0:
        took = None
        if i > 0 and j > 0:
            sim = 0.0 if asr[j - 1]["ph"] else _sim(off_chars[i - 1], asr[j - 1]["chars"])
            if D[i][j] == D[i - 1][j - 1] + (sim - _MATCH_BIAS):
                took = "match"
        if took is None and i > 0 and D[i][j] == D[i - 1][j] - _GAP_OFF:
            took = "skipoff"
        if took is None:
            took = "skipasr"
        if took == "match":
            off_match_j[i - 1] = j - 1
            i -= 1
            j -= 1
        elif took == "skipoff":
            i -= 1
        else:
            leftover.append(j - 1)
            j -= 1
    leftover.reverse()

    # 공식 줄 시각: 매칭이면 세그 s, 아니면 이웃 사이 보간(절대 드롭 안 함)
    off_time = [None] * n
    for k in range(n):
        if off_match_j[k] is not None:
            off_time[k] = asr[off_match_j[k]]["s"]
    anchors = [(k, off_time[k]) for k in range(n) if off_time[k] is not None]
    if not anchors:
        s_lo = asr[0]["s"] if asr else 0.0
        s_hi = asr[-1]["e"] if asr else (s_lo + n * 3.0)
        for k in range(n):
            off_time[k] = s_lo + (s_hi - s_lo) * (k / max(1, n))
    else:
        fk, ft = anchors[0]
        for k in range(fk):
            off_time[k] = max(0.0, ft - (fk - k) * 0.6)
        lk, lt = anchors[-1]
        for k in range(lk + 1, n):
            off_time[k] = lt + (k - lk) * 0.6
        for a in range(len(anchors) - 1):
            ka, ta = anchors[a]
            kb, tb = anchors[a + 1]
            if kb - ka > 1:
                for k in range(ka + 1, kb):
                    off_time[k] = ta + (tb - ta) * ((k - ka) / (kb - ka))

    segs = []
    for k in range(n):
        j = off_match_j[k]
        if j is not None:
            a = asr[j]
            words_out, adlib = _word_align(lines[k], a["words"], a["s"], a["e"])
            s0 = round(words_out[0]["s"], 2) if words_out else round(float(off_time[k]), 2)
            e0 = round(max(a["e"], s0 + 0.4), 2)
            seg = {"s": s0, "e": e0, "text": lines[k][:200], "manual": True}
            if words_out:
                seg["words"] = words_out
            segs.append(seg)
            # ★줄에 붙은 즉흥(애드립) 되살리기 — 공식 단어에 안 쓰인 asr 단어 그룹. 단, 그 그룹이 실은
            #   인접 공식 줄이면(뭉친 절) 중복이라 건너뜀(그 줄은 따로 배치됨).
            for txt, gs, ge in adlib:
                ac = _kchars(txt)
                if ac and any(_sim(off_chars[q], ac) >= _REPEAT_CLEAN for q in range(n)):
                    continue
                segs.append({"s": gs, "e": max(ge, gs + 0.4), "text": txt[:200], "improv": True})
        else:
            s0 = round(float(off_time[k]), 2)
            segs.append({"s": s0, "e": round(s0 + 1.2, 2), "text": lines[k][:200], "manual": True})

    # 건너뛴 ASR(잉여) → ♪ 보존 / 담긴 '연속' 공식 줄이면 반복(후렴)으로 깨끗한 글 재구성 / 아니면 즉흥 초안
    for j in leftover:
        a = asr[j]
        if a["ph"]:
            segs.append({"s": a["s"], "e": a["e"], "text": "♪", "improv": True, "placeholder": True})
            continue
        if not a["text"].strip():
            continue
        cand = [k for k in range(n) if _sim(off_chars[k], a["chars"]) >= _REPEAT_CLEAN]
        run = _best_run(cand)
        if run:
            for line, ws, we in _split_span(a["s"], a["e"], [lines[k] for k in run]):
                segs.append({"s": ws, "e": we, "text": line[:200], "manual": True})
        else:
            segs.append({"s": a["s"], "e": a["e"], "text": a["text"][:200], "improv": True})

    segs.sort(key=lambda s: (s["s"], s["e"]))
    return segs


def _interp_paste(lines, old, dur):
    """base 없음/부실 → 붙여넣은 줄마다 고유 시각(보간). 시각 앵커, 부실하면 곡 전체 균등."""
    n = len(lines)
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

    segs, prev = [], None
    for i, l in enumerate(lines):
        s = _interp(i / n)
        e = _interp((i + 1) / n)
        if prev is not None and s <= prev + 0.05:
            s = prev + 0.3
        if e <= s:
            e = s + 1.2
        segs.append({"s": round(s, 2), "e": round(e, 2), "text": l[:200], "manual": True})
        prev = s
    return segs


def build_paste_result(lines, old, base, dur):
    """붙여넣은 공식 줄(lines)을 배치한 가사 result dict 반환.
    base(ASR 골격, ≥5세그)가 있으면 순서정렬 오버레이(즉흥 보존), 없으면 old 시각 앵커에 보간(폴백)."""
    if base and len(base) >= 5:
        segs = _align_overlay(lines, base)
        src = "overlay"
    else:
        segs = _interp_paste(lines, old, dur)
        src = "pasted"
    result = {"status": "ready", "language": "manual", "source": src, "segments": segs}
    if src == "overlay" and base:  # ASR 골격 보존 — 다시 붙여넣어도 원본 ASR 위에 얹히게(♪·단어시각 포함)
        result["base"] = [{"s": o.get("s"), "e": o.get("e"), "text": o.get("text", ""),
                           "words": o.get("words", []),
                           **({"placeholder": True} if o.get("placeholder") else {})} for o in base]
    return result
