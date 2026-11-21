# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Pristine sqlglot source at the pinned commit (the patches apply to it).
ARG SQLGLOT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<'PY'
import io, tarfile, urllib.request
url = "https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) == 2 and parts[1]:
            m.name = "sqlglot/" + parts[1]
            tar.extract(m, "/app", filter="data")
PY

# The packages sqlglot's tests need (the organisers' own lock file), so the
# selector can run cheap candidate tests against the patched code at run time.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /app/requirements.lock

COPY map/map.json.gz /app/map.json.gz
COPY map/tpcds.json /app/tpcds.json
COPY selector.py fastverify.py standins.py standins_tpcds.py tools/pickle_map.py tools/import_graph.py /app/
RUN python /app/pickle_map.py /app/map.json.gz /app/map.pkl && rm /app/map.json.gz \
    && python /app/import_graph.py /app/sqlglot /app/imports.json \
    && python -m compileall -q -j 4 /usr/local/lib/python3.13 /app/selector.py /app/fastverify.py /app/standins.py /app/standins_tpcds.py \
       /app/sqlglot/sqlglot /app/sqlglot/tests >/dev/null || true \
    && chmod -R a+rX /app /usr/local/lib/python3.13

# a tiny launcher so selector.py itself runs from its compiled .pyc
RUN printf 'import sys\nsys.path.insert(0, "/app")\nimport selector\nselector.main()\n' > /app/run.py
CMD ["python", "-S", "/app/run.py"]
