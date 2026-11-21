# Impact index

`data/index.json.gz` is built from sqlglot at the pinned commit only (no labels):

- `br_trace.py`: a pytest plugin that records, per test, the lines it runs
  (`sys.monitoring`), the sqlglot classes it instantiates (with ancestors), and
  the repository files it opens. setUpClass lines count for the whole class.
  Process pools run inline, with pickle round-trips, so their work is credited
  to the submitting test.
- `build_index.py`: merges two traces with different xdist splits. Lines whose
  attribution differs are lazily cached code and are widened at run time. It
  also records import-time lines, module importers and classes per module.
- `to_marshal.py`: runs in `docker build`, so the index loads fast.

Regenerate with `tools/trace.sh <sqlglot checkout> <workdir>`.

## Comparison operand profiles

The tracing import hook records operand types, the classes that implement their comparison methods, and whether a string enum member is canonical for its string value.
Each process records its own observations, including subprocesses created by the executor and optimizer tests.
The packed index contains source locations and type information from the unmodified pinned suite.

To regenerate it, use a fresh checkout of the pinned sqlglot revision at `$SRC`, an empty directory at `$OUT`, and the Python 3.13 image with `requirements.lock` installed from `trace.sh`.
Run these commands from this repository.

```sh
docker run --rm --cpus 4 -v "$SRC:/src" -v "$OUT:/out" \
  -v "$PWD/tools/operand_trace:/trace:ro" -w /src \
  -e PYTHON_CPU_COUNT=2 -e PYTHONPATH=/trace:/src -e BR_ROOT=/src \
  -e BR_OUT=/out -e SKIP_INTEGRATION=0 br-trace-arm python -m pytest tests -q -n 2 &&
  python3 tools/pack_operands.py "$OUT" data/operands.json.gz
```
