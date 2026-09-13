FROM ubuntu:24.04@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254
RUN apt-get update && apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils cloud-image-utils ca-certificates && rm -rf /var/lib/apt/lists/*
USER 1000:1000
ENTRYPOINT ["qemu-system-x86_64"]
