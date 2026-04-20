# Assignment4

## Overview

In this assignment, you will be tasked with creating a client-server application where the client downloads a pre-specified file from the server using **UDP**. Since UDP (User Datagram Protocol) is unreliable and does not provide congestion control, you will design and implement mechanisms to ensure **reliability** and **congestion control** at the application layer. In particular, you should implement:

- **Reliability:** Ensure that all packets are delivered to the client in the correct order and without loss.
- **Congestion Control:** Implement a congestion control algorithm to prevent overwhelming the network. 

---

## Part 1: Reliability (40%)

Since UDP does not provide reliability, your task is to build a reliable data transfer protocol on top of it. You’ll implement a sliding window protocol that ensures reliable and in-order delivery. While we won’t fix the exact design for you — you’ll have some flexibility — your protocol should include the following key ideas:

- **Packet Numbering:**  
  Each packet should carry a sequence number in its header. The client will use these sequence numbers to put the received data in order and detect missing packets.

- **Acknowledgments (ACKs):**  
  The client should send ACKs to let the server know which packets have arrived successfully. You can use cumulative ACKs (like TCP does), which generally perform well. You can also add Selective ACKs (SACKs) for better efficiency — see [RFC 2018](https://datatracker.ietf.org/doc/html/rfc2018) if you want to learn how TCP implements SACK. Of course, you can have your own implementation of the SACKs. 

- **Timeouts:**  
  If the server doesn’t receive an ACK within a certain time (called the *Retransmission Timeout*, or RTO), it should retransmit the packet. The RTO value could be estimated using a method similar to what we discussed in class.

- **Fast Retransmit:**  
  Waiting for timeouts can be slow. To speed things up, you should also implement a fast retransmit mechanism — for example, resend a packet after receiving three duplicate ACKs. If you use SACKs, you can design an even smarter mechanism.

---

### Connection Setup

Implement reliability only for the server-to-client transfer. For the initial client-to-server message (the file request), you can simply retry up to five times until the request succeeds. Use a 2-second timeout between retries. You can assume that the server handles only one client at a time.

### Sender Window Size

Recall that the sender window size (SWS) limits the number of bytes that can be “in flight” at once. Since we’re not implementing congestion control in this part, just use a fixed SWS value, provided as a command-line argument when starting the server.

---

### Packet Format

Assume UDP packets with a maximum payload size of **1200 bytes**. You shouldn’t send payloads larger than this number. The first 10 bytes of this payload will be the headers for the reliability protocols, while the remaining bytes will carry the data.

**Structure:**
- The first **4 bytes (32 bits)** are the **sequence number**.  
  - For data packets (server → client), it represents the data sequence number.  
  - For ACK packets (client → server), it represents the next expected sequence number (like in TCP).
- The next **6 bytes** are **reserved for optional features** (e.g., SACK or timestamps). You can use them or leave them unused. The server should not send data in these bytes.
- The rest of the bytes (up to **1190 bytes**) carry the actual data.

```
| Sequence Number (4 bytes) | Reserved / Optional (6 bytes) | Data (up to 1190 bytes) |
```

---

### File Transfer Process

The server will have a file called `data.txt` that the client wants to download. To keep things simple, the client can just send a one-byte message indicating that it wants to download the file (no need to send the filename). When the server receives this message, it starts sending the file. The client should write the data into a file named `received_data.txt`.  

Once the entire file is sent, the server should send a special segment with `"EOF"` (assume the file does not end with EOF characters) in the payload to signal the end of transfer. After that, both the client and server can terminate. We don’t care about the specifics of the termination logic as long as you correctly terminate the client and server processes.

---

### Analysis

You’ll now test your protocol in Mininet to see how well it performs.

- **Setup:**  
  Use the provided simple two-host topology (h1 and h2 connected via a switch s1) and a Ryu controller running a basic learning switch. Use your reliable transfer implementation (without congestion control) to transfer the file between the server and client.

- **Experiments:**  
  Use the provided `data.txt` file and run two sets of experiments:
  - Measure download time for different packet loss rates (1% to 5%) while keeping delay fixed.
  - Measure download time for different delay jitter values (20 ms to 100 ms) while keeping loss fixed.  
  You can introduce loss and delay on the Mininet link using `tc qdisc` commands.

- **Plotting Results:**  
  Repeat each experiment five times to smooth out random noise. For each set, make a line plot showing **download time vs. loss (or delay)** and include **90% confidence intervals**. Add these plots and a short explanation of your observations in your report.

---

### What to Submit

Submit your client and server files named:

```
p1_client.py
p1_server.py
```

We should be able to run your code as:

```bash
# Running server
python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>

# Running client
python3 p1_client.py <SERVER_IP> <SERVER_PORT>
```

Here:
- `<SERVER_IP>` and `<SERVER_PORT>` are the server’s IP address and port.
- `<SWS>` is the fixed sender window size for the experiment.

Also submit a **short report (max 2 pages)** that includes:
- A short description of your header structure, enhancements (if any), and design choices. If something is similar to TCP, you can simply say so.
- The analysis results: plots and a brief explanation of what you observed. Make sure all text and legends in the plots are readable.

---

### Grading

Your score for this part will depend on both **correctness** and **performance**:

- 50% — correctness and completion of all parts  
- 25% — meeting performance targets (we’ll share these limits later)  
- 25% — efficiency of your protocol compared to others (we’ll test all submissions, rank them by file download time for each test case, take the average rank, and assign marks based on decile rank, rounded up). For example, if your performance is at the 51st percentile, you’ll get 15 marks.
