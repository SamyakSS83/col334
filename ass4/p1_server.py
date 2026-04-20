#!/usr/bin/env python3
"""
UDP Reliable File Transfer Server with Sliding Window Protocol
Implements: Byte-level ACKs, Selective ACKs, Fast Retransmit, Dynamic Timeout Estimation

Key Protocol Details:
- Sequence numbers are BYTE offsets (not packet numbers)
- ACKs are cumulative byte acknowledgments
- SWS = maximum UNIQUE unacknowledged BYTES (retransmissions don't count)
- SACK blocks indicate byte ranges received out-of-order
"""

import socket
import sys
import time
import struct
import os
from typing import Dict, Set, Tuple, Optional, List

class ReliableUDPServer:
    def __init__(self, host: str, port: int, sws: int):
        self.host = host
        self.port = port
        self.sws = sws  # Sender window size in BYTES (unique unacked bytes)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Bind to 0.0.0.0 to accept on all interfaces (needed for Mininet)
        self.sock.bind(('0.0.0.0', self.port))
        
        # Constants
        self.MAX_PAYLOAD = 1200
        self.HEADER_SIZE = 20
        self.DATA_SIZE = self.MAX_PAYLOAD - self.HEADER_SIZE  # 1180 bytes per packet

        # Timeout and RTT estimation (TCP algorithm)
        self.estimated_rtt = 0.05  # Initial estimate in seconds (50ms - more realistic)
        self.dev_rtt = 0.025       # 25ms deviation
        self.alpha = 0.125  # For EWMA of RTT
        self.beta = 0.25    # For EWMA of deviation
        self.first_rtt_sample = True  # Flag to detect first RTT measurement
        
        # Fast retransmit
        self.dup_ack_threshold = 3
        
    def calculate_timeout(self) -> float:
        """Calculate RTO using TCP's algorithm"""
        return self.estimated_rtt + 4 * self.dev_rtt
    
    def update_rtt(self, sample_rtt: float):
        """Update RTT estimates using TCP's algorithm"""
        if self.first_rtt_sample:
            # First measurement - use it directly
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
            self.first_rtt_sample = False
        else:
            # Subsequent measurements - use EWMA
            self.dev_rtt = (1 - self.beta) * self.dev_rtt + self.beta * abs(sample_rtt - self.estimated_rtt)
            self.estimated_rtt = (1 - self.alpha) * self.estimated_rtt + self.alpha * sample_rtt
    
    def create_packet(self, byte_offset: int, data: bytes) -> bytes:
        """Create a packet with byte offset as sequence number and data"""
        # Format: 4 bytes byte_offset + 16 bytes reserved = 20 bytes total
        header = struct.pack('!I', byte_offset) + b'\x00' * 16
        return header + data
    
    def parse_ack(self, packet: bytes) -> Tuple[int, List[Tuple[int, int]]]:
        """
        Parse ACK packet to get cumulative ACK (byte offset) and SACK blocks
        Returns: (cumulative_ack_bytes, sack_blocks)
        SACK blocks are list of (start_byte, end_byte) tuples
        """
        if len(packet) < 4:
            return 0, []
        
        cumulative_ack = struct.unpack('!I', packet[:4])[0]
        sack_blocks = []
        
        # Parse SACK blocks from remaining bytes
        # Each SACK block is encoded as 2 bytes (relative packet offset from cumulative_ack)
        if len(packet) >= 10:
            for i in range(3):
                offset = 4 + i * 2
                if offset + 2 <= len(packet):
                    rel_packet_offset = struct.unpack('!H', packet[offset:offset+2])[0]
                    if rel_packet_offset != 0:
                        # Convert relative packet offset to byte range
                        packet_num = (cumulative_ack // self.DATA_SIZE) + rel_packet_offset
                        start_byte = packet_num * self.DATA_SIZE
                        end_byte = start_byte + self.DATA_SIZE
                        sack_blocks.append((start_byte, end_byte))
        
        return cumulative_ack, sack_blocks
    
    def send_file(self, client_addr: tuple, filename: str = "data.txt"):
        """Send file to client using byte-level sliding window protocol"""
        print(f"Sending file {filename} to {client_addr}", flush=True)
        
        # Read file data
        if not os.path.exists(filename):
            print(f"Error: File {filename} not found", flush=True)
            return
        
        with open(filename, 'rb') as f:
            file_data = f.read()
        
        total_bytes = len(file_data)
        num_packets = (total_bytes + self.DATA_SIZE - 1) // self.DATA_SIZE
        
        print(f"File size: {total_bytes} bytes, Packets: {num_packets}, SWS: {self.sws} bytes", flush=True)
        
        # Pre-create and cache all packets (optimization)
        packet_cache: Dict[int, bytes] = {}  # byte_offset -> full_packet
        packet_sizes: Dict[int, int] = {}    # byte_offset -> data_size
        
        for i in range(num_packets):
            byte_offset = i * self.DATA_SIZE
            start = byte_offset
            end = min(start + self.DATA_SIZE, total_bytes)
            data = file_data[start:end]
            packet_cache[byte_offset] = self.create_packet(byte_offset, data)
            packet_sizes[byte_offset] = len(data)
        
        # Sending state (byte-oriented)
        base = 0  # First unacknowledged BYTE
        next_byte = 0  # Next BYTE to send
        acked_bytes: Set[int] = set()  # Set of acked byte offsets (start of each packet)
        sent_times: Dict[int, float] = {}  # byte_offset -> send_time (for RTT calculation)
        sent_once: Set[int] = set()  # Track which byte offsets have been sent at least once
        
        last_ack_recv = 0
        dup_ack_count = 0
        retransmit_count = 0
        packets_sent = 0
        
        # Track unique unacked bytes incrementally (OPTIMIZATION - avoid O(n) recalculation)
        unique_unacked_bytes = 0
        
        # Dict-based timeout tracking (simpler and more efficient than heap for small windows)
        timeouts: Dict[int, float] = {}  # byte_offset -> timeout_time
        
        # Socket setup with adaptive timeout
        self.sock.settimeout(0.001)  # 1ms - allows reasonable ACK batch processing
        start_time = time.time()
        
        while base < total_bytes:
            current_time = time.time()
            
            # === PHASE 1: SEND NEW PACKETS (within window) ===
            while next_byte < total_bytes:
                # Check if window allows more packets (using incremental tracking)
                if unique_unacked_bytes >= self.sws:
                    break
                
                # Send packet at next_byte offset
                if next_byte in packet_cache:
                    try:
                        self.sock.sendto(packet_cache[next_byte], client_addr)
                        sent_times[next_byte] = current_time
                        
                        # Only count towards window if this is first send
                        if next_byte not in sent_once:
                            sent_once.add(next_byte)
                            unique_unacked_bytes += packet_sizes[next_byte]
                        
                        packets_sent += 1
                        
                        # Add/update timeout in dict
                        timeouts[next_byte] = current_time + self.calculate_timeout()
                    except OSError as e:
                        # Network may be down, just continue
                        pass
                
                next_byte += self.DATA_SIZE
            
            # === PHASE 2: PROCESS ACKS (batch processing) ===
            acks_processed = 0
            while acks_processed < 20:  # Process up to 20 ACKs per iteration
                try:
                    ack_packet, _ = self.sock.recvfrom(1024)
                    acks_processed += 1
                    cumulative_ack, sack_blocks = self.parse_ack(ack_packet)
                    
                    # --- Handle Cumulative ACK (byte-level) ---
                    if cumulative_ack > last_ack_recv:
                        # New cumulative ACK - mark all bytes < cumulative_ack as acked
                        byte_offset = last_ack_recv
                        while byte_offset < cumulative_ack:
                            if byte_offset not in acked_bytes:
                                acked_bytes.add(byte_offset)
                                # Decrease window usage (packet is now acked)
                                if byte_offset in sent_once:
                                    unique_unacked_bytes -= packet_sizes.get(byte_offset, self.DATA_SIZE)
                                # Update RTT estimate for first transmission
                                if byte_offset in sent_times:
                                    sample_rtt = current_time - sent_times[byte_offset]
                                    self.update_rtt(sample_rtt)
                                    del sent_times[byte_offset]
                                # Remove timeout
                                timeouts.pop(byte_offset, None)
                            byte_offset += self.DATA_SIZE
                        
                        # Slide window forward
                        base = cumulative_ack
                        last_ack_recv = cumulative_ack
                        dup_ack_count = 0
                    
                    elif cumulative_ack == last_ack_recv and cumulative_ack < total_bytes:
                        # Duplicate ACK detected
                        dup_ack_count += 1
                        
                        # Fast Retransmit on 3 duplicate ACKs
                        if dup_ack_count == self.dup_ack_threshold:
                            if base not in acked_bytes and base in packet_cache:
                                try:
                                    self.sock.sendto(packet_cache[base], client_addr)
                                    sent_times[base] = current_time
                                    retransmit_count += 1
                                    # Update timeout
                                    timeouts[base] = current_time + self.calculate_timeout()
                                except OSError:
                                    pass
                            dup_ack_count = 0
                    
                    # --- Handle SACK Blocks (byte ranges) ---
                    max_sacked_byte = cumulative_ack
                    for start_byte, end_byte in sack_blocks:
                        # Mark this byte range as acked
                        byte_offset = start_byte
                        while byte_offset < end_byte and byte_offset < total_bytes:
                            if byte_offset not in acked_bytes:
                                acked_bytes.add(byte_offset)
                                # Decrease window usage
                                if byte_offset in sent_once:
                                    unique_unacked_bytes -= packet_sizes.get(byte_offset, self.DATA_SIZE)
                                if byte_offset in sent_times:
                                    sample_rtt = current_time - sent_times[byte_offset]
                                    self.update_rtt(sample_rtt)
                                    del sent_times[byte_offset]
                                # Remove timeout
                                timeouts.pop(byte_offset, None)
                            max_sacked_byte = max(max_sacked_byte, byte_offset + self.DATA_SIZE)
                            byte_offset += self.DATA_SIZE
                    
                    # --- Selective Retransmission: Find and retransmit gaps ---
                    if sack_blocks and max_sacked_byte > cumulative_ack:
                        byte_offset = cumulative_ack
                        while byte_offset < max_sacked_byte:
                            if byte_offset not in acked_bytes and byte_offset in packet_cache:
                                # Gap detected - retransmit immediately
                                try:
                                    self.sock.sendto(packet_cache[byte_offset], client_addr)
                                    sent_times[byte_offset] = current_time
                                    retransmit_count += 1
                                    # Update timeout
                                    timeouts[byte_offset] = current_time + self.calculate_timeout()
                                except OSError:
                                    pass
                            byte_offset += self.DATA_SIZE
                
                except socket.timeout:
                    break
            
            # === PHASE 3: TIMEOUT RETRANSMISSION ===
            # Check for packets that have timed out (dict-based, O(n) where n = packets in window)
            for byte_offset in list(timeouts.keys()):
                if timeouts[byte_offset] <= current_time:
                    # Timeout occurred - retransmit if still unacked
                    if byte_offset not in acked_bytes and byte_offset in packet_cache:
                        if byte_offset >= base:  # Still relevant
                            try:
                                self.sock.sendto(packet_cache[byte_offset], client_addr)
                                sent_times[byte_offset] = current_time
                                retransmit_count += 1
                                # Update timeout for next check
                                timeouts[byte_offset] = current_time + self.calculate_timeout()
                            except OSError:
                                pass
                    else:
                        # Packet was acked or is no longer relevant, remove timeout
                        del timeouts[byte_offset]
        
        # === SEND EOF MARKER ===
        eof_packet = self.create_packet(total_bytes, b"EOF")
        for _ in range(10):
            try:
                self.sock.sendto(eof_packet, client_addr)
                time.sleep(0.005)
            except OSError:
                # Network may be torn down, ignore
                break
        
        elapsed = time.time() - start_time
        print(f"Transfer complete in {elapsed:.3f} seconds", flush=True)
        print(f"Throughput: {total_bytes / elapsed / 1024 / 1024:.2f} MB/s", flush=True)
        print(f"Packets sent: {packets_sent}, Retransmissions: {retransmit_count} ({retransmit_count*100/packets_sent:.1f}%)", flush=True)
        print(f"Final RTT estimate: {self.estimated_rtt*1000:.2f}ms", flush=True)
    
    def run(self):
        """Main server loop"""
        print(f"Server listening on {self.host}:{self.port} with SWS={self.sws}")
        
        # Wait for client request
        self.sock.settimeout(None)
        data, client_addr = self.sock.recvfrom(1024)
        print(f"Received request from {client_addr}")
        
        # Send file
        self.send_file(client_addr)
        
        print("Server shutting down")
        self.sock.close()

def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <SERVER_IP> <SERVER_PORT> <SWS>", flush=True)
        sys.exit(1)
    
    try:
        server_ip = sys.argv[1]
        server_port = int(sys.argv[2])
        sws = int(sys.argv[3])
        
        server = ReliableUDPServer(server_ip, server_port, sws)
        server.run()
    except Exception as e:
        print(f"ERROR: Server failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
