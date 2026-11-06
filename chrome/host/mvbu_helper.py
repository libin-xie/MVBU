#!/usr/bin/env python3
"""MVBU helper: bridges the VS Code extension to UniVerse / UniData via Rocket uopy.

Protocol: one JSON object per line on stdin/stdout.
  request : {"id": 1, "method": "run_command", "params": {...}}
  response: {"id": 1, "result": {...}}  or  {"id": 1, "error": {"message": "..."}}

Run with --mock to use recorded responses (no uopy / server needed).
Run with --debug (or MVBU_DEBUG=1) to also log Python tracebacks.

Errors never escape as tracebacks: every failure becomes an error response
  {"id": 1, "error": {"message": "...", "code": 30106, "type": "UOError", "disconnected": false}}
and one log line. "disconnected": true means the server session was lost and has been dropped.
"""
import json
import os
import re
import socket
import sys
import time
import traceback

PROTO_OUT = sys.stdout
# Anything a library prints must never corrupt the protocol stream.
sys.stdout = sys.stderr

FM, VM, SM = "\xfe", "\xfd", "\xfc"
HERE = os.path.dirname(os.path.abspath(__file__))
MOCK_DIR = os.path.join(HERE, "mock")
DEBUG = "--debug" in sys.argv or bool(os.environ.get("MVBU_DEBUG"))


def log(*a):
    try:
        print("[mvbu-helper]", *a, file=sys.stderr, flush=True)
    except Exception:  # stderr closed: nothing more we can do
        pass


# --------------------------------------------------------------------------- errors
# uopy / UniRPC codes that mean the network session is gone. The session object is then unusable,
# so it is dropped and the extension is told to connect again.
CONNECTION_LOST_CODES = {81001, 81006, 81009, 81015, 81023}  # UniRPC: closed, sequence failure, internal, timed out, connection error
CONNECTION_LOST_RE = re.compile(r"RPC failed|connection (was )?(reset|closed|aborted|lost)|forcibly closed|broken pipe|not connected to the server", re.I)


class HelperError(Exception):
    """An error with a ready-made message (and optional code), raised by the helper itself."""

    def __init__(self, message, code=None):
        Exception.__init__(self, message)
        self.code = code


def error_code(e):
    """Numeric code of a uopy UOError (attribute or the '[30105]' in its text), else None."""
    for attr in ("code", "error_code", "errcode"):
        v = getattr(e, attr, None)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    m = re.search(r"\[(\d{4,6})\]", str(e))
    return int(m.group(1)) if m else None


def _chain(e):
    seen = []
    while e is not None and e not in seen and len(seen) < 10:
        seen.append(e)
        e = e.__cause__ or e.__context__
    return seen


def is_connection_lost(e):
    for x in _chain(e):
        if isinstance(x, (ConnectionError, BrokenPipeError, socket.timeout, EOFError)):
            return True
        if error_code(x) in CONNECTION_LOST_CODES or CONNECTION_LOST_RE.search(str(x)):
            return True
    return False


def describe_error(e, method="", params=None):
    """Exception -> error payload for the extension. Never raises."""
    try:
        params = params or {}
        code = error_code(e)
        msg = (str(e) or e.__class__.__name__).strip()
        # Say which API / command the server refused, so the message stands on its own.
        if method == "call_subroutine" and params.get("name"):
            msg += " (API %s, %d argument%s sent)" % (params["name"], len(params.get("args") or []), "" if len(params.get("args") or []) == 1 else "s")
        cause = [x for x in _chain(e)[1:] if not isinstance(x, HelperError)]
        if cause and str(cause[-1]) and str(cause[-1]) not in msg:
            msg += " \u2014 cause: %s: %s" % (cause[-1].__class__.__name__, cause[-1])
        return {"message": msg, "code": code, "type": e.__class__.__name__, "disconnected": is_connection_lost(e)}
    except Exception:  # pragma: no cover - last resort
        return {"message": repr(e), "code": None, "type": "Exception", "disconnected": False}


