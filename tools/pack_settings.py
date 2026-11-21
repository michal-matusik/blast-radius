"""Combine two full test runs, retaining stable setting-read attribution."""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def read(directory):
    sites = defaultdict(lambda: defaultdict(set))
    files = list(Path(directory).glob("settings-*.json"))
    if not files:
        raise ValueError(f"No observations in {directory}")
    for file in files:
        for site, key, ids in json.loads(file.read_bytes()):
            sites[site][key].update(ids)
    return sites


left, right = map(read, sys.argv[1:3])
result = {}
for site in left.keys() & right.keys():
    if left[site] == right[site]:
        result[site] = {key: sorted(ids) for key, ids in left[site].items()}
Path(sys.argv[3]).write_bytes(gzip.compress(json.dumps(result, sort_keys=True).encode(), mtime=0))
