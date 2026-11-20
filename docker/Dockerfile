# Reproducible Lean C environment (Ubuntu 24.04, clang-18, CBMC 5.95, bubblewrap,
# V2: Frama-C 33 + WP + Alt-Ergo (opam), Z3, CVC5, Lean 4).
#
#   docker build -t leanc -f docker/Dockerfile .
#   docker run --rm -it --security-opt seccomp=unconfined --cap-add SYS_ADMIN leanc leanc doctor
#
# bubblewrap needs user namespaces inside the container; if your runtime forbids
# them, `leanc doctor` reports the sandbox as unavailable and Lean C refuses to
# execute untrusted test programs (static analysis and CBMC still run).
# Build without the V2 tools: docker build --build-arg LEANC_V2=0 ...
FROM ubuntu:24.04

ARG LEANC_V2=1
ARG LEAN_VERSION=4.35.0
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 OPAMROOT=/opt/opam OPAMYES=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        clang-18 libclang-rt-18-dev llvm-18 gcc g++ libc6-dev cbmc bubblewrap \
        python3 python3-pip python3-venv ca-certificates curl zstd \
    && ln -sf /usr/bin/clang-18 /usr/local/bin/clang && ln -sf /usr/bin/clang++-18 /usr/local/bin/clang++ \
    && rm -rf /var/lib/apt/lists/*

# V2 tools: Frama-C/WP (Ubuntu's frama-c-base ships without WP), provers, Lean 4.
RUN if [ "$LEANC_V2" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends opam libgmp-dev zlib1g-dev pkg-config m4 z3 cvc5 \
      && opam init --bare --disable-sandboxing -n \
      && opam switch create leanc ocaml-base-compiler.4.14.2 \
      && opam install --switch=leanc -y alt-ergo frama-c \
      && opam clean -a -c -s --logs \
      && mkdir -p /opt/lean \
      && curl -sSfL "https://releases.lean-lang.org/lean4/v${LEAN_VERSION}/lean-${LEAN_VERSION}-linux.tar.zst" \
         | tar --zstd -x -C /opt/lean \
      && rm -rf /var/lib/apt/lists/*; \
    fi
ENV LEANC_FRAMAC=/opt/opam/leanc/bin/frama-c

WORKDIR /opt/leanc
COPY . /opt/leanc
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -e ".[dev]"
ENV PATH="/opt/venv/bin:${PATH}"

RUN useradd -m leanc && chown -R leanc /opt/leanc
USER leanc
CMD ["leanc", "doctor"]
