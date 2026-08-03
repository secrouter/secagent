# UC100: GitLab merge-request review agent (webhook server).
# Build the base first:  docker build -f docker/base.Dockerfile -t secagent-base:latest .
ARG BASE_IMAGE=secagent-base:latest
FROM ${BASE_IMAGE}

COPY --chown=secagent:secagent . /app
RUN python3.11 -m pip install ".[review,tokenizer]"

USER secagent
EXPOSE 8080
ENV SECAGENT_LLM__BASE_URL="http://llm:8000/v1"

# Secrets (GitLab token, webhook secret) come from the environment / a secret mount,
# e.g. SECAGENT_GITLAB__TOKEN, SECAGENT_GITLAB__WEBHOOK_SECRET — never baked into the image.
ENTRYPOINT ["secagent"]
CMD ["review", "serve", "--host", "0.0.0.0", "--port", "8080"]
