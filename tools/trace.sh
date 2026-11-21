#!/bin/sh
# Regenerate data/index.json.gz from the pinned sqlglot commit.
# usage: tools/trace.sh SQLGLOT_CHECKOUT WORKDIR
# Runs natively (duckdb segfaults under amd64 emulation on Apple Silicon);
# sqlglot is pure Python, so the coverage is platform independent.
set -eu
SRC=$(cd "$1" && pwd); WORK=$2; HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$WORK/trace_a" "$WORK/trace_b"
cp "$HERE/../requirements.lock" "$WORK/req.lock"
printf 'FROM python:3.13-slim\nCOPY req.lock /req.lock\nRUN pip install --no-cache-dir -r /req.lock\n' > "$WORK/Dockerfile"
docker build -q -t br-trace-arm "$WORK"
run() {
  out=$1; shift
  docker run --rm -v "$SRC:/src" -v "$HERE:/tools:ro" -v "$WORK/$out:/out" -w /src \
    -e SKIP_INTEGRATION=0 -e PYTHONPATH=/src:/tools -e BR_OUT=/out -e BR_ROOT=/src \
    -e PYTHONDONTWRITEBYTECODE=1 br-trace-arm \
    python -m pytest tests -q -p no:cacheprovider -p br_trace "$@" | tail -1
}
run trace_a -n 4
run trace_b -n 3 --dist loadfile   # a different split exposes lazily cached code
python3 "$HERE/build_index.py" "$SRC" "$HERE/../sample/tests.json" "$HERE/../data/index.json.gz" \
  "$WORK/trace_a" "$WORK/trace_b"
