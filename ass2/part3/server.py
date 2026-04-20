import socket
import threading
import json
import queue

words = []
running = True
conn_queue = queue.Queue()


def load_words(filename):
    with open(filename, "r") as f:
        line = f.readline().strip()
    return [w for w in line.split(",") if w]


def process_request(offset, k):
    try:
        offset = int(offset)
        k = int(k)
        if k <= 0:
            return "HUH!\n"
    except Exception:
        return "HUH!\n"

    n = len(words)
    if offset >= n:
        return "EOF\n"

    end = min(offset + k, n)
    chunk = words[offset:end]
    resp = ",".join(chunk)
    if end >= n:
        resp += ",EOF"
    return resp + "\n"


def serve_one_client(conn):
    conn.settimeout(60)

    # very very important!
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    buf = b""
    try:
        while True:
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk

            line, buf = buf.split(b"\n", 1)
            req = line.decode("utf-8", "ignore").strip()
            if not req:
                continue

            if "," not in req:
                conn.sendall(b"HUH!\n")
                continue
            off_s, k_s = req.split(",", 1)

            resp = process_request(off_s, k_s)

            conn.sendall(resp.encode("utf-8"))
    except socket.timeout:
        return
    except Exception:
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass


def dispatcher():
    while running:
        try:
            conn, addr = conn_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        threading.Thread(target=serve_one_client, args=(conn,), daemon=True).start()


def main():
    global words
    with open("config.json") as f:
        cfg = json.load(f)

    if "server_port" not in cfg:
        cfg["server_port"] = cfg.get("port", 8887)
    if "filename" not in cfg:
        cfg["filename"] = "words.txt"

    server_ip = cfg["server_ip"]
    server_port = int(cfg["server_port"])
    filename = cfg["filename"]

    words = load_words(filename)
    repeat = int(cfg.get("repeat", 1))
    if repeat > 1:
        words = words * repeat
    print(f"Server : tokens={len(words)} (repeat={repeat})")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((server_ip, server_port))
    sock.listen(128)
    print(f"Server listening on {server_ip}:{server_port}  (tokens={len(words)})")

    threading.Thread(target=dispatcher, daemon=True).start()

    try:
        while running:
            conn, addr = sock.accept()
            conn_queue.put((conn, addr))
    except KeyboardInterrupt:
        print("Shutting down server...")
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
