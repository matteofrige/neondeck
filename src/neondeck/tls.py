"""Optional HTTPS for the LAN listener: a private, name-constrained CA and a server certificate signed by it."""
from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import plistlib
import socket
import ssl
import uuid
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("neondeck.tls")

CA_YEARS = 10
LEAF_DAYS = 397  # the longest lifetime Apple platforms accept
RENEW_BEFORE = dt.timedelta(days=30)
FILES = ("ca.key", "ca.pem", "server.key", "server.pem")

# Limits the damage if the CA key is stolen: the CA cannot sign for public domains.
PERMITTED = [
    x509.DNSName("local"),
    *(x509.IPAddress(ipaddress.ip_network(n)) for n in
      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")),
]


def tls_dir(home: Path) -> Path:
    d = home / "tls"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _short_host() -> str:
    return socket.gethostname().split(".")[0]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load_ca(d: Path):
    cert = x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())
    key = serialization.load_pem_private_key((d / "ca.key").read_bytes(), None)
    return cert, key


def ensure_ca(home: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    d = tls_dir(home)
    if (d / "ca.pem").is_file() and (d / "ca.key").is_file():
        try:
            return _load_ca(d)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"CA locale illeggibile in {d}: {exc}") from exc
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"Neondeck Local CA ({_short_host()})"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Neondeck"),
    ])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365 * CA_YEARS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.NameConstraints(permitted_subtrees=PERMITTED, excluded_subtrees=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    _atomic_write(d / "ca.key", _key_pem(key), 0o600)
    _atomic_write(d / "ca.pem", cert.public_bytes(serialization.Encoding.PEM), 0o644)
    log.info("Creata la CA locale in %s", d)
    return cert, key


def _san_set(cert: x509.Certificate) -> set[str]:
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {*san.get_values_for_type(x509.DNSName),
            *(str(i) for i in san.get_values_for_type(x509.IPAddress))}


def _leaf_ok(d: Path, ca_cert: x509.Certificate, wanted: set[str]) -> bool:
    try:
        leaf = x509.load_pem_x509_certificate((d / "server.pem").read_bytes())  # first cert of the chain
        if not (d / "server.key").is_file():
            return False
        if leaf.not_valid_after_utc - _now() < RENEW_BEFORE:
            return False
        if _san_set(leaf) != wanted:
            return False
        leaf.verify_directly_issued_by(ca_cert)
        return True
    except Exception:  # unreadable or foreign files: regenerate
        return False


def ensure_leaf(home: Path, names: list[str], ips: list[str]) -> bool:
    """Create or renew the server certificate; True when it was regenerated."""
    ca_cert, ca_key = ensure_ca(home)
    d = tls_dir(home)
    names = list(dict.fromkeys(names))
    ips = [str(ipaddress.ip_address(i)) for i in dict.fromkeys(ips)]
    if _leaf_ok(d, ca_cert, set(names) | set(ips)):
        return False
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    cn = names[0] if names else (ips[0] if ips else "neondeck")
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    chain = cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    _atomic_write(d / "server.key", _key_pem(key), 0o600)
    _atomic_write(d / "server.pem", chain, 0o644)
    log.info("Certificato server generato per %s", ", ".join(names + ips))
    return True


def server_context(home: Path, names: list[str], ips: list[str]) -> ssl.SSLContext:
    ensure_ca(home)
    ensure_leaf(home, names, ips)
    d = tls_dir(home)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(d / "server.pem", d / "server.key")
    return ctx


def ca_pem(home: Path) -> bytes:
    ensure_ca(home)
    return (tls_dir(home) / "ca.pem").read_bytes()


def _der(home: Path) -> bytes:
    return x509.load_pem_x509_certificate(ca_pem(home)).public_bytes(serialization.Encoding.DER)


def ca_fingerprint(home: Path) -> str | None:
    path = home / "tls" / "ca.pem"
    if not path.is_file():
        return None
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except ValueError:
        return None
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def mobileconfig(home: Path, hostname: str) -> bytes:
    """Unsigned iOS profile that installs the CA as a root certificate."""
    fp = ca_fingerprint(home) or ""
    if not fp:
        ensure_ca(home)
        fp = ca_fingerprint(home) or ""
    ident = "io.neondeck.ca." + fp.replace(":", "")[:16]
    root = {
        "PayloadType": "com.apple.security.root",
        "PayloadContent": _der(home),
        "PayloadCertificateFileName": "neondeck-ca.cer",
        "PayloadDisplayName": "Neondeck Local CA",
        "PayloadIdentifier": ident + ".root",
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, fp + "/root")).upper(),
        "PayloadVersion": 1,
    }
    profile = {
        "PayloadType": "Configuration",
        "PayloadDisplayName": f"Neondeck ({hostname})",
        "PayloadIdentifier": ident,
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, fp)).upper(),
        "PayloadVersion": 1,
        "PayloadDescription": "Permette all'iPhone di fidarsi della connessione HTTPS verso Neondeck sul tuo computer.",
        "PayloadContent": [root],
    }
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML)


def status(home: Path) -> dict:
    d = home / "tls"
    out = {"ca": False, "ca_fingerprint": None, "ca_expires": None, "leaf_expires": None, "leaf_names": []}
    try:
        ca = x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())
    except (OSError, ValueError):
        return out
    out.update(ca=True, ca_fingerprint=ca_fingerprint(home), ca_expires=ca.not_valid_after_utc.isoformat())
    try:
        leaf = x509.load_pem_x509_certificate((d / "server.pem").read_bytes())
        out["leaf_expires"] = leaf.not_valid_after_utc.isoformat()
        out["leaf_names"] = sorted(_san_set(leaf))
    except (OSError, ValueError, x509.ExtensionNotFound):
        pass
    return out


def rotate_ca(home: Path) -> None:
    d = tls_dir(home)
    for name in FILES:
        (d / name).unlink(missing_ok=True)
    ensure_ca(home)


def default_names(cfg: dict) -> list[str]:
    names = [f"{cfg.get('lan_hostname', 'neondeck')}.local"]
    bonjour = f"{_short_host().lower()}.local"
    if bonjour not in names:
        names.append(bonjour)
    return names
