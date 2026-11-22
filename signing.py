"""
signing.py - digital (cryptographic) PDF signatures for Toolkit.

    create_certificate(...)  -> make a self-signed .pfx (+ .cer to share with recipients)
    sign_pdf(...)            -> PAdES signature, visible or invisible, optional free timestamp
    verify_pdf(...)          -> who signed, when, intact?, trusted?

Libraries:  cryptography (certificates)  and  pyHanko (PDF signing/validation).
    py -m pip install pyhanko cryptography

A self-signed certificate gives a real, tamper-evident signature.  Viewers show it as
"validity unknown" until the recipient trusts your certificate once (send them the .cer).
"""

from __future__ import annotations

import datetime as dt
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".toolkit"
DEFAULT_PFX = CONFIG_DIR / "my_certificate.pfx"
DEFAULT_CER = CONFIG_DIR / "my_certificate.cer"
TRUSTED_DIR = CONFIG_DIR / "trusted"          # drop other people's .cer files here
PROFILE_FILE = CONFIG_DIR / "signer.json"     # remembered name / location / reason

# Free public timestamp servers (RFC 3161).  Tried in order; signing still works if all fail.
TSA_URLS = ["https://freetsa.org/tsr", "http://timestamp.digicert.com", "http://timestamp.sectigo.com"]


class SigningError(Exception):
    pass


# --------------------------------------------------------------- certificate