def to_str(value):
    """Convert a uopy DynArray / nested list / str into a plain mark-delimited string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    inner = getattr(value, "list", None)
    if inner is not None and not isinstance(value, list):
        return to_str(inner)
    if isinstance(value, (list, tuple)):
        parts = []
        for f in value:
            if isinstance(f, (list, tuple)):
                vals = []
                for v in f:
                    vals.append(SM.join(to_str(s) for s in v) if isinstance(v, (list, tuple)) else to_str(v))
                parts.append(VM.join(vals))
            else:
                parts.append(to_str(f))
        return FM.join(parts)
    return str(value)


def dict_item(item_id, rec):
    """Dictionary record (list of attributes) -> item dict, or None if not a field definition."""
    head = rec[0] if rec else ""
    typ = head[:1].upper()
    get = lambda n: rec[n].strip() if len(rec) > n else ""
    if typ == "D":
        # D-type: 1 field no, 2 conversion, 3 heading, 4 format, 5 S/M, 6 association
        if not get(1).isdigit():
            return None
        return {"id": item_id, "type": "D", "loc": int(get(1)), "conv": get(2), "heading": get(3),
                "format": get(4), "multi": get(5).upper().startswith("M"), "desc": head[1:].strip()}
    if typ in ("I", "V"):
        return {"id": item_id, "type": "I", "loc": None, "expr": get(1), "conv": get(2), "heading": get(3),
                "format": get(4), "multi": get(5).upper().startswith("M"), "desc": head[1:].strip()}
    if typ in ("A", "S"):
        # A/S-type (Pick style): 1 field no, 3 heading, 7 conversion, 9 justification, 10 width
        if not get(1).isdigit():
            return None
        return {"id": item_id, "type": typ, "loc": int(get(1)), "conv": get(7), "heading": get(3),
                "format": (get(10) + get(9)) if get(10) else "", "multi": False, "desc": ""}
    return None


# --------------------------------------------------------------------------- real backend
class UopyBackend:
    def __init__(self):
        import uopy  # noqa: imported lazily so --mock works without it
        self.uopy = uopy
        self.session = None
        self.pending = None  # Command awaiting a reply
        self.info = {}
        u = uopy
        self.EXEC_COMPLETE = getattr(u, "EXEC_COMPLETE", 0)
        self.EXEC_REPLY_REQUIRED = getattr(u, "EXEC_REPLY_REQUIRED", 1)
        self.EXEC_MORE_OUTPUT = getattr(u, "EXEC_MORE_OUTPUT", 2)

    def connect(self, p):
        self.disconnect({})
        kw = dict(
            host=p["host"], user=p["user"], password=p.get("password", ""),
            account=p["account"], service=p.get("service") or ("udcs" if p.get("database") == "UniData" else "uvcs"),
            port=int(p.get("port") or 31438),
        )
        if p.get("ssl"):
            kw["ssl"] = True
        if p.get("timeout"):
            kw["timeout"] = int(p["timeout"])
        self.session = self.uopy.connect(**kw)
        self.info = {"database": p.get("database"), "account": p["account"], "host": p["host"]}
        init = []
        for c in p.get("initCommands") or []:
            try:
                init.append({"command": c, "output": self._run_simple(c)})
            except Exception as e:  # init commands are best effort
                init.append({"command": c, "error": str(e)})
        return {"connected": True, "info": self.info, "init": init}

    def disconnect(self, _p):
        self.pending = None
        if self.session is not None:
            try:
                self.session.close()
            except Exception as e:  # a dead socket cannot be closed cleanly; that is fine
                log("close:", e)
        self.session = None
        return {"connected": False}

    drop = disconnect  # called after the connection was lost

    def _need(self):
        if self.session is None:
            raise HelperError("Not connected to the server. Connect again.")

    def _drain(self, cmd, out):
        while cmd.status == self.EXEC_MORE_OUTPUT:
            nxt = getattr(cmd, "next_response", None)
            if nxt is None:
                break
            nxt()
            out.append(to_str(cmd.response))
        return out

    def _result(self, cmd, out):
        if cmd.status == self.EXEC_REPLY_REQUIRED:
            self.pending = cmd
            return {"status": "reply_required", "output": "".join(out)}
        self.pending = None
        return {"status": "complete", "output": "".join(out)}

    def _run_simple(self, text):
        cmd = self.uopy.Command(text)
        cmd.run()
        return "".join(self._drain(cmd, [to_str(cmd.response)]))

    def run_command(self, p):
        self._need()
        cmd = self.uopy.Command(p["command"])
        cmd.run()
        return self._result(cmd, self._drain(cmd, [to_str(cmd.response)]))

    def reply(self, p):
        if self.pending is None:
            raise HelperError("No command is waiting for a reply")
        cmd = self.pending
        cmd.reply(p.get("text", ""))
        return self._result(cmd, self._drain(cmd, [to_str(cmd.response)]))

    def cancel(self, _p):
        cmd, self.pending = self.pending, None
        if cmd is not None and hasattr(cmd, "cancel"):
            try:
                cmd.cancel()
            except Exception as e:
                log("cancel failed:", e)
        return {"cancelled": True}

    def call_subroutine(self, p):
        self._need()
        args = list(p.get("args") or [])
        sub = self.uopy.Subroutine(p["name"], len(args))
        for i, a in enumerate(args):
            sub.args[i] = a
        sub.call()
        return {"args": [to_str(a) for a in sub.args]}

    def read_dict(self, p):
        """Return dictionary items for labelling and the Analyst view.

        Each item: {id, type, loc, heading, conv, format, multi, desc, expr}. D/A/S items carry
        their field number in loc; I/V (calculated) items have loc None and the formula in expr.
        """
        self._need()
        u = self.uopy
        dflag = getattr(u, "DICT_FILE", 1)
        f = u.File(p["file"], dflag)
        ids = []
        try:
            lst = u.List(9)
            lst.select(f)
            ids = to_str(lst.read_list()).split(FM)
        except Exception as e:
            log("List.select failed, falling back to SSELECT:", e)
            self._run_simple('SSELECT DICT %s TO 9' % p["file"])
            ids = to_str(u.List(9).read_list()).split(FM)
        items = []
        for i in [x for x in ids if x]:
            try:
                rec = to_str(f.read(i)).split(FM)
            except Exception:
                continue
            item = dict_item(i, rec)
            if item:
                items.append(item)
        try:
            f.close()
        except Exception:
            pass
        return {"items": items}

    def read_record(self, p):
        """Read one record. Returns {found, record} (record is mark-delimited)."""
        self._need()
        f = self.uopy.File(p["file"])
        try:
            rec = to_str(f.read(p["id"]))
            return {"found": True, "record": rec}
        except Exception as e:
            msg = str(e)
            if re.search(r"30001|not found|does not exist|no record", msg, re.I):
                return {"found": False, "record": ""}
            raise
        finally:
            try:
                f.close()
            except Exception:
                pass

    def select_records(self, p):
        """Run a SELECT command, then read up to `limit` of the selected records.

        Returns {total, rows: [{id, record}], output}. The select list is used up, so it
        does not stay active for later commands.
        """
        self._need()
        u = self.uopy
        out = self._run_simple(p["command"])
        ids = []
        try:
            ids = [x for x in to_str(u.List(0).read_list()).split(FM) if x]
        except Exception as e:  # empty list raises on some releases
            log("read_list after select:", e)
        total = len(ids)
        if not total:
            m = re.search(r"(\d+)\s+record", out or "")
            if m and int(m.group(1)) == 0:
                total = 0
        limit = max(1, int(p.get("limit") or 100))
        f = u.File(p["file"])
        rows = []
        try:
            for rid in ids[:limit]:
                try:
                    rows.append({"id": rid, "record": to_str(f.read(rid))})
                except Exception:
                    rows.append({"id": rid, "record": "", "missing": True})
        finally:
            try:
                f.close()
            except Exception:
                pass
        return {"total": total, "rows": rows, "output": out}

    def ping(self, _p):
        return {"connected": self.session is not None, "info": self.info}


def parse_dict_listing(txt):
    """Parse a recorded LIST DICT listing (fixed columns, wrapped cells) into dict items."""
    cols = [(0, 15), (15, 22), (22, 36), (36, 49), (49, 65), (65, 72), (72, 200)]
    rows, cur, started = [], None, False
    for raw in txt.splitlines():
        line = raw.rstrip("\r")
        if not started:
            started = line.startswith("Name......")
            continue
        if not line.strip() or re.match(r"^\d+ records? listed", line):
            cur = None
            continue
        raw_cells = [line[a:b].rstrip() for a, b in cols]
        cells = [c.strip() for c in raw_cells]
        if line[:1] != " " and cells[1]:
            cur = cells
            rows.append(cur)
        elif cur is not None:
            # wrapped cell: a leading space in the continuation means a word break
            for k, c in enumerate(cells):
                if c:
                    joiner = " " if raw_cells[k].startswith(" ") else ""
                    cur[k] = (cur[k] + joiner + c) if cur[k] else c
    items = []
    for name, tl, definition, conv, heading, fmt, depth in rows:
        parts = tl.split()
        typ = parts[0] if parts else ""
        if typ in ("D", "A", "S") and len(parts) > 1 and parts[1].isdigit():
            items.append({"id": name, "type": typ, "loc": int(parts[1]), "conv": conv, "heading": heading,
                          "format": fmt, "multi": depth.upper().startswith("M"), "desc": ""})
        elif typ in ("I", "V"):
            items.append({"id": name, "type": "I", "loc": None, "expr": definition, "conv": conv, "heading": heading,
                          "format": fmt, "multi": depth.upper().startswith("M"), "desc": ""})
    return items


# --------------------------------------------------------------------------- mock backend
class MockUOError(Exception):
    """Looks like uopy's UOError: has .code and prints as 'Error [code] : text'."""

    def __init__(self, code, text):
        Exception.__init__(self, "Error [%d] : %s" % (code, text))
        self.code = code


