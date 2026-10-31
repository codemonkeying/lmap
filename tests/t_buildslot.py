# 4 processes want a build slot (BUILD_SLOTS=2). Each reserves 7 GB on the test dir and records the other
# builds' reservations it sees. Expect: never more than 2 at once; a holder sees 7 GB (the other) or 0.
import importlib.machinery, importlib.util, os, sys, time, fcntl, multiprocessing as mp, types
os.environ["XDG_STATE_HOME"] = sys.argv[1]
CNT = os.path.join(sys.argv[1], "cnt")
def load():
    ld = importlib.machinery.SourceFileLoader("lmap", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "lmap"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(m); return m
def bump(d, seen=None):
    fd = os.open(CNT, os.O_RDWR | os.O_CREAT); fcntl.flock(fd, fcntl.LOCK_EX)
    s = (os.pread(fd, 999, 0).decode().split() or ["0", "0"]); cur, mx = int(s[0]) + d, int(s[1]); rest = s[2:]
    if seen is not None: rest.append(str(seen))
    os.ftruncate(fd, 0); os.pwrite(fd, " ".join([str(cur), str(max(mx, cur))] + rest).encode(), 0)
    fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
def proc():
    m = load(); m.BUILD_SLOTS = 2
    ctx = types.SimpleNamespace(log=lambda s: None)
    with m.BuildSlot(ctx, "t") as sl:
        sl.reserve(7e9, sys.argv[1]); bump(1)
        time.sleep(0.4); seen = sl.reserved(sys.argv[1], sl.fd); bump(-1, int(seen / 1e9))
if __name__ == "__main__":
    t = time.time(); ps = [mp.Process(target=proc) for _ in range(4)]; [p.start() for p in ps]; [p.join() for p in ps]
    s = open(CNT).read().split()
    print("exit", [p.exitcode for p in ps], "final", s[0], "max-at-once", s[1], "others-reserved-GB", s[2:], f"{time.time()-t:.1f}s")
