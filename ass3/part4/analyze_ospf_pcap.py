#!/usr/bin/env python3
"""
Analyze OSPF packet captures to extract LSA timing
Outputs timing relative to experiment start
"""

import subprocess
import sys
import re
import os

def check_tools():
    """Check if required tools are available"""
    tools = {
        'tcpdump': 'tcpdump',
        'tshark': 'tshark (wireshark-cli package)'
    }
    
    missing = []
    for tool, desc in tools.items():
        try:
            subprocess.run([tool, '--version'], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            missing.append(desc)
    
    if missing:
        print(f"⚠️  Warning: Missing tools: {', '.join(missing)}")
        print("   Install with: sudo apt-get install tcpdump wireshark-cli")
        print()
        return False
    return True

def analyze_pcap_with_tcpdump(pcap_file):
    """
    Analyze OSPF packets using tcpdump
    Returns list of (relative_time, packet_type, description)
    """
    print(f"Analyzing {pcap_file} with tcpdump...")
    print("-" * 80)
    
    try:
        # Run tcpdump to extract OSPF packets with timestamps
        # Try different approaches to capture OSPF
        cmd = ['tcpdump', '-r', pcap_file, '-tt', '-n', '-v']
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"Error running tcpdump: {result.stderr}")
            return [], None
        
        lines = result.stdout.strip().split('\n')
        if not lines or lines[0] == '':
            print(f"No packets found in {pcap_file}")
            return [], None
        
        events = []
        first_time = None
        
        for line in lines:
            # Parse tcpdump output: timestamp IP > IP: OSPFv2, ...
            # Try multiple patterns since OSPF might be formatted differently
            
            # Pattern 1: Standard OSPF
            match = re.match(r'([0-9.]+)\s+IP\s+([0-9.]+)\s+>\s+([0-9.]+):\s+OSPFv2,?\s*(.+)', line)
            if not match:
                # Pattern 2: Just IP protocol 89
                match = re.match(r'([0-9.]+)\s+IP\s+([0-9.]+)\s+>\s+([0-9.]+):\s+ip-proto-89\s*(.+)', line)
            if not match:
                # Pattern 3: Check if line contains OSPF keyword
                if 'OSPF' in line or 'ospf' in line:
                    # Try to extract basic info
                    timestamp_match = re.match(r'([0-9.]+)', line)
                    if timestamp_match:
                        timestamp = float(timestamp_match.group(1))
                        if first_time is None:
                            first_time = timestamp
                        rel_time = timestamp - first_time
                        events.append((rel_time, 'unknown', 'unknown', line[20:80]))
                continue
            
            if match:
                timestamp = float(match.group(1))
                src_ip = match.group(2)
                dst_ip = match.group(3)
                packet_info = match.group(4) if len(match.groups()) >= 4 else ''
                
                if first_time is None:
                    first_time = timestamp
                
                rel_time = timestamp - first_time
                events.append((rel_time, src_ip, dst_ip, packet_info))
        
        print(f"Found {len(events)} OSPF packets")
        print()
        return events, first_time
        
    except Exception as e:
        print(f"Error analyzing pcap: {e}")
        return [], None

def analyze_pcap_with_tshark(pcap_file):
    """
    Analyze OSPF LSAs using tshark for more detailed parsing
    """
    print(f"Analyzing {pcap_file} with tshark (detailed)...")
    print("-" * 80)
    
    try:
        # First try to get all OSPF packets with basic info
        cmd = [
            'tshark', '-r', pcap_file,
            '-Y', 'ospf',  # Just OSPF packets
            '-T', 'fields',
            '-e', 'frame.time_relative',
            '-e', 'ip.src',
            '-e', 'ip.dst',
            '-e', 'ospf.msg',
            '-E', 'separator=|'
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"tshark error: {result.stderr}")
            return []
        
        lines = result.stdout.strip().split('\n')
        if not lines or lines[0] == '':
            print("No OSPF packets found")
            return []
        
        ospf_events = []
        ospf_msg_types = {
            '1': 'Hello',
            '2': 'Database Description',
            '3': 'Link State Request',
            '4': 'Link State Update',
            '5': 'Link State Acknowledgment'
        }
        
        for line in lines:
            parts = line.split('|')
            if len(parts) >= 4:
                rel_time = float(parts[0])
                src_ip = parts[1]
                dst_ip = parts[2]
                msg_type = parts[3]
                msg_name = ospf_msg_types.get(msg_type, f'Type {msg_type}')
                ospf_events.append((rel_time, src_ip, dst_ip, msg_name))
        
        print(f"Found {len(ospf_events)} OSPF packets")
        print()
        return ospf_events
        
    except Exception as e:
        print(f"Could not use tshark: {e}")
        return []

def print_ospf_timeline(events, router_name):
    """Print OSPF events in chronological order"""
    print(f"OSPF PACKET TIMELINE for {router_name}:")
    print("-" * 80)
    print(f"{'Time (s)':<12} {'Source':<18} {'Destination':<18} {'Packet Info'}")
    print("-" * 80)
    
    for rel_time, src, dst, info in events:
        # Truncate info if too long
        info_short = info[:40] + "..." if len(info) > 40 else info
        print(f"{rel_time:10.3f}   {src:<18} {dst:<18} {info_short}")
    
    print("-" * 80)
    print()

