"""SQLite — 곡 메타·잡 상태만 저장. 오디오 실체는 파일시스템(data/)."""
import aiosqlite

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,          -- youtube | file
    source TEXT NOT NULL,               -- URL 또는 원본 파일명
    status TEXT NOT NULL DEFAULT 'queued',  -- queued|downloading|separating|ready|error
    progress REAL NOT NULL DEFAULT 0,   -- 0~100
    error TEXT,                         -- 사용자에게 보여줄 한국어 사유
    duration REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS practice_state (
    song_id INTEGER PRIMARY KEY REFERENCES songs(id) ON DELETE CASCADE,
    state TEXT NOT NULL,                -- JSON: 위치·배속·볼륨·솔로/음소거·A-B·저장 루프
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcriptions (
    song_id INTEGER PRIMARY KEY REFERENCES songs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued|analyzing|ready|error
    progress REAL NOT NULL DEFAULT 0,
    bpm REAL,
    notes TEXT,                         -- JSON [{start,dur,midi,conf,string,fret,gi,glen}]
    tex TEXT,                           -- alphaTex (렌더 정본)
    key_json TEXT,                      -- JSON {tonic,mode,label} 키 추정 (rev9)
    chords TEXT,                        -- JSON [{bar,label}] 마디 코드 초안 (rev9)
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

MIGRATIONS = [
    "ALTER TABLE transcriptions ADD COLUMN key_json TEXT",
    "ALTER TABLE transcriptions ADD COLUMN chords TEXT",
    "ALTER TABLE transcriptions ADD COLUMN beat_offset REAL",  # 첫 박 시각 — 커서·코드 정렬 기준
    # 동적 그리드(실연주 템포 추종): gi→절대시각 배열. 고정 bpm 그리드는 사람 연주에서 구간별로
    # 반 칸씩 어긋남(실증: 곡6 인트로 40마디 홀수칸 80%)
    "ALTER TABLE transcriptions ADD COLUMN slots TEXT",
    "ALTER TABLE transcriptions ADD COLUMN meter TEXT",  # '4/4'|'12/8' — 12/8 은 마디 48슬롯 표기
    # 그리드 버전: 1=마디 16칸(구), 2=박당 12칸(16분·셋잇단 공존 — Longview 류 부분 셋잇단)
    "ALTER TABLE transcriptions ADD COLUMN grid_v INTEGER",
    "ALTER TABLE transcriptions ADD COLUMN meter_override TEXT",  # 자동 판정 오류 시 수동 고정
    "ALTER TABLE songs ADD COLUMN description TEXT",  # 곡 설명/메모(사용자 요청 2026-07-10)
    # 검출 감도('normal'|'simple') — 밀집 믹스에서 타브 과밀 억제(사용자 요청 2026-07-10)
    "ALTER TABLE transcriptions ADD COLUMN sensitivity TEXT",
    # 빠르기 보정('half'|NULL) — 비트 추적의 2배 템포 오검출 수동 교정(벧엘 129→64.5 실증)
    "ALTER TABLE transcriptions ADD COLUMN tempo_override TEXT",
    "ALTER TABLE songs ADD COLUMN artist TEXT",  # 아티스트(yt-dlp 추정+수동, 2026-07-10)
    "ALTER TABLE transcriptions ADD COLUMN key_override TEXT",  # 키 직접 입력('F#m' 등, 2026-07-10)
    "ALTER TABLE transcriptions ADD COLUMN lyrics TEXT",  # 가사 받아쓰기 JSON(SP-5, 2026-07-10)
    "ALTER TABLE transcriptions ADD COLUMN sections TEXT",  # 곡 구간(경계·그룹·보컬힌트, 2026-07-10)
    # 음정 검출 정밀도: 'full'=정확(느림)·NULL/'tiny'=빠름. '정확하게 다시 분석' 버튼용(2026-07-14)
    "ALTER TABLE transcriptions ADD COLUMN crepe_mode TEXT",
    # 첫 음 정박 스냅: 1/NULL=켬(기본)·0=끔. 첫 음이 박에서 살짝 벗어났을 때 가장 가까운 박으로 당김.
    # 사용자가 귀로 듣고 당김음이면 끄게(체크박스). (2026-07-15)
    "ALTER TABLE transcriptions ADD COLUMN lead_snap INTEGER",
    # 수동 박자 앵커: {"base":[슬롯시각...], "anchors":[[gi,t],...]} — 사용자가 마디선을 실박으로 끌어
    # 격자를 구간별 워프(가변 템포 잔여 드리프트 보정, 2026-07-16). base 는 첫 앵커 때 슬롯 스냅샷.
    "ALTER TABLE transcriptions ADD COLUMN tab_anchors TEXT",
    # 박자 엔진 선택: NULL/'plp'=기본(국소펄스·가변) | 'beat_track'=단일템포 | 'beat_this'=신경망 SOTA(다운로드).
    # 사용자 A/B 비교용(2026-07-16) — 내 판단 대신 사용자가 직접 고르고 듣게.
    "ALTER TABLE transcriptions ADD COLUMN beat_engine TEXT",
    # 음정 검출 엔진: NULL/'bp'=basic-pitch(다성) | 'f0'=단음 F0→분절(CREPE-Notes 방식, 과검출 회피).
    # 사용자 귀 A/B 용(2026-07-16).
    "ALTER TABLE transcriptions ADD COLUMN detect_engine TEXT",
    # 분석 소스 스템: NULL/'bass'=베이스 스템 | 'guitar'=기타 스템(고음 베이스 솔로가 분리에서 기타로
    # 라우팅될 때, 기타 스템을 대신 채보해 베이스 타브로 렌더. 사용자 요청 2026-07-16).
    "ALTER TABLE transcriptions ADD COLUMN source_stem TEXT",
]


async def init():
    config.ensure_dirs()
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:  # 기존 DB 컬럼 보강 (이미 있으면 무시)
            try:
                await conn.execute(stmt)
            except Exception:  # noqa: BLE001
                pass
        # 재시작 시 고아 잡 복구: 진행 중이던 곡은 다시 큐로 (REQ-OPS-004 최소 배선)
        await conn.execute(
            "UPDATE songs SET status='queued', progress=0"
            " WHERE status IN ('downloading', 'separating')")
        # 타브 잡도 동일 복구 — 단 이전 결과(notes)가 있으면 ready 로 되돌림(재분석 11분 낭비 방지,
        # 실증: 재시작에 고아가 된 queued 타브가 화면을 '분석 중 0%'에 고착시킴)
        await conn.execute(
            "UPDATE transcriptions SET status='ready', progress=100"
            " WHERE status IN ('queued', 'analyzing') AND notes IS NOT NULL")
        await conn.execute(
            "UPDATE transcriptions SET status='queued', progress=0"
            " WHERE status='analyzing'")
        await conn.commit()


async def create_song(title: str, source_type: str, source: str) -> int:
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO songs (title, source_type, source) VALUES (?, ?, ?)",
            (title, source_type, source))
        await conn.commit()
        return cur.lastrowid


async def update_song(song_id: int, **fields):
    keys = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            f"UPDATE songs SET {keys} WHERE id=?", (*fields.values(), song_id))
        await conn.commit()


