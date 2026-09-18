# Hugging Face Spaces deployment.
#
# Docker rather than the Gradio SDK because this needs Python 3.13 — `tomllib`
# is standard-library only from 3.11, and the repository analyzer uses it. The
# Gradio SDK pins an older interpreter.
#
# Spaces runs containers as uid 1000 and expects the app on port 7860.

FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

# Spaces provides uid 1000. Creating it explicitly means the same image runs
# identically locally.
RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PATCHPILOT_WORKSPACE_DIR=/home/user/workspace

WORKDIR /home/user/app

# Dependencies first, so a code change does not reinstall them.
COPY --chown=user requirements-space.txt ./
RUN pip install --no-cache-dir --user -r requirements-space.txt

COPY --chown=user src/ ./src/
COPY --chown=user examples/ ./examples/
COPY --chown=user pyproject.toml README.md ./
RUN pip install --no-cache-dir --user --no-deps -e .

# The pre-built retrieval index, so the demo answers immediately. Embedding a
# repository takes several minutes on CPU — too slow for a container start, and
# far too slow for a web request.
RUN mkdir -p /home/user/workspace \
 && cp -r examples/index /home/user/workspace/qdrant

# Warm the embedding model into the image rather than downloading it on the
# first request, which would make a visitor wait ~30 s for nothing.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"

EXPOSE 7860
CMD ["python", "-m", "patchpilot.web.app"]
