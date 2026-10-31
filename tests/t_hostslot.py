# 3 processes x 8 threads hammer host_slot on one fake host (limit 4); a shared flock'd counter file records the
# max simultaneous holders across processes. Expect max == 4 exactly.
import importlib.machinery, importlib.util, os, sys, threading, time, fcntl, multiprocessing as mp
os.environ["XDG_STATE_HOME"] = sys.argv[1]
def load():
    ld = importlib.machinery.SourceFileLoader("lmap", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "lmap"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(m); return m
CNT = os.path.join(sys.argv[1], "cnt")
def bump(d):
    fd = os.open(CNT, os.O_RDWR | os.O_CREAT); fcntl.flock(fd, fcntl.LOCK_EX)
    s = os.pread(fd, 64, 0).decode().split() or ["0", "0"]; cur, mx = int(s[0]) + d, int(s[1])
    mx = max(mx, cur); os.ftruncate(fd, 0); os.pwrite(fd, f"{cur} {mx}".encode(), 0); fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
def proc():
    m = load(); m.SERVERS["fake.test"] = {"start": 4, "max": 4, "slots": 4}
    def w():
        for _ in range(15):
            with m.host_slot("http://fake.test/x"):
                bump(1); time.sleep(0.01); bump(-1)
    ts = [threading.Thread(target=w) for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
if __name__ == "__main__":
    t = time.time(); ps = [mp.Process(target=proc) for _ in range(3)]; [p.start() for p in ps]; [p.join() for p in ps]
    print("exit codes", [p.exitcode for p in ps], "final/max", open(CNT).read(), f"{time.time()-t:.1f}s")
