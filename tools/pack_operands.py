"""Pack raw type observations from all tracing processes into one compact index."""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

profiles = defaultdict(set)
for path in Path(sys.argv[1]).glob("*.jsonl"):
    for line in path.read_text().splitlines():
        site, *profile = json.loads(line)
        profiles[site].add(tuple(profile))
if not profiles:
    raise ValueError("No operand observations")
types = sorted(set.union(*profiles.values()))
ids = {profile: i for i, profile in enumerate(types)}
sites = {s: sorted(ids[p] for p in rows) for s, rows in profiles.items()}
data = json.dumps([types, sites], sort_keys=True, separators=(",", ":")).encode()
Path(sys.argv[2]).write_bytes(gzip.compress(data, mtime=0))
