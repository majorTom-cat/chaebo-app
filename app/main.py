"""chaebo — 로컬 웹앱 진입점.

불변식(SPEC.manifest): /api/health 유지 · 기본 바인딩 127.0.0.1 · 외부 전송 없음.
스템 서빙은 StaticFiles(HTTP Range 지원) — SP-3 실측: Range 없으면 seek 동결.
"""
import asyncio
import json
import mimetypes
import os
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import config, db, gpu, jobs, migrate, system

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# 참조 없는 create_task 는 CPython 이벤트루프가 약참조만 잡아 실행 중 GC 로 조용히 소멸할 수 있다
# (특히 build_pitch_stems 가 사라지면 폴링이 영원히 ready:false — 에러도 없음, 코드리뷰 2026-07-14).
# set 에 붙잡고 끝나면 뗀다.
_bg_tasks: set = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 데이터를 공유 위치로 모으기(설치마다 곡이 다르던 문제) — DB 를 열기 '전'에 1회. 옛 위치 원본은
    # 보존(복사만). 큰 곡이 많으면 몇 초 걸릴 수 있어 스레드로(이벤트루프 블록 회피). 실패해도 앱은 뜬다.
    try:
        await asyncio.to_thread(migrate.migrate_to_shared)
    except Exception:  # noqa: BLE001
        pass
    await db.init()
    _spawn(system.device())  # 장치 감지 예열(첫 호출 torch 임포트 ~수 초 — cold 페이지가 배지 놓침)
    if os.environ.get("CHAEBO_NO_WORKER") != "1":  # UI 검증용: 잡 워커 없이 화면만
        await jobs.start()
    yield
    await jobs.stop()


