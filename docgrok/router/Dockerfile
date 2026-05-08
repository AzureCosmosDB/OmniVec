FROM rust:latest AS builder

RUN apt-get update && apt-get install -y pkg-config libssl-dev musl-tools && rm -rf /var/lib/apt/lists/*
RUN rustup target add x86_64-unknown-linux-musl

WORKDIR /app
COPY Cargo.toml Cargo.lock* ./
# Create dummy src for dependency caching
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release --target x86_64-unknown-linux-musl 2>/dev/null || true

# Now copy real source and build
COPY src/ src/
RUN touch src/main.rs && cargo build --release --target x86_64-unknown-linux-musl

FROM alpine:3.20
RUN apk add --no-cache ca-certificates

COPY --from=builder /app/target/x86_64-unknown-linux-musl/release/docgrok-router /usr/local/bin/docgrok-router

EXPOSE 8080
ENV RUST_LOG=docgrok_router=info
CMD ["docgrok-router"]