async def get_song(song_id: int):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM songs WHERE id=?", (song_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_songs():
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM songs ORDER BY created_at DESC, id DESC")
        return [dict(r) for r in await cur.fetchall()]


async def delete_song(song_id: int):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        # ON DELETE CASCADE 는 SQLite 기본이 FK OFF 라 실제로 동작 안 함(연결마다 PRAGMA 필요) →
        # 자식 행을 명시 삭제한다. 안 그러면 곡 삭제 때 transcriptions·practice_state 가 고아로 무한
        # 누적되고, get_tab 이 삭제된 곡의 유령 채보를 200 으로 돌려줬다(코드리뷰 2026-07-14).
        await conn.execute("DELETE FROM transcriptions WHERE song_id=?", (song_id,))
        await conn.execute("DELETE FROM practice_state WHERE song_id=?", (song_id,))
        await conn.execute("DELETE FROM songs WHERE id=?", (song_id,))
        await conn.commit()


async def get_state(song_id: int):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT state FROM practice_state WHERE song_id=?", (song_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def put_state(song_id: int, state_json: str):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO practice_state (song_id, state, updated_at)"
            " VALUES (?, ?, datetime('now','localtime'))"
            " ON CONFLICT(song_id) DO UPDATE SET state=excluded.state,"
            " updated_at=excluded.updated_at", (song_id, state_json))
        await conn.commit()


async def get_setting(key: str, default=None):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cur = await conn.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        await conn.commit()


async def get_limits() -> dict:
    """수집 한도(REQ-ING-003 '설정 가능') — 설정값 우선, 없으면 env/기본."""
    dur = await get_setting("max_duration_min")
    size = await get_setting("max_file_mb")
    return {
        "max_duration_min": int(dur) if dur else config.MAX_DURATION_MIN,
        "max_file_mb": int(size) if size else config.MAX_FILE_MB,
    }


async def get_transcription(song_id: int):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM transcriptions WHERE song_id=?", (song_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_transcription(song_id: int, **fields):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO transcriptions (song_id) VALUES (?)"
            " ON CONFLICT(song_id) DO NOTHING", (song_id,))
        keys = ", ".join(f"{k}=?" for k in fields)
        await conn.execute(
            f"UPDATE transcriptions SET {keys},"
            " updated_at=datetime('now','localtime') WHERE song_id=?",
            (*fields.values(), song_id))
        await conn.commit()


async def queued_song_ids():
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cur = await conn.execute("SELECT id FROM songs WHERE status='queued' ORDER BY id")
        return [r[0] for r in await cur.fetchall()]


async def queued_tab_ids():
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT song_id FROM transcriptions WHERE status='queued' ORDER BY song_id")
        return [r[0] for r in await cur.fetchall()]


async def outdated_tab_ids(grid_v: int):
    """그리드 버전이 낮은 완료 채보 — 기동 시 재정량화 백필(캐시라 초 단위)."""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT song_id FROM transcriptions WHERE status='ready'"
            " AND (grid_v IS NULL OR grid_v < ?) ORDER BY song_id", (grid_v,))
        return [r[0] for r in await cur.fetchall()]
