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
