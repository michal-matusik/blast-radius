# Blast Radius starter

A working submission for the Blast Radius track. It runs every test on every commit, so it never misses a failure and scores badly. Your job is to run less without missing.

## The problem

Your image gets a batch of code changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change, print which tests to run and how to split them across up to 8 workers.

| | |
| --- | --- |
| `/data/commits/<id>.patch` | One unified diff against the pinned commit. Each stands alone: none builds on another. |
| `/data/tests.json` | Every test, numbered, with its file and the seconds it took us to run. |
| stdout | One JSON line a commit: `{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}` |

Each inner list is one worker's test numbers. `"workers": []` runs nothing, which is the right answer for a change that breaks nothing.

## Scoring

Lower is better. [SCORING.md](SCORING.md) has the exact rules; in short:

- A commit costs the recorded seconds of its busiest worker, plus 2 seconds for every worker.
- A commit where you left out a test that fails adds 300 seconds.
- The CPU seconds your program uses are added. Running sqlglot's tests while you decide is allowed, and you pay for the time.
- Your image runs 3 times and the middle total counts.

"Fails" means pytest reports a failure, an error or a timeout for the test, or the test's file cannot be imported. A test with subtests is one test: one failing subtest fails it.

## Try it

You need Docker, make and Python 3.9 or newer. The kit unpacks into the folder you are in, so unpack it into an empty one.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
```

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

## Compare full scores locally

Run `make sample-full` for three fresh containers with the same limits as `make sample`.
The runner uses the official scorer and reports simulated seconds, misses, CPU time and total per run, followed by the median total rounded to one decimal place.
It reads the container's cgroup v2 CPU counter around the selector, including its child processes.
The local timing requires the starter image's `/usr/local/bin/python`, adds a small measurement overhead and varies by machine.
Compare on the same machine without overlapping runs.
Use `make sample-full IMAGE=blast-radius-claude` in Claude's worktree and `make sample-full IMAGE=blast-radius-codex` in Codex's worktree.

## Static selector

The selector maps changed paths to test files and selects every test in those files.
For each commit, it tries up to eight workers by assigning the longest tests first to the least loaded worker.
It picks the schedule with the lowest busiest-worker time plus two seconds per worker, using fewer workers on ties.
The index follows Python imports transitively, includes package initializers, and adds static links for lazy dialect references and SQL fixtures.
For edits wholly inside indexed top-level function bodies, the selector can use a narrower set of test files.
The function index follows sibling references and statically resolved named imports or module attributes.
It retains file-level selection for dynamic or unresolved uses, unsupported edits, and functions without a distinct set of known consumers.
For dialect parser and generator methods, the index follows the owning class and retains references through base classes, sibling classes, and exported functions.
When static analysis cannot distinguish which tests reach the owner, or an edit can change class structure, the selector retains file-level selection.
The generator selector also uses per-test call traces from the pinned, unpatched dialect test suite.
It handles edits inside generator method bodies and top-level dispatch helpers, validates the hunk against the original source, and checks that the result compiles.
The embedded source is covered by [SQLGLOT-LICENSE](SQLGLOT-LICENSE).
It keeps observed consumers and retains static selection for test IDs without usable coverage.
Import-time code, unobserved symbols and structural edits retain the static fallback.
Coverage reflects the baseline run and can miss calls that only occur under a different test environment.

An unknown or unsupported patch path falls back to the full test suite.
The index is built from a clean checkout of the pinned sqlglot revision and does not use sample patches, test IDs or failure labels.

To regenerate the index, run `python3 build_dependencies.py /path/to/sqlglot dependencies.json` with the pinned checkout.
The Docker build copies the precomputed index, so the selector needs no network or sqlglot installation at runtime.

To regenerate the trace, install `requirements.lock` in Python 3.13 using the starter Docker image, then run the tracer against a Git archive of the pinned revision.
Pass an output path outside that source archive.

```sh
python trace_generator_methods.py /path/to/pinned-source /path/to/generator_coverage.json
```

## Submit

Push to a public GitHub repository with the `Dockerfile` at its top, then enter the repository and the commit hash on the track page. The final scores the commit you pick. If you pick none, your best practice submission counts.

## Rules

- Submit your own team's repository. A submission with the same repository, commit or archive as another team's is disqualified, the later one.
- Your build may download only from public package registries: PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot’s own repository at the pinned commit. Nothing from an address your team controls. The final rebuilds every image after the cutoff, so anything else could change a submission after the deadline.
- No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
- Only the final counts. The practice board carries no prize.

## How your image runs

- The build has the network, 4 cores, 8 GB of memory and 10 minutes. It may clone sqlglot at the pinned commit, install it and run its tests to make coverage or import maps.
- Your repository may hold 20 MB. Git submodules and Git LFS files are not fetched: clone sqlglot in the `Dockerfile` and commit maps as plain files.
- The run has no network, 2 cores, 4 GB of memory and 3 minutes for the whole batch (4.5 in the final, which has about one and a half times as many commits).
- The disk is read-only. `/tmp` holds 64 MB and is empty at the start of every run.
- The container runs as user `nobody`, with `HOME=/tmp` and at most 256 processes.
- stdout may hold 2 MB. More rejects the run.

## Things that will catch you

- Logs go to stderr. Any line on stdout that is not an answer rejects the run.
- `score.py` names a broken rule by its code, as in `Rejected: not_json on line 1`. The table in SCORING.md says what each code means.
- Files your program needs must be readable by `nobody`. A map written by `root` during the build with mode 600 is not.
- pytest 9 prints a test as `PASSED` beside `SUBFAILED` lines when one of its subtests fails. That test failed.
- sqlglot skips its executor tests unless `SKIP_INTEGRATION=0` is set. We labeled with it set, so those tests can fail.
- `requirements.lock` is every package in the environment we labeled in, on the image the `Dockerfile` starts from. To run sqlglot's tests the way we did, `pip install -r requirements.lock` and put the sqlglot checkout on `PYTHONPATH`; sqlglot's own `pip install -e .[dev]` picks newer versions.
- The scorer is linux/amd64. On an Apple Silicon Mac, build and run with `--platform linux/amd64`, as the Makefile does, or your timings and wheels will differ.
- A change that only adds a comment is not promised to be harmless.
