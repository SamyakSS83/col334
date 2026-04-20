import socket
import threading
import json
import queue

words = []
running = True
conn_queue = queue.Queue()  


def load_words(filename):
    with open(filename) as f:
        line = f.readline().strip()
        return line.split(",")


def process_request(offset, k):
    offset = int(offset)
    k = int(k)
    if offset >= len(words):
        return "EOF\n"
    chunk = words[offset:offset+k]
    resp = ",".join(chunk)
    if offset + k >= len(words):
        resp += ",EOF"
    return resp + "\n"


def serve_one_client(conn):
    try:
        while True:
            data = b""
            while not data.endswith(b"\n"):  # read full line
                part = conn.recv(1024)
                if not part:  # client closed connection
                    raise ConnectionError
                data += part

            req = data.decode().strip()
            if "," not in req:  # invalid request
                break

            offset, k = req.split(",")
            resp = process_request(offset, k)
            conn.sendall(resp.encode())

            if resp.startswith("EOF"):  # reached end of file
                break
    except Exception:
        pass
    finally:
        conn.close()


def dispatcher():
    while running:
        try:
            conn, addr = conn_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        threading.Thread(
            target=serve_one_client,
            args=(conn,),
            daemon=True
        ).start()


def main():
    global words
    # Load config
    with open("config.json") as f:
        cfg = json.load(f)
    server_ip = cfg["server_ip"]
    server_port = cfg["server_port"]
    filename = cfg["filename"]

    words = load_words(filename)

    # Setup TCP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((server_ip, server_port))
    sock.listen(8)
    print(f"Server listening on {server_ip}:{server_port}")

    # Start dispatcher thread
    dispatcher_thread = threading.Thread(target=dispatcher, daemon=True)
    dispatcher_thread.start()

    try:
        while running:
            conn, addr = sock.accept()
            conn_queue.put((conn, addr))  # enqueue connection
    except KeyboardInterrupt:
        print("Shutting down server...")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
