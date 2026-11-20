# Reproducible Lean C environment (Ubuntu 24.04, clang-18, CBMC 5.95, bubblewrap).
#
#   docker build -t leanc -f docker/Dockerfile .
#   docker run --rm -it --security-opt seccomp=unconfined --cap-add SYS_ADMIN leanc leanc doctor
#
# bubblewrap needs user namespaces inside the container; if your runtime forbids
# them, `leanc doctor` reports the sandbox as unavailable and Lean C refuses to
# execute untrusted test programs (static analysis and CBMC still run).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        clang-18 libclang-rt-18-dev llvm-18 gcc libc6-dev cbmc bubblewrap \
        python3 python3-pip python3-venv ca-certificates \
    && ln -sf /usr/bin/clang-18 /usr/local/bin/clang \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/leanc
COPY . /opt/leanc
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -e ".[dev]"
ENV PATH="/opt/venv/bin:${PATH}"

RUN useradd -m leanc && chown -R leanc /opt/leanc
USER leanc
CMD ["leanc", "doctor"]
