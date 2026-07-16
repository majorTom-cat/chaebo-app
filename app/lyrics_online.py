"""온라인 가사 조회 — LRCLIB(무료·키 불필요·사람이 옮긴 싱크 가사 DB). 로컬 열람 한정(공유·내보내기 금지,
docs/licenses.md). ASR(whisper) 은 곡을 못 찾을 때만 폴백 — 유명곡은 여기서 정확한 싱크 가사를 바로 얻는다.

반환: {"source":"lrclib", "synced":bool, "segments":[{"s","e","text"}]} 또는 None(못 찾음).
분석 파이프라인의 lyrics 포맷과 동일해 그대로 저장한다."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

_UA = "chaebo/1.0 (local practice app; https://github.com/majorTom-cat/chaebo)"
_NOISE = re.compile(
    r"(?i)\b(official(\s+(video|audio|mv|lyric\w*))?|mv|m/v|lyrics?|가사|audio|video|live|"
    r"performance|clip|teaser|hd|4k|remaster(ed)?|color\s*coded)\b")


def _norm(s: str | None) -> str:
    """제목 비교용 정규화 — 영숫자·한글만(공백·기호·대소문자 제거)."""
    return re.sub(r"[^a-z0-9가-힣]", "", (s or "").lower())


def _clean_title(title: str) -> str:
    """제목에서 채널·부제·태그 노이즈 제거 → 검색어. '나를 향한 주의 사랑 (official) | 어노인팅…' → '나를 향한 주의 사랑'."""
    t = re.split(r"[|/]", title or "")[0]              # '|','/' 뒤(채널·부제) 버림
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]|【[^】]*】", " ", t)  # (official)·[MV]·【…】 제거
    t = _NOISE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _parse(synced: str | None, plain: str | None, duration: float | None):
    """LRC(싱크) → 시각 있는 segments. 없으면 plain 을 곡 길이에 균등 분배(시각 근사)."""
    if synced:
        segs = []
        for line in synced.splitlines():
            for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", line):  # 한 줄 다중 타임태그 지원
                t = int(m.group(1)) * 60 + float(m.group(2))
                txt = line[m.end():].strip()
                txt = re.sub(r"\[(\d+):(\d+(?:\.\d+)?)\].*", "", txt).strip()  # 뒤 타임태그 제거
                if txt:
                    segs.append({"s": round(t, 2), "text": txt})
        segs.sort(key=lambda x: x["s"])
        # 중복 시각/빈 줄 정리 + 끝시각
        out = []
        for s in segs:
            if out and abs(out[-1]["s"] - s["s"]) < 0.01:
                continue
            out.append(s)
        for i, s in enumerate(out):
            s["e"] = round(out[i + 1]["s"] if i + 1 < len(out) else (duration or s["s"] + 4), 2)
        if len(out) >= 3:
            return {"source": "lrclib", "synced": True, "segments": out}
    if plain:
        lines = [l.strip() for l in plain.splitlines() if l.strip()]
        if len(lines) >= 3 and duration:
            n = len(lines)
            segs = [{"s": round(duration * i / n, 2), "e": round(duration * (i + 1) / n, 2), "text": l}
                    for i, l in enumerate(lines)]
            return {"source": "lrclib", "synced": False, "segments": segs}
    return None


def fetch_lrclib(title: str, artist: str | None, duration: float | None):
    """제목·가수·길이로 LRCLIB 검색 → 길이 맞는 싱크 가사 우선. 못 찾으면 None(→ whisper 폴백)."""
    q = _clean_title(title)
    if not q:
        return None
    queries = []
    if artist:
        queries.append(f"{q} {artist}")
    queries.append(q)
    seen = set()
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        try:
            res = _get("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": query}))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(res, list) or not res:
            continue

        def dur_ok(r):
            d = r.get("duration") or 0
            return (not duration) or abs(d - duration) <= 15  # 길이 15초 이내(같은 곡 다른 버전 배제)

        def title_ok(r):
            # ★제목 일치 게이트 — 오매칭 방지(실증: 'Longview'(그린데이)가 밴드명 Longview 의 'Further'로
            #   잘못 매칭). 틀린 가사는 ASR보다 나쁘다. 정규화 제목이 서로 포함될 때만 수용(보수적).
            nt = _norm(r.get("trackName")); nq = _norm(q)
            return bool(nt) and bool(nq) and (nq in nt or nt in nq)

        # 1순위: 길이+제목 맞는 싱크, 2순위: 제목 맞는 싱크, 3순위: 길이+제목 맞는 plain
        for pick in (lambda r: r.get("syncedLyrics") and dur_ok(r) and title_ok(r),
                     lambda r: r.get("syncedLyrics") and title_ok(r),
                     lambda r: r.get("plainLyrics") and dur_ok(r) and title_ok(r)):
            for r in res:
                if pick(r):
                    parsed = _parse(r.get("syncedLyrics"), r.get("plainLyrics"), duration)
                    if parsed:
                        parsed["matched"] = f"{r.get('trackName')} / {r.get('artistName')}"
                        return parsed
    return None
