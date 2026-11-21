"""Keep complete bounded numeric domains and callbacks with stable attribution."""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def read(directory):
    domains, calls = defaultdict(set), defaultdict(set)
    paths = list(Path(directory).glob("expr-*.json"))
    if not paths:
        raise ValueError("Missing expression trace")
    for path in paths:
        domain, observed = json.loads(path.read_bytes())
        for site, values in domain.items():
            domains[site].update(tuple(v) if isinstance(v, list) else v for v in values)
        for site, ids in observed.items():
            calls[site].update(ids)
    return domains, calls


a, x = read(sys.argv[1])
b, y = read(sys.argv[2])
domains = {site: sorted(a[site] | b[site], key=repr) for site in a.keys() & b.keys()
           if None not in a[site] | b[site] and len(a[site] | b[site]) <= 32}
calls = {site: sorted(x[site]) for site in x.keys() & y.keys() if x[site] == y[site] and "*" not in x[site]}
Path(sys.argv[3]).write_bytes(gzip.compress(json.dumps([domains, calls], sort_keys=True).encode(), mtime=0))
