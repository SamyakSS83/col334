#!/usr/bin/env python3
"""
Part 2 Test CLI - run normal or bonus controller tests
This script chooses between `p2_l2spf.py` and `p2bonus_l2spf.py` when starting the controller
and then uses the runner to execute a link-failure iperf test.
"""
import argparse
import os
import sys
import subprocess
from pathlib import Path

from p2_runner_sdn import main as runner_main


def start_controller(script, log_path):
    ryu = '/home/threesamyak/micromamba/envs/forryu/bin/ryu-manager'
    cmd = [ryu, '--observe-links', 'ryu.topology.switches', script]
    fd = open(log_path, 'w')
    proc = subprocess.Popen(cmd, stdout=fd, stderr=subprocess.STDOUT, cwd=str(Path(__file__).resolve().parents[1]))
    return proc, fd


def parse_args():
    p = argparse.ArgumentParser(description='Part2 test - choose normal or bonus controller')
    p.add_argument('--bonus', action='store_true', help='Run bonus controller (p2bonus_l2spf.py)')
    p.add_argument('--start-controller', action='store_true', help='Start controller automatically')
    p.add_argument('--udp', action='store_true', help='Use UDP iperf')
    p.add_argument('--iperf-duration', type=int, default=15)
    p.add_argument('--failure-time', type=int, default=3)
    p.add_argument('--failure-duration', type=int, default=5)
    return p.parse_args()


def main():
    args = parse_args()
    controller_script = 'part2/p2bonus_l2spf.py' if args.bonus else 'part2/p2_l2spf.py'
    controller_proc = None
    controller_fd = None
    try:
        if args.start_controller:
            ts = __import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')
            logp = f'/tmp/p2_ctrl_{ts}.log'
            controller_proc, controller_fd = start_controller(controller_script, logp)
            print(f'Controller started: {controller_script} (log: {logp})')

        # Delegate to runner with translated argv
        runner_argv = [
            '--iperf-duration', str(args.iperf_duration),
            '--failure-time', str(args.failure_time),
            '--failure-duration', str(args.failure_duration),
        ]
        if args.udp:
            runner_argv.append('--udp')
        # call runner's main
        sys.argv = [sys.argv[0]] + runner_argv
        runner_main()

    finally:
        if controller_proc:
            controller_proc.terminate()
            controller_fd.close()


if __name__ == '__main__':
    main()
