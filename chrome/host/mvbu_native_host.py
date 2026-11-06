#!/usr/bin/env python3
"""MVBU native messaging host for Chrome / Edge.

Chrome starts this program when the MVBU extension page opens, and talks to it with
"native messaging": each message is a 4-byte length (native byte order) + UTF-8 JSON.

It wraps the same helper the VS Code extension uses (mvbu_helper.py, Rocket uopy), so
commands, API calls, errors and the 81009 handling behave the same.

  extension -> host : {"id": 1, "method": "run_command", "params": {...}}
                      first message: {"id": 0, "method": "init", "params": {"mock": false, "debug": false}}
  host -> extension : {"id": 1, "result": {...}}  or  {"id": 1, "error": {...}}
                      {"log": "..."}                                  one log line
                      {"id": 1, "chunk": 0, "of": 3, "data": "..."}   a reply split into parts (Chrome's 1 MB limit)

Extra methods: secret_get / secret_set / secret_delete keep passwords in the system store
(macOS Keychain, Windows Credential Manager, or the Python 'keyring' package).
"""
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for d in (HERE, os.path.join(HERE, "..", "..", "helper")):
    if os.path.exists(os.path.join(d, "mvbu_helper.py")):
        sys.path.insert(0, os.path.abspath(d))
        break

RAW_IN = sys.stdin.buffer
RAW_OUT = sys.stdout.buffer
if sys.platform == "win32":  # native messaging is binary: no CR/LF translation
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

MAX_OUT = 900 * 1024  # Chrome refuses host messages over 1 MB
SERVICE = "MVBU"


def write_frame(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    RAW_OUT.write(struct.pack("@I", len(data)))
    RAW_OUT.write(data)
    RAW_OUT.flush()


def send(obj):
    """Send one message, split into chunks when it is too big for Chrome."""
    text = json.dumps(obj, ensure_ascii=False)
    if len(text.encode("utf-8")) <= MAX_OUT:
        write_frame(obj)
        return
    size = MAX_OUT // 4  # characters; up to 4 bytes each in UTF-8
    parts = [text[i:i + size] for i in range(0, len(text), size)]
    for i, p in enumerate(parts):
        write_frame({"id": obj.get("id"), "chunk": i, "of": len(parts), "data": p})


class LogPipe:
    """stdout/stderr replacement: every printed line becomes a {"log": ...} message.

    Nothing may be written to the real stdout except framed messages, or Chrome drops the connection.
    """

    def __init__(self):
        self.buf = ""

    def write(self, s):
        self.buf += str(s)
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                try:
                    write_frame({"log": line})
                except Exception:
                    pass
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


sys.stdout = sys.stderr = LogPipe()
import mvbu_helper as H  # noqa: E402  (after the pipes are in place: it prints on import errors)


def read_frame():
    head = RAW_IN.read(4)
    if len(head) < 4:
        return None
    (n,) = struct.unpack("@I", head)
    body = RAW_IN.read(n)
    if len(body) < n:
        return None
    return json.loads(body.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- passwords
def _keyring():
    try:
        import keyring  # noqa
        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def secret_store():
    if _keyring():
        return "keyring"
    if sys.platform == "darwin":
        return "keychain"
    if sys.platform == "win32":
        return "wincred"
    return "none"


def _win():
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                    ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    return ctypes, CREDENTIAL, adv


def secret(op, key, value=None):
    store = secret_store()
    target = "%s:%s" % (SERVICE, key)
    if store == "keyring":
        kr = _keyring()
        if op == "get":
            return kr.get_password(SERVICE, key)
        if op == "set":
            kr.set_password(SERVICE, key, value)
            return True
        try:
            kr.delete_password(SERVICE, key)
        except Exception:
            pass
        return True
    if store == "keychain":
        if op == "get":
            p = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", key, "-w"], capture_output=True, text=True)
            return p.stdout.rstrip("\n") if p.returncode == 0 else None
        if op == "set":
            p = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", key, "-w", value], capture_output=True, text=True)
            if p.returncode != 0:
                raise H.HelperError("Could not save the password in the Keychain: %s" % (p.stderr.strip() or p.returncode))
            return True
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", key], capture_output=True)
        return True
    if store == "wincred":
        ctypes, CREDENTIAL, adv = _win()
        if op == "get":
            pc = ctypes.POINTER(CREDENTIAL)()
            if not adv.CredReadW(target, 1, 0, ctypes.byref(pc)):
                return None
            try:
                c = pc.contents
                return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
            finally:
                adv.CredFree(pc)
        if op == "set":
            blob = value.encode("utf-8")
            c = CREDENTIAL()
            c.Type, c.Persist, c.TargetName, c.UserName = 1, 2, target, key
            c.CredentialBlobSize = len(blob)
            c.CredentialBlob = ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char))
            if not adv.CredWriteW(ctypes.byref(c), 0):
                raise H.HelperError("Could not save the password in Windows Credential Manager (error %d)" % ctypes.get_last_error())
            return True
        adv.CredDeleteW(target, 1, 0)
        return True
    raise H.HelperError("No password store on this computer. Install one with: %s -m pip install keyring" % sys.executable)


class Host:
    def __init__(self):
        self.backend = None
        self.init_error = "Not initialised"

    def init(self, p):
        if self.backend is not None:
            try:
                self.backend.disconnect({})
            except Exception:
                pass
        mock = bool(p.get("mock"))
        H.DEBUG = bool(p.get("debug"))
        self.backend, self.init_error = None, None
        try:
            self.backend = H.MockBackend() if mock else H.UopyBackend()
        except ImportError:
            self.init_error = "Python package 'uopy' is not installed for %s. Run: %s -m pip install uopy" % (sys.executable, sys.executable)
        except Exception as e:
            self.init_error = "Could not start the helper: %s" % (str(e) or e.__class__.__name__)
        return {"ready": self.init_error is None, "mock": mock, "python": sys.version.split()[0], "executable": sys.executable,
                "platform": sys.platform, "secrets": secret_store(), "error": self.init_error}

    def request(self, req):
        method = req.get("method")
        params = req.get("params") or {}
        rid = req.get("id")
        if method == "init":
            return {"id": rid, "result": self.init(params)}
        if method in ("secret_get", "secret_set", "secret_delete"):
            try:
                return {"id": rid, "result": {"value": secret(method[7:], str(params.get("key")), params.get("value"))}}
            except Exception as e:
                err = H.describe_error(e, method, {})
                H.log("%s failed: %s" % (method, err["message"]))
                return {"id": rid, "error": err}
        return H.handle(self.backend, self.init_error, req)


def main():
    host = Host()
    try:
        while True:
            try:
                req = read_frame()
            except ValueError as e:
                H.log("ignored a message that is not valid JSON:", e)
                continue
            if req is None:  # Chrome closed the connection (the MVBU tab was closed)
                break
            if not isinstance(req, dict):
                continue
            if req.get("method") == "shutdown":
                send({"id": req.get("id"), "result": {"bye": True}})
                break
            send(host.request(req))
    except (BrokenPipeError, OSError, KeyboardInterrupt):
        pass
    finally:
        if host.backend is not None:
            try:
                host.backend.disconnect({})
            except Exception:
                pass


if __name__ == "__main__":
    main()
