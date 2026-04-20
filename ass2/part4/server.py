import socket
import threading
import json
import queue

words = []
running = True
conn_queue = queue.Queue()

pending = {}
active = []
lock = threading.Lock()
cv = threading.Condition(lock)

def load_words(filename):
    with open(filename, "r") as f:
        line = f.readline().strip()
    return [w for w in line.split(",") if w]

def process_request(offset, k):
    try:
        offset = int(offset); k = int(k)
        if k <= 0: return "HUH!\n"
    except Exception:
        return "HUH!\n"
    n = len(words)
    if offset >= n: return "EOF\n"
    end = min(offset + k, n)
    resp = ",".join(words[offset:end])
    if end >= n: resp += ",EOF"
    return resp + "\n"

def serve_one_client(conn):
    conn.settimeout(60)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    buf = b""
    try:
        with lock:
            pending[conn] = queue.Queue()
            active.append(conn)
            cv.notify_all()

        while True:
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk: return
                buf += chunk

            line, buf = buf.split(b"\n", 1)
            req = line.decode("utf-8", "ignore").strip()
            entry = None if "," not in req else tuple(req.split(",", 1))

            with lock:
                q = pending.get(conn)
                if q is None: return
                q.put(entry)
                cv.notify_all()
    finally:
        with lock:
            pending.pop(conn, None)
            if conn in active: active.remove(conn)
            cv.notify_all()
        try: conn.close()
        except: pass

def arbiter():
    idx = 0
    while True:
        with lock:
            if not active:
                cv.wait(timeout=0.1)
                continue
            n = len(active)
            idx %= n
            c = active[idx]
            q = pending.get(c)

            while (c in active) and (q is not None) and q.qsize() == 0:
                cv.wait(timeout=0.5)
                q = pending.get(c)

            if (c not in active) or (q is None):
                continue

            try:
                entry = q.get_nowait()
            except queue.Empty:
                continue

            cv.notify_all()
            idx = (idx + 1) % len(active)

        try:
            if entry is None:
                resp = "HUH!\n"
            else:
                off_s, k_s = entry
                resp = process_request(off_s, k_s)
            c.sendall(resp.encode("utf-8"))
        except Exception:
            with lock:
                pending.pop(c, None)
                if c in active: active.remove(c)
                cv.notify_all()
            try: c.close()
            except: pass

def dispatcher():
    while running:
        try:
            conn, _ = conn_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        threading.Thread(target=serve_one_client, args=(conn,), daemon=True).start()

def main():
    global words, running
    with open("config.json") as f:
        cfg = json.load(f)
    cfg.setdefault("server_port", cfg.get("port", 8887))
    cfg.setdefault("filename", "words.txt")

    server_ip  = cfg["server_ip"]
    server_port = int(cfg["server_port"])
    filename   = cfg["filename"]

    words = load_words(filename)
    repeat = int(cfg.get("repeat", 1))
    if repeat > 1: words *= repeat
    print(f"Server : tokens={len(words)} (repeat={repeat})")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((server_ip, server_port))
    sock.listen(128)
    print(f"Server listening on {server_ip}:{server_port}  (tokens={len(words)})")

    threading.Thread(target=dispatcher, daemon=True).start()
    threading.Thread(target=arbiter, daemon=True).start()

    try:
        while True:
            conn, addr = sock.accept()
            conn_queue.put((conn, addr))
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        with lock: cv.notify_all()
        try: sock.close()
        except: pass

if __name__ == "__main__":
    main()
