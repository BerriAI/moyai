FROM mirror.gcr.io/library/debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive
# Mirrors Moyai's computer image (xvfb, openbox, xterm, xdotool, chromium, node) plus GTK
# accessibility so native apps expose an AT-SPI tree. open-computer-use is installed in both
# arms; the agent setup removes it for the baseline arm.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ripgrep curl jq ca-certificates xvfb openbox xterm xdotool x11-xserver-utils \
    fonts-dejavu-core dbus dbus-x11 at-spi2-core python3 python3-gi python3-pil \
    gir1.2-gtk-3.0 gir1.2-atspi-2.0 mousepad nodejs npm imagemagick \
    && rm -rf /var/lib/apt/lists/* \
    && npm i -g open-computer-use@1.1.2
ENV DISPLAY=:99 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/moyai-dbus \
    XDG_RUNTIME_DIR=/tmp/xdg \
    NO_AT_BRIDGE=0 \
    GTK_A11Y=always
WORKDIR /workspace
COPY eval/ /eval/
RUN mkdir -p /logs/agent /logs/verifier /workspace/out
