"""Build step: convert the JSON index to marshal for the image's Python.

marshal is tied to the interpreter version, so this runs inside docker build.
"""
import gzip
import json
import marshal
import sys

src, dst = sys.argv[1:3]
d = json.loads(gzip.decompress(open(src, "rb").read()))
d["lines"] = {f: {int(k): v for k, v in m.items()} for f, m in d["lines"].items()}
with open(dst, "wb") as fh:
    marshal.dump(d, fh)
