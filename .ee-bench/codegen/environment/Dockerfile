# Go variant — adapted from the codegen skill (skill ships no native Go template).
# Hardcoded: owner=dpaia, repo=buf, go version=1.23, project_root=/app.
# Only Jinja2 variable is {{ instance.base_commit }} (changes per datapoint).
FROM golang:1.23

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y build-essential git curl wget python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Test runner that emits JUnit XML (consumed by ee_bench_parser_junit.py)
RUN go install gotest.tools/gotestsum@v1.12.2

WORKDIR /app
RUN git init && \
    git remote add origin https://github.com/dpaia/buf.git && \
    git fetch origin --depth 1 {{ instance.base_commit }} && \
    git reset --hard FETCH_HEAD

# Pre-fetch/cache module dependencies
RUN go mod download

LABEL ee-bench.type="codegen"
LABEL ee-bench.version="1.0"
RUN rm -rf /app/.ee-bench/ 2>/dev/null || true