app = FastAPI(title="chaebo", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def cache_policy(request: Request, call_next):
    """캐시 정책(실측 2026-07-09: 페이지당 33~40MB 재전송이 체감 느림의 주범).
    스템 오디오 = 곡별 불변 → 장기 캐시 · vendor = 1일 · 앱 UI/API = no-cache(304 재검증으로 옛 UI 방지)."""
    response = await call_next(request)
    p = request.url.path
    if p.startswith("/stems/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif p.startswith("/static/vendor/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    else:
        # 앱 JS/HTML/API 는 절대 캐시 금지(no-store). no-cache(재검증)는 파일 교체가 애매하거나
        # WebView 가 캐시하면 옛 코드가 그대로 뜬다(사용자 실증 2026-07-13: 업그레이드 후 수정 미반영).
        response.headers["Cache-Control"] = "no-store"
    return response
config.ensure_dirs()
# Windows 레지스트리 mimetypes 가 .js/.mjs 를 text/plain 으로 주면 ES 모듈 로드가 거부됨(실증:
# vendor SignalsmithStretch.mjs) — 브라우저는 모듈 스크립트 MIME 를 엄격 검사한다
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
app.mount("/stems", StaticFiles(directory=config.STEMS_DIR), name="stems")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def library_page(request: Request):
    return templates.TemplateResponse(request, "library.html")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html")


@app.get("/api/settings")
async def get_app_settings():
    limits = await db.get_limits()
    sync = await db.get_setting("sync_ms")
    drift = await db.get_setting("sync_drift_ms_per_min")
    sync_ver = await db.get_setting("sync_formula_version")
    st = await system.status()
    sync_val = int(sync) if sync is not None else 0
    drift_val = float(drift) if drift is not None else 0.0
    # 저장된 보정값이 '옛 공식 세대'에 맞춰진 것이면 stale — 프런트가 0 으로 리셋하고 재보정을 안내한다.
    # (값이 0/0 이면 리셋할 게 없으니 stale 아님. 스탬프가 현재와 같으면 이 공식으로 맞춘 값이라 유효.)
    sync_stale = (sync_val != 0 or drift_val != 0) and sync_ver != config.SYNC_FORMULA_VERSION
    return {
        "device": st["device"],
        "nvidia": st["nvidia"],
        "can_enable_gpu": st["can_enable_gpu"],
        "open_mode": _read_open_mode(),
        "data_dir": str(config.DATA_DIR),
        "max_duration_min": limits["max_duration_min"],
        "max_file_mb": limits["max_file_mb"],
        "sync_ms": sync_val,  # 소리-화면 싱크(기기 속성 — 전역)
        "sync_stale": sync_stale,  # True 면 옛 공식 보정 → 프런트가 0 리셋·재보정 안내(사용자 요청 2026-07-13)
        # 곡이 진행될수록 실기기 출력이 선형으로 밀리는 걸 상쇄(ms/분). 측정 근거: 앱 표시시계는
        # 드리프트 0, 밀림은 하드웨어 출력경로 → 고정 offset 으론 못 잡아 '위치 비례' 보정 추가(2026-07-13).
        "sync_drift_ms_per_min": drift_val,
        "version": f"chaebo v{config.APP_VERSION} (로컬 전용)",
    }


class SettingsIn(BaseModel):
    max_duration_min: int | None = None
    max_file_mb: int | None = None
    sync_ms: int | None = None
    sync_drift_ms_per_min: float | None = None


@app.put("/api/settings")
async def put_app_settings(body: SettingsIn):
    if body.max_duration_min is not None:
        if not (1 <= body.max_duration_min <= 180):
            raise HTTPException(422, "곡 길이 한도는 1~180분 사이로 정해주세요")
        await db.set_setting("max_duration_min", body.max_duration_min)
    if body.max_file_mb is not None:
        if not (1 <= body.max_file_mb <= 2000):
            raise HTTPException(422, "파일 크기 한도는 1~2000MB 사이로 정해주세요")
        await db.set_setting("max_file_mb", body.max_file_mb)
    if body.sync_ms is not None:
        # 블루투스 실지연이 300 초과 사례 있음(transport.js SYNC_MAX=1000 과 일치) — 예전 ±300 검증은
        # 클라 값을 조용히 안 저장하던 버그. ±1000 으로 일치.
        if not (-1000 <= body.sync_ms <= 1000):
            raise HTTPException(422, "싱크 보정은 -1000~1000ms 사이로 정해주세요")
        await db.set_setting("sync_ms", body.sync_ms)
    if body.sync_drift_ms_per_min is not None:
        # 블루투스 큰 드리프트 사례(분당 수백 ms) — ±120 은 표현조차 못 했음(사용자 실측 2026-07-13). ±500 으로.
        if not (-500 <= body.sync_drift_ms_per_min <= 500):
            raise HTTPException(422, "밀림 보정은 분당 -500~500ms 사이로 정해주세요")
        await db.set_setting("sync_drift_ms_per_min", body.sync_drift_ms_per_min)
    # 싱크 보정을 쓸 때마다 '지금 공식 세대'를 함께 도장 — 다음 세대에서 이 값이 stale 인지 판정하는 근거.
    # (0 으로 리셋해도 스탬프가 현재가 되어 재알림이 멈춘다 — 프런트의 stale 리셋이 여기로 영구화된다.)
    if body.sync_ms is not None or body.sync_drift_ms_per_min is not None:
        await db.set_setting("sync_formula_version", config.SYNC_FORMULA_VERSION)
    limits = await db.get_limits()
    sync = await db.get_setting("sync_ms")
    drift = await db.get_setting("sync_drift_ms_per_min")
    limits["sync_ms"] = int(sync) if sync is not None else 0
    limits["sync_drift_ms_per_min"] = float(drift) if drift is not None else 0.0
    return limits


@app.get("/licenses", response_class=HTMLResponse)
async def licenses_page():
    """분리 모델·구성요소 라이선스 고지(설정 화면 링크 대상) — docs/licenses.md 원문."""
    text = (config.BASE_DIR / "docs" / "licenses.md").read_text(encoding="utf-8")
    import html as _html
    return HTMLResponse(
        "<meta charset='utf-8'><title>라이선스 고지 — chaebo</title>"
        "<body style='background:#0f1115;color:#e8eaf0;font-family:sans-serif;margin:0;padding:24px'>"
        "<a href='/settings' style='color:#f0a848'>← 설정으로</a>"
        "<pre style='white-space:pre-wrap;font-size:12px;line-height:1.6;max-width:1000px'>"
        + _html.escape(text) + "</pre>")


# 연습 3형제(믹서·타브·코드) = 한 문서 안의 뷰 3개 — 탭 전환이 페이지 리로드(깜빡임·스템 재로드)가
# 되지 않게(사용자 실증 2026-07-10). 주소는 셋 다 유지, 초기 뷰만 다르다.
async def _practice_shell(request: Request, song_id: int, view: str):
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    if song["status"] != "ready":
        return HTMLResponse(
            "<meta charset='utf-8'><body style='font-family:sans-serif;background:#0f1115;"
            "color:#e8eaf0;display:grid;place-items:center;height:100vh;margin:0'>"
            "<p>아직 준비 중인 곡이에요 — <a href='/' style='color:#f0a848'>라이브러리로 돌아가기</a></p>")
    return templates.TemplateResponse(
        request, "practice.html", {"song": _song_view(song), "view": view})


@app.get("/songs/{song_id}/practice", response_class=HTMLResponse)
async def practice_page(request: Request, song_id: int):
    return await _practice_shell(request, song_id, "mixer")


@app.get("/songs/{song_id}/tab", response_class=HTMLResponse)
async def tab_page(request: Request, song_id: int):
    return await _practice_shell(request, song_id, "tab")


class ChordEdit(BaseModel):
    bar: int
    label: str | None = None  # None/빈 문자열 = 수동 수정 해제(자동 추정으로 복귀)


@app.patch("/api/songs/{song_id}/chords")
async def edit_chord(song_id: int, body: ChordEdit):
    """코드 수동 수정(REQ-CHORD-001 잔여 배선, 사용자 요청 2026-07-10) — manual 표시로 영속,
    음 편집에 따른 재계산에서도 보존. 빈 라벨 = 자동 추정 복귀.
    한 마디 두 코드: 라벨을 띄어쓰기/'/'로 구분해 최대 2개(전반·후반) — 예: 'Fm Db'."""
    row = await db.get_transcription(song_id)
    if not row or row["status"] != "ready" or not row.get("notes"):
        raise HTTPException(409, "아직 코드가 없어요 — 먼저 분석해 주세요")
    from app.tab_worker import effective_key, estimate_chords, merge_manual_chords, sanitize_mixed, to_alphatex
    meter = row.get("meter") or "4/4"
    grid_v = row.get("grid_v") or 1
    bar_slots = 48 if (meter == "12/8" or grid_v >= 2) else 16
    chords = json.loads(row.get("chords") or "[]")
    labels = [p for p in re.split(r"[\s/]+", (body.label or "").strip()) if p][:4]
    chords = [c for c in chords if c["bar"] != body.bar]
    if labels:
        # 코드 개수 → 마디 내 위치(코드 차트 관례): 1개=마디 / 2개=반씩 / 3개=반+3·4박 / 4개=박마다
        q = bar_slots // 4
        pos_by_count = {1: [0], 2: [0, 2 * q], 3: [0, 2 * q, 3 * q], 4: [0, q, 2 * q, 3 * q]}
        for pos, lab in zip(pos_by_count[len(labels)], labels):
            chords.append({"bar": body.bar, "pos": pos, "label": lab[:12], "manual": True})
    else:
        # 자동 복귀 — 노트에서 재추정한 값(있으면)을 이 마디에 채움(분할 항목 포함)
        notes = json.loads(row["notes"])
        if grid_v >= 2 and meter != "12/8":
            notes, _ = sanitize_mixed(notes)
        auto = estimate_chords(notes, effective_key(notes, row.get("key_override")),
                               bar_slots=bar_slots)
        chords.extend(c for c in auto if c["bar"] == body.bar)
    chords.sort(key=lambda c: (c["bar"], c.get("pos", 0)))
    # 악보 위 코드 심볼도 즉시 반영 — tex 재생성
    song = await db.get_song(song_id)
    notes2 = json.loads(row["notes"])
    meter2 = row.get("meter") or "4/4"
    grid_v2 = row.get("grid_v") or 1
    families = None
    if grid_v2 >= 2 and meter2 != "12/8":
        notes2, families = sanitize_mixed(notes2)
    key = effective_key(notes2, row.get("key_override"))
    tex = to_alphatex(notes2, row["bpm"], song["title"][:80], key=key, chords=chords,
                      meter=meter2, families=families)
    await db.upsert_transcription(
        song_id, chords=json.dumps(chords, ensure_ascii=False), tex=tex)
    return {"ok": True, "chords": chords}


def _clean_lookup_title(title: str) -> str:
    """유튜브·파일명식 제목 정돈 — 괄호 덩어리·흔한 꼬리표 제거, _-를 공백으로(조회 적중률.
    실증: 'another_one_bites_the_dust' 는 언더스코어 그대로면 검색 0건)."""
    t = re.sub(r"[\[(（【].*?[\])）】]", " ", title)
    t = re.sub(r"(?i)\b(official|mv|m/v|music video|audio|lyrics?|live|가사|공식|뮤직비디오|라이브)\b", " ", t)
    t = re.sub(r"[_\-–—]+", " ", t)
    return re.sub(r"\s+", " ", t).strip() or title


def _rank_deezer_matches(items: list, title: str, duration: float | None,
                         artist: str | None = None) -> list:
    """검색 결과 랭킹 — 길이 근접(±5초 강한 신호) + 제목 토큰 + 아티스트 토큰(커버 배제 실증
    2026-07-10: Longview 가 Shuttlecraft 커버로 매칭). 문턱 미달은 버림."""
    def score(it):
        s = 0.0
        if duration and it.get("duration"):
            diff = abs(float(it["duration"]) - duration)
            s += 3.0 if diff <= 5 else (1.0 if diff <= 15 else -1.0)
        want = set(_clean_lookup_title(title).lower().split())
        got = set(str(it.get("title", "")).lower().split())
        if want:
            s += 2.0 * len(want & got) / len(want)
        if artist:
            wa = set(artist.lower().split())
            ga = set(str((it.get("artist") or {}).get("name", "")).lower().split())
            if wa and (wa | ga):
                # 대칭 겹침(합집합 분모) — 'Queen' 질의에 'Queen Factory'가 만점 받던 것 교정
                s += 2.5 * len(wa & ga) / len(wa | ga)
        return s
    ranked = sorted(items, key=score, reverse=True)
    return [it for it in ranked if score(it) > 0.5]


@app.get("/api/songs/{song_id}/lookup")
async def lookup_song_meta(song_id: int):
    """인터넷 참고(사용자 승인 2026-07-10) — Deezer 공개 API(키 불필요)로 BPM 대조.
    전송되는 건 곡 제목·아티스트뿐(개인정보 없음). 실패해도 로컬 기능엔 영향 없음."""
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    import urllib.parse
    import urllib.request

    def fetch_json(url: str):
        req = urllib.request.Request(url, headers={"User-Agent": "chaebo/0.1 (personal)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))

    q_title = _clean_lookup_title(song["title"])
    artist = (song.get("artist") or "").strip() or None
    # 필드 검색이 커버·리믹스를 크게 걸러줌(실측) — 실패 시 일반 검색 폴백
    queries = ([f'artist:"{artist}" track:"{q_title}"', f"{artist} {q_title}"]
               if artist else [q_title])
    try:
        items = []
        for q in queries:
            data = await asyncio.to_thread(
                fetch_json, "https://api.deezer.com/search?limit=8&q=" + urllib.parse.quote(q))
            items = _rank_deezer_matches(data.get("data") or [], song["title"],
                                         song.get("duration"), artist)
            if items:
                break
        if not items:
            return {"found": False,
                    "reason": "인터넷에서 이 곡을 찾지 못했어요 — 아티스트를 채우면 잘 찾아요"}
        # BPM 은 트랙 상세에만 있고 비어 있는 곡도 많다 — 상위 3개 중 BPM 있는 것을 우선
        first = None
        for it in items[:3]:
            track = await asyncio.to_thread(
                fetch_json, f'https://api.deezer.com/track/{it["id"]}')
            info = {
                "found": True,
                "bpm": float(track.get("bpm") or 0) or None,
                "title": track.get("title"),
                "artist": (track.get("artist") or {}).get("name"),
                "duration": track.get("duration"),
                "source": "deezer",
            }
            if info["bpm"]:
                return info
            first = first or info
        return first
    except Exception:  # noqa: BLE001 — 네트워크 실패는 기능 아님을 안내
        return {"found": False, "reason": "인터넷 조회에 실패했어요 — 연결을 확인하고 다시 시도해주세요"}


@app.post("/api/songs/{song_id}/lyrics", status_code=202)
async def start_lyrics(song_id: int):
    """가사 받아쓰기 시작(수동/기존 곡 백필) — 보컬 스템에 whisper(로컬, 곡당 수십 초)."""
    song = await db.get_song(song_id)
    if not song or song["status"] != "ready":
        raise HTTPException(409, "먼저 곡 분리가 끝나야 가사를 받아쓸 수 있어요")
    await db.upsert_transcription(
        song_id, lyrics=json.dumps({"status": "queued"}, ensure_ascii=False))
    await jobs.queue.put(("lyrics", song_id))
    return {"ok": True}


@app.post("/api/songs/{song_id}/sections", status_code=202)
async def start_sections(song_id: int):
    """곡 구간 감지 시작(수동/백필) — 경계·반복 그룹·보컬 힌트(라벨은 사용자가 직접)."""
    song = await db.get_song(song_id)
    if not song or song["status"] != "ready":
        raise HTTPException(409, "먼저 곡 분리가 끝나야 구간을 나눌 수 있어요")
    await db.upsert_transcription(
        song_id, sections=json.dumps({"status": "queued"}, ensure_ascii=False))
    await jobs.queue.put(("sections", song_id))
    return {"ok": True}


class SectionEdit(BaseModel):
    index: int
    name: str | None = None


@app.patch("/api/songs/{song_id}/sections")
async def edit_section(song_id: int, body: SectionEdit):
    """구간 이름 수정 — 자동은 '구간 N'뿐(정직: 코러스 등 기능 라벨은 사람 몫)."""
    row = await db.get_transcription(song_id)
    data = json.loads(row.get("sections") or "{}") if row else {}
    secs = data.get("sections")
    if not secs or not (0 <= body.index < len(secs)):
        raise HTTPException(409, "고칠 구간을 찾지 못했어요")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "구간 이름을 비울 수는 없어요")
    secs[body.index]["name"] = name[:40]
    secs[body.index]["manual"] = True
    await db.upsert_transcription(song_id, sections=json.dumps(data, ensure_ascii=False))
    return {"ok": True, "sections": data}


class LyricEdit(BaseModel):
    index: int              # segments 배열 인덱스
    text: str | None = None  # 빈 문자열/None = 그 소절 삭제


@app.patch("/api/songs/{song_id}/lyrics")
async def edit_lyric(song_id: int, body: LyricEdit):
    """가사 소절 수정 — 받아쓰기는 추정이므로 수동 수정 경로 필수(정직 UI 원칙)."""
    row = await db.get_transcription(song_id)
    data = json.loads(row.get("lyrics") or "{}") if row else {}
    segs = data.get("segments")
    if not segs or not (0 <= body.index < len(segs)):
        raise HTTPException(409, "고칠 가사 소절을 찾지 못했어요")
    text = (body.text or "").strip()
    if text:
        segs[body.index]["text"] = text[:200]
        segs[body.index]["manual"] = True
        # 문장이 바뀌면 단어별 시각은 낡은 정보 — 지워서 균등 폴백 배치로(정직)
        segs[body.index].pop("words", None)
    else:
        segs.pop(body.index)
    await db.upsert_transcription(song_id, lyrics=json.dumps(data, ensure_ascii=False))
    return {"ok": True, "lyrics": data}


class KeyEdit(BaseModel):
    label: str | None = None  # 'F#m'·'Bb' 등 — None/빈 문자열 = 자동 추정 복귀


@app.patch("/api/songs/{song_id}/key")
async def edit_key(song_id: int, body: KeyEdit):
    """키 직접 입력(사용자 요청 2026-07-10) — 추정이 틀린 곡의 교정. 코드(다이어토닉 표)·조표·부제가
    키에 걸려 있으므로 함께 재계산(수동 코드는 보존). 재분석에도 유지(CHAEBO_KEY)."""
    row = await db.get_transcription(song_id)
    if not row or row["status"] != "ready" or not row.get("notes"):
        raise HTTPException(409, "아직 분석 결과가 없어요 — 먼저 분석해 주세요")
    from app.tab_worker import (effective_key, estimate_chords, merge_manual_chords,
                                parse_key_label, sanitize_mixed, to_alphatex)
    label = (body.label or "").strip()
    if label and not parse_key_label(label):
        raise HTTPException(422, "키 표기를 읽지 못했어요 — 예: A, F#m, Bb")
    override = parse_key_label(label)["label"] if label else None
    notes = json.loads(row["notes"])
    meter = row.get("meter") or "4/4"
    grid_v = row.get("grid_v") or 1
    bar_slots = 48 if (meter == "12/8" or grid_v >= 2) else 16
    families = None
    if grid_v >= 2 and meter != "12/8":
        notes, families = sanitize_mixed(notes)
    key = effective_key(notes, override)
    chords = merge_manual_chords(
        estimate_chords(notes, key, bar_slots=bar_slots),
        json.loads(row.get("chords") or "[]"))
    song = await db.get_song(song_id)
    tex = to_alphatex(notes, row["bpm"], song["title"][:80], key=key, chords=chords,
                      meter=meter, families=families)
    await db.upsert_transcription(
        song_id, key_override=override,
        key_json=json.dumps(key, ensure_ascii=False),
        chords=json.dumps(chords, ensure_ascii=False), tex=tex)
    return {"ok": True, "key": key}


@app.get("/songs/{song_id}/chords", response_class=HTMLResponse)
async def chord_sheet_page(request: Request, song_id: int):
    """코드 악보 — 믹서·타브와 같은 문서의 3번째 뷰(사용자 요청 2026-07-10)."""
    return await _practice_shell(request, song_id, "chords")


@app.get("/songs/{song_id}/chords/print", response_class=HTMLResponse)
async def chord_sheet_print(request: Request, song_id: int):
    """코드 악보 인쇄 전용(새 탭) — 흰 종이 위 격자만."""
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    return templates.TemplateResponse(request, "tab_chords.html", {"song": _song_view(song)})


@app.get("/songs/{song_id}/tab/print", response_class=HTMLResponse)
async def tab_print_page(request: Request, song_id: int):
    """인쇄/PDF 전용 페이지(새 탭) — 악보만 일반 문서 흐름으로 렌더해 여러 장 분할이 자연스럽다.
    (본 화면에서 @media print 로 가리는 방식은 첫 줄만 인쇄되는 클리핑 실증 — 폐기)"""
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    return templates.TemplateResponse(request, "tab_print.html", {"song": _song_view(song)})


class TabStart(BaseModel):
    meter: str | None = None  # '4/4'|'12/8'|'auto' — 자동 박자 판정이 틀렸을 때 수동 고정
    sensitivity: str | None = None  # 'normal'|'simple' — 밀집 믹스에서 과밀 타브 억제
    tempo: str | None = None  # 'auto'|'half'|'double' — 템포 배수 오검출 수동 교정
    mode: str | None = None  # 'tiny'(빠름·기본)|'full'(정확·느림) — '정확하게 다시 분석'
    lead_snap: bool | None = None  # 첫 음 정박 스냅 on/off(기본 켬) — 사용자 귀 검증용 토글


@app.post("/api/songs/{song_id}/tab", status_code=202)
async def start_tab(song_id: int, body: TabStart | None = None):
    song = await db.get_song(song_id)
    if not song or song["status"] != "ready":
        raise HTTPException(409, "먼저 곡 분리가 끝나야 타브를 만들 수 있어요")
    fields = dict(status="queued", progress=0, error=None)
    if body and body.meter:
        if body.meter not in ("4/4", "12/8", "auto"):
            raise HTTPException(422, "박자는 4/4, 12/8, auto 중 하나로 정해주세요")
        fields["meter_override"] = None if body.meter == "auto" else body.meter
    if body and body.sensitivity:
        if body.sensitivity not in ("normal", "simple"):
            raise HTTPException(422, "분석 감도는 normal 또는 simple 로 정해주세요")
        fields["sensitivity"] = body.sensitivity
    if body and body.tempo:
        if body.tempo not in ("auto", "half", "double"):
            raise HTTPException(422, "빠르기는 auto, half, double 중 하나로 정해주세요")
        fields["tempo_override"] = None if body.tempo == "auto" else body.tempo
    if body and body.mode:
        if body.mode not in ("tiny", "full", "auto"):
            raise HTTPException(422, "분석 정밀도는 tiny 또는 full 로 정해주세요")
        # tiny/auto = 기본(빠름) = NULL, full = 정확(느림). 버튼이 매번 명시해 모드가 고정된다.
        fields["crepe_mode"] = "full" if body.mode == "full" else None
    if body and body.lead_snap is not None:
        fields["lead_snap"] = 1 if body.lead_snap else 0  # 체크박스 값 고정(끄면 0, 켜면 1)
    await db.upsert_transcription(song_id, **fields)
    await jobs.queue.put(("tab", song_id))
    return await db.get_transcription(song_id)


@app.get("/api/songs/{song_id}/tab")
async def get_tab(song_id: int):
    row = await db.get_transcription(song_id)
    if not row:
        return {"status": "none"}
    # 구버전 분석 결과 자동 업그레이드: 저장된 노트에서 키·코드·조표 재계산(재분석 없음 — ms 단위)
    needs_upgrade = row["status"] == "ready" and row.get("notes") and not row.get("key_json")
    if not needs_upgrade and row["status"] == "ready" and row.get("notes") and row.get("key_json"):
        # 철자 백필: 플랫 조성인데 샵 표기로 저장된 구버전(예: Fm 곡의 A#m → Bbm)
        from app.tab_worker import prefers_flats
        if prefers_flats(json.loads(row["key_json"])) and "#" in (row.get("chords") or "") + row["key_json"]:
            needs_upgrade = True
    if needs_upgrade:
        from app.tab_worker import effective_key, estimate_chords, to_alphatex
        notes = json.loads(row["notes"])
        meter = row.get("meter") or "4/4"
        key = effective_key(notes, row.get("key_override"))
        # bar_slots 에 grid_v 반영(divergence #2 교정 2026-07-14) — 편집 경로들은 grid_v>=2 면 48 을 쓰는데
        # 이 구버전 업그레이드만 16 을 써서 grid_v2 곡의 코드가 편집 후와 달라졌다. 일치시킨다.
        chords = estimate_chords(
            notes, key, bar_slots=48 if (meter == "12/8" or (row.get("grid_v") or 1) >= 2) else 16)
        song = await db.get_song(song_id)
        tex = to_alphatex(notes, row["bpm"], song["title"][:80], key=key, chords=chords, meter=meter)
        fields = dict(tex=tex,
                      key_json=json.dumps(key, ensure_ascii=False),
                      chords=json.dumps(chords, ensure_ascii=False))
        if row.get("beat_offset") is None:  # 구버전 백필 — 분석 산출 파일에 남아 있음
            tab_file = jobs.stems_dir(song_id) / "tab.json"
            if tab_file.exists():
                fields["beat_offset"] = json.loads(
                    tab_file.read_text(encoding="utf-8")).get("offset") or 0
        await db.upsert_transcription(song_id, **fields)
        row = await db.get_transcription(song_id)
    for field in ("notes", "key_json", "chords", "slots", "lyrics", "sections"):
        if row.get(field):
            row[field] = json.loads(row[field])
    row["offset"] = row.get("beat_offset") or 0  # 클라이언트 계약명
    row["meter"] = row.get("meter") or "4/4"
    row["grid_v"] = row.get("grid_v") or 1
    row["bar_slots"] = 48 if (row["meter"] == "12/8" or row["grid_v"] >= 2) else 16
    return row


class TabShift(BaseModel):
    slots: int  # ±1 — 16분음표 한 칸


# 곡별 편집 잠금 — shift/put 은 읽기-수정-쓰기라 연타·동시 저장이 서로를 덮음(적대 리뷰 확정)
_tab_edit_locks: dict[int, asyncio.Lock] = {}


def _tab_lock(song_id: int) -> asyncio.Lock:
    return _tab_edit_locks.setdefault(song_id, asyncio.Lock())


@app.post("/api/songs/{song_id}/tab/shift")
async def shift_tab_phase(song_id: int, body: TabShift):
    """박자 시작점 이동 — 1에서 시작하지 않는 곡의 수동 보정(재분석 없이 gi 정수 이동)."""
    async with _tab_lock(song_id):
        return await _shift_tab_phase(song_id, body)


def _tex_from_notes(row, song, notes, key_override, existing_chords, bar_slots, families):
    """채보 tex 재생성의 '공통 꼬리' — 이 3줄(키·코드·tex)이 여러 편집 경로에 복붙돼 드리프트 위험이었다
    (코드리뷰 2026-07-14). 각 호출부는 자신의 families/겹침정규화·bar_slots·수동코드를 계산해 넘긴다.
    ★주의(리뷰 발견): 앞단(정규화·bar_slots)은 경로마다 미세하게 다르다 — 여기서 통일하지 않는다."""
    from app.tab_worker import effective_key, estimate_chords, merge_manual_chords, to_alphatex
    key = effective_key(notes, key_override)
    chords = merge_manual_chords(estimate_chords(notes, key, bar_slots=bar_slots), existing_chords)
    tex = to_alphatex(notes, row["bpm"], song["title"][:80], key=key, chords=chords,
                      meter=row.get("meter") or "4/4", families=families)
    return key, chords, tex


async def _shift_tab_phase(song_id: int, body: TabShift):
    row = await db.get_transcription(song_id)
    if not row or row["status"] != "ready":
        raise HTTPException(409, "타브 초안이 아직 없어요")
    if body.slots not in (-1, 1):
        raise HTTPException(422, "한 번에 한 칸씩만 옮길 수 있어요")
    notes = json.loads(row["notes"])
    # '한 칸' = 16분 상당: v2(박당 12칸) 4/4 는 3칸, 12/8·구그리드는 1칸
    unit = 3 if ((row.get("grid_v") or 1) >= 2 and (row.get("meter") or "4/4") != "12/8") else 1
    step = body.slots * unit
    if step < 0 and any(nt["gi"] + step < 0 for nt in notes):
        raise HTTPException(409, "더 이상 앞으로 옮길 수 없어요")
    grid = 60.0 / row["bpm"] / 4
    slots = json.loads(row["slots"]) if row.get("slots") else None
    if slots:
        # 동적 그리드: 기준 이동 = 슬롯 배열을 밀거나 앞에 외삽 추가
        for _ in range(abs(step)):
            if step > 0:
                slots = slots[1:]
            else:
                slots = [round(2 * slots[0] - slots[1], 3)] + slots
        new_offset = slots[0]
        for nt in notes:
            nt["gi"] += step
            nt["start"] = slots[nt["gi"]] if nt["gi"] < len(slots) else nt["start"]
    else:
        new_offset = (row.get("beat_offset") or 0) - step * grid
        for nt in notes:
            nt["gi"] += step
            nt["start"] = round(new_offset + nt["gi"] * grid, 3)
    song = await db.get_song(song_id)
    from app.tab_worker import sanitize_mixed
    meter = row.get("meter") or "4/4"
    grid_v = row.get("grid_v") or 1
    bar_slots = 48 if (meter == "12/8" or grid_v >= 2) else 16
    families = None
    if grid_v >= 2 and meter != "12/8":
        # shift(±3칸)가 셋잇단 오프셋을 불법 위치(7·11)로 보내 조판에서 증발(적대 리뷰 확정) — 위생 스냅
        notes, families = sanitize_mixed(notes)
    else:
        # ★_put_tab_notes 엔 있는데 여기엔 없던 겹침 정규화(divergence #1 교정 2026-07-14): 지속이 다음 음
        # 시작을 넘으면 조판기가 그 음을 스킵(증발). 겹침 없으면 무변화(no-op)라 안전. 두 경로 일치.
        for cur, nxt in zip(notes, notes[1:]):
            if cur["glen"] > nxt["gi"] - cur["gi"]:
                cur["glen"] = max(1, nxt["gi"] - cur["gi"])
    key, chords, tex = _tex_from_notes(
        row, song, notes, row.get("key_override"), json.loads(row.get("chords") or "[]"), bar_slots, families)
    await db.upsert_transcription(
        song_id, notes=json.dumps(notes, ensure_ascii=False), tex=tex,
        beat_offset=new_offset,
        key_json=json.dumps(key, ensure_ascii=False),
        chords=json.dumps(chords, ensure_ascii=False),
        slots=json.dumps(slots, ensure_ascii=False) if slots else None)
    return {"ok": True}


class TabNotes(BaseModel):
    notes: list


@app.put("/api/songs/{song_id}/tab/notes")
async def put_tab_notes(song_id: int, body: TabNotes):
    """보정 저장(REQ-TAB-003) — 수정 노트로 tex 재생성 후 영속."""
    async with _tab_lock(song_id):
        return await _put_tab_notes(song_id, body)


async def _put_tab_notes(song_id: int, body: TabNotes):
    row = await db.get_transcription(song_id)
    if not row or row["status"] != "ready":
        raise HTTPException(409, "타브 초안이 아직 없어요")
    song = await db.get_song(song_id)
    from app.tab_worker import sanitize_mixed
    notes = sorted(body.notes, key=lambda n: n["gi"])
    meter = row.get("meter") or "4/4"
    grid_v = row.get("grid_v") or 1
    bar_slots = 48 if (meter == "12/8" or grid_v >= 2) else 16
    families = None
    if grid_v >= 2 and meter != "12/8":
        # 48그리드 위생: 어긋난 오프셋 스냅 + 가족 판정 + 지속 겹침 자름(음 증발 계열 결함의 근본 방지)
        notes, families = sanitize_mixed(notes)
    else:
        # 지속 겹침 정규화 — 이동·추가로 음이 앞 음의 지속 안에 들어가면 조판기가 스킵(실증)
        for cur, nxt in zip(notes, notes[1:]):
            if cur["glen"] > nxt["gi"] - cur["gi"]:
                cur["glen"] = max(1, nxt["gi"] - cur["gi"])
    key, chords, tex = _tex_from_notes(  # 키·코드·tex 공통 꼬리(보정 쌓이면 키·코드도 재계산, 수동코드 보존)
        row, song, notes, row.get("key_override"), json.loads(row.get("chords") or "[]"), bar_slots, families)
    await db.upsert_transcription(
        song_id, notes=json.dumps(notes, ensure_ascii=False), tex=tex,
        key_json=json.dumps(key, ensure_ascii=False),
        chords=json.dumps(chords, ensure_ascii=False))
    return {"ok": True, "tex": tex, "key": key, "chords": chords, "notes": notes}


class PitchBody(BaseModel):
    semitones: int


def _pitch_stem_urls(song_id: int, semitones: int) -> dict:
    if semitones == 0:
        song = {"id": song_id, "status": "ready"}
        return _song_view(song)["stems"]
    return {s: f"/stems/{song_id}/shift_{semitones}/{s}.m4a" for s in config.STEMS}


@app.post("/api/songs/{song_id}/pitch")
async def build_pitch(song_id: int, body: PitchBody):
    """키(피치) 재생 준비(REQ-PLAY-009) — 반음 시프트 스템 생성(캐시). 폴링으로 완료 확인."""
    song = await db.get_song(song_id)
    if not song or song["status"] != "ready":
        raise HTTPException(409, "곡 분리가 끝나야 키를 바꿀 수 있어요")
    if not (-12 <= body.semitones <= 12):
        raise HTTPException(422, "키는 -12~+12 반음 사이로 정해주세요")
    if body.semitones == 0 or jobs.pitch_ready(song_id, body.semitones):
        return {"ready": True, "stems": _pitch_stem_urls(song_id, body.semitones)}
    _spawn(jobs.build_pitch_stems(song_id, body.semitones))
    return {"ready": False}


@app.get("/api/songs/{song_id}/pitch")
async def pitch_status(song_id: int, semitones: int):
    if semitones == 0 or jobs.pitch_ready(song_id, semitones):
        return {"ready": True, "stems": _pitch_stem_urls(song_id, semitones)}
    err = jobs.pitch_errors.get((song_id, semitones))
    return {"ready": False, "error": err} if err else {"ready": False}


@app.get("/api/songs/{song_id}/peaks")
async def song_peaks(song_id: int, request: Request):
    song = await db.get_song(song_id)
    if not song or song["status"] != "ready":
        raise HTTPException(404, "아직 파형을 만들 수 없어요")
    from fastapi.responses import Response

    from app import peaks
    # 사전압축 gzip + 수동 304 — FileResponse 는 ETag 만 붙이고 조건 검사를 안 해 매번 전송됐음(실측).
    # 효과: 첫 방문 7.6MB→~1.2MB, 재방문 0바이트(304).
    gz = await asyncio.get_event_loop().run_in_executor(None, peaks.ensure_gz, song_id)
    stat = gz.stat()
    etag = f'W/"{int(stat.st_mtime)}-{stat.st_size}"'
    common = {"ETag": etag, "Vary": "Accept-Encoding"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=common)
    body = await asyncio.get_event_loop().run_in_executor(None, gz.read_bytes)
    return Response(body, media_type="application/json",
                    headers={**common, "Content-Encoding": "gzip"})


@app.get("/api/songs/{song_id}/state")
async def get_practice_state(song_id: int):
    raw = await db.get_state(song_id)
    return json.loads(raw) if raw else {}


@app.put("/api/songs/{song_id}/state")
async def put_practice_state(song_id: int, request: Request):
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    body = await request.json()
    await db.put_state(song_id, json.dumps(body, ensure_ascii=False))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/diag-log")
def diag_log():
    """진단용(사용자 요청 2026-07-14: 오류를 복사해 보낼 수 있게). 실행 중 버전 + chaebo-log.txt 끝부분을
    돌려준다. 실기기 런타임 버그(최소화 크래시·업데이트 멈춤 등)는 개발 PC(헤드리스)에서 재현이 안 돼
    이 로그가 유일한 단서다. 외부 전송 없음(로컬 조회) — 사용자가 직접 복사해 붙여넣는다."""
    text = ""
    try:
        lp = config.BASE_DIR / "chaebo-log.txt"
        if lp.exists():
            text = lp.read_text(encoding="utf-8", errors="replace")[-8000:]  # 마지막 8KB
        else:
            text = "(로그 파일이 아직 없어요 — 앱 창으로 실행할 때만 생겨요)"
    except Exception as e:  # noqa: BLE001
        text = f"(로그를 읽지 못했어요: {e})"
    return {"version": config.APP_VERSION, "log": text}


@app.get("/api/system")
async def system_info():
    # 정직 표기용 종합 상태: device(가속 켜졌나)·nvidia(하드웨어 있나)·can_enable_gpu.
    return await system.status()


# 열기 방식(앱 창/웹브라우저) — 설치 시 선택값이 파일에 저장되고, 여기서 바꾼다. run.py 가 인자
# 없이 실행될 때 이 파일을 읽는다({app}\open_mode.txt). 설치 후에도 방식 전환 가능(사용자 지적).
_MODE_FILE = config.BASE_DIR / "open_mode.txt"


def _read_open_mode() -> str:
    try:
        m = _MODE_FILE.read_text(encoding="utf-8").strip().lower()
        return "web" if m == "web" else "app"
    except Exception:  # noqa: BLE001
        return "app"


class OpenModeIn(BaseModel):
    mode: str


@app.post("/api/open-mode")
async def set_open_mode(body: OpenModeIn):
    mode = "web" if body.mode == "web" else "app"
    try:
        _MODE_FILE.write_text(mode, encoding="utf-8")
        return {"ok": True, "mode": mode}
    except Exception:  # noqa: BLE001
        return {"ok": False, "mode": _read_open_mode()}


@app.get("/api/gpu/progress")
async def gpu_progress():
    return gpu.state()


@app.post("/api/gpu/enable")
async def gpu_enable():
    st = await system.status()
    if not st.get("can_enable_gpu"):
        # NVIDIA 가 없거나 이미 GPU 모드 — 켤 게 없음
        return {"ok": False, "reason": "not_applicable",
                "message": "이 컴퓨터에서는 GPU 가속을 켤 수 없어요(NVIDIA 그래픽카드가 필요해요)."}
    # 분리/다운로드가 도는 중이면 torch 파일이 잠겨 교체가 실패할 수 있어 거부(정직 안내)
    active = [s for s in await db.list_songs()
              if s["status"] in ("downloading", "separating", "queued")]
    if active:
        return {"ok": False, "reason": "busy",
                "message": "지금 분리 중인 곡이 있어요 — 끝난 뒤에 다시 눌러 주세요."}
    started = gpu.start()
    return {"ok": started, "message": "GPU 가속을 켜는 중이에요…" if started else "이미 켜는 중이에요"}


class AddByUrl(BaseModel):
    url: str


def _song_view(song: dict) -> dict:
    if song["status"] == "ready":
        song["stems"] = {}
        for s in config.STEMS:
            # 압축 재생본(m4a) 우선 — 없으면 wav 폴백(백필 전 구간·ffmpeg 부재)
            ext = "m4a" if (config.STEMS_DIR / str(song["id"]) / f"{s}.m4a").exists() else "wav"
            song["stems"][s] = f"/stems/{song['id']}/{s}.{ext}"
    return song


class SongMeta(BaseModel):
    title: str | None = None
    description: str | None = None
    artist: str | None = None


@app.patch("/api/songs/{song_id}")
async def update_song_meta(song_id: int, body: SongMeta):
    """제목·설명·아티스트 수정(사용자 요청 2026-07-10) — 파일명/유튜브 제목 그대로는 불편.
    아티스트는 다운로드 때 추정(yt-dlp 메타)하고 여기로 직접 고친다."""
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    fields = {}
    if body.title is not None:
        t = body.title.strip()
        if not t:
            raise HTTPException(422, "제목을 비울 수는 없어요")
        fields["title"] = t[:200]
    if body.description is not None:
        fields["description"] = body.description.strip()[:500]
    if body.artist is not None:
        fields["artist"] = body.artist.strip()[:200]
    if fields:
        await db.update_song(song_id, **fields)
    # 제목이 바뀌면 악보(\title)도 함께 — 다음 편집까지 옛 제목이 박혀 있는 어긋남 방지
    if "title" in fields:
        row = await db.get_transcription(song_id)
        if row and row["status"] == "ready" and row.get("notes"):
            from app.tab_worker import (effective_key, estimate_chords, merge_manual_chords,
                                        sanitize_mixed, to_alphatex)
            notes = json.loads(row["notes"])
            meter = row.get("meter") or "4/4"
            grid_v = row.get("grid_v") or 1
            bar_slots = 48 if (meter == "12/8" or grid_v >= 2) else 16
            families = None
            if grid_v >= 2 and meter != "12/8":
                notes, families = sanitize_mixed(notes)
            key = effective_key(notes, row.get("key_override"))
            chords = merge_manual_chords(
                estimate_chords(notes, key, bar_slots=bar_slots),
                json.loads(row.get("chords") or "[]"))
            tex = to_alphatex(notes, row["bpm"], fields["title"][:80], key=key, chords=chords,
                              meter=meter, families=families)
            await db.upsert_transcription(song_id, tex=tex)
    return await db.get_song(song_id)


@app.post("/api/songs", status_code=201)
async def add_song_by_url(body: AddByUrl):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, "올바른 주소가 아니에요. 유튜브 링크를 붙여넣어 주세요")
    song_id = await db.create_song(title=url, source_type="youtube", source=url)
    await jobs.queue.put(song_id)
    return await db.get_song(song_id)


@app.post("/api/songs/upload", status_code=201)
async def add_song_by_file(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTS:
        raise HTTPException(422, f"지원하지 않는 파일 형식이에요({ext or '확장자 없음'}). mp3, wav, flac, m4a 만 올릴 수 있어요")
    song_id = await db.create_song(
        title=Path(file.filename).stem[:200], source_type="file", source=file.filename)
    dest = config.RAW_DIR / f"{song_id}{ext}"
    size = 0
    limits = await db.get_limits()
    limit = limits["max_file_mb"] * 1024 * 1024
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                await db.delete_song(song_id)
                raise HTTPException(413, f"파일이 너무 커요. {limits['max_file_mb']}MB 이하만 올릴 수 있어요")
            f.write(chunk)
    await jobs.queue.put(song_id)
    return await db.get_song(song_id)


@app.get("/api/songs")
async def list_songs():
    return [_song_view(s) for s in await db.list_songs()]


@app.get("/api/songs/{song_id}")
async def get_song(song_id: int):
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    return _song_view(song)


@app.post("/api/songs/{song_id}/retry")
async def retry_song(song_id: int):
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    if song["status"] not in ("error", "stopped"):
        raise HTTPException(409, "실패했거나 멈춘 곡만 다시 분석할 수 있어요")
    await db.update_song(song_id, status="queued", progress=0, error=None)
    await jobs.queue.put(song_id)
    return await db.get_song(song_id)


@app.post("/api/songs/{song_id}/cancel")
async def cancel_song(song_id: int):
    """분석(다운로드/분리)을 멈추고 '멈춤' 상태로 둔다(사용자 요청) — 다시 분석/삭제 선택 가능."""
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    if song["status"] not in ("queued", "downloading", "separating"):
        raise HTTPException(409, "지금은 멈출 수 있는 상태가 아니에요")
    await jobs.cancel(song_id)
    return await db.get_song(song_id)


@app.delete("/api/songs/{song_id}", status_code=204)
async def delete_song(song_id: int):
    song = await db.get_song(song_id)
    if not song:
        raise HTTPException(404, "곡을 찾을 수 없어요")
    raw = jobs.raw_path(song_id)
    if raw:
        raw.unlink(missing_ok=True)
    shutil.rmtree(jobs.stems_dir(song_id), ignore_errors=True)
    await db.delete_song(song_id)


def _ver_gt(a: str, b: str) -> bool:
    """a > b (예: '0.6.10' > '0.6.9') — 숫자 파트만 비교."""
    def t(v):
        return tuple(int(p) for p in str(v or "0").split(".")[:4] if p.isdigit())
    return t(a) > t(b)


@app.get("/api/update-check")
async def update_check():
    """공개 version.json 을 읽어 새 버전 여부 안내(사용자 요청 2026-07-13). URL 미설정이면 꺼짐.
    보내는 사용자 데이터 없음(GET). 조회 실패는 기능 저하 없이 무시(로컬 우선)."""
    if not config.UPDATE_INFO_URL:
        return {"enabled": False, "current": config.APP_VERSION}
    import urllib.request
    try:
        req = urllib.request.Request(config.UPDATE_INFO_URL, headers={"User-Agent": "chaebo"})
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=6).read())
        info = json.loads(raw)
        latest = str(info.get("version") or "").strip()
        return {"enabled": True, "current": config.APP_VERSION, "latest": latest,
                "newer": bool(latest) and _ver_gt(latest, config.APP_VERSION),
                "url": info.get("url"), "notes": info.get("notes")}
    except Exception:  # noqa: BLE001
        return {"enabled": True, "current": config.APP_VERSION, "error": True}


@app.post("/api/apply-update")
async def apply_update():
    """빠른 업데이트(사용자 요청 2026-07-13) — version.json 의 app_zip(프로그램 파일만)을 받아 app/·run.py
    만 교체한다. 엔진·파이썬·GPU torch·AI 모델·곡 데이터는 안 건드림(그래서 몇 MB·몇 초). 실패는 안전하게
    중단(설치 안 망가짐) → 사용자는 '받으러 가기'(전체 설치)로 폴백. 완료 후 앱 재시작해야 새 코드가 뜬다."""
    if not config.UPDATE_INFO_URL:
        raise HTTPException(400, "업데이트 설정이 없어요")
    import shutil
    import tempfile
    import urllib.request
    import zipfile

    def _work():
        info = json.loads(urllib.request.urlopen(
            urllib.request.Request(config.UPDATE_INFO_URL, headers={"User-Agent": "chaebo"}),
            timeout=10).read())
        zip_url = info.get("app_zip")
        if not zip_url:
            raise RuntimeError("빠른 업데이트 주소가 없어요")
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with urllib.request.urlopen(
                    urllib.request.Request(zip_url, headers={"User-Agent": "chaebo"}), timeout=90) as r:
                (tdp / "app.zip").write_bytes(r.read())
            with zipfile.ZipFile(tdp / "app.zip") as z:
                # zip-slip 방어(코드리뷰 2026-07-14): 항목 경로가 추출 폴더를 벗어나면(../ 등) 거부.
                # 자동 업데이트는 신뢰 앵커라 gist 탈취 시 임의 파일 덮어쓰기(RCE) 위험 — 미리 막는다.
                dest = (tdp / "x").resolve()
                for m in z.namelist():
                    if m.endswith("/"):
                        continue
                    tgt = (dest / m).resolve()
                    if dest != tgt and dest not in tgt.parents:
                        raise RuntimeError("업데이트 파일에 안전하지 않은 경로가 있어요")
                z.extractall(dest)
            # GitHub archive 는 최상위 폴더 1개(chaebo-app-main/) 안에 app/·run.py 가 들어있다
            cands = [p for p in (tdp / "x").iterdir() if p.is_dir()]
            src = next((c for c in cands if (c / "app").is_dir() and (c / "run.py").is_file()),
                       tdp / "x")
            if not (src / "app").is_dir() or not (src / "run.py").is_file():
                raise RuntimeError("업데이트 파일 형식이 올바르지 않아요")
            # ★버전 확인부터 먼저(사용자 요청 2026-07-13): 받은 코드의 APP_VERSION 이 '지금 실행 중'보다
            # 진짜 새 버전일 때만 교체한다. 안 그러면 CDN 캐시 등으로 같은(또는 옛) 코드를 받아 교체·재시작만
            # 반복하는 무한 루프가 난다(사용자 실측: 6.20 인데 '지금 업데이트'가 계속 재적용). 같으면 교체 안 함.
            delivered = None
            try:
                mm = re.search(r'APP_VERSION\s*=\s*"([^"]+)"',
                               (src / "app" / "config.py").read_text(encoding="utf-8"))
                delivered = mm.group(1) if mm else None
            except Exception:  # noqa: BLE001 — 못 읽으면 아래에서 그냥 교체(기존 동작)
                delivered = None
            if delivered and not _ver_gt(delivered, config.APP_VERSION):
                return {"replaced": False, "version": config.APP_VERSION}  # 이미 최신 — 교체·재시작 안 함
            # 교체(제자리 덮어쓰기): app/ 트리 + run.py. 파이썬 파일은 잠기지 않아 재시작 때 반영.
            shutil.copytree(src / "app", config.BASE_DIR / "app", dirs_exist_ok=True)
            shutil.copy2(src / "run.py", config.BASE_DIR / "run.py")
            # 새 .py 를 옛 바이트코드(.pyc)가 가리지 않게 — 델타 교체 후 __pycache__ 제거(재시작 시 새로 컴파일).
            for pc in (config.BASE_DIR / "app").rglob("__pycache__"):
                shutil.rmtree(pc, ignore_errors=True)
        return {"replaced": True, "version": delivered or info.get("version")}

    try:
        res = await asyncio.get_event_loop().run_in_executor(None, _work)
        return {"ok": True, "version": res["version"], "already_current": not res["replaced"]}
    except Exception as e:  # noqa: BLE001 — 실패해도 기존 설치는 그대로(안전)
        print(f"[update] 빠른 업데이트 실패: {e}", flush=True)
        return {"ok": False}


@app.post("/api/restart")
def restart_app():
    """앱 자동 재시작(업데이트 적용 후 — 사용자 요청 2026-07-13) = '자동으로 껐다 켜기'.
    현재 서버가 완전히 내려간 뒤 새 앱을 띄우는 '재시작 도우미'(run.py --relaunch)를 분리 프로세스로
    먼저 실행하고, 잠깐 뒤 이 프로세스를 종료한다. 도우미는 health 가 끊길 때까지 기다렸다가 같은
    포트로 새로 뜬다(포트·단일 인스턴스 충돌 방지). 앱 창 모드면 창이 닫혔다가 새 창으로, 웹 모드면
    새 탭으로 다시 열린다. 새 파이썬 코드(델타 교체본)가 그때 로드된다."""
    import subprocess

    run_py = config.BASE_DIR / "run.py"
    if not run_py.is_file():
        raise HTTPException(400, "재시작 스크립트를 찾지 못했어요")

    # 재시작 후 열기 방식 = 저장된 설정(open_mode.txt)을 따른다. 인자를 안 붙이면 run.py 가 이 파일을
    # 읽는다. 예전엔 '최초 실행 인자'(run.bat web 의 web)를 그대로 물려줘, 사용자가 설정에서 앱으로 바꿔도
    # 재시작이 옛 인자로 웹을 다시 열었다(사용자 지적 2026-07-14: 웹으로 켰다가 앱으로 바꿔도 또 웹으로 열림).
    # 이제 설정 파일이 정본 — 설정에서 바꾼 방식이 재시작/업데이트 후에도 유지된다.
    cmd = [sys.executable, str(run_py), "--relaunch"]

    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 부모(현재 앱)가 죽어도 살아남게 분리.
        flags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(cmd, cwd=str(config.BASE_DIR), close_fds=True, creationflags=flags)
    except Exception as e:  # noqa: BLE001
        print(f"[restart] 재시작 도우미 실행 실패: {e}", flush=True)
        raise HTTPException(500, "재시작을 시작하지 못했어요")

    # 응답을 먼저 흘려보내고(프런트가 '다시 시작 중' 표시할 시간) + 도우미가 기동할 여유를 준 뒤 종료.
    import threading
    import time as _time

    def _bye():
        _time.sleep(0.8)
        os._exit(0)  # 트레이 '종료'와 동일 — 데몬 서버 스레드까지 즉시 정리(포트·뮤텍스 해제)

    threading.Thread(target=_bye, daemon=True).start()
    return {"ok": True}


def run():
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")  # REQ-SEC-001: 기본 로컬 전용
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    run()
