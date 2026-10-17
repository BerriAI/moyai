FROM public.ecr.aws/docker/library/python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep curl jq util-linux nodejs npm tar attr xvfb openbox xterm xdotool chromium tint2 x11vnc x11-xserver-utils fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv pytest pillow==12.3.0 websockify==0.13.0 playwright==1.63.0
COPY skills /opt/moyai/skills
COPY desktop.py browser_guest.py browser_driver.py /opt/moyai/
COPY checkpoint.py service.py /opt/moyai-lambda/
RUN mkdir -p /workspace /var/lib/moyai-runtime
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 80
CMD ["python", "/opt/moyai-lambda/service.py"]
