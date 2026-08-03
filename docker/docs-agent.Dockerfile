# UC1: documentation deep-dive agent.
# Build the base first:  docker build -f docker/base.Dockerfile -t secagent-base:latest .
ARG BASE_IMAGE=secagent-base:latest
FROM ${BASE_IMAGE}

COPY --chown=secagent:secagent . /app
RUN python3.11 -m pip install ".[docs,tokenizer]"

USER secagent
ENV SECAGENT_LLM__BASE_URL="http://llm:8000/v1"

# Default: index a mounted repo and build docs into /out. Override the command to
# point at your repository and output directory.
ENTRYPOINT ["secagent"]
CMD ["docs", "build", "/repo", "-o", "/out"]
