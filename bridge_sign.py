"""Digital signing for the Bridge (stage 3).

No certificate jargon reaches the page: a "signing identity" is a name + password (a self-signed
certificate in ~/.toolkit), "Sign" asks only for the password and writes  Name_signed.pdf  next to the
original.  Verification turns pyHanko's results into one plain sentence per signature.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from bridge_util import UserError, fitz, log, nice_time, safe, unique
from bridge_edit import load_signature, signature_art


def _signing():
    import signing
    return signing


def _cert_summary():
    """Name / email / expiry from the public .cer (no password needed)."""
    s = _signing()
    if not s.DEFAULT_CER.exists():
        return None
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    data = s.DEFAULT_CER.read_bytes()
    try:
        cert = x509.load_der_x509_certificate(data)
    except ValueError:
        cert = x509.load_pem_x509_certificate(data)

    def attr(oid):
        v = cert.subject.get_attributes_for_oid(oid)
        return v[0].value if v else ""
    exp = cert.not_valid_after_utc
    return {"name": attr(NameOID.COMMON_NAME), "email": attr(NameOID.EMAIL_ADDRESS),
            "org": attr(NameOID.ORGANIZATION_NAME), "expires": exp.strftime("%b %Y"),
            "expired": exp < dt.datetime.now(dt.timezone.utc)}


def _initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p[:1].isalpha()]
    return ("".join(p[0] for p in parts[:1] + parts[-1:]) if len(parts) > 1 else name[:2]).upper() or "?"


def _verdict(r: dict) -> tuple[str, str]:
    """(level, sentence) for one verify_pdf result.  level: ok | warn | bad"""
    who = r.get("signer_name") or "the signer"
    if "error" in r:
        return "bad", "This signature couldn't be checked - the file may be damaged."
    if not r.get("intact"):
        return "bad", "The document was changed after it was signed - don't trust its contents."
    if not r.get("valid"):
        return "bad", "The signature itself doesn't check out, so it may be forged - don't trust the name or the contents."
    if r.get("modified_after") not in ("NONE", "LTA_UPDATES", None):
        return "warn", "The signature is intact, but someone added changes (such as form entries or notes) afterwards."
    if r.get("trusted"):
        return "ok", f"Valid. Signed by {who} and not changed since."
    return "ok", (f"Not changed since {who} signed it. Your PC doesn't know {who} yet - "
                  "trust them once if you're sure this file really came from them.")


class SignMixin:
    """Mixed into app.Bridge; relies on self._lock, self._model, self._source, self._target, self._window."""

    # ---- identity
    @safe
    def identity_get(self):
        s = _signing()
        prof = s.load_profile()
        info = _cert_summary() if s.DEFAULT_PFX.exists() else None
        sig = load_signature()
        out = {"exists": bool(info), "reason": prof.get("reason") or "I approve this document",
               "location": prof.get("location", ""), "has_signature_image": bool(sig),
               "profile_name": prof.get("name", ""), "profile_email": prof.get("email", "")}
        if info:
            out.update(info, initials=_initials(info["name"]))
        return out

    @safe
    def identity_create(self, name: str, password: str, email: str = "", org: str = "", country: str = ""):
        name, email = (name or "").strip(), (email or "").strip()
        if not name:
            raise UserError("Type your name as you'd like it to appear on signatures.")
        if len(password or "") < 6:
            raise UserError("Choose a password of at least 6 characters - you'll type it each time you sign.")
        s = _signing()
        s.create_certificate(name, email, org or "", country or "", password)
        s.save_profile(name=name, email=email, org=org or "", country=country or "")
        log.info("signing identity created for %s", name)
        return {**self.identity_get(), "status": f"Signing identity set up for {name}."}

    @safe
    def identity_export(self, path: str | None = None):
        """Copy the shareable .cer somewhere so recipients can trust your signatures."""
        s = _signing()
        if not s.DEFAULT_CER.exists():
            raise UserError("Set up your signing identity first.")
        info = _cert_summary() or {}
        default_name = f"{info.get('name') or 'My'} - signing certificate.cer"
        if not path:
            if not self._window:
                return {"saved": str(s.DEFAULT_CER)}
            import webview
            res = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(Path.home() / "Documents"),
                                                  save_filename=default_name, file_types=("Certificate (*.cer)",))
            if not res:
                return {"cancelled": True}
            path = res if isinstance(res, str) else res[0]
        p = Path(path)
        if p.suffix.lower() != ".cer":
            p = p.with_suffix(".cer")
        shutil.copy(s.DEFAULT_CER, p)
        return {"saved": str(p), "status": f"Saved {p.name}. Send it to the people who receive your signed documents."}

    # ---- sign
    @safe
    def sign_document(self, opts: dict):
        """opts: password, reason, location, timestamp, lock, page, rect (display points) or None, with_image"""
        s = _signing()
        if not s.DEFAULT_PFX.exists():
            raise UserError("Set up your signing identity first.")
        password = opts.get("password") or ""
        try:
            info = s.certificate_info(s.DEFAULT_PFX, password)
        except s.SigningError:
            raise UserError("That password doesn't match your signing identity. Check Caps Lock and try again.")
        if info["expired"]:
            raise UserError("Your signing identity has expired. Set up a new one (it takes a few seconds).")
        with self._lock:
            m = self._need_doc()
            src = self._source or m.path or Path.home() / "Documents" / "Document.pdf"
            stem = src.stem[:-len("_edited")] if src.stem.endswith("_edited") else src.stem
            target = unique(src.with_name(f"{stem}_signed.pdf"))
            so = s.SignOptions(pfx_path=s.DEFAULT_PFX, password=password,
                               reason=(opts.get("reason") or "").strip(), location=(opts.get("location") or "").strip(),
                               timestamp=bool(opts.get("timestamp", True)),
                               certify="lock" if opts.get("lock") else "no")
            rect = opts.get("rect")
            if rect:
                i = int(opts.get("page", 0))
                pg = m.page(i)
                r = self._R(i, rect) * ~pg.transformation_matrix        # pyHanko wants PDF user space
                so.page = i
                so.box = (min(r.x0, r.x1), min(r.y0, r.y1), max(r.x0, r.x1), max(r.y0, r.y1))
                if opts.get("with_image"):
                    sig = self._build_signature(opts["sig"]) if opts.get("sig") else load_signature()
                    so.image = signature_art(sig)[0] if sig else None
            data = m.doc.tobytes(garbage=3, deflate=True)
        rep = s.sign_pdf(data, target, so)       # outside the lock: the timestamp server can take a few seconds
        s.save_profile(reason=so.reason, location=so.location)
        log.info("signed %s -> %s", src, target)
        status = f"Signed and saved as {target.name}."
        if rep.get("warning"):
            status += " (No internet timestamp - the PC's clock was used.)"
        return {"signed": str(target), "name": target.name, "timestamped": rep.get("timestamped", False), "status": status}

    # ---- verify
    def _check_file(self):
        return self._on_disk()

    @safe
    def verify_report(self):
        with self._lock:
            path = self._check_file()
            dirty = bool(self._model and self._model.dirty)
        if not path:
            raise UserError("Save the document first.")
        s = _signing()
        results = s.verify_pdf(path)
        out = []
        for k, r in enumerate(results):
            signer = r.get("signer", "") or ""
            cn = next((p.split(":", 1)[1].strip() for p in signer.split(",") if p.strip().lower().startswith("common name")), signer)
            r["signer_name"] = cn or "someone"
            level, sentence = _verdict(r)
            out.append({"index": k, "signer": r["signer_name"], "when": nice_time(r.get("signed_at")), "level": level,
                        "sentence": sentence, "trusted": bool(r.get("trusted")), "timestamped": bool(r.get("timestamp")),
                        "locked": bool(r.get("certification")), "can_trust": level == "ok" and not r.get("trusted")})
        return {"file": path.name, "signatures": out, "unsaved_edits": dirty}

    @safe
    def trust_signer(self, index: int):
        """Remember the certificate of signature #index so this PC shows it as trusted from now on."""
        with self._lock:
            path = self._check_file()
        if not path:
            raise UserError("Save the document first.")
        import io
        from pyhanko.pdf_utils.reader import PdfFileReader
        s = _signing()
        r = PdfFileReader(io.BytesIO(Path(path).read_bytes()), strict=False)
        sigs = list(r.embedded_signatures)
        if not 0 <= int(index) < len(sigs):
            raise UserError("That signature isn't in the file any more.")
        checked = s.verify_pdf(path)
        if int(index) >= len(checked) or _verdict(dict(checked[int(index)]))[0] != "ok":
            raise UserError("That signature doesn't check out, so it can't be trusted from here.")
        cert = sigs[int(index)].signer_cert
        name = cert.subject.human_friendly
        cn = next((p.split(":", 1)[1].strip() for p in name.split(",") if p.strip().lower().startswith("common name")), name)
        s.TRUSTED_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c for c in cn if c.isalnum() or c in " -_.").strip() or "signer"
        (s.TRUSTED_DIR / f"{safe_name}.cer").write_bytes(cert.dump())
        return {**self.verify_report(), "status": f"{cn} is now trusted on this PC."}

    @safe
    def trusted_folder(self):
        s = _signing()
        s.TRUSTED_DIR.mkdir(parents=True, exist_ok=True)
        return self.open_external(str(s.TRUSTED_DIR))
