#!/usr/bin/env python3
"""
UDP Reliable File Transfer Client with Sliding Window Protocol
Implements: Byte-level ACKs, Selective ACKs, Out-of-order packet buffering

Key Protocol Details:
- ACKs are cumulative BYTE acknowledgments (not packet numbers)
- SACK blocks indicate out-of-order packets received
- Unbounded receiver buffer (no flow control)
"""

import socket
import sys
import time
import struct
from typing import Dict, Set, List

class ReliableUDPClient:
    def __init__(self, server_ip: str, server_port: int):
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # Constants
        self.MAX_PAYLOAD = 1200
        self.HEADER_SIZE = 20
        self.DATA_SIZE = self.MAX_PAYLOAD - self.HEADER_SIZE  # 1180 bytes

        # Connection parameters
        self.REQUEST_TIMEOUT = 2.0
        self.MAX_RETRIES = 5
        
    def parse_packet(self, packet: bytes) -> tuple:
        """Parse received packet to get byte offset and data"""
        if len(packet) < self.HEADER_SIZE:
            return None, None
        
        byte_offset = struct.unpack('!I', packet[:4])[0]
        data = packet[self.HEADER_SIZE:]
        return byte_offset, data
    
    def create_ack(self, cumulative_ack_bytes: int, sack_packet_offsets: List[int]) -> bytes:
        """
        Create ACK packet with cumulative ACK (in bytes) and SACK blocks
        
        cumulative_ack_bytes: Next expected byte offset
        sack_packet_offsets: List of out-of-order packet numbers (relative to cumulative_ack)
        """
        # Format: 4 bytes cumulative_ack + up to 3 SACK blocks (2 bytes each, relative packet offset)
        ack_packet = struct.pack('!I', cumulative_ack_bytes)
        
        # Add up to 3 SACK blocks as relative packet offsets
        for i in range(3):
            if i < len(sack_packet_offsets):
                ack_packet += struct.pack('!H', sack_packet_offsets[i])
            else:
                ack_packet += struct.pack('!H', 0)
        
        return ack_packet
    
    def send_request(self) -> bool:
        """Send file request to server with retries"""
        request = b'R'  # Simple request byte
        
        for attempt in range(self.MAX_RETRIES):
            print(f"Sending request (attempt {attempt + 1}/{self.MAX_RETRIES})")
            self.sock.sendto(request, (self.server_ip, self.server_port))
            
            # Wait for first data packet
            self.sock.settimeout(self.REQUEST_TIMEOUT)
            try:
                data, _ = self.sock.recvfrom(self.MAX_PAYLOAD + 100)
                # Put it back for processing
                byte_offset, packet_data = self.parse_packet(data)
                if byte_offset is not None:
                    print(f"Connection established, received packet at byte offset {byte_offset}")
                    return True, data
            except socket.timeout:
                print(f"Request timeout")
                continue
        
        return False, None
    
    def receive_file(self, output_file: str = "received_data.txt"):
        """Receive file from server with byte-level reliability"""
        print(f"Requesting file from {self.server_ip}:{self.server_port}")
        
        # Send request
        success, first_packet = self.send_request()
        if not success:
            print("Failed to establish connection after retries")
            return False
        
        # Receiving state (byte-oriented)
        received_packets: Dict[int, bytes] = {}  # byte_offset -> data
        expected_byte = 0  # Next in-order byte we expect
        last_ack_time = time.time()
        ack_interval = 0.001  # Send ACKs frequently
        
        # Set socket to non-blocking with short timeout
        self.sock.settimeout(0.01)
        
        # Process first packet
        byte_offset, data = self.parse_packet(first_packet)
        if byte_offset is not None:
            if data == b"EOF":
                # Empty file case
                with open(output_file, 'wb') as f:
                    pass
                print("Received empty file")
                return True
            received_packets[byte_offset] = data
        
        start_time = time.time()
        eof_received = False
        eof_byte_offset = -1
        
        consecutive_timeouts = 0
        max_consecutive_timeouts = 100
        
        while True:
            current_time = time.time()
            
            # Try to receive packets
            try:
                packet, _ = self.sock.recvfrom(self.MAX_PAYLOAD + 100)
                byte_offset, data = self.parse_packet(packet)
                
                if byte_offset is None:
                    continue
                
                consecutive_timeouts = 0
                
                # Check for EOF
                if data == b"EOF":
                    eof_received = True
                    eof_byte_offset = byte_offset
                    print(f"Received EOF marker at byte offset {eof_byte_offset}")
                else:
                    # Store packet if new
                    if byte_offset not in received_packets:
                        received_packets[byte_offset] = data
                
                # Send ACK immediately when we receive a packet
                # Calculate cumulative ACK (next expected byte offset)
                while expected_byte in received_packets:
                    expected_byte += len(received_packets[expected_byte])
                
                # Calculate SACK blocks (out-of-order packets as relative offsets)
                sack_relative_offsets = []
                cumulative_packet_num = expected_byte // self.DATA_SIZE
                
                for byte_off in sorted(received_packets.keys()):
                    if byte_off >= expected_byte and len(sack_relative_offsets) < 3:
                        packet_num = byte_off // self.DATA_SIZE
                        relative_offset = packet_num - cumulative_packet_num
                        if 0 < relative_offset < 65536:  # Must fit in 2 bytes
                            sack_relative_offsets.append(relative_offset)
                
                ack_packet = self.create_ack(expected_byte, sack_relative_offsets)
                self.sock.sendto(ack_packet, (self.server_ip, self.server_port))
                last_ack_time = current_time
                
                # Check if we're done
                if eof_received and expected_byte >= eof_byte_offset:
                    break
                
            except socket.timeout:
                consecutive_timeouts += 1
                
                # Send periodic ACKs even without new packets
                if current_time - last_ack_time > ack_interval:
                    # Recalculate expected byte
                    while expected_byte in received_packets:
                        expected_byte += len(received_packets[expected_byte])
                    
                    # Recalculate SACK
                    sack_relative_offsets = []
                    cumulative_packet_num = expected_byte // self.DATA_SIZE
                    
                    for byte_off in sorted(received_packets.keys()):
                        if byte_off >= expected_byte and len(sack_relative_offsets) < 3:
                            packet_num = byte_off // self.DATA_SIZE
                            relative_offset = packet_num - cumulative_packet_num
                            if 0 < relative_offset < 65536:
                                sack_relative_offsets.append(relative_offset)
                    
                    ack_packet = self.create_ack(expected_byte, sack_relative_offsets)
                    self.sock.sendto(ack_packet, (self.server_ip, self.server_port))
                    last_ack_time = current_time
                
                # Check if we're done
                if eof_received and expected_byte >= eof_byte_offset:
                    break
                
                # Exit if too many consecutive timeouts
                if consecutive_timeouts >= max_consecutive_timeouts:
                    if eof_received:
                        print("Transfer complete (timeout after EOF)")
                        break
                    else:
                        print("Warning: Many consecutive timeouts, transfer may be incomplete")
                        break
        
        # Write received data to file in order
        with open(output_file, 'wb') as f:
            byte_offset = 0
            while byte_offset in received_packets:
                f.write(received_packets[byte_offset])
                byte_offset += len(received_packets[byte_offset])
        
        elapsed = time.time() - start_time
        total_bytes = sum(len(data) for data in received_packets.values())
        
        print(f"File received successfully: {output_file}")
        print(f"Received {len(received_packets)} packets, {total_bytes} bytes in {elapsed:.2f} seconds")
        print(f"Throughput: {total_bytes / elapsed / 1024 / 1024:.2f} MB/s")
        
        return True
    
    def run(self):
        """Main client loop"""
        success = self.receive_file()
        self.sock.close()
        
        if success:
            print("Client completed successfully")
        else:
            print("Client failed")
            sys.exit(1)

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <SERVER_IP> <SERVER_PORT>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    
    client = ReliableUDPClient(server_ip, server_port)
    client.run()

if __name__ == "__main__":
    main()
