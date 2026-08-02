#!/bin/sh
set -eu

ROOT=/data/rapid-scada

if [ ! -f "$ROOT/Config/ScadaInstanceConfig.xml" ]; then
  mkdir -p "$ROOT"
  cp -a /opt/rapidscada-seed/. "$ROOT/"
  mkdir -p "$ROOT/logs"
  sed -i 's#<LogDir>.*</LogDir>#<LogDir>/data/rapid-scada/logs/</LogDir>#' "$ROOT/Config/ScadaInstanceConfig.xml"
fi

mkdir -p "$ROOT/logs"

if [ ! -f "$ROOT/Views/HelloWorld/T3000-Starter.mim" ]; then
  cp /usr/local/share/rapid-scada/starter-mimic.mim "$ROOT/Views/HelloWorld/T3000-Starter.mim"
fi

shutdown() {
  kill "$SERVER_PID" "$COMM_PID" "$AGENT_PID" 2>/dev/null || true
}

trap shutdown INT TERM

cd "$ROOT/ScadaServer"
dotnet ScadaServerWkr.dll &
SERVER_PID=$!

cd "$ROOT/ScadaComm"
dotnet ScadaCommWkr.dll &
COMM_PID=$!

cd "$ROOT/ScadaAgent"
dotnet ScadaAgentWkr.dll &
AGENT_PID=$!

cd "$ROOT/ScadaWeb"
exec dotnet ScadaWeb.dll --urls=http://0.0.0.0:10008
