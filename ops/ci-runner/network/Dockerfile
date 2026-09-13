FROM ubuntu@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254
RUN apt-get update && apt-get install -y --no-install-recommends iptables && rm -rf /var/lib/apt/lists/*
COPY apply.sh /apply.sh
ENTRYPOINT ["/bin/sh", "/apply.sh"]
