"""데이터를 '한 곳'(사용자당 공유 위치)에 모은다 — 설치 경로가 달라도 같은 곡 라이브러리.

배경(2026-07-14): DATA_DIR 이 설치-상대(BASE_DIR/data)라 개발 체크아웃 E:/chaebo(9곡)·D:/chaebo(1곡)·
설치판이 각자 다른 곡을 보였다(사용자 지적: 앱/웹 곡이 다름). config._default_data_dir 이 기본을
%LOCALAPPDATA%/chaebo/data(설치기 데이터 위치와 동일·설치경로 무관)로 바꾸고, 이 모듈이 옛 위치의
데이터를 공유 위치로 '복사'해 모은다.

원칙(하드 규칙 8·3 관점):
  1) 원본 파괴 금지 — 복사만. 마커를 남긴 뒤에도 옛 data/ 폴더는 그대로 둔다(사용자가 나중에 지움).
  2) 공유가 비었으면 통째 복사(ID 유지·가장 안전). 차 있으면 '재키(re-key)' 병합 — 새 ID 를 부여해
     충돌을 피하고, 같은 곡(source_type+source 동일)은 건너뛴다(두 번 실행해도 중복 안 생김).
  3) CHAEBO_DATA 를 명시 지정했으면(테스트·파워유저) 아무것도 안 한다.
  4) 스키마 진화에 견디게 — 컬럼을 PRAGMA 로 읽어 '양쪽에 다 있는' 컬럼만 옮긴다.
"""
import os
import shutil
import sqlite3
from pathlib import Path

from app import config

MARKER_NAME = ".migrated-to-shared"
_CHILD = {  # 자식 테이블: song 을 가리키는 FK 컬럼명
    "transcriptions": "song_id",
    "practice_state": "song_id",
}


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _song_count(db_path: Path) -> int:
    con = _connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    finally:
        con.close()


def _columns(con: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _insert_remapped(dst: sqlite3.Connection, table: str, row: dict, id_field: str, new_id):
    """row(dict) 를 dst.table 에 넣되 id_field 를 new_id 로 바꾼다. 양쪽에 다 있는 컬럼만 사용."""
    dst_cols = _columns(dst, table)
    data = {k: v for k, v in row.items() if k in dst_cols}
    data[id_field] = new_id
    cols = list(data)
    dst.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [data[c] for c in cols],
    )


def _copy_song_files(legacy: Path, shared: Path, old_id, new_id):
    """stems/<id>/ 통째 + raw/<id>.* 를 복사(파형·타브·가사·피치캐시 포함). 원본 보존."""
    s_src = legacy / "stems" / str(old_id)
    if s_src.exists():
        shutil.copytree(s_src, shared / "stems" / str(new_id), dirs_exist_ok=True)
    (shared / "raw").mkdir(parents=True, exist_ok=True)
    raw_dir = legacy / "raw"
    if raw_dir.exists():
        for f in raw_dir.glob(f"{old_id}.*"):
            shutil.copy2(f, shared / "raw" / f"{new_id}{f.suffix}")


def _fast_copy(legacy: Path, shared: Path) -> int:
    """공유가 비었을 때 — DB·stems·raw 를 통째 복사(ID 유지). app_settings 도 함께 온다."""
    shared.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy / "chaebo.sqlite3", shared / "chaebo.sqlite3")
    for sub in ("stems", "raw"):
        src = legacy / sub
        if src.exists():
            shutil.copytree(src, shared / sub, dirs_exist_ok=True)
    return _song_count(shared / "chaebo.sqlite3")


def _merge(legacy: Path, shared: Path) -> int:
    """공유에 이미 곡이 있을 때 — 새 ID 를 부여해 병합. 같은 source 는 건너뜀. app_settings 는 공유 것 유지."""
    src = _connect(legacy / "chaebo.sqlite3")
    dst = _connect(shared / "chaebo.sqlite3")
    try:
        existing = {(r["source_type"], r["source"]) for r in dst.execute(
            "SELECT source_type, source FROM songs")}
        next_id = (dst.execute("SELECT COALESCE(MAX(id),0) FROM songs").fetchone()[0]) + 1
        migrated = 0
        for song in src.execute("SELECT * FROM songs"):
            song = dict(song)
            if (song["source_type"], song["source"]) in existing:
                continue
            old_id, new_id = song["id"], next_id
            next_id += 1
            _copy_song_files(legacy, shared, old_id, new_id)   # 파일 먼저(실패 시 DB 행 안 남게)
            _insert_remapped(dst, "songs", song, "id", new_id)
            for table, fk in _CHILD.items():
                r = src.execute(f"SELECT * FROM {table} WHERE {fk}=?", (old_id,)).fetchone()
                if r:
                    _insert_remapped(dst, table, dict(r), fk, new_id)
            migrated += 1
        dst.commit()
        return migrated
    finally:
        src.close()
        dst.close()


def migrate_to_shared() -> str:
    """옛 위치 data/ 를 공유 위치로 1회 모은다. 반환: 사람이 읽을 결과 문자열('노옵'|'복사 N'|'병합 N')."""
    if os.environ.get("CHAEBO_DATA"):
        return "노옵(CHAEBO_DATA 지정)"
    shared = config.DATA_DIR
    legacy = config.LEGACY_DATA_DIR
    try:
        if legacy.resolve() == shared.resolve():
            return "노옵(옛==공유)"   # 설치판: BASE_DIR/data == 공유
    except Exception:
        return "노옵(경로해석 실패)"
    legacy_db = legacy / "chaebo.sqlite3"
    if not legacy_db.exists():
        return "노옵(옛 DB 없음)"
    marker = legacy / MARKER_NAME
    if marker.exists():
        return "노옵(이미 이관)"
    try:
        legacy_songs = _song_count(legacy_db)
    except Exception as e:  # noqa: BLE001
        return f"노옵(옛 DB 읽기 실패: {e})"
    if legacy_songs == 0:
        marker.write_text("empty", encoding="utf-8")
        return "노옵(옛 곡 0)"

    shared.mkdir(parents=True, exist_ok=True)
    shared_db = shared / "chaebo.sqlite3"
    try:
        shared_songs = _song_count(shared_db) if shared_db.exists() else 0
        if shared_songs == 0:
            n = _fast_copy(legacy, shared)
            result = f"복사 {n}"
        else:
            n = _merge(legacy, shared)
            result = f"병합 {n}"
    except Exception as e:  # noqa: BLE001 — 마이그레이션 실패해도 앱은 떠야 한다(원본은 무손상)
        print(f"[chaebo migrate] 실패(원본 보존): {e}")
        return f"실패(원본보존): {e}"

    marker.write_text(f"{result} -> {shared}", encoding="utf-8")
    print(f"[chaebo migrate] {result} songs: {legacy} -> {shared}")
    return result