def create_certificate(common_name: str, email: str = "", organisation: str = "", country: str = "",
                       password: str = "", years: int = 5, pfx_path: Path = DEFAULT_PFX,
                       cer_path: Path = DEFAULT_CER, key_bits: int = 3072) -> tuple[Path, Path]:
    """Generate a self-signed signing certificate.  Returns (pfx_path, cer_path)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        raise SigningError("The 'cryptography' package is missing:  py -m pip install cryptography")
    if not common_name.strip():
        raise SigningError("A name is required for the certificate.")
    if not password:
        raise SigningError("Choose a password to protect your private key.")

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_bits)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name.strip())]
    if email.strip():
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email.strip()))
    if organisation.strip():
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organisation.strip()))
    if country.strip():
        attrs.append(x509.NameAttribute(NameOID.COUNTRY_NAME, country.strip().upper()[:2]))
    name = x509.Name(attrs)
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(name).issuer_name(name)
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(minutes=5))
               .not_valid_after(now + dt.timedelta(days=365 * years))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                            data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION,
                                                     x509.ObjectIdentifier("1.2.840.113583.1.1.5")]),  # Adobe PDF signing
                              critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if email.strip():
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email.strip())]), critical=False)
    cert = builder.sign(key, hashes.SHA256())

    pfx_path = Path(pfx_path); cer_path = Path(cer_path)
    pfx_path.parent.mkdir(parents=True, exist_ok=True)
    pfx_path.write_bytes(pkcs12.serialize_key_and_certificates(
        common_name.encode(), key, cert, None, serialization.BestAvailableEncryption(password.encode())))
    cer_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    return pfx_path, cer_path


def certificate_info(pfx_path: Path, password: str) -> dict:
    """Subject / validity of the certificate inside a .pfx (also checks the password)."""
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        key, cert, _ = pkcs12.load_key_and_certificates(Path(pfx_path).read_bytes(), password.encode())
    except ValueError as e:
        raise SigningError("Could not open the certificate - wrong password?") from e
    if cert is None:
        raise SigningError("The .pfx contains no certificate.")
    return {"subject": cert.subject.rfc4514_string(),
            "name": _cn(cert),
            "valid_from": cert.not_valid_before_utc.isoformat(" ", "minutes"),
            "valid_to": cert.not_valid_after_utc.isoformat(" ", "minutes"),
            "expired": cert.not_valid_after_utc < dt.datetime.now(dt.timezone.utc),
            "self_signed": cert.issuer == cert.subject}


def _cn(cert) -> str:
    from cryptography.x509.oid import NameOID
    try:
        return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except IndexError:
        return cert.subject.rfc4514_string()


# ------------------------------------------------------------------ profile

def load_profile() -> dict:
    try:
        return json.loads(PROFILE_FILE.read_text())
    except Exception:
        return {}


def save_profile(**kw):
    CONFIG_DIR.mkdir(exist_ok=True)
    p = load_profile(); p.update({k: v for k, v in kw.items() if v is not None})
    PROFILE_FILE.write_text(json.dumps(p, indent=1))


# ------------------------------------------------------------------- signing

@dataclass
class SignOptions:
    pfx_path: Path = DEFAULT_PFX
    password: str = ""
    reason: str = ""
    location: str = ""
    contact: str = ""
    field_name: str = ""                 # auto-named if empty
    page: int = 0                        # 0-based
    box: tuple | None = None             # (x0, y0, x1, y1) in PDF user space, origin bottom-left; None = invisible
    image: str | bytes | None = None     # PNG/JPG of a handwritten signature to show in the box
    timestamp: bool = True
    certify: str = "no"                  # "no" | "forms" (allow form filling only) | "lock" (no changes)
    tsa_urls: list = field(default_factory=lambda: list(TSA_URLS))


def sign_pdf(src: str | Path | bytes, out_path: str | Path, opts: SignOptions) -> dict:
    """Sign a PDF.  src may be a path or raw PDF bytes.  Returns a small report dict."""
    try:
        from pyhanko import stamp
        from pyhanko.pdf_utils import images
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.sign import fields, signers, timestamps
    except ImportError:
        raise SigningError("pyHanko is missing:  py -m pip install pyhanko")

    pfx = Path(opts.pfx_path)
    if not pfx.exists():
        raise SigningError(f"Certificate not found: {pfx}\nUse 'Create my certificate' first.")
    certificate_info(pfx, opts.password)  # clean password check before pyHanko (which logs a noisy traceback)
    import logging
    logging.getLogger("pyhanko").setLevel(logging.ERROR)
    signer = signers.SimpleSigner.load_pkcs12(pfx_file=str(pfx), passphrase=opts.password.encode())
    if signer is None:
        raise SigningError("Could not load the certificate - wrong password?")

    data = Path(src).read_bytes() if not isinstance(src, bytes) else src

    existing = _existing_field_names(data)
    name = opts.field_name or next(f"Signature{i}" for i in range(1, 1000) if f"Signature{i}" not in existing)

    certify = opts.certify != "no"
    perms = {"forms": fields.MDPPerm.FILL_FORMS, "lock": fields.MDPPerm.NO_CHANGES}.get(opts.certify, fields.MDPPerm.FILL_FORMS)
    meta = signers.PdfSignatureMetadata(
        field_name=name, md_algorithm="sha256", subfilter=fields.SigSeedSubFilter.PADES,
        reason=opts.reason or None, location=opts.location or None, contact_info=opts.contact or None,
        certify=certify, docmdp_permissions=perms if certify else None,
        embed_validation_info=False, use_pades_lta=False)

    style = None
    if opts.box:
        lines = ["Digitally signed by %(signer)s", "Date: %(ts)s"]
        if opts.reason:
            lines.append("Reason: " + opts.reason.replace("%", "%%"))
        if opts.location:
            lines.append("Location: " + opts.location.replace("%", "%%"))
        bg = None
        if opts.image:
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(opts.image) if isinstance(opts.image, bytes) else opts.image).convert("RGBA")
                bg = images.PdfImage(im, opacity=0.35)
            except Exception:
                bg = None
        style = stamp.TextStampStyle(stamp_text="\n".join(lines), background=bg, border_width=1,
                                     background_opacity=0.35 if bg else 0)

    report = {"field": name, "timestamped": False, "tsa": None, "signer": signer.signing_cert.subject.human_friendly}

    def _do(timestamper):
        w2 = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
        if opts.box:
            fields.append_signature_field(w2, sig_field_spec=fields.SigFieldSpec(
                sig_field_name=name, on_page=opts.page, box=tuple(float(v) for v in opts.box)))
        pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=style, timestamper=timestamper)
        out = io.BytesIO()
        pdf_signer.sign_pdf(w2, output=out)
        return out.getvalue()

    signed = None
    if opts.timestamp:
        for url in opts.tsa_urls:
            try:
                signed = _do(timestamps.HTTPTimeStamper(url, timeout=10))
                report.update(timestamped=True, tsa=url)
                break
            except Exception:
                signed = None
    if signed is None:
        signed = _do(None)
        if opts.timestamp:
            report["warning"] = "No timestamp server could be reached - signed with the local clock instead."

    out_path = Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_bytes(signed); tmp.replace(out_path)
    report["output"] = str(out_path)
    return report


def _existing_field_names(data: bytes) -> set:
    """Names of all signature fields (filled or empty) already in the PDF."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.fields import enumerate_sig_fields
    names = set()
    try:
        r = PdfFileReader(io.BytesIO(data), strict=False)
        for fq_name, _value, _ref in enumerate_sig_fields(r):
            names.add(fq_name)
    except Exception:
        pass
    return names


