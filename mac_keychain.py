"""The Mac's Keychain for saved secrets - AI keys, the Shared AI code and My details.

The Mac counterpart of Windows Credential Manager (keys) and DPAPI (My details).  Items are generic passwords in
the person's login keychain: service = the same names the Windows side uses ("Toolkit/ai/relay", ...), account
"Toolkit".  Both stay fixed whatever the app is called (like the ~/.toolkit folder), so a rename keeps them.
Uses Apple's Security framework through pyobjc, which pywebview already installs on a Mac.
"""

from __future__ import annotations

import platform

IS_MAC = platform.system() == "Darwin"
ACCOUNT = "Toolkit"

_NOT_FOUND = -25300          # errSecItemNotFound
_MESSAGES = {
    -128: "the request was cancelled",                    # errSecUserCanceled
    -25293: "the Keychain password wasn't accepted",      # errSecAuthFailed
    -25308: "the Keychain is locked",                     # errSecInteractionNotAllowed
}


class KeychainError(Exception):
    def __init__(self, status: int, action: str):
        self.status = status
        super().__init__(f"Keychain {action} failed: {_MESSAGES.get(status, f'error {status}')}")

    @property
    def reason(self) -> str:
        """Plain words for a message: 'the Keychain is locked' etc."""
        return _MESSAGES.get(self.status, "the Keychain said no")


def _sec():
    import Security            # pyobjc-framework-Security (a pywebview dependency on a Mac)
    return Security


def _query(service: str) -> dict:
    S = _sec()
    return {S.kSecClass: S.kSecClassGenericPassword, S.kSecAttrService: service, S.kSecAttrAccount: ACCOUNT}


def get(service: str) -> bytes | None:
    """The saved bytes, or None if nothing is saved.  Raises KeychainError if the Keychain refuses."""
    S = _sec()
    q = {**_query(service), S.kSecReturnData: True, S.kSecMatchLimit: S.kSecMatchLimitOne}
    status, data = S.SecItemCopyMatching(q, None)
    if status == _NOT_FOUND:
        return None
    if status != 0:
        raise KeychainError(status, "read")
    return bytes(data) if data is not None else b""


def set(service: str, data: bytes, label: str = "", comment: str = ""):
    """Save (or replace) the bytes for `service`.  `label` is what Keychain Access shows."""
    S = _sec()
    from Foundation import NSData
    blob = NSData.dataWithBytes_length_(data, len(data))
    status = S.SecItemUpdate(_query(service), {S.kSecValueData: blob})
    if status == _NOT_FOUND:
        item = {**_query(service), S.kSecValueData: blob, S.kSecAttrLabel: label or service}
        if comment:
            item[S.kSecAttrComment] = comment
        status = S.SecItemAdd(item, None)
        status = status[0] if isinstance(status, tuple) else status
    if status != 0:
        raise KeychainError(status, "save")


def delete(service: str):
    """Remove the item; nothing saved is fine."""
    status = _sec().SecItemDelete(_query(service))
    if status not in (0, _NOT_FOUND):
        raise KeychainError(status, "delete")
