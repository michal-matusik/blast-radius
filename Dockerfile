# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Base sources of sqlglot at the pinned commit: the selector applies each patch
# to them and diffs the ASTs; the import check imports the patched modules.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ADD https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
 && tar -xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 \
      sqlglot-${SQLGLOT_SHA}/sqlglot sqlglot-${SQLGLOT_SHA}/tests \
 && rm /tmp/sqlglot.tar.gz \
 && python -m compileall -q /app/src/sqlglot \
 && chmod -R a+rX /app

# Impact index recorded from the base test suite (tools/README.md).
COPY data/index.json.gz /app/index.json.gz
COPY tools/to_marshal.py /app/to_marshal.py
RUN python /app/to_marshal.py /app/index.json.gz /app/index.marshal && chmod a+r /app/index.*
COPY data/operands.json.gz /app/data/operands.json.gz
COPY selector.py operand_profiles.py /app/
RUN chmod a+r /app/selector.py
CMD ["python", "/app/selector.py"]
