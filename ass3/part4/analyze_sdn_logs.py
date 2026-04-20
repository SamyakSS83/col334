#!/usr/bin/env python3
"""
Analyze SDN controller logs to extract flow change times
Outputs timing relative to experiment start
"""

import re
import sys
from datetime import datetime

def parse_controller_log(log_file):
    """
    Parse controller.log to extract flow changes with timestamps
    Returns list of (relative_time, event_description)
    """
    events = []
    start_time = None
    
    # Regex patterns
    flow_change_pattern = r'\[FLOW_CHANGE\] t=([0-9.]+) (.+)'
    timestamp_pattern = r'(\d{2}:\d{2}:\d{2})'
    
    print("=" * 80)
    print("SDN FLOW CHANGE ANALYSIS")
    print("=" * 80)
    print()
    
    try:
        with open(log_file, 'r') as f:
            for line in f:
                # Look for [FLOW_CHANGE] entries
                flow_match = re.search(flow_change_pattern, line)
                if flow_match:
                    abs_time = float(flow_match.group(1))
                    details = flow_match.group(2)
                    
                    if start_time is None:
                        start_time = abs_time
                        print(f"Experiment start time: {abs_time:.3f}s (absolute)")
                        print()
                    
                    rel_time = abs_time - start_time
                    events.append((rel_time, details))
    
    except FileNotFoundError:
        print(f"Error: Could not find {log_file}")
        return []
    except Exception as e:
        print(f"Error parsing log: {e}")
        return []
    
    return events, start_time

def print_flow_changes(events, show_all=False):
    """Print flow changes in chronological order with relative times"""
    if not events:
        print("No flow changes detected in log")
        return
    
    if show_all:
        print("FLOW CHANGES (relative to experiment start):")
        print("-" * 80)
        print(f"{'Time (s)':<12} {'Event'}")
        print("-" * 80)
        
        for rel_time, details in events:
            # Parse details to make them more readable
            switch_match = re.search(r'switch=(\S+)', details)
            dst_match = re.search(r'dst=([0-9./]+)', details)
            port_match = re.search(r'port=(\d+)', details)
            
            switch = switch_match.group(1) if switch_match else "?"
            dst = dst_match.group(1) if dst_match else "?"
            port = port_match.group(1) if port_match else "?"
            
            event_desc = f"{switch}: route to {dst} via port {port}"
            print(f"{rel_time:10.3f}   {event_desc}")
        
        print("-" * 80)
        print()

def analyze_convergence(events, link_down_time=4.0, link_up_time=9.0):
    """
    Analyze flow changes to determine convergence phases
    """
    print("CONVERGENCE ANALYSIS:")
    print("-" * 80)
    
    # Categorize events
    initial_flows = [e for e in events if e[0] < link_down_time]
    during_failure = [e for e in events if link_down_time <= e[0] < link_up_time]
    after_recovery = [e for e in events if e[0] >= link_up_time]
    
    print(f"Initial routing setup: {len(initial_flows)} flow changes (t < {link_down_time:.3f}s)")
    print(f"During link failure: {len(during_failure)} flow changes ({link_down_time:.3f}s - {link_up_time:.3f}s)")
    print(f"After link recovery: {len(after_recovery)} flow changes (t >= {link_up_time:.3f}s)")
    print()
    
    # Show initial flows
    if initial_flows:
        print("Initial flows (first 10):")
        for rel_time, details in initial_flows:
            switch_match = re.search(r'switch=(\S+)', details)
            dst_match = re.search(r'dst=([0-9./]+)', details)
            switch = switch_match.group(1) if switch_match else "?"
            dst = dst_match.group(1) if dst_match else "?"
            print(f"  t={rel_time:.3f}s: {switch} -> {dst}")
        # if len(initial_flows) > 10:
        #     print(f"  ... and {len(initial_flows) - 10} more")
        print()
    
    # Show flows during failure
    if during_failure:
        print("Flows during failure:")
        for rel_time, details in during_failure:
            switch_match = re.search(r'switch=(\S+)', details)
            dst_match = re.search(r'dst=([0-9./]+)', details)
            switch = switch_match.group(1) if switch_match else "?"
            dst = dst_match.group(1) if dst_match else "?"
            print(f"  t={rel_time:.3f}s: {switch} updated route to {dst}")
        print()
    
    if after_recovery:
        first_recovery_flow = min(e[0] for e in after_recovery)
        last_recovery_flow = max(e[0] for e in after_recovery)
        convergence_time = last_recovery_flow - link_up_time
        
        print(f"First flow change after recovery: t = {first_recovery_flow:.3f}s")
        print(f"Last flow change after recovery: t = {last_recovery_flow:.3f}s")
        print(f"Convergence time (from link up to last flow): {convergence_time:.3f}s")
        print()
        
        print("Recovery flow changes:")
        for rel_time, details in after_recovery:
            switch_match = re.search(r'switch=(\S+)', details)
            dst_match = re.search(r'dst=([0-9./]+)', details)
            switch = switch_match.group(1) if switch_match else "?"
            dst = dst_match.group(1) if dst_match else "?"
            print(f"  t={rel_time:.3f}s: {switch} updated route to {dst}")
    else:
        print("⚠️  No flow changes detected after link recovery")
    
    print("-" * 80)
    print()

def main():
    if len(sys.argv) > 1:
        log_file = sys.argv[1]
    else:
        log_file = "sdn_logs/controller.log"
    
    print(f"Analyzing: {log_file}")
    print()
    
    events, start_time = parse_controller_log(log_file)
    
    if events:
        # Don't print all flow changes by default, just show in analysis
        # print_flow_changes(events, show_all=False)
        
        # Try to read convergence.log for timing info
        try:
            with open('sdn_logs/convergence.log', 'r') as f:
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
                    analyze_convergence(events, link_down_time, link_up_time)
                else:
                    print("Using default timing (link down at 4s, up at 9s)")
                    analyze_convergence(events)
        except:
            print("Could not read convergence.log, using default timing")
            analyze_convergence(events)
    
    print("Analysis complete!")

if __name__ == '__main__':
    main()
