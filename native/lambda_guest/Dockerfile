FROM public.ecr.aws/docker/library/python:3.13-slim AS node-runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils && rm -rf /var/lib/apt/lists/*
COPY install-node.sh /opt/moyai/install-node.sh
RUN sh /opt/moyai/install-node.sh

FROM node-runtime
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep jq util-linux tar attr xvfb openbox xterm xdotool chromium tint2 x11vnc x11-xserver-utils fonts-dejavu-core dbus-x11 at-spi2-core python3-gi gir1.2-atspi-2.0 && rm -rf /var/lib/apt/lists/*
COPY tools/package.json tools/package-lock.json /opt/moyai/tools/
RUN npm ci --ignore-scripts --prefix /opt/moyai/tools && ln -sf /opt/moyai/tools/node_modules/.bin/ocu /usr/local/bin/ocu
RUN pip install --no-cache-dir uv pytest pillow==12.3.0 websockify==0.13.0 playwright==1.63.0
COPY skills /opt/moyai/skills
COPY desktop.py browser_guest.py browser_driver.py ocu_guest.py /opt/moyai/
COPY checkpoint.py service.py /opt/moyai-lambda/
RUN mkdir -p /workspace /var/lib/moyai-runtime
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1
ENV DISPLAY=:99 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/moyai-dbus XDG_RUNTIME_DIR=/tmp/moyai-xdg NO_AT_BRIDGE=0 GTK_A11Y=always
EXPOSE 80
CMD ["python", "/opt/moyai-lambda/service.py"]
