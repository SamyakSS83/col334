#!/usr/bin/env python3
"""
Basic test script to verify correctness of the implementation
"""

import subprocess
import time
import os
import sys

def test_basic_transfer():
    """Test basic file transfer without network issues"""
    print("="*60)
    print("Testing basic file transfer (localhost)")
    print("="*60)
    
    # Ensure data.txt exists
    if not os.path.exists('data.txt'):
        print("Creating test data file...")
        subprocess.run(['dd', 'if=/dev/urandom', 'of=data.txt', 'bs=1024', 'count=1024'], 
                      capture_output=True)
    
    file_size = os.path.getsize('data.txt')
    print(f"Test file size: {file_size} bytes ({file_size/1024:.1f} KB)")
    
    # Start server
    print("\nStarting server...")
    server_proc = subprocess.Popen(
        ['python3', 'p1_server.py', '127.0.0.1', '9999', '65536'],
        stdout=subprocess.PIPE, 
        stderr=subprocess.STDOUT,
        text=True
    )
    
    # Wait for server to start
    time.sleep(1)
    
    # Start client
    print("Starting client...")
    start_time = time.time()
    client_proc = subprocess.Popen(
        ['python3', 'p1_client.py', '127.0.0.1', '9999'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    # Wait for client to complete
    client_stdout, _ = client_proc.communicate(timeout=30)
    elapsed = time.time() - start_time
    
    # Kill server
    server_proc.terminate()
    server_stdout, _ = server_proc.communicate(timeout=2)
    
    # Print outputs
    print("\n--- Server Output ---")
    print(server_stdout.decode() if isinstance(server_stdout, bytes) else server_stdout)
    print("\n--- Client Output ---")
    print(client_stdout)
    
    # Check results
    print("\n" + "="*60)
    print("VERIFICATION")
    print("="*60)
    
    if not os.path.exists('received_data.txt'):
        print("❌ FAILED: received_data.txt not found")
        return False
    
    # Compare files
    with open('data.txt', 'rb') as f:
        original = f.read()
    with open('received_data.txt', 'rb') as f:
        received = f.read()
    
    if original == received:
        print(f"✅ SUCCESS: File transfer correct!")
        print(f"   File size: {len(original)} bytes")
        print(f"   Transfer time: {elapsed:.2f} seconds")
        print(f"   Throughput: {len(original)/elapsed/1024:.2f} KB/s")
        return True
    else:
        print(f"❌ FAILED: File mismatch!")
        print(f"   Original: {len(original)} bytes")
        print(f"   Received: {len(received)} bytes")
        if len(original) != len(received):
            print(f"   Size difference: {abs(len(original)-len(received))} bytes")
        return False

if __name__ == "__main__":
    try:
        success = test_basic_transfer()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
   
