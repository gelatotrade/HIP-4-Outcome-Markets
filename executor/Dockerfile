FROM rust:1.82-bookworm AS build
WORKDIR /src

# secp256k1 ships C sources that need a host toolchain at build time.
# rust:bookworm (non-slim) already includes build-essential.

COPY Cargo.toml Cargo.lock ./
COPY src ./src

RUN cargo build --release \
 && strip target/release/executor \
 && cp target/release/executor /executor

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /executor /usr/local/bin/executor

ENV RUST_LOG=info,executor=debug
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8765/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/executor"]
