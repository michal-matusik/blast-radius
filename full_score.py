"""Run the official sample three times with local container CPU accounting."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from score import run_total_microseconds, score_run

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "sample"
STDOUT_LIMIT = 2_000_000
RUN_TIMEOUT_SECONDS = 180


def run_sample(image, flags, command, output):
    name = f"blast-radius-score-{uuid4().hex}"
    marker = f"{name}-cpu-usec"
    with output.open("wb") as stdout:
        try:
            result = subprocess.run(
                [
                    "docker", "run", *flags, "--name", name,
                    "--mount", f"type=bind,src={ROOT / 'container_cpu.py'},dst=/__container_cpu.py,readonly",
                    "--entrypoint", "/usr/local/bin/python",
                    image, "/__container_cpu.py", marker, *command,
                ],
                stdout=stdout,
                stderr=subprocess.PIPE,
                timeout=RUN_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            raise RuntimeError(f"Sample container exceeded {RUN_TIMEOUT_SECONDS} seconds") from error

    logs, separator, measurement = result.stderr.rpartition(f"\n{marker} ".encode())
    sys.stderr.buffer.write(logs if separator else result.stderr)
    sys.stderr.buffer.flush()
    if result.returncode:
        raise RuntimeError(f"Sample container exited with status {result.returncode}")
    if not separator or not measurement.strip().isdigit():
        raise RuntimeError("Container did not report cgroup CPU time")
    if output.stat().st_size > STDOUT_LIMIT:
        raise RuntimeError(f"Selector stdout exceeded {STDOUT_LIMIT} bytes")
    return int(measurement)


def rounded_median_tenths(totals):
    _, middle, _ = sorted(totals)
    return (middle + 50_000) // 100_000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("docker_flags", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    flags = args.docker_flags
    if flags[:1] == ["--"]:
        flags = flags[1:]

    configuration = json.loads(subprocess.check_output([
        "docker", "image", "inspect", "--format", "{{json .Config}}", args.image,
    ]))
    command = (configuration.get("Entrypoint") or []) + (configuration.get("Cmd") or [])
    if not command:
        raise RuntimeError("The image has no default command")

    tests = json.loads((SAMPLE / "tests.json").read_text())["tests"]
    answers = json.loads((SAMPLE / "answers.json").read_text())["commits"]
    if {file.stem for file in (SAMPLE / "commits").glob("*.patch")} != answers.keys():
        raise RuntimeError("Sample patches and answers disagree")

    totals = []
    with TemporaryDirectory(prefix="blast-radius-score-") as directory:
        output = Path(directory) / "output.jsonl"
        for index in range(1, 4):
            cpu = run_sample(args.image, flags, command, output)
            result = score_run(output.read_bytes(), tests, answers)
            if not result["ok"]:
                raise RuntimeError(f"Run {index} rejected by official scorer: {result}")
            simulated = result["simulated_milliseconds"]
            total = run_total_microseconds(simulated, cpu)
            totals.append(total)
            print(
                f"Run {index}: simulated {simulated / 1000:.3f} s, "
                f"misses {result['misses']}, CPU {cpu / 1_000_000:.6f} s, "
                f"total {total / 1_000_000:.6f} s",
                flush=True,
            )
    tenths = rounded_median_tenths(totals)
    print(f"Local full score (median of 3): {tenths // 10}.{tenths % 10} s")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
