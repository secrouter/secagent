# secagent agent image: pi (the loop) + secagent (the toolset) + the pi extension.
#
# Build the base first:
#   docker build -f docker/base.Dockerfile -t secagent-base:latest .
#
# One image serves all three roles; pick the role via the command:
#   pi interactive deep-dive ->  docker run -it secagent-agent
#   deterministic docs build ->  secagent docs build /repo -o /out
#   GitLab review webhook    ->  secagent review serve --port 8080
ARG BASE_IMAGE=secagent-base:latest
FROM ${BASE_IMAGE}

COPY --chown=secagent:secagent . /app
RUN python3.11 -m pip install ".[docs,review,tokenizer]"

USER secagent
EXPOSE 8080
ENV SECAGENT_REPO=/repo \
    SECAGENT_LLM__BASE_URL="http://llm:8000/v1"

# Default: launch pi with the secagent extension loaded, pointed at the local Gemma
# provider (configure ~/.pi/agent/models.json — see pi/models.example.json). Override
# the command for the docs-build or review-webhook roles (see docker-compose.yml).
ENTRYPOINT ["pi"]
CMD ["--extension", "/app/pi/extensions/secagent.ts", \
     "--provider", "local-gemma", "--model", "gemma-3-12b-it"]
