"""잡 워커 — 다운로드(유튜브)→분리(MSST 서브프로세스)→ready.

분리는 별도 프로세스로 격리한다: SP-1 실측에서 피크 6.8GB/커밋 12.7GB —
서버 프로세스와 분리해 분리가 죽어도(메모리 등) 서버·다른 곡은 산다.
진행률: 다운로드 0~25 · 분리 25~98(tqdm 파싱) · 완료 100. (REQ-OPS-001)
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import config, db, gpu

# Windows: pythonw(무콘솔)로 실행되면 자식 프로세스가 새 콘솔창을 띄운다 — ffmpeg·ffprobe·yt-dlp·
# 분석 워커·MSST 창이 번쩍이던 문제(사용자 실측 2026-07-12). gpu/system 은 이미 적용, jobs 만 누락됐었다.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 무진행 감시(watchdog): 분석·분리(heavy) 서브프로세스가 이 시간 동안 아무 출력(tqdm 진행)도 없으면
# '멈춤'으로 보고 트리째 종료한다. 멈춘 고아 워커가 CPU·메모리(실증: tab_worker 1.8GB·CPU 1시간,
# 2026-07-13)를 물고 PC 를 느리게 하던 문제 방지. 20분 무출력은 어떤 정상 단계(모델 로딩 등)보다도 길다.
_HEAVY_IDLE_TIMEOUT = 60 * int(os.environ.get("CHAEBO_ANALYZE_IDLE_MIN", "20"))

queue: asyncio.Queue = asyncio.Queue()
_worker_task = None
# 실행 중 서브프로세스(곡별) — 중지 시 종료 대상. '멈춤'의 단일 신호는 DB status='stopped'.
_running_procs: dict = {}


def raw_path(song_id: int) -> Path | None:
    hits = list(config.RAW_DIR.glob(f"{song_id}.*"))
    return hits[0] if hits else None


def stems_dir(song_id: int) -> Path:
    return config.STEMS_DIR / str(song_id)


async def start():
    global _worker_task
    for sid in await db.queued_song_ids():
        queue.put_nowait(sid)
    for sid in await db.queued_tab_ids():  # 재시작 복구 — 타브 잡도 (고아 실증)
        queue.put_nowait(("tab", sid))
    for sid in await db.outdated_tab_ids(2):  # 그리드 v2(48칸) 마이그레이션 — 캐시 재정량화(초 단위)
        # 사람이 보정한 곡(conf=1.0 마커 존재)은 자동 재분석이 보정을 날린다(적대 리뷰 확정) — 건너뜀
        row = await db.get_transcription(sid)
        try:
            notes = json.loads(row["notes"]) if row and row.get("notes") else []
        except Exception:  # noqa: BLE001
            notes = []
        if any(n.get("conf") == 1.0 for n in notes):
            continue
        queue.put_nowait(("tab", sid))
    _worker_task = asyncio.create_task(_worker())
    asyncio.create_task(_backfill_playback())  # 기존 곡 압축 재생본 백필(없으면 wav 그대로)


async def transcode_stems(song_id: int):
    """재생용 압축본(AAC m4a) — wav 74MB×6 을 화면 전환마다 통째로 재로딩해 수 초 걸리던 문제의 근본 해소.
    wav 는 분석(CREPE·파형) 원본으로 유지, 실패 시 재생은 wav 폴백이라 치명 아님."""
    out = stems_dir(song_id)
    for s in config.STEMS:
        wav, m4a = out / f"{s}.wav", out / f"{s}.m4a"
        if not wav.exists() or (m4a.exists() and m4a.stat().st_size > 0):
            continue
        tmp = out / f"{s}.m4a.part"
        try:
            # -f mp4 명시: .part 임시 확장자로는 컨테이너 추론이 안 됨(실증)
            code, _ = await _run(["ffmpeg", "-y", "-i", str(wav),
                                  "-c:a", "aac", "-b:a", "192k", "-f", "mp4", str(tmp)])
        except Exception:  # noqa: BLE001 — ffmpeg 부재 등: wav 폴백
            return
        if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(m4a)
        else:
            tmp.unlink(missing_ok=True)


async def _backfill_playback():
    for song in await db.list_songs():
        if song["status"] == "ready":
            await transcode_stems(song["id"])


_pitch_locks: dict = {}
pitch_errors: dict = {}  # (song_id, semitones) -> 사용자용 실패 사유(무한 '바꾸는 중' 방지 — 리뷰 확정)


async def build_pitch_stems(song_id: int, semitones: int) -> bool:
    """키(피치) 재생용 시프트 스템(REQ-PLAY-009) — ffmpeg 코어 필터(asetrate→aresample→atempo,
    LGPL-안전: rubberband 는 GPL 빌드 전용이라 배제)로 반음 단위 피치만 변경(속도 불변).
    결과는 stems/{id}/shift_{n}/*.m4a 캐시 — 이미 있으면 재생성 안 함. 반환: 전체 성공 여부."""
    key = (song_id, semitones)
    lock = _pitch_locks.setdefault(key, asyncio.Lock())
    async with lock:
        src_dir = stems_dir(song_id)
        out_dir = src_dir / f"shift_{semitones}"
        out_dir.mkdir(exist_ok=True)
        f = 2 ** (semitones / 12)
        ok = True
        for s in config.STEMS:
            src = src_dir / f"{s}.m4a"
            if not src.exists():
                src = src_dir / f"{s}.wav"
            dst = out_dir / f"{s}.m4a"
            if dst.exists() and dst.stat().st_size > 0:
                continue
            tmp = out_dir / f"{s}.part"
            try:
                code, _ = await _run([
                    "ffmpeg", "-y", "-i", str(src),
                    "-af", f"asetrate=44100*{f:.8f},aresample=44100,atempo={1 / f:.8f}",
                    "-c:a", "aac", "-b:a", "192k", "-f", "mp4", str(tmp)])
            except Exception:  # noqa: BLE001 — ffmpeg 부재 등
                pitch_errors[key] = "키를 바꾸지 못했어요 — ffmpeg 가 설치돼 있는지 확인해주세요"
                return False
            if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(dst)
            else:
                tmp.unlink(missing_ok=True)
                pitch_errors[key] = "키를 바꾸는 중 오류가 났어요 — 잠시 후 다시 시도해주세요"
                ok = False
        if ok:
            pitch_errors.pop(key, None)
        return ok


def pitch_ready(song_id: int, semitones: int) -> bool:
    out_dir = stems_dir(song_id) / f"shift_{semitones}"
    return all((out_dir / f"{s}.m4a").exists() and (out_dir / f"{s}.m4a").stat().st_size > 0
               for s in config.STEMS)


async def stop():
    if _worker_task:
        _worker_task.cancel()


async def _kill_tree(proc):
    """자식 프로세스 '트리' 전체 종료. proc.kill() 은 직속 자식만 죽여, 분리(MSST/torch 워커)·다운로드
    (yt-dlp 가 부르는 ffmpeg)가 띄운 손자 프로세스가 살아남아 파일을 잠그고 임시폴더 정리를 막았다
    (코드리뷰 2026-07-14: 좀비·TemporaryDirectory PermissionError). Windows=taskkill /T(트리 종료),
    실패·비윈도우는 직속 kill 폴백. taskkill 은 서브프로세스로 비동기 실행(이벤트루프 안 막음)."""
    pid = getattr(proc, "pid", None)
    if pid is not None and os.name == "nt":
        try:
            tk = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=_NO_WINDOW)
            await asyncio.wait_for(tk.wait(), timeout=10)
            return
        except Exception:  # noqa: BLE001 — taskkill 실패 시 아래 직속 kill 로 폴백
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — 이미 종료됐거나 경합
        pass


async def cancel(song_id: int):
    """분석(다운로드/분리)을 중지하고 '멈춤' 상태로 둔다(사용자 요청 2026-07-12).
    - 진행 중이 아니면(이미 완료·오류) 아무것도 안 함 — 완료 결과를 덮지 않게.
    - DB status 를 먼저 stopped 로 → 즉시 UI 반영 + 워커의 단일 신호(대기 중이면 dequeue 때 건너뜀).
    - 실행 중 서브프로세스가 있으면 트리째 종료해 무거운 작업을 실제로 멈춘다(손자까지)."""
    song = await db.get_song(song_id)
    if not song or song["status"] not in ("queued", "downloading", "separating"):
        return
    await db.update_song(song_id, status="stopped", progress=0, error=None)
    proc = _running_procs.get(song_id)
    if proc and proc.returncode is None:
        await _kill_tree(proc)


async def _worker():
    while True:
        item = await queue.get()
        kind, song_id = item if isinstance(item, tuple) else ("sep", item)
        try:
            # GPU 가속 켜는 중(torch 를 CUDA 판으로 uninstall+재설치)에는 분석 시작을 미룬다 —
            # 교체 중 torch import 가 깨져 '엔진 오류'가 나던 문제(사용자 지적 2026-07-13).
            while gpu.state().get("running"):
                await asyncio.sleep(1)
            if kind == "sep":
                song = await db.get_song(song_id)
                # 멈춤(중지)됐거나 이미 처리됨 — 중복 큐 항목·대기 중 취소 방어(DB status 가 단일 소스)
                if not song or song["status"] != "queued":
                    continue
            if kind == "tab":
                await _process_tab(song_id)
            elif kind == "lyrics":
                await _process_lyrics(song_id)
            elif kind == "sections":
                await _process_sections(song_id)
            else:
                await _process(song_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 사유는 상태로 사용자에게 노출
            # 사용자가 중지해 서브프로세스가 죽은 것이면 '멈춤'을 오류로 덮지 않는다
            if kind == "sep":
                cur = await db.get_song(song_id)
                if cur and cur["status"] == "stopped":
                    continue
            if kind == "tab":
                await db.upsert_transcription(song_id, status="error",
                                              error=f"채보 중 오류가 났어요: {e}")
            elif kind == "lyrics":
                await db.upsert_transcription(
                    song_id, lyrics=json.dumps(
                        {"status": "error", "error": f"가사 받아쓰기 중 오류: {e}"},
                        ensure_ascii=False))
            elif kind == "sections":
                await db.upsert_transcription(
                    song_id, sections=json.dumps(
                        {"status": "error", "error": f"구간 감지 중 오류: {e}"},
                        ensure_ascii=False))
            else:
                await db.update_song(song_id, status="error",
                                     error=f"처리 중 오류가 났어요: {e}")
        finally:
            queue.task_done()


async def _process_tab(song_id: int):
    """타브 초안 — CREPE 파이프라인 서브프로세스 (CPU 수 분, 서버와 격리)."""
    song = await db.get_song(song_id)
    row0 = await db.get_transcription(song_id)
    # 분석 소스 스템 — 기본 bass. 고음 베이스 솔로가 분리에서 기타로 라우팅된 곡은 'guitar' 로 채보
    # (사용자 요청 2026-07-16). 검출·파형 근거가 그 스템이 된다. 없으면 bass 로 폴백.
    src_stem = (row0.get("source_stem") if row0 else None) or "bass"
    bass = stems_dir(song_id) / f"{src_stem}.wav"
    if not bass.exists():
        bass = stems_dir(song_id) / "bass.wav"
        src_stem = "bass"
    drums = stems_dir(song_id) / "drums.wav"
    if not bass.exists() or not drums.exists():
        raise RuntimeError("분리된 베이스/드럼이 아직 없어요")
    await db.upsert_transcription(song_id, status="analyzing", progress=2, error=None)

    out_json = stems_dir(song_id) / "tab.json"

    async def on_line(text: str):
        m = re.match(r"PROG (\d+)", text)
        if m:
            await db.upsert_transcription(song_id, progress=int(m.group(1)))

    env_extra = {}
    row = await db.get_transcription(song_id)
    if row and row.get("meter_override"):
        env_extra["CHAEBO_METER"] = row["meter_override"]  # 자동 판정 수동 고정
    if row and row.get("sensitivity"):
        env_extra["CHAEBO_SENS"] = row["sensitivity"]  # 검출 감도(단순 모드 — 과밀 억제)
    if row and row.get("tempo_override"):
        env_extra["CHAEBO_TEMPO"] = row["tempo_override"]  # 2배 템포 오검출 교정
    if row and row.get("key_override"):
        env_extra["CHAEBO_KEY"] = row["key_override"]  # 키 직접 입력 — 재분석에도 유지
    if row and row.get("crepe_mode"):
        env_extra["CHAEBO_CREPE_MODEL"] = row["crepe_mode"]  # 'full'=정확(느림) — 음정 정밀도
    if row and row.get("beat_engine"):
        env_extra["CHAEBO_BEAT_ENGINE"] = row["beat_engine"]  # 박자 엔진 선택(beat_track|beat_this), NULL=plp 기본
    if row and row.get("detect_engine"):
        env_extra["CHAEBO_DETECT_ENGINE"] = row["detect_engine"]  # 음정 검출 엔진(f0), NULL=bp 기본
    if src_stem != "bass":
        env_extra["CHAEBO_SOURCE_STEM"] = src_stem  # 캐시 키 종속 — 소스 바꾸면 재검출(tab.json 같은 파일 재사용 방지)
    if row and row.get("lead_snap") == 1:
        env_extra["CHAEBO_LEAD_SNAP"] = "1"  # 사용자가 첫 음 정박 스냅 명시적으로 켬 — 기본은 끔(패싱음/당김음 보존)
    if row and row.get("guitar_ranges"):
        env_extra["CHAEBO_GUITAR_RANGES"] = row["guitar_ranges"]  # 이 구간만 기타 스템으로 검출·병합(부분 베이스 솔로)
    code, tail = await _run(
        [config.PYTHON, "-m", "app.tab_worker", str(bass), str(drums),
         str(out_json), song["title"][:80]],
        cwd=config.BASE_DIR, on_line=on_line, heavy=True, env_extra=env_extra)
    if code != 0 or not out_json.exists():
        raise RuntimeError(f"분석이 실패했어요. 마지막 로그: {tail[-300:]}")

    data = json.loads(out_json.read_text(encoding="utf-8"))
    await db.upsert_transcription(
        song_id, status="ready", progress=100, bpm=data["bpm"],
        beat_offset=data.get("offset") or 0,
        notes=json.dumps(data["notes"], ensure_ascii=False), tex=data["tex"],
        key_json=json.dumps(data.get("key"), ensure_ascii=False),
        chords=json.dumps(data.get("chords") or [], ensure_ascii=False),
        slots=json.dumps(data.get("slots"), ensure_ascii=False) if data.get("slots") else None,
        meter=data.get("meter") or "4/4",
        grid_v=data.get("grid_v") or 1)
    # 가사 받아쓰기·구간 감지(SP-5, 사용자 승인 2026-07-10) — 채보 완료 후 자동으로 이어서(각 수십 초)
    row2 = await db.get_transcription(song_id)
    if not (row2 and row2.get("lyrics")):
        queue.put_nowait(("lyrics", song_id))
    else:
        # ★재분석 tex 는 가사 없이 생성된다(워커 서브프로세스는 DB 가사를 모름) — 기존 가사(LRCLIB·
        #   붙여넣기·애드립 받아쓰기 전부)를 새 악보에 재부착. 안 하면 '다시 분석'마다 오선 아래
        #   가사가 통째로 사라짐(v0.7.0 잠복 결함 — 사용자 발견 2026-07-18 "가사 다시 뺐어?").
        try:
            lyr = json.loads(row2["lyrics"])
            if lyr.get("status") == "ready":
                await _regen_score_lyrics(song_id, lyr)
        except Exception as e:  # noqa: BLE001 — 가사 재부착 실패해도 채보 결과 자체는 유효
            print(f"[가사 재부착 실패 — 채보는 유효] song {song_id}: {e}", flush=True)
    if not (row2 and row2.get("sections")):
        queue.put_nowait(("sections", song_id))


async def _process_sections(song_id: int):
    """곡 구간 감지 — 원본 오디오 경계(librosa) + 보컬 유무 힌트 서브프로세스.
    결과: transcriptions.sections JSON {status, sections:[{s,e,group,name,has_vocal}]}."""
    raw = raw_path(song_id)
    vocals = stems_dir(song_id) / "vocals.wav"
    if raw is None or not vocals.exists():
        raise RuntimeError("원본 오디오/보컬 스템이 아직 없어요")
    await db.upsert_transcription(
        song_id, sections=json.dumps({"status": "running"}, ensure_ascii=False))
    out_json = stems_dir(song_id) / "sections.json"
    code, tail = await _run(
        [config.PYTHON, "-m", "app.sections_worker", str(raw), str(vocals), str(out_json)],
        cwd=config.BASE_DIR, heavy=True)
    if code != 0 or not out_json.exists():
        raise RuntimeError(f"구간 감지가 실패했어요. 마지막 로그: {tail[-300:]}")
    data = json.loads(out_json.read_text(encoding="utf-8"))
    # 기존 사용자 이름은 보존(같은 인덱스·비슷한 경계면) — v1 은 단순히 새 결과로 대체하되
    # 수동 이름이 있던 구간은 시작점이 ±3초 안에서 일치하면 이름을 승계
    row = await db.get_transcription(song_id)
    prev = json.loads(row.get("sections") or "{}") if row else {}
    prev_named = [s for s in (prev.get("sections") or []) if s.get("manual")]
    for sec in data.get("sections") or []:
        for old in prev_named:
            if abs(old["s"] - sec["s"]) <= 3.0:
                sec["name"] = old["name"]
                sec["manual"] = True
                break
    await db.upsert_transcription(
        song_id, sections=json.dumps(
            {"status": "ready", "sections": data.get("sections") or []}, ensure_ascii=False))


async def _process_lyrics(song_id: int):
    """가사 — ①LRCLIB(사람이 옮긴 정확한 싱크 가사, 유명곡) 먼저 시도 → 있으면 그걸 사용(whisper 스킵).
    ②없으면 보컬 스템에 faster-whisper(small·CPU) 폴백. 결과 {status, language, segments:[{s,e,text}]}.
    ★똑똑한 붙여넣기(pending_paste): 골격 없는 붙여넣기가 whisper 를 먼저 요청한 것 — LRCLIB 를 건너뛰고
    (force_asr) whisper 로 즉흥(애드립)을 잡은 뒤, 붙여넣은 공식 가사를 그 위에 얹어 저장한다."""
    vocals = stems_dir(song_id) / "vocals.wav"
    # 붙여넣기가 남긴 배경 업그레이드 요청(상태 덮기 전에 읽는다) — 있으면 화면의 즉시 붙여넣기를 유지한 채
    # whisper 로 즉흥(애드립)을 잡아 오버레이로 교체한다(force_asr = LRCLIB 건너뜀).
    row0 = await db.get_transcription(song_id)
    cur0 = json.loads(row0.get("lyrics") or "{}") if (row0 and row0.get("lyrics")) else {}
    pending_paste = cur0.get("pending_paste")  # 있으면 = 붙여넣기 배경 업그레이드(LRCLIB 건너뛰고 whisper)
    pending_old = cur0.get("pending_old") or []
    song = await db.get_song(song_id)

    if pending_paste:
        # ★배경 업그레이드 — 절대 화면을 비우지 않는다('running'으로 안 덮음). 이미 즉시 붙여넣기가
        #   status='ready'+세그먼트로 보이고 있으니 그대로 두고 whisper 만 돌린다. 실패해도 즉시
        #   붙여넣기를 유지하고 upgrading 표식만 떼어 폴링을 멈춘다(가사·전체가사 버튼 안 사라지게).
        from app.lyrics_paste import build_paste_result
        asr_segs = []
        try:
            if vocals.exists():
                out_json = stems_dir(song_id) / "lyrics.json"
                code, tail = await _run(
                    [config.PYTHON, "-m", "app.lyrics_worker", str(vocals), str(out_json)],
                    cwd=config.BASE_DIR, heavy=True)
                if code == 0 and out_json.exists():
                    asr_segs = json.loads(out_json.read_text(encoding="utf-8")).get("segments") or []
        except Exception as e:  # noqa: BLE001 — 실패해도 즉시 붙여넣기 유지(화면 안 비게)
            print(f"[붙여넣기 배경 받아쓰기 실패 — 즉시 붙여넣기 유지] song {song_id}: {e}", flush=True)
        lines = [l.strip() for l in (pending_paste or "").splitlines() if l.strip()]
        dur = float((song or {}).get("duration") or 0)
        base = asr_segs if len(asr_segs) >= 5 else None
        # base 있으면 오버레이(즉흥 보존), 없으면 이전 세그 앵커에 보간 — 어느 쪽이든 upgrading 없는 확정본
        result = build_paste_result(lines, pending_old or asr_segs, base, dur)
        await db.upsert_transcription(song_id, lyrics=json.dumps(result, ensure_ascii=False))
        await _regen_score_lyrics(song_id, result)
        return

    # ── 일반 받아쓰기(가사 없던 곡) — 여기서만 'running'으로 표시(보여줄 기존 가사가 없으니 안전) ──
    if not vocals.exists():
        raise RuntimeError("분리된 보컬이 아직 없어요")
    await db.upsert_transcription(song_id, lyrics=json.dumps({"status": "running"}, ensure_ascii=False))
    # ① LRCLIB — 정확한 싱크 가사(있으면 whisper 안 돌림)
    from app.lyrics_online import fetch_lrclib
    loop = asyncio.get_event_loop()
    lrc = await loop.run_in_executor(
        None, fetch_lrclib, (song or {}).get("title") or "", (song or {}).get("artist"),
        (song or {}).get("duration"))
    if lrc and lrc.get("segments"):
        result = {"status": "ready", "language": "auto", "source": "lrclib",
                  "synced": lrc.get("synced", True), "segments": lrc["segments"]}
        await db.upsert_transcription(song_id, lyrics=json.dumps(result, ensure_ascii=False))
        await _regen_score_lyrics(song_id, result)
        return
    # ② whisper ASR
    out_json = stems_dir(song_id) / "lyrics.json"
    code, tail = await _run(
        [config.PYTHON, "-m", "app.lyrics_worker", str(vocals), str(out_json)],
        cwd=config.BASE_DIR, heavy=True)
    if code != 0 or not out_json.exists():
        raise RuntimeError(f"받아쓰기가 실패했어요. 마지막 로그: {tail[-300:]}")
    data = json.loads(out_json.read_text(encoding="utf-8"))
    result = {"status": "ready", "language": data.get("language"), "segments": data.get("segments") or []}
    await db.upsert_transcription(song_id, lyrics=json.dumps(result, ensure_ascii=False))
    await _regen_score_lyrics(song_id, result)


async def _regen_score_lyrics(song_id: int, lyrics: dict):
    """가사가 생기면 타브 악보(alphaTab) 오선 아래 가사도 갱신 — 노트가 있을 때만(저장 키·코드 재사용)."""
    row = await db.get_transcription(song_id)
    if not row or not row.get("notes") or not row.get("tex"):
        return
    song = await db.get_song(song_id)
    if not song:
        return
    from app.tab_worker import build_tab_tex
    tex = build_tab_tex(json.loads(row["notes"]), row["bpm"], song["title"][:80],
                        json.loads(row.get("key_json") or "null"),
                        json.loads(row.get("chords") or "null"),
                        row.get("meter") or "4/4", row.get("grid_v") or 1, lyrics=lyrics)
    await db.upsert_transcription(song_id, tex=tex)


async def _run(cmd: list[str], cwd=None, on_line=None, heavy=False, env_extra=None, song_id=None) -> tuple[int, str]:
    """heavy=True: 분석 프로세스를 낮은 우선순위+스레드 절반으로 — 분석 중에도 PC 를 쓸 수 있게.
    (실증: 기본 설정은 전 코어 점유+메모리 페이징으로 다른 작업 불가 — 사용자 피드백)"""
    kwargs = {}
    import os as _os
    # 자식(파이썬) 프로세스의 파이프 출력은 Windows 기본이 cp949 — 우리는 utf-8 로 읽으므로
    # 한글 제목이 �로 깨졌음(실증: 곡 5·8, 2026-07-10). 항상 utf-8 을 강제한다.
    env = {**_os.environ, **(env_extra or {}), "PYTHONIOENCODING": "utf-8"}
    if heavy:
        env["OMP_NUM_THREADS"] = str(config.WORK_THREADS)
        env["MKL_NUM_THREADS"] = str(config.WORK_THREADS)
    # 무콘솔 강제(창 번쩍임 제거) + heavy 는 낮은 우선순위 — 두 플래그를 OR 로 합친다.
    flags = _NO_WINDOW
    if heavy and hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):  # Windows
        flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
    if flags:
        kwargs["creationflags"] = flags
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs)
    if song_id is not None:
        _running_procs[song_id] = proc  # 중지 요청 시 이 프로세스를 종료(곡별 순차라 1개)
    tail: list[str] = []
    assert proc.stdout
    buf = b""
    timed_out = False
    # heavy(분석·분리)만 무진행 감시 — 다운로드는 네트워크 특성상 제외(자체 진행 로그 있음).
    idle = _HEAVY_IDLE_TIMEOUT if heavy else None
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(1024), timeout=idle)
            except asyncio.TimeoutError:
                # idle 동안 출력 0 = 멈춘 것으로 판단. 트리째 종료(고아가 CPU/메모리 물고 PC 를 느리게 함).
                timed_out = True
                await _kill_tree(proc)
                tail.append(f"[자동 종료] {idle // 60}분 동안 진행이 없어 분석을 멈췄어요")
                tail[:] = tail[-30:]
                break
            if not chunk:
                break
            buf += chunk
            # tqdm 은 \r 로 갱신하므로 \r/\n 둘 다 라인 경계로 본다
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                line, buf = buf[:m.start()], buf[m.end():]
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    tail.append(text)
                    tail[:] = tail[-30:]
                    if on_line:
                        await on_line(text)
        if not timed_out:
            await proc.wait()
    finally:
        if song_id is not None:
            _running_procs.pop(song_id, None)
    if timed_out:
        return 124, "\n".join(tail)  # 124 = 시간초과(관례). 호출부가 실패로 처리·재큐 안 함.
    return proc.returncode or 0, "\n".join(tail)


async def _process(song_id: int):
    song = await db.get_song(song_id)
    if not song:
        return

    if song["source_type"] == "youtube" and raw_path(song_id) is None:
        await _download(song_id, song["source"])

    src = raw_path(song_id)
    if src is None:
        raise RuntimeError("원본 오디오 파일이 없어요")

    if song["duration"] is None:  # 업로드 곡 길이 기록(라이브러리 —:— 갭 해소) + 한도 검사
        duration = await _probe_duration(src)
        if duration:
            limits = await db.get_limits()
            if duration > limits["max_duration_min"] * 60:
                raise RuntimeError(
                    f"곡이 너무 길어요({int(duration // 60)}분). {limits['max_duration_min']}분 이하만 추가할 수 있어요")
            await db.update_song(song_id, duration=duration)

    if (await db.get_song(song_id))["status"] == "stopped":
        return  # 다운로드~분리 사이에 중지됨 — 무거운 분리 시작 안 함
    await db.update_song(song_id, status="separating", progress=25)
    await _separate(song_id, src)

    out = stems_dir(song_id)
    missing = [s for s in config.STEMS
               if not (out / f"{s}.wav").exists() or (out / f"{s}.wav").stat().st_size == 0]
    if missing:
        raise RuntimeError(f"분리 결과가 불완전해요(없는 악기: {', '.join(missing)})")
    await transcode_stems(song_id)  # 재생용 압축본 — ready 전에 만들어 첫 로딩부터 빠르게
    await db.update_song(song_id, status="ready", progress=100)


async def _probe_duration(src: Path) -> float | None:
    """ffprobe(전 포맷) → 실패 시 soundfile(wav/flac) 순."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(src),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=_NO_WINDOW)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return float(out.decode().strip())
    except Exception:  # noqa: BLE001
        if proc is not None and proc.returncode is None:  # 타임아웃 등 — 고아 방지(코드검사 2026-07-17)
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
        try:
            import soundfile as sf
            return float(sf.info(str(src)).duration)
        except Exception:  # noqa: BLE001 — 길이는 표시용, 실패해도 진행
            return None


async def _download(song_id: int, url: str):
    await db.update_song(song_id, status="downloading", progress=1)

    # 메타 먼저 — 길이 한도 검사(REQ-ING-003) 및 제목·아티스트 확보. 실패 사유는 한국어로.
    # 아티스트 추정(사용자 요청 2026-07-10): artist/creator → 없으면 업로더(뮤직 채널 ' - Topic' 제거).
    # 빈 값은 'NA' 센티널 — 빈 줄은 아래 필터에 걸러져 줄 정렬이 밀리기 때문
    code, meta = await _run([config.PYTHON, "-m", "yt_dlp",
                             "--no-playlist", "--skip-download",
                             "--print", "title", "--print", "duration",
                             "--print", "%(artist,creator|NA)s",
                             "--print", "%(uploader|NA)s", url])
    if code != 0:
        raise RuntimeError("영상 정보를 가져올 수 없어요(주소가 잘못됐거나 연령 제한 등으로 다운로드가 막힌 영상이에요)")
    lines = [l for l in meta.splitlines() if l.strip()]
    artist = None
    if len(lines) >= 4:
        title, dur_s, art_s, up_s = lines[-4], lines[-3], lines[-2], lines[-1]
        if art_s != "NA":
            artist = art_s
        elif up_s != "NA":
            artist = re.sub(r"\s*-\s*Topic$", "", up_s)
    else:  # 구버전 yt-dlp 등 — 제목·길이만이라도
        title = lines[-2] if len(lines) >= 2 else url
        dur_s = lines[-1] if lines else ""
    try:
        duration = float(dur_s)
    except (ValueError, IndexError):
        duration = None
    limits = await db.get_limits()
    if duration and duration > limits["max_duration_min"] * 60:
        raise RuntimeError(
            f"영상이 너무 길어요({int(duration // 60)}분). {limits['max_duration_min']}분 이하만 추가할 수 있어요")
    await db.update_song(song_id, title=title[:200], duration=duration,
                         artist=(artist or "")[:200] or None)

    async def on_line(text: str):
        m = re.search(r"\[download\]\s+([\d.]+)%", text)
        if m:
            await db.update_song(song_id, progress=1 + float(m.group(1)) * 0.24)

    code, tail = await _run([config.PYTHON, "-m", "yt_dlp", url,
                             "-x", "--audio-format", "wav", "--no-playlist", "--newline",
                             "-o", str(config.RAW_DIR / f"{song_id}.%(ext)s")],
                            on_line=on_line, song_id=song_id)
    if code != 0 or raw_path(song_id) is None:
        raise RuntimeError("다운로드에 실패했어요. 잠시 후 다시 시도해 주세요")


async def _tuned_msst_config() -> Path:
    """장치 적응형 분리 설정(사용자 지적 2026-07-10: GPU 용 batch 가 CPU 에 그대로 물려져 있었음).
    기본 파일은 CPU 안전값(batch 1 — 실측: 출력 동일·시간 -30%·메모리 60%↓), GPU 감지 시에만
    8 로 올린다(MSST 원본값 — GPU PC 실측 전이므로 원본 유지). 실패 시 기본 파일 그대로 = CPU 안전."""
    from app import system
    try:
        dev = await system.device()
        if dev != "gpu":
            return config.MSST_CONFIG
        import yaml
        cfg = yaml.safe_load(config.MSST_CONFIG.read_text(encoding="utf-8"))
        cfg["inference"]["batch_size"] = 8
        out = config.MSST_CONFIG.parent / "config_htdemucs_6stems.gpu.yaml"
        out.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return out
    except Exception:  # noqa: BLE001 — 튜닝 실패는 기본(CPU 안전) 설정으로
        return config.MSST_CONFIG


async def _separate(song_id: int, src: Path):
    out = stems_dir(song_id)
    out.mkdir(parents=True, exist_ok=True)

    import os
    if os.environ.get("CHAEBO_FAKE_SEP") == "1":  # 테스트 전용 — 호출 시점 판독(캐시 무관)
        for s in config.STEMS:
            shutil.copyfile(src, out / f"{s}.wav")
        return

    with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as td:
        in_dir = Path(td)
        work_src = in_dir / f"{song_id}{src.suffix}"
        shutil.copyfile(src, work_src)

        async def on_line(text: str):
            m = re.search(r"(\d+)%\|", text)
            if m:
                await db.update_song(song_id, progress=25 + int(m.group(1)) * 0.73)

        # PYTHONPATH 에 app/compat 주입 — demucs.states 의 diffq 임포트를 스텁으로 충족
        # (pip 에 cp311 diffq 휠이 없음 — 우리는 비양자화 ckpt 만 쓰므로 스텁으로 충분)
        _pp = os.environ.get("PYTHONPATH", "")
        code, tail = await _run(
            [config.PYTHON, "inference.py",
             "--model_type", "htdemucs",
             "--config_path", str(await _tuned_msst_config()),
             "--start_check_point", str(config.MSST_CKPT),
             "--input_folder", str(in_dir),
             "--store_dir", str(in_dir / "result")],
            cwd=config.MSST_DIR, on_line=on_line, heavy=True, song_id=song_id,
            env_extra={"PYTHONPATH": str(config.COMPAT_DIR) + (os.pathsep + _pp if _pp else "")})
        if code != 0:
            raise RuntimeError(f"분리가 실패했어요(엔진 오류). 마지막 로그: {tail[-300:]}")

        produced = in_dir / "result" / str(song_id)
        for s in config.STEMS:
            f = produced / f"{s}.wav"
            if f.exists():
                shutil.move(str(f), out / f"{s}.wav")
