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
sed -i 's#<Directory>/opt/scada/</Directory>#<Directory>/data/rapid-scada/</Directory>#' "$ROOT/ScadaAgent/Config/ScadaAgentConfig.xml"

# The editor finds a project by walking upward from the mimic file until
# it reaches an .rsproj file. Keep a minimal project marker beside the
# safe demo mimic in both the runtime and web-editor paths.
mkdir -p "$ROOT/Views/HelloWorld" "$ROOT/ScadaWeb/HelloWorld"
if [ ! -f "$ROOT/Views/HelloWorld/T3000-Starter.mim" ]; then
  cp /usr/local/share/rapid-scada/starter-mimic.mim "$ROOT/Views/HelloWorld/T3000-Starter.mim"
fi
if [ ! -f "$ROOT/ScadaWeb/HelloWorld/T3000-Starter.mim" ]; then
  cp /usr/local/share/rapid-scada/starter-mimic.mim "$ROOT/ScadaWeb/HelloWorld/T3000-Starter.mim"
fi
if [ ! -f "$ROOT/Views/HelloWorld/HelloWorld.rsproj" ]; then
  cat > "$ROOT/Views/HelloWorld/HelloWorld.rsproj" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<ScadaProject>
  <AdminVersion>6.4.7.0</AdminVersion>
  <ProjectVersion>6.0</ProjectVersion>
  <Description>Rapid SCADA T3000 starter screen project.</Description>
</ScadaProject>
EOF
fi
if [ ! -f "$ROOT/ScadaWeb/HelloWorld/HelloWorld.rsproj" ]; then
  cp "$ROOT/Views/HelloWorld/HelloWorld.rsproj" "$ROOT/ScadaWeb/HelloWorld/HelloWorld.rsproj"
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
