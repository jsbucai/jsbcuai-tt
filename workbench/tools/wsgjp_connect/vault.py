"""Windows current-user DPAPI storage, outside project and its backups."""
import json
import os
from pathlib import Path


class Vault:
    def __init__(self, root=None):
        self.root = Path(root or Path(os.environ['LOCALAPPDATA']) / 'ULWorkbench' / 'wsgjp')

    def load(self, name):
        import win32crypt
        if name not in {'credentials', 'token'}:
            raise ValueError('invalid vault entry')
        raw = (self.root / (name + '.dpapi')).read_bytes()
        return json.loads(win32crypt.CryptUnprotectData(raw, None, None, None, 1)[1])

    def _save(self, name, value):
        import win32crypt
        import win32api
        import win32security
        import ntsecuritycon
        self.root.mkdir(parents=True, exist_ok=True)
        handle = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(handle, win32security.TokenUser)[0]
        finally:
            handle.Close()
        acl = win32security.ACL()
        for identity in (sid, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)):
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
                win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS, identity)
        win32security.SetNamedSecurityInfo(str(self.root), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        sealed = win32crypt.CryptProtectData(raw, 'UL ERP read-only connection', None, None, None, 1)
        temporary = self.root / (name + '.dpapi.tmp')
        temporary.write_bytes(sealed)
        os.replace(temporary, self.root / (name + '.dpapi'))

    def save_credentials(self, value):
        from gateway import validate_credentials
        validate_credentials(value)
        self._save('credentials', value)

    def save_token(self, value):
        self._save('token', value)
