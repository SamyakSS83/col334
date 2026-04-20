import socket
import json
import time
import sys
import os

def count_words(line, word_count):
	for word in line.strip().split(","):
		if word and word != "EOF":
			word_count[word] = word_count.get(word, 0) + 1

def main():
	quiet = "--quiet" in sys.argv
	with open("config.json") as f:
		cfg = json.load(f)
	server_ip = cfg["server_ip"]
	server_port = cfg["server_port"]
	k = int(os.environ.get("K", cfg["k"]))
	p = int(cfg["p"])
	num_iterations = int(cfg.get("num_iterations", 1))

	word_count = {}
	start = time.time()
	for _ in range(num_iterations):
		s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		s.connect((server_ip, server_port))
		offset = p
		while True:
			req = f"{offset},{k}\n"
			s.sendall(req.encode())
			data = b""
			while not data.endswith(b"\n"):
				part = s.recv(1024)
				if not part:
					break
				data += part
			resp = data.decode().strip()
			if resp == "EOF":
				break
			count_words(resp, word_count)
			if "EOF" in resp:
				break
			offset += k
		s.close()
	elapsed = int((time.time() - start) * 1000)
	if not quiet:
		for w, c in word_count.items():
			print(f"{w}, {c}")
	print(f"ELAPSED_MS:{elapsed}")

if __name__ == "__main__":
	main()
