FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        libcairo2 \
        libgdk-pixbuf2.0-0 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY templates /app/templates
COPY fonts /app/fonts

ENV HOST=0.0.0.0
ENV PORT=9080
ENV OUTPUT_PATH=/data/trmnl.png

EXPOSE 9080

CMD ["python", "/app/app.py"]
