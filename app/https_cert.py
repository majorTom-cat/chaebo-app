"""자체서명 인증서 — 휴대폰/태블릿에서 '근음 듣기'(마이크)를 쓰려면 보안 연결(HTTPS)이 필요하다.

브라우저의 getUserMedia(마이크)는 보안 컨텍스트(localhost 또는 HTTPS)에서만 동작한다. 같은 와이파이라도
http://192.168... 같은 LAN 평문 접속은 보안 컨텍스트가 아니라 마이크가 막힌다. 그래서 LAN 모드일 때만
이 PC 가 직접 만든 자체서명 인증서로 HTTPS 서버(별도 포트)를 함께 띄운다. 휴대폰에서 처음 열면
'안전하지 않음' 경고가 한 번 뜨지만(공인 인증서가 아니라서) '계속'을 누르면 정상 동작한다.

cryptography 는 requirements 의 보장된 항목이 아니라(전이 의존) 첫 사용 시 설치한다 — 빠른 업데이트가
pip 을 돌리지 않는 점을 반영한 온디맨드 설치(gpu.py 와 같은 패턴). 오프라인 등으로 실패하면 None 을
돌려주고 HTTPS 는 건너뛴다(앱 본체·데스크톱 로컬은 영향 없음, 튜너는 'PC 에서는 돼요' 안내로 폴백).

인증서/키는 데이터 폴더가 아니라 사용자별 %LOCALAPPDATA%\\chaebo\\certs\\ 에 둔다(개인키 — 공유/동봉 금지).
"""
import datetime
import ipaddress
import os
import subprocess
import sys

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _cert_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "chaebo", "certs")
    os.makedirs(d, exist_ok=True)
    return d


def _python_exe() -> str:
    """pip 을 돌릴 파이썬 — 번들판은 config.PYTHON(임베더블) 이 정확, 없으면 현재 인터프리터."""
    py = sys.executable
    try:
        from app import config
        py = getattr(config, "PYTHON", py) or py
    except Exception:
        pass
    return py


def _ensure_cryptography() -> bool:
    """cryptography 임포트 가능하면 True. 없으면 pip 으로 설치 시도(온디맨드). 실패 시 False."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        subprocess.run(
            [_python_exe(), "-m", "pip", "install", "--no-warn-script-location", "cryptography"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=420,
            creationflags=_NO_WINDOW,
        )
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


def ensure_cert(ips):
    """localhost + 주어진 LAN IP 들을 SAN 에 담은 자체서명 인증서를 만든다.
    이미 있고 IP 목록이 같으며 만료 전이면 그대로 재사용. (certfile, keyfile) 반환, 실패 시 None."""
    d = _cert_dir()
    cert_path = os.path.join(d, "chaebo.crt")
    key_path = os.path.join(d, "chaebo.key")
    marker_path = os.path.join(d, "chaebo.san")
    ip_list = [ip for ip in (ips or []) if ip]
    want_san = ",".join(sorted(set(["127.0.0.1", "localhost"] + ip_list)))

    # 재사용: 파일·마커가 있고 SAN 이 같으면(만료 여유 10년이라 별도 만료검사 생략) 그대로 쓴다.
    try:
        if (os.path.isfile(cert_path) and os.path.isfile(key_path)
                and os.path.isfile(marker_path)
                and open(marker_path, encoding="utf-8").read().strip() == want_san):
            return cert_path, key_path
    except Exception:
        pass

    if not _ensure_cryptography():
        return None

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "chaebo local")])

        san = [x509.DNSName("localhost")]
        for ip in ["127.0.0.1"] + ip_list:
            try:
                san.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except Exception:
                pass

        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(want_san)
        return cert_path, key_path
    except Exception:
        return None
