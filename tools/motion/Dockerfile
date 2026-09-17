FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends zoneminder mariadb-server python3 ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY motion-worker.py /usr/local/bin/porchlight-motion-worker
WORKDIR /work
ENTRYPOINT ["python3", "/usr/local/bin/porchlight-motion-worker"]