class MockBackend:
    """Recorded responses so the extension can be exercised without a server."""

    def __init__(self):
        self.connected = False
        self.pending = None
        self.info = {}

    def _file(self, name):
        path = os.path.join(MOCK_DIR, name)
        # exact-case match, also on case-insensitive disks (macOS), like file names on a server
        if name in os.listdir(MOCK_DIR) and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        return None

    def connect(self, p):
        if p.get("password") == "bad":
            raise RuntimeError("[mock] 39129: Invalid user name or password")
        self.connected = True
        self.info = {"database": p.get("database"), "account": p.get("account"), "host": p.get("host"), "mock": True}
        return {"connected": True, "info": self.info, "init": [{"command": c, "output": ""} for c in p.get("initCommands") or []]}

    def disconnect(self, _p):
        self.connected = False
        self.pending = None
        return {"connected": False}

    drop = disconnect

    def _need(self):
        if not self.connected:
            raise HelperError("Not connected to the server. Connect again.")

    def run_command(self, p):
        self._need()
        c = p["command"].strip()
        up = c.upper()
        time.sleep(0.05)
        m = re.match(r"LIST\s+DICT\s+(\S+)", up)
        if m:
            txt = self._file("LIST_DICT_%s.txt" % m.group(1))
            if txt:
                return {"status": "complete", "output": txt}
        if up.startswith("SLEEP"):
            time.sleep(float(up.split()[1]) if len(up.split()) > 1 else 5)
        if up.startswith("CLEAR.FILE") or "?" in up:
            self.pending = c
            return {"status": "reply_required", "output": "[mock] %s\nAre you sure? (Y/N) " % c}
        if up.startswith("CT "):
            parts = c.split()
            return {"status": "complete", "output": "[mock] CT output\n\n     %s\n0001 [line 1 of %s]\n0002 [line 2]\n0003 [line 3]\n" % (" ".join(parts[2:]) or parts[1], parts[1])}
        if up.startswith("BASIC "):
            parts = c.split()
            prog = parts[2] if len(parts) > 2 else "?"
            return {"status": "complete", "output": "[mock] Compiling: Source = '%s/%s', Object = '%s.O/%s'\n*\n\nCompilation Complete.\n" % (parts[1], prog, parts[1], prog)}
        if up.startswith("ERR.RPC"):
            # what uopy raises when the server or a firewall has closed an idle connection
            try:
                raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
            except ConnectionResetError as e:
                raise MockUOError(81009, "The RPC failed") from e
        if up.startswith("ERR"):
            raise RuntimeError("[mock] command failed: %s" % c)
        return {"status": "complete", "output": "[mock] %s\n\n%s\n%s\n3 records listed.\n" % (c, "-" * 200, "A very wide line " * 15)}

    def reply(self, p):
        if self.pending is None:
            raise RuntimeError("No command is waiting for a reply")
        c, self.pending = self.pending, None
        return {"status": "complete", "output": "%s\n[mock] done (%s)\n" % (p.get("text", ""), c)}

    def cancel(self, _p):
        self.pending = None
        return {"cancelled": True}

    def call_subroutine(self, p):
        self._need()
        args = list(p.get("args") or [])
        name = p["name"].upper()
        if name == "ERR.SUB":
            raise RuntimeError("[mock] Subroutine not found in catalog: ERR.SUB")
        if name == "ERR.LOAD":
            raise MockUOError(30105, "Unable to load the subroutine on the server")
        if name == "ERR.ARGS" or (name == "GET.PRODUCT" and len(args) != 4):
            raise MockUOError(30106, "Wrong number of arguments supplied to the subroutine")
        out = list(args)
        if len(out) >= 3:
            out[2] = FM.join(["[ISBN]", "[Sample title]", "2", "[Rating]", "[Time]", "[Aspect]", "[Genre]",
                              VM.join(["[Director 1]", "[Director 2]"]), VM.join([SM.join(["[Writer 1a]", "[Writer 1b]"]), "[Writer 2]"])])
        if len(out) >= 4:
            out[3] = "0"
        return {"args": out}

    def read_dict(self, p):
        self._need()
        txt = self._file("LIST_DICT_%s.txt" % p["file"])  # file names are case-sensitive, as on a server
        if txt is None:
            raise RuntimeError("[mock] File DICT %s not found" % p["file"])
        return {"items": parse_dict_listing(txt)}

    def _data(self, name):
        txt = self._file("%s.json" % name)
        if txt is None:
            raise RuntimeError("[mock] Unable to open file %s: file not found" % name)
        return json.loads(txt)

    @staticmethod
    def _rec(fields):
        def one(v):
            if isinstance(v, list):
                return VM.join(SM.join(x) if isinstance(x, list) else str(x) for x in v)
            return str(v)
        return FM.join(one(v) for v in fields)

    def read_record(self, p):
        self._need()
        data = self._data(p["file"])
        rec = data.get(p["id"])
        return {"found": rec is not None, "record": self._rec(rec) if rec is not None else ""}

    def select_records(self, p):
        """Understands the SELECT ... WITH ... BY ... commands the Analyst view builds."""
        self._need()
        cmd = p["command"]
        m = re.match(r"\s*S?SELECT\s+(\S+)(.*)$", cmd, re.I)
        if not m:
            raise RuntimeError("[mock] not a SELECT command: %s" % cmd)
        data = self._data(m.group(1))
        rest = m.group(2)
        locs = {}
        for it in parse_dict_listing(self._file("LIST_DICT_%s.txt" % m.group(1).upper()) or ""):
            if it["loc"] is not None:
                locs.setdefault(it["id"].upper(), it["loc"])

        def values(rid, field):
            loc = locs.get(field.upper())
            if loc == 0:
                return [rid]
            if loc is None:
                return [""]
            rec = data[rid]
            v = rec[loc - 1] if loc - 1 < len(rec) else ""
            if isinstance(v, list):
                return [" ".join(x) if isinstance(x, list) else str(x) for x in v] or [""]
            return [str(v)]

        def like(val, pat):
            rx = "^" + ".*".join(re.escape(x) for x in pat.split("...")) + "$"
            return re.match(rx, val, re.I) is not None

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None

        def test(val, op, want):
            a, b = num(val), num(want)
            cmp = (a > b) - (a < b) if a is not None and b is not None else (val > want) - (val < want)
            return {"=": val == want, "NE": val != want, "GT": cmp > 0, "LT": cmp < 0, "GE": cmp >= 0,
                    "LE": cmp <= 0, "LIKE": like(val, want), "UNLIKE": not like(val, want)}[op]

        where = re.split(r"\bBY(?:\.DSND)?\s", rest, maxsplit=1, flags=re.I)[0]
        conds = re.findall(r'WITH\s+(\S+)\s+(=|NE|GT|LT|GE|LE|LIKE|UNLIKE)\s+(?:"([^"]*)"|\'([^\']*)\')', where, re.I)
        use_or = bool(re.search(r"\bOR\s+WITH\b", where, re.I))
        ids = []
        for rid in data:
            res = [any(test(v, op.upper(), a if a or not b else b) for v in values(rid, f)) for f, op, a, b in conds]
            if not res or (any(res) if use_or else all(res)):
                ids.append(rid)
        for desc, field in reversed(re.findall(r"\bBY(\.DSND)?\s+(\S+)", rest, re.I)):
            ids.sort(key=lambda r: (num(values(r, field)[0]) is None, num(values(r, field)[0]) or 0, values(r, field)[0]), reverse=bool(desc))
        limit = max(1, int(p.get("limit") or 100))
        rows = [{"id": r, "record": self._rec(data[r])} for r in ids[:limit]]
        return {"total": len(ids), "rows": rows, "output": "%d record(s) selected to SELECT list #0." % len(ids)}

    def ping(self, _p):
        return {"connected": self.connected, "info": self.info}


