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

COPY map/map.json.gz /app/map.json.gz
COPY selector.py tools/pickle_map.py /app/
RUN python /app/pickle_map.py /app/map.json.gz /app/map.pkl && rm /app/map.json.gz \
    && python -m compileall -q -j 4 /usr/local/lib/python3.13 /app/selector.py >/dev/null || true \
    && chmod -R a+rX /app /usr/local/lib/python3.13

# a tiny launcher so selector.py itself runs from its compiled .pyc
RUN printf 'import sys\nsys.path.insert(0, "/app")\nimport selector\nselector.main()\n' > /app/run.py
CMD ["python", "-S", "/app/run.py"]