def analyze_convergence_timing(events, link_down_time, link_up_time):
    """Analyze OSPF convergence around link failure"""
    print("OSPF CONVERGENCE ANALYSIS:")
    print("-" * 80)
    
    # Categorize packets
    before_failure = [e for e in events if e[0] < link_down_time]
    during_failure = [e for e in events if link_down_time <= e[0] < link_up_time]
    after_recovery = [e for e in events if e[0] >= link_up_time]
    
    print(f"Before link failure (t < {link_down_time:.1f}s): {len(before_failure)} packets")
    print(f"During link failure ({link_down_time:.1f}s - {link_up_time:.1f}s): {len(during_failure)} packets")
    print(f"After link recovery (t >= {link_up_time:.1f}s): {len(after_recovery)} packets")
    print()
    
    if during_failure:
        print("OSPF packets during failure (detecting failure):")
        for rel_time, src, dst, info in during_failure[:10]:  # Show first 10
            print(f"  t={rel_time:.3f}s: {src} -> {dst}: {info[:60]}")
        if len(during_failure) > 10:
            print(f"  ... and {len(during_failure) - 10} more packets")
        print()
    
    if after_recovery:
        print("OSPF packets after recovery (re-establishing routes):")
        for rel_time, src, dst, info in after_recovery[:10]:  # Show first 10
            print(f"  t={rel_time:.3f}s: {src} -> {dst}: {info[:60]}")
        if len(after_recovery) > 10:
            print(f"  ... and {len(after_recovery) - 10} more packets")
        print()
        
        first_recovery = min(e[0] for e in after_recovery)
        last_recovery = max(e[0] for e in after_recovery)
        convergence_time = last_recovery - link_up_time
        
        print(f"First OSPF packet after recovery: t = {first_recovery:.3f}s")
        print(f"Last OSPF packet in recovery: t = {last_recovery:.3f}s")
        print(f"OSPF convergence window: {convergence_time:.3f}s")
    
    print("-" * 80)
    print()

def main():
    print("=" * 80)
    print("OSPF PACKET CAPTURE ANALYSIS")
    print("=" * 80)
    print()
    
    # Check if tools are available
    has_tools = check_tools()
    
    # Find pcap files
    pcap_files = []
    if len(sys.argv) > 1:
        # Use provided files
        pcap_files = sys.argv[1:]
    else:
        # Look for default locations
        for f in ['ospf_logs/ospf_s1.pcap', 'ospf_logs/ospf_s2.pcap']:
            if os.path.exists(f):
                pcap_files.append(f)
    
    if not pcap_files:
        print("Error: No pcap files found")
        print("Usage: python3 analyze_ospf_pcap.py [pcap_file1] [pcap_file2] ...")
        print("   Or place pcap files in ospf_logs/ospf_s*.pcap")
        sys.exit(1)
    
    # Try to read link timing from convergence.log
    link_down_time = 4.0
    link_up_time = 9.0
    try:
        with open('ospf_logs/convergence.log', 'r') as f:
            content = f.read()
            link_down_match = re.search(r'Link Failure at: t = ([0-9.]+)s', content)
            link_up_match = re.search(r'Link Recovery at: t = ([0-9.]+)s', content)
            
            if link_down_match and link_up_match:
                link_down_time = float(link_down_match.group(1))
                link_up_time = float(link_up_match.group(1))
                print(f"Link timing from convergence.log:")
                print(f"  Link down at: t = {link_down_time:.3f}s")
                print(f"  Link up at: t = {link_up_time:.3f}s")
                print()
    except:
        print(f"Using default link timing (down at {link_down_time}s, up at {link_up_time}s)")
        print()
    
    # Analyze each pcap file
    for pcap_file in pcap_files:
        router_name = os.path.basename(pcap_file).replace('.pcap', '')
        print("\n" + "=" * 80)
        print(f"Router: {router_name}")
        print("=" * 80)
        print()
        
        if not os.path.exists(pcap_file):
            print(f"Error: {pcap_file} not found")
            continue
        
        # First try basic tcpdump analysis
        events, first_time = analyze_pcap_with_tcpdump(pcap_file)
        
        if events:
            print_ospf_timeline(events[:20], router_name)  # Show first 20
            if len(events) > 20:
                print(f"(Showing first 20 of {len(events)} packets)\n")
            
            analyze_convergence_timing(events, link_down_time, link_up_time)
        
        # If tshark available, show LSA details
        if has_tools:
            ospf_events = analyze_pcap_with_tshark(pcap_file)
            if ospf_events:
                print("OSPF PACKET BREAKDOWN BY TYPE:")
                print("-" * 80)
                
                # Count by type
                type_counts = {}
                for _, _, _, msg_type in ospf_events:
                    type_counts[msg_type] = type_counts.get(msg_type, 0) + 1
                
                for msg_type, count in sorted(type_counts.items()):
                    print(f"  {msg_type}: {count} packets")
                print()
                
                # Show LSA Updates (type 4) in detail
                lsa_updates = [e for e in ospf_events if 'Update' in e[3]]
                if lsa_updates:
                    print("LSA UPDATE PACKETS (Link State Updates):")
                    print("-" * 80)
                    for rel_time, src, dst, msg_type in lsa_updates:
                        print(f"  t={rel_time:.3f}s: {src} -> {dst}")
                    
                    print()
                
                # Analyze timing relative to link events
                before_down = [e for e in lsa_updates if e[0] < link_down_time]
                during_failure = [e for e in lsa_updates if link_down_time <= e[0] < link_up_time]
                after_recovery = [e for e in lsa_updates if e[0] >= link_up_time]
                
                print("LSA Updates by phase:")
                print(f"  Before failure: {len(before_down)} updates")
                print(f"  During failure: {len(during_failure)} updates")
                print(f"  After recovery: {len(after_recovery)} updates")
                print()
    
    print("=" * 80)
    print("Analysis complete!")
    print()
    print("To view full packet details:")
    for pcap_file in pcap_files:
        print(f"  tcpdump -r {pcap_file} -v | less")
    print()

if __name__ == '__main__':
    main()