class _Gone(Exception):
    """The extension closed the pipe; stop quietly."""


def send(obj):
    try:
        PROTO_OUT.write(json.dumps(obj) + "\n")
        PROTO_OUT.flush()
    except (BrokenPipeError, OSError, ValueError) as e:
        raise _Gone(e)


def handle(backend, init_error, req):
    """Run one request. Returns the response object; never raises (except _Gone from send)."""
    rid = req.get("id")
    method = str(req.get("method") or "")
    params = req.get("params") or {}
    try:
        if init_error:
            raise HelperError(init_error)
        fn = getattr(backend, method, None) if method and not method.startswith("_") else None
        if fn is None or method == "drop":
            raise HelperError("Unknown method: %s" % method)
        result = fn(params)
        json.dumps(result)  # make sure it can be sent before claiming success
        return {"id": rid, "result": result}
    except Exception as e:
        err = describe_error(e, method, params)
        if err["disconnected"] and backend is not None:
            try:
                backend.drop({})
            except Exception:
                pass
        tag = "%s%s" % (err["type"], " %s" % err["code"] if err["code"] else "")
        log("%s failed [%s]: %s%s" % (method or "?", tag, err["message"], " \u2014 session dropped, connect again" if err["disconnected"] else ""))
        if DEBUG:
            log(traceback.format_exc())
        return {"id": rid, "error": err}


def main():
    mock = "--mock" in sys.argv
    backend = None
    init_error = None
    try:
        backend = MockBackend() if mock else UopyBackend()
    except ImportError:
        init_error = "Python package 'uopy' is not installed for %s. Run: %s -m pip install uopy" % (sys.executable, sys.executable)
    except Exception as e:  # pragma: no cover
        init_error = "Could not start the helper: %s" % (str(e) or e.__class__.__name__)
    try:
        # bad bytes on stdin must not end the loop with UnicodeDecodeError
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        send({"id": 0, "result": {"ready": init_error is None, "mock": mock, "python": sys.version.split()[0], "error": init_error}})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("not an object")
            except ValueError as e:
                log("ignored a request that is not valid JSON:", e)
                continue
            if req.get("method") == "shutdown":
                send({"id": req.get("id"), "result": {"bye": True}})
                break
            send(handle(backend, init_error, req))
    except _Gone:
        pass
    except KeyboardInterrupt:
        pass
    except Exception as e:  # pragma: no cover - should never happen; keep it to one line
        log("helper stopped: %s: %s" % (e.__class__.__name__, e))
    finally:
        if backend is not None:
            try:
                backend.disconnect({})
            except Exception:
                pass


if __name__ == "__main__":
    main()
