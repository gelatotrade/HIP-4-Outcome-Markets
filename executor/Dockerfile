FROM rust:1.81-slim AS build
WORKDIR /src

# Cache deps first
COPY Cargo.toml ./
RUN mkdir src && echo 'fn main(){}' > src/main.rs && echo '' > src/lib.rs \
 && cargo build --release || true

COPY src ./src
RUN cargo build --release && \
    cp target/release/executor /executor

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
