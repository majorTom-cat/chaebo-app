"""자체(로컬) CA + LAN 서버 인증서 — 휴대폰/태블릿에서 '앱 설치·오프라인·마이크(근음 듣기)'를 쓰려면
보안 연결(HTTPS)이 필요한데, 브라우저는 '신뢰된' 인증서가 아니면 서비스워커(PWA)·마이크를 막는다.

그래서 이 PC 안에 **로컬 CA(인증기관)**를 하나 만들고, LAN 서버 인증서를 그 CA 로 서명한다. 사용자가 폰에
**CA 인증서를 한 번 '신뢰'로 설치**하면, 그 CA 가 서명한 LAN 서버 인증서를 폰이 신뢰한다(PC IP 가 바뀌어도
서버 인증서만 새로 발급하면 되고 폰 재설치 불필요 — leaf 단독 방식의 단점 해소).

- CA 개인키·서버 개인키는 %LOCALAPPDATA%\\chaebo\\certs\\ 에만(사용자 전용) — 절대 동봉·공유·전송 금지.
- 폰에 배포되는 건 **CA 공개 인증서(DER)** 뿐(개인키 아님). 다운로드는 LAN 으로만.
- iOS 대비: 서버 인증서 유효기간 ≤825일 + EKU serverAuth + SAN(IP) 필수. CA 는 장수명(10년).
- cryptography 는 requirements 보장 아님(전이) → 첫 사용 시 온디맨드 pip(gpu.py 패턴). 실패 시 None → HTTPS 건너뜀.
"""
import datetime
import ipaddress
import os
import subprocess
import sys

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SERVER_DAYS = 800     # iOS 825일 한도 아래
_CA_DAYS = 3650


def _cert_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "chaebo", "certs")
    os.makedirs(d, exist_ok=True)
    return d


def _python_exe() -> str:
    py = sys.executable
    try:
        from app import config
        py = getattr(config, "PYTHON", py) or py
    except Exception:
        pass
    return py


def _ensure_cryptography() -> bool:
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


def ensure_ca():
    """로컬 CA(인증기관) — 한 번 만들어 재사용. (ca_cert_path, ca_key_path) 반환, 실패 시 None.
    폰이 이 CA 를 신뢰로 설치하면 이 CA 가 서명한 LAN 서버 인증서를 모두 신뢰한다."""
    d = _cert_dir()
    ca_crt = os.path.join(d, "chaebo-ca.crt")
    ca_key = os.path.join(d, "chaebo-ca.key")
    if os.path.isfile(ca_crt) and os.path.isfile(ca_key):
        return ca_crt, ca_key
    if not _ensure_cryptography():
        return None
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "chaebo local CA")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=_CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        with open(ca_key, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        with open(ca_crt, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return ca_crt, ca_key
    except Exception:
        return None


def ensure_cert(ips):
    """CA 로 서명한 LAN 서버 인증서. 이미 있고 IP 같고 만료 여유(>30일) 있으면 재사용. (certfile, keyfile) 반환.
    certfile 은 leaf + CA 체인(PEM). 실패 시 None."""
    ca = ensure_ca()
    if not ca:
        return None
    ca_crt_path, ca_key_path = ca
    d = _cert_dir()
    cert_path = os.path.join(d, "chaebo.crt")
    key_path = os.path.join(d, "chaebo.key")
    marker_path = os.path.join(d, "chaebo.san")
    ip_list = [ip for ip in (ips or []) if ip]
    want = "ca2|" + ",".join(sorted(set(["127.0.0.1", "localhost"] + ip_list)))

    try:
        from cryptography import x509
        if (all(os.path.isfile(p) for p in (cert_path, key_path, marker_path))
                and open(marker_path, encoding="utf-8").read().strip() == want):
            leaf = x509.load_pem_x509_certificate(open(cert_path, "rb").read())
            remain = leaf.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
            if remain.days > 30:
                return cert_path, key_path
    except Exception:
        pass

    if not _ensure_cryptography():
        return None
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        from cryptography.hazmat.primitives import hashes, serialization

        ca_cert = x509.load_pem_x509_certificate(open(ca_crt_path, "rb").read())
        ca_key = serialization.load_pem_private_key(open(ca_key_path, "rb").read(), None)

        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        san = [x509.DNSName("localhost")]
        for ip in ["127.0.0.1"] + ip_list:
            try:
                san.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except Exception:
                pass

        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "chaebo local")]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=_SERVER_DAYS))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        with open(cert_path, "wb") as f:  # leaf + CA 체인
            f.write(cert.public_bytes(serialization.Encoding.PEM))
            f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(want)
        return cert_path, key_path
    except Exception:
        return None


def ca_cert_der():
    """폰 설치용 CA 공개 인증서(DER 바이트). CA 없으면 만든다. 실패 시 None. (개인키 아님 — 공개 인증서만)"""
    ca = ensure_ca()
    if not ca:
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = x509.load_pem_x509_certificate(open(ca[0], "rb").read())
        return cert.public_bytes(serialization.Encoding.DER)
    except Exception:
        return None
