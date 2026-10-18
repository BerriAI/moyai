#!/bin/sh
# Starts the eval desktop (Xvfb + window manager + accessibility bus) and the task's apps.
set -eu
mkdir -p "$XDG_RUNTIME_DIR" /var/lib/eval /workspace/out
chmod 700 "$XDG_RUNTIME_DIR"
if [ ! -S /tmp/.X11-unix/X99 ]; then
  nohup Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
  for _ in $(seq 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep .1; done
fi
if [ ! -S /run/moyai-dbus ]; then
  dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork --nopidfile
fi
nohup openbox >/tmp/openbox.log 2>&1 &
nohup /usr/libexec/at-spi-bus-launcher --launch-immediately >/tmp/atspi.log 2>&1 &
sleep 1
sh /eval/launch_apps.sh
sleep 3
