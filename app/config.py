"""경로·한도 설정. 전부 환경변수로 재정의 가능(테스트·배포 대비)."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    """공유 데이터 위치 — 설치 경로와 무관한 '사용자당 1곳'. 설치기(chaebo.iss)가 데이터를 두는
    %LOCALAPPDATA%\\chaebo\\data 와 동일하게 맞춰, 개발 체크아웃(E:/chaebo)·다른 설치·설치판이
    모두 같은 곡 라이브러리를 보게 한다(사용자 지적 2026-07-14: 앱/웹 곡이 다름 — 설치마다 data/ 가
    따로였음). 옛 설치-상대 위치의 데이터는 app.migrate 가 여기로 1회 복사(원본 보존)한다."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "chaebo" / "data"
    return Path.home() / ".chaebo" / "data"  # 비윈도우/환경변수 부재 폴백


DATA_DIR = Path(os.environ.get("CHAEBO_DATA", _default_data_dir()))
# 옛(설치-상대) 데이터 위치 — 마이그레이션 출발점. 설치판은 BASE_DIR==공유부모라 LEGACY==DATA_DIR(무동작).
LEGACY_DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
STEMS_DIR = DATA_DIR / "stems"
DB_PATH = DATA_DIR / "chaebo.sqlite3"

# 분리 엔진(MSST) — 스파이크 검증 경로 재사용. Phase 3 포장 시 재배치.
MSST_DIR = Path(os.environ.get("CHAEBO_MSST", BASE_DIR / "spikes" / "sp1" / "work" / "msst"))
# 자체 설정 사본(app/engine) — MSST 저장소 안 파일은 gitignore 라 튜닝이 유실됨.
# 실측 튜닝(2026-07-10): inference.batch_size 1 (출력 동일·시간 -30%·메모리 60% 절감)
MSST_CONFIG = Path(os.environ.get(
    "CHAEBO_MSST_CONFIG", BASE_DIR / "app" / "engine" / "config_htdemucs_6stems.yaml"))
MSST_CKPT = Path(os.environ.get(
    "CHAEBO_MSST_CKPT", BASE_DIR / "spikes" / "sp1" / "work" / "ckpt" / "htdemucs_6s.th"))

# 분리·다운로드 서브프로세스용 파이썬(이 venv)
PYTHON = os.environ.get("CHAEBO_PY", sys.executable)

# 부트스트랩이 내려받은 ffmpeg(vendor-tools) — 시스템에 없어도 동작하게 PATH 선두에.
# 서버가 띄우는 모든 서브프로세스(yt-dlp·분리·피치)가 env 를 상속하므로 여기 한 곳이면 충분
_FFMPEG_BIN = BASE_DIR / "vendor-tools" / "ffmpeg" / "bin"
if _FFMPEG_BIN.exists() and str(_FFMPEG_BIN) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = str(_FFMPEG_BIN) + os.pathsep + os.environ.get("PATH", "")

# diffq 스텁(app/compat) — demucs.states 가 임포트하지만 실사용은 양자화 ckpt 뿐(우리는 비양자화).
# cp311 휠이 없어 pip 설치 불가 → 분리 서브프로세스 PYTHONPATH 로 주입(사이트패키지 무수술)
COMPAT_DIR = BASE_DIR / "app" / "compat"

# 입력 한도 (REQ-ING-003 — 설정 가능)
MAX_FILE_MB = int(os.environ.get("CHAEBO_MAX_FILE_MB", "200"))
MAX_DURATION_MIN = int(os.environ.get("CHAEBO_MAX_DURATION_MIN", "20"))

ALLOWED_EXTS = {".mp3", ".wav", ".flac", ".m4a"}
STEMS = ["vocals", "drums", "bass", "guitar", "piano", "other"]

# 런타임 표시·업데이트 비교용 버전(설치기 버전은 chaebo.iss/readme 별도 — 릴리스마다 함께 올린다).
APP_VERSION = "0.7.10"

# 소리-화면 싱크 '공식 세대'(사용자 지적 2026-07-13: 싱크 구현이 바뀌면 옛 보정값이 stale 해진다).
# 표시시계 공식(heardTime·워크릿 _latency·drift 수식 등)이 바뀔 때마다 이 값을 올린다. 저장된
# sync_ms/sync_drift_ms_per_min 에 이 스탬프를 함께 기록하고, 로드 시 스탬프가 다르면 '옛 공식으로 맞춘
# 값'이라 그대로 쓰면 오히려 어긋나므로 0 으로 리셋하고 재보정을 안내한다. (스탬프 없는 기존 DB=옛 세대로 취급)
SYNC_FORMULA_VERSION = os.environ.get("CHAEBO_SYNC_FORMULA_VER", "2")
# 업데이트 확인(사용자 요청 2026-07-13) — 앱이 이 '공개' URL 의 version.json 을 읽어 최신 버전을 안내한다.
# 비공개 리포라 릴리스 API 직독 불가 → 관리자가 공개 gist/파일 하나만 두고 raw URL 을 여기(또는 환경변수
# CHAEBO_UPDATE_URL)에 넣는다. 형식: {"version":"0.6.9","url":"받는 곳","notes":"요약"}. 비우면 기능 꺼짐.
# 보내는 사용자 데이터 없음(GET 조회만) — '외부 전송 0' 원칙 부합(로컬 우선, 실패해도 기능 저하 없음).
UPDATE_INFO_URL = os.environ.get(
    "CHAEBO_UPDATE_URL",
    "https://gist.githubusercontent.com/majorTom-cat/4451c2bec5aae7d4412e6eeefe314865/raw/version.json",
).strip()

# 테스트용: 1 이면 분리 대신 스텁 스템 생성(실분리는 별도 관찰로 검증)
FAKE_SEP = os.environ.get("CHAEBO_FAKE_SEP") == "1"

# 분석(분리·채보) 서브프로세스 스레드 수 — 기본 코어 절반: PC 를 계속 쓸 수 있게 (사용자 피드백 2026-07-07)
WORK_THREADS = int(os.environ.get("CHAEBO_WORK_THREADS", max(2, (os.cpu_count() or 4) // 2)))


def ensure_dirs():
    for d in (DATA_DIR, RAW_DIR, STEMS_DIR):
        d.mkdir(parents=True, exist_ok=True)
