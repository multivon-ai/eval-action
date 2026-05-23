# Container action for multivon-ai/eval-action.
# Pinned to a multivon-eval minor version so behavior is deterministic
# across runner upgrades. Users who want a different version should
# pin the action with `multivon-ai/eval-action@v1.2` (matches the
# multivon-eval minor).
FROM python:3.12-slim

# Avoid Python output buffering — useful for streaming step logs.
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /action

# Pin the same minor version we test against. Action consumers can
# override via @v1.0, @v1.1, etc. — each tag rebuilds with a different
# multivon-eval pin. v1.x action tracks multivon-eval 0.9.x.
RUN pip install --upgrade pip && pip install "multivon-eval>=0.9.0,<1.0" "PyYAML>=6.0"

COPY src/ /action/src/
COPY entrypoint.sh /action/entrypoint.sh
RUN chmod +x /action/entrypoint.sh

ENTRYPOINT ["/action/entrypoint.sh"]
