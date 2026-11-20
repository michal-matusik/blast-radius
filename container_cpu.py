"""Measure the selector and its descendants using the container's cgroup."""
from pathlib import Path
import subprocess
import sys

CPU_STAT = Path("/sys/fs/cgroup/cpu.stat")


def cpu_microseconds():
    for line in CPU_STAT.read_text().splitlines():
        key, value = line.split()
        if key == "usage_usec":
            return int(value)
    raise RuntimeError("cgroup v2 cpu.stat has no usage_usec counter")


if __name__ == "__main__":
    marker, *command = sys.argv[1:]
    start = cpu_microseconds()
    result = subprocess.run(command, check=False)
    elapsed = cpu_microseconds() - start
    sys.stderr.write(f"\n{marker} {elapsed}\n")
    sys.exit(result.returncode if result.returncode >= 0 else 128 - result.returncode)
