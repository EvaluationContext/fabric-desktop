# Fabric Desktop agent as a container. Build from the repository root:
#   docker build -f deploy/linux/Dockerfile -t fabric-desktop-agent .
# Run with the config, certificate and project mounted, and a volume for the queue:
#   docker run -d --name fd-agent \
#     -v /etc/fabric-desktop:/etc/fabric-desktop:ro \
#     -v /srv/fabric/sales-metadata:/srv/fabric/sales-metadata:ro \
#     -v fd-agent-home:/var/lib/fabric-desktop \
#     fabric-desktop-agent
FROM python:3.12-slim

RUN useradd --system --create-home --home-dir /var/lib/fabric-desktop fabric-desktop
COPY sidecar /tmp/sidecar
RUN pip install --no-cache-dir /tmp/sidecar && rm -rf /tmp/sidecar

ENV FABRIC_DESKTOP_AGENT_CONFIG=/etc/fabric-desktop/agent.toml \
    FABRIC_DESKTOP_HOME=/var/lib/fabric-desktop \
    PYTHONUNBUFFERED=1
USER fabric-desktop
VOLUME ["/var/lib/fabric-desktop"]
# docker stop sends SIGTERM: the agent finishes running jobs first (use --stop-timeout for long ones).
STOPSIGNAL SIGTERM
ENTRYPOINT ["fabric-desktop", "agent"]
CMD ["run"]
