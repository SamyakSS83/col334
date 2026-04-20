import socket
import json
import time
import sys
import os

DEBUG_DETAIL = "--debug-detail" in sys.argv

def get_arg_val(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

def readline(sock, buf):
    while b"\n" not in buf:
        try:
            chunk = sock.recv(1024)
        except socket.timeout:
            return None, buf
        if not chunk:
            return None, buf
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return line.decode("utf-8", "ignore"), buf

def count_words(line, word_count):
    for w in line.strip().split(","):
        if w and w != "EOF":
            word_count[w] = word_count.get(w, 0) + 1

def main():
    with open("config.json") as f:
        cfg = json.load(f)

    if "server_port" not in cfg:
        cfg["server_port"] = cfg.get("port", 8887)

    server_ip = cfg["server_ip"]
    server_port = int(cfg["server_port"])
    k = int(os.environ.get("K", cfg["k"]))
    p = int(cfg["p"])
    num_iterations = int(cfg.get("num_iterations", 1))

    batch_size = int(get_arg_val("--batch-size", os.environ.get("BATCH_SIZE", "1")))
    client_id = get_arg_val("--client-id", os.environ.get("CLIENT_ID", "client"))

    os.makedirs("logs", exist_ok=True)
    log_path = os.path.join("logs", f"{client_id}.log")
    lf = open(log_path, "a", buffering=1)
    t_launch_ms = int(time.time() * 1000)
    
    lf.write(f"CLIENT {client_id} BATCH {batch_size}\n")
    lf.write(f"START {t_launch_ms}\n")

    word_count = {}
    wall_t0 = time.time()
    first_byte_ms = None

    for _ in range(num_iterations):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect((server_ip, server_port))

        offset = p
        buf = b""
        done = False

        pipeline_ok = True

        while True:
            to_send = max(1, batch_size) if pipeline_ok else 1
            cursor = offset
            for _ in range(to_send):
                if first_byte_ms is None:
                    first_byte_ms = int(time.time() * 1000)
                s.sendall(f"{cursor},{k}\n".encode())

                if DEBUG_DETAIL:
                    lf.write(f"REQ {cursor},{k}\n")

                cursor += k

            received_in_burst = 0
            ct = 0
            reconnect_needed = False

            while ct < to_send:
                line, buf2 = readline(s, buf)
                buf = buf2
                
                if DEBUG_DETAIL:
                    lf.write(f" RESP {line}\n")
                    lf.write(f"    BUF {buf}\n")

                if line is None:
                    offset += received_in_burst
                    reconnect_needed = True
                    pipeline_ok = False
                    break

                resp = line.strip()
                if resp == "EOF":
                    offset += received_in_burst
                    done = True
                    break

                count_words(resp, word_count)
                toks = [w for w in resp.split(",") if w and w != "EOF"]
                received_in_burst += len(toks)
                ct += 1

            if not reconnect_needed and not done:
                offset += received_in_burst

            if done:
                break

            if reconnect_needed:
                try:
                    s.close()
                except Exception:
                    pass
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(30)
                s.connect((server_ip, server_port))
                buf = b""
                if DEBUG_DETAIL:
                    lf.write(" RECONNECT\n")
                continue

        s.close()

    t_end_ms = int(time.time() * 1000)
    if first_byte_ms is None:
        elapsed_ms = int((time.time() - wall_t0) * 1000)
    else:
        elapsed_ms = t_end_ms - first_byte_ms

    total_words = sum(word_count.values())
    lf.write(f"END {t_end_ms}\n")
    lf.write(f"TOTAL_MS {elapsed_ms}\n")
    lf.write(f"WORDS {total_words}\n")

    lf.close()

if __name__ == "__main__":
    main()
