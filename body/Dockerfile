# Build from project root: docker build -t os -f body/Dockerfile .
FROM alpine:3.20

RUN apk update && apk add --no-cache \
    bash curl ca-certificates git \
    coreutils procps \
    python3 py3-pip \
    nodejs npm \
    chromium \
    nss freetype harfbuzz ttf-freefont \
    udev dbus-libs libstdc++ \
    xvfb-run xorg-server mesa-dri-gallium \
    x11vnc openbox xterm pcmanfm \
    font-noto font-noto-emoji \
    adwaita-icon-theme \
    docker-cli docker-cli-compose \
  && update-ca-certificates || true

# Optional: CRIU for kernel checkpoint/restore (may not work on all hosts)
RUN apk add --no-cache criu 2>/dev/null || true

# Install uv to /opt so /root can be mounted from host
ENV UV_INSTALL_DIR=/opt/uv
RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/opt/uv:/opt/tools/bin:/usr/bin:$PATH"
ENV UV_TOOL_BIN_DIR=/opt/tools/bin
ENV UV_TOOL_DIR=/opt/tools

# Install MCP tools (system-wide, single Python)
RUN uv tool install mcp-proxy \
 && uvx --from "browser-use[cli]" browser-use --help >/dev/null 2>&1 || true \
 && pip install --break-system-packages linux-mcp-server dill anthropic openai httpx mcp \
 && npm install -g @modelcontextprotocol/server-filesystem \
 && rm -rf /root/.cache /tmp/* /var/cache/apk/* \
 && find / -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true \
 && find / -type f -name '*.pyc' -delete 2>/dev/null || true \
 && rm -rf /usr/share/doc /usr/share/man /usr/share/misc \
 && rm -rf /usr/share/fonts/misc /usr/share/fonts/opensans \
    /usr/share/fonts/encodings /usr/share/fonts/75dpi \
    /usr/share/fonts/100dpi /usr/share/fonts/cyrillic \
 && cd /usr/share/icons/Adwaita && rm -rf 96x96 64x64 512x512 256x256 cursors scalable-up-to-32

RUN mkdir -p /data/criu_checkpoints

ENV KERNEL_PORT=8080 KERNEL_HOST=0.0.0.0
ENV CRIU_CHECKPOINT_DIR=/data/criu_checkpoints

# Openbox menu config (stored outside /root so it survives mount)
RUN mkdir -p /etc/xdg/openbox \
 && printf '<?xml version="1.0" encoding="UTF-8"?>\n<openbox_menu xmlns="http://openbox.org/3.4/menu">\n<menu id="root-menu" label="Menu">\n  <item label="Browser"><action name="Execute"><execute>chromium --no-sandbox --disable-dev-shm-usage --disable-gpu</execute></action></item>\n  <item label="Terminal"><action name="Execute"><execute>xterm -fa "Noto Sans Mono" -fs 12 -bg black -fg white -geometry 120x35</execute></action></item>\n  <separator />\n</menu>\n</openbox_menu>\n' > /etc/xdg/openbox/menu.xml

RUN printf '#!/bin/sh\nexec /repo/scripts/evolve.sh "$@"\n' > /usr/local/bin/evolve \
 && chmod +x /usr/local/bin/evolve

COPY body/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8765 8080 5900

ENTRYPOINT ["/entrypoint.sh"]
