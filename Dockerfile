# Container action for multivon-ai/eval-action.
# Installs multivon-eval from a floating range (>=0.10.0,<1.0), so a
# fresh image build picks up the latest 0.x release — builds are NOT
# pinned to a single minor and are not bit-for-bit reproducible across
# rebuilds. The floor is 0.10.0 because the `staleness:` input depends
# on the staleness CLI that shipped in multivon-eval 0.10.0.
FROM python:3.12-slim

# Avoid Python output buffering — useful for streaming step logs.
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /action

# Floating range: any multivon-eval 0.10+ (pre-1.0). If you need a
# deterministic engine version, fork and pin (e.g. ==0.12.*) — the
# action tags (@v1.0, @v1.1, …) do not freeze the engine version.
RUN pip install --upgrade pip && pip install "multivon-eval>=0.10.0,<1.0" "PyYAML>=6.0,<7.0"

COPY src/ /action/src/
COPY entrypoint.sh /action/entrypoint.sh
RUN chmod +x /action/entrypoint.sh

ENTRYPOINT ["/action/entrypoint.sh"]
