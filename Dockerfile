FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# The bundled Claude Code CLI writes session state under $HOME; mount an emptyDir at /tmp in k8s.
ENV HOME=/tmp PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["rhapsode"]