# -------------------------------------------------------------- verification

def verify_pdf(path: str | Path | bytes) -> list[dict]:
    """Validate every signature.  Trusted roots = your own certificate + anything in ~/.toolkit/trusted/."""
    try:
        from pyhanko.keys import load_cert_from_pemder
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation import validate_pdf_signature
        from pyhanko_certvalidator import ValidationContext
    except ImportError:
        raise SigningError("pyHanko is missing:  py -m pip install pyhanko")

    roots = []
    for cer in [DEFAULT_CER] + (sorted(TRUSTED_DIR.glob("*.cer")) + sorted(TRUSTED_DIR.glob("*.crt")) + sorted(TRUSTED_DIR.glob("*.pem")) if TRUSTED_DIR.exists() else []):
        if Path(cer).exists():
            try:
                roots.append(load_cert_from_pemder(str(cer)))
            except Exception:
                pass
    # nothing is fetched: the addresses would be the file's own (whoever wrote it would learn it was opened, and
    # could aim the app at this computer's network), and only the certificates trusted here can make it "trusted"
    vc = ValidationContext(trust_roots=roots, allow_fetching=False, revocation_mode="soft-fail")

    data = Path(path).read_bytes() if not isinstance(path, bytes) else path
    r = PdfFileReader(io.BytesIO(data), strict=False)
    results = []
    for sig in r.embedded_signatures:
        entry = {"field": sig.field_name}
        try:
            st = validate_pdf_signature(sig, vc)
            entry.update({
                "signer": st.signing_cert.subject.human_friendly if st.signing_cert else "?",
                "signed_at": st.signer_reported_dt.isoformat(" ", "minutes") if st.signer_reported_dt else "?",
                "timestamp": (st.timestamp_validity.timestamp.isoformat(" ", "minutes")
                              if st.timestamp_validity and st.timestamp_validity.timestamp else None),
                "intact": bool(st.intact), "valid": bool(st.valid), "trusted": bool(st.trusted),
                "modified_after": st.modification_level.name if st.modification_level else "NONE",
                "covers_whole_doc": bool(st.coverage and st.coverage.name == "ENTIRE_FILE"),
                "certification": bool(sig.sig_object.get("/Reference")),
                "summary": st.summary(),
            })
        except Exception as e:
            entry.update({"intact": False, "valid": False, "trusted": False, "error": str(e)})
        results.append(entry)
    return results


def describe(results: list[dict]) -> str:
    if not results:
        return "This document has no digital signatures."
    out = []
    for r in results:
        if "error" in r:
            out.append(f"[{r['field']}]  could not validate: {r['error']}"); continue
        if not r["intact"]:
            verdict = "INVALID - the document was changed after signing"
        elif not r.get("valid"):
            verdict = "INVALID - the signature itself doesn't check out (it may be forged)"
        elif r["modified_after"] not in ("NONE", "LTA_UPDATES"):
            verdict = f"signature intact, but the document was modified afterwards ({r['modified_after'].lower().replace('_', ' ')})"
        elif r["trusted"]:
            verdict = "VALID and trusted"
        else:
            verdict = "VALID - signer certificate not yet trusted (self-signed or unknown issuer)"
        line = f"[{r['field']}]  {verdict}\n   Signed by: {r['signer']}\n   Date: {r['signed_at']}"
        if r.get("timestamp"):
            line += f"   (timestamped {r['timestamp']})"
        if r.get("certification"):
            line += "\n   Certification signature (document locked by the signer)"
        out.append(line)
    return "\n\n".join(out)
