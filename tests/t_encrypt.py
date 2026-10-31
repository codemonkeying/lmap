#!/usr/bin/env python3
"""export --encrypt: the AES-256 zip opens with the passphrase (lmap's own check and, if installed, 7-Zip), refuses a
wrong one, catches a flipped bit, and the passphrase never appears on a command line. Plus read_capped: in-memory
HTTP answers over the cap are refused, not cut short. Usage: t_encrypt.py <scratch>"""
import http.server, importlib.machinery, importlib.util, os, shutil, subprocess, sys, tempfile, threading
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
os.environ["LMAP_MAX_BODY"] = str(1 << 20); os.environ.setdefault("LMAP_BACKOFF", "0.01")
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


class C:
    def log(self, *a, **k):
        pass


PW = "correct horse battery staple"
src = tmp / "src"; (src / "sub").mkdir(parents=True)
(src / "big.bin").write_bytes(os.urandom(9_000_001))          # crosses the 4 MiB chunk edge, odd length
(src / "sub" / "note.txt").write_text("hello\n"); (src / "empty").write_bytes(b"")

# the passphrase must never reach argv: record every command lmap starts
seen = []
real_run, real_popen = subprocess.run, subprocess.Popen
subprocess.run = lambda a, *k, **kw: (seen.append(list(map(str, a))) or real_run(a, *k, **kw))
z = L.encrypt_zip(C(), src, tmp / "o.zip", PW)
subprocess.run = real_run
check(z.is_file() and not Path(str(z) + ".part").exists(), "zip written, no .part left")
check(not any(PW in " ".join(a) for a in seen), f"passphrase on no command line ({len(seen)} commands run)")
check(L.aes_zip_check(C(), z, PW, src) == 3, "own check: 3 entries decrypt to their sources")
try:
    L.aes_zip_check(C(), z, "wrong passphrase here"); check(False, "wrong passphrase refused")
except L.CliError as e:
    check("passphrase" in e.msg, f"wrong passphrase refused ({e.msg})")
b = bytearray(z.read_bytes()); b[300] ^= 1; (tmp / "flip.zip").write_bytes(b)
try:
    L.aes_zip_check(C(), tmp / "flip.zip", PW); check(False, "flipped bit caught")
except L.CliError as e:
    check("authentication" in e.msg, f"flipped bit caught ({e.msg})")
if shutil.which("7z"):                                        # independent reader (test only: private tmp)
    r = real_run(["7z", "x", "-p" + PW, "-o" + str(tmp / "x"), str(z)], capture_output=True, stdin=subprocess.DEVNULL)
    check(r.returncode == 0 and (tmp / "x" / "big.bin").read_bytes() == (src / "big.bin").read_bytes(),
          "7-Zip extracts it with the passphrase, bytes identical")
else:
    print("SKIP 7-Zip not installed")


# read_capped through http_get: over the cap = an error, under it = the full body
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        n = (2 << 20) if self.path == "/big" else 1000
        self.send_response(200); self.send_header("Content-Length", str(n)); self.end_headers()
        try:
            self.wfile.write(b"x" * n)
        except (BrokenPipeError, ConnectionResetError):
            pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
check(len(L.http_get(base + "/small") or b"") == 1000, "small answer read whole")
try:
    L.http_get(base + "/big"); check(False, "2 MB answer over a 1 MB cap refused")
except L.CliError as e:
    check("refusing" in e.msg, f"2 MB answer over a 1 MB cap refused ({e.msg[-40:]})")
srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
