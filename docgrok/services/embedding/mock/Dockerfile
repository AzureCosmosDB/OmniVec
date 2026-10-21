FROM rust:latest AS builder
RUN rustup target add x86_64-unknown-linux-musl
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src/ src/
RUN cargo build --locked --release --target x86_64-unknown-linux-musl

FROM alpine:3.20
RUN addgroup -S model && adduser -S model -G model
COPY --from=builder /app/target/x86_64-unknown-linux-musl/release/omnivec-mock-embedding /usr/local/bin/omnivec-mock-embedding
USER model
EXPOSE 8000
ENV RUST_LOG=info
CMD ["omnivec-mock-embedding"]
