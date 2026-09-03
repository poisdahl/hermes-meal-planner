#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh --provider oda|meny --household NAME

Installs one household into the standard non-root Hermes home. Set HERMES_HOME,
HERMES_PYTHON, MEAL_PLANNER_AGENT_BROWSER, or MEAL_PLANNER_BROWSER_EXECUTABLE
only when the standard locations do not apply. Set MEAL_PLANNER_NODE when an
agent-browser wrapper needs a non-standard Node.js 24+ executable. MENY also
needs MEAL_PLANNER_VIPPS_PHONE_NUMBER, or an interactive private prompt.
EOF
}

provider=
household=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider)
      provider="${2:-}"
      shift 2
      ;;
    --household)
      household="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$provider" != "oda" && "$provider" != "meny" ]]; then
  echo "--provider must be oda or meny" >&2
  exit 2
fi
if [[ -z "$household" || "$household" == *$'\n'* || "$household" == *$'\r'* ]]; then
  echo "--household must be a non-empty single-line name" >&2
  exit 2
fi
vipps_phone_number="${MEAL_PLANNER_VIPPS_PHONE_NUMBER:-}"
if [[ "$provider" == "meny" && -z "$vipps_phone_number" && -t 0 ]]; then
  read -r -s -p "Vipps mobile number (8 digits): " vipps_phone_number
  printf '\n'
fi
if [[ "$provider" == "meny" && ! "$vipps_phone_number" =~ ^[0-9]{8}$ ]]; then
  echo "MENY requires an 8-digit MEAL_PLANNER_VIPPS_PHONE_NUMBER" >&2
  exit 2
fi
if ! command -v hermes >/dev/null 2>&1; then
  echo "Hermes Agent is not installed or is not on PATH" >&2
  exit 1
fi
mcp_add_help="$(hermes mcp add --help 2>&1 || true)"
if [[ "$mcp_add_help" != *"--connect-timeout"* ]]; then
  echo "Hermes Agent 0.20.5 or newer is required" >&2
  exit 1
fi

source_root="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$source_root"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
private_root="${MEAL_PLANNER_HOME:-$hermes_home/meal-planner}"
config_path="$private_root/config.json"
socket_path="$private_root/service.sock"
browser_socket_directory="${MEAL_PLANNER_BROWSER_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}/hermes-meal-planner-$(id -u)}"
unit_path="$hermes_home/systemd/hermes-meal-planner.service"
xdg_config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
user_unit_path="$xdg_config_home/systemd/user/hermes-meal-planner.service"
launchd_label="${MEAL_PLANNER_LAUNCHD_LABEL:-com.hermes-agent.meal-planner}"
launch_agent_path="${MEAL_PLANNER_LAUNCH_AGENT_PATH:-$HOME/Library/LaunchAgents/$launchd_label.plist}"
launchd_stdout_path="${MEAL_PLANNER_STDOUT_LOG:-$HOME/Library/Logs/$launchd_label.out.log}"
launchd_stderr_path="${MEAL_PLANNER_STDERR_LOG:-$HOME/Library/Logs/$launchd_label.err.log}"
existing_install=false
if [[ -e "$config_path" || -e "$private_root/state/state.json" || -e "$unit_path" || -e "$user_unit_path" || -e "$launch_agent_path" ]]; then
  existing_install=true
fi

find_hermes_python() {
  local candidate
  for candidate in \
    "${HERMES_PYTHON:-}" \
    "$hermes_home/hermes-agent/venv/bin/python" \
    /usr/local/lib/hermes-agent/venv/bin/python; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

python="$(find_hermes_python || true)"
if [[ -z "$python" ]] || ! "$python" -c 'import mcp; import tools.mcp_oauth' >/dev/null 2>&1; then
  echo "Hermes managed Python with MCP support was not found; set HERMES_PYTHON to its venv/bin/python" >&2
  exit 1
fi

if [[ -n "${MEAL_PLANNER_AGENT_BROWSER:-}" && -x "$MEAL_PLANNER_AGENT_BROWSER" ]]; then
  agent_browser="$MEAL_PLANNER_AGENT_BROWSER"
elif command -v agent-browser >/dev/null 2>&1; then
  agent_browser="$(command -v agent-browser)"
elif [[ -x "$HOME/.local/lib/hermes-meal-planner/node_modules/.bin/agent-browser" ]]; then
  agent_browser="$HOME/.local/lib/hermes-meal-planner/node_modules/.bin/agent-browser"
elif [[ -x "$hermes_home/node/bin/agent-browser" ]]; then
  agent_browser="$hermes_home/node/bin/agent-browser"
else
  echo "agent-browser is missing. Install it under your home and set MEAL_PLANNER_AGENT_BROWSER." >&2
  exit 1
fi
runtime_path="/usr/local/bin:/usr/bin:/bin"
agent_browser_header=
if [[ "$(LC_ALL=C head -c 2 -- "$agent_browser" 2>/dev/null || true)" == '#!' ]]; then
  agent_browser_header="$(head -n 1 -- "$agent_browser" 2>/dev/null || true)"
fi
if [[ "$agent_browser_header" == *node* ]]; then
  if [[ -n "${MEAL_PLANNER_NODE:-}" && -x "$MEAL_PLANNER_NODE" ]]; then
    node="$MEAL_PLANNER_NODE"
  elif [[ -x "$hermes_home/node/bin/node" ]]; then
    node="$hermes_home/node/bin/node"
  elif command -v node >/dev/null 2>&1; then
    node="$(command -v node)"
  else
    echo "agent-browser requires Node.js 24 or newer; install it or set MEAL_PLANNER_NODE" >&2
    exit 1
  fi
  node_major="$("$node" -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || true)"
  if [[ ! "$node_major" =~ ^[0-9]+$ || "$node_major" -lt 24 ]]; then
    echo "agent-browser requires Node.js 24 or newer" >&2
    exit 1
  fi
  runtime_path="$(dirname -- "$node"):$runtime_path"
fi

if [[ -n "${MEAL_PLANNER_BROWSER_EXECUTABLE:-}" && -x "$MEAL_PLANNER_BROWSER_EXECUTABLE" ]]; then
  chromium="$MEAL_PLANNER_BROWSER_EXECUTABLE"
else
  chromium=
  for candidate in chromium chromium-browser google-chrome-stable google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      chromium="$(command -v "$candidate")"
      break
    fi
  done
  if [[ -z "$chromium" && "$(uname -s)" == "Darwin" ]]; then
    for candidate in \
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      "/Applications/Chromium.app/Contents/MacOS/Chromium" \
      "$HOME/Applications/Chromium.app/Contents/MacOS/Chromium"; do
      if [[ -x "$candidate" ]]; then
        chromium="$candidate"
        break
      fi
    done
  fi
fi
if [[ -z "$chromium" ]]; then
  echo "Chromium is missing. Install it or set MEAL_PLANNER_BROWSER_EXECUTABLE" >&2
  exit 1
fi
resolved_chromium="$(readlink -f -- "$chromium" 2>/dev/null || printf '%s' "$chromium")"
if [[ "$chromium" == /snap/* || "$resolved_chromium" == /snap/* ]] \
  || { [[ -r "$chromium" ]] && grep -Eq '/snap/bin/chromium|snap run chromium' "$chromium"; }; then
  echo "Snap Chromium cannot use the private Hermes profile. Install a non-snap Chromium/Chrome and set MEAL_PLANNER_BROWSER_EXECUTABLE." >&2
  exit 1
fi
if [[ "$provider" == "oda" && ! -f "$hermes_home/mcp-tokens/oda-weekly.json" ]]; then
  echo "Oda OAuth is not ready. Authenticate Oda with Hermes first, then rerun this installer." >&2
  exit 1
fi
if [[ "$provider" == "oda" ]]; then
  "$python" - "$hermes_home/config.yaml" <<'PY'
from pathlib import Path
import sys
import yaml

path = Path(sys.argv[1])
value = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
server = ((value or {}).get("mcp_servers") or {}).get("oda-weekly")
if not isinstance(server, dict) or server.get("enabled") is not False:
    raise SystemExit("disable the raw oda-weekly MCP server before installing the guarded meal planner")
PY
fi
case "$(uname -s)" in
  Darwin)
    if ! command -v launchctl >/dev/null 2>&1; then
      echo "launchctl is required for supervised macOS installation." >&2
      exit 1
    fi
    if [[ ! "$launchd_label" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
      echo "MEAL_PLANNER_LAUNCHD_LABEL contains unsupported characters" >&2
      exit 2
    fi
    launchd_domain="gui/$(id -u)"
    service_manager=launchd
    ;;
  Linux)
    if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
      echo "A running user systemd manager is required for supervised installation." >&2
      exit 1
    fi
    service_manager=systemd
    ;;
  *)
    echo "Only systemd-based Linux and macOS are supported." >&2
    exit 1
    ;;
esac

umask 077
mkdir -p "$private_root/state" "$private_root/browser/profile" "$private_root/secrets/recipe-libraries" "$browser_socket_directory" "$hermes_home/skills/meal-planner"
if [[ "$service_manager" == "systemd" ]]; then
  mkdir -p "$(dirname -- "$unit_path")" "$(dirname -- "$user_unit_path")"
else
  mkdir -p "$(dirname -- "$launch_agent_path")" "$(dirname -- "$launchd_stdout_path")" "$(dirname -- "$launchd_stderr_path")"
fi
chmod 700 "$private_root" "$private_root/state" "$private_root/browser" "$private_root/browser/profile" "$private_root/secrets" "$private_root/secrets/recipe-libraries" "$browser_socket_directory"

if [[ -e "$config_path" ]]; then
  "$python" -c 'from pathlib import Path; from service import config; import sys; value=config(Path(sys.argv[1])); value["provider"] == sys.argv[2] or sys.exit("existing config uses a different provider"); value["household"] == sys.argv[3] or sys.exit("existing config uses a different household"); value["provider"] != "meny" or value.get("vipps_phone_number") or sys.exit("existing MENY config is missing vipps_phone_number")' "$config_path" "$provider" "$household"
else
  PROVIDER="$provider" HOUSEHOLD="$household" VIPPS_PHONE_NUMBER="$vipps_phone_number" "$python" - "$config_path" <<'PY'
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
value = {
    "instance": "my-agent",
    "household": os.environ["HOUSEHOLD"],
    "provider": os.environ["PROVIDER"],
    "confirmation_policy": "fresh",
    "primary_recipe_library_id": "builtin",
    "recipe_libraries": [
        {"library_id": "builtin", "provider": "builtin", "read_only": False},
    ],
    "email_automation_profile": None,
    "profile_overrides": {},
}
if value["provider"] == "meny":
    value["vipps_phone_number"] = os.environ["VIPPS_PHONE_NUMBER"]
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(value, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
fi
chmod 600 "$config_path"

if [[ "$existing_install" == true && "$service_manager" == "systemd" ]]; then
  if systemctl --user is-active --quiet hermes-meal-planner.service; then
    systemctl --user stop hermes-meal-planner.service
  fi
elif [[ "$existing_install" == true ]] && launchctl print "$launchd_domain/$launchd_label" >/dev/null 2>&1; then
  launchctl bootout "$launchd_domain/$launchd_label"
fi

"$python" - "$private_root/state" "$config_path" <<'PY'
from pathlib import Path
import sys

from core import StateStore
from service import config

StateStore(Path(sys.argv[1]), config(Path(sys.argv[2])))
PY

cp "$source_root/skill/SKILL.md" "$hermes_home/skills/meal-planner/SKILL.md"
chmod 600 "$hermes_home/skills/meal-planner/SKILL.md"

if [[ "$service_manager" == "systemd" ]]; then
  SOURCE_ROOT="$source_root" \
  INSTALL_HERMES_HOME="$hermes_home" \
  INSTALL_HERMES_PYTHON="$python" \
  INSTALL_AGENT_BROWSER="$agent_browser" \
  INSTALL_BROWSER_EXECUTABLE="$chromium" \
  INSTALL_PRIVATE_ROOT="$private_root" \
  INSTALL_CONFIG_PATH="$config_path" \
  INSTALL_SOCKET_PATH="$socket_path" \
  INSTALL_BROWSER_SOCKET_DIRECTORY="$browser_socket_directory" \
  INSTALL_RUNTIME_PATH="$runtime_path" \
    "$python" - "$source_root/systemd/hermes-meal-planner.service" "$unit_path" <<'PY'
import os
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
values = {
    "@SOURCE_ROOT@": os.environ["SOURCE_ROOT"],
    "@HERMES_HOME@": os.environ["INSTALL_HERMES_HOME"],
    "@HERMES_PYTHON@": os.environ["INSTALL_HERMES_PYTHON"],
    "@AGENT_BROWSER@": os.environ["INSTALL_AGENT_BROWSER"],
    "@BROWSER_EXECUTABLE@": os.environ["INSTALL_BROWSER_EXECUTABLE"],
    "@PRIVATE_ROOT@": os.environ["INSTALL_PRIVATE_ROOT"],
    "@CONFIG_PATH@": os.environ["INSTALL_CONFIG_PATH"],
    "@SOCKET_PATH@": os.environ["INSTALL_SOCKET_PATH"],
    "@BROWSER_SOCKET_DIRECTORY@": os.environ["INSTALL_BROWSER_SOCKET_DIRECTORY"],
    "@RUNTIME_PATH@": os.environ["INSTALL_RUNTIME_PATH"],
}
for marker, value in values.items():
    if "\n" in value or "\r" in value or '"' in value or "\\" in value:
        raise SystemExit("installation paths contain characters unsupported by the systemd unit")
    source = source.replace(marker, value.replace("%", "%%"))
Path(sys.argv[2]).write_text(source, encoding="utf-8")
PY
  chmod 600 "$unit_path"
  cp "$unit_path" "$user_unit_path"
  chmod 600 "$user_unit_path"
else
  SOURCE_ROOT="$source_root" \
  INSTALL_LAUNCHD_LABEL="$launchd_label" \
  INSTALL_HERMES_HOME="$hermes_home" \
  INSTALL_HERMES_PYTHON="$python" \
  INSTALL_AGENT_BROWSER="$agent_browser" \
  INSTALL_BROWSER_EXECUTABLE="$chromium" \
  INSTALL_PRIVATE_ROOT="$private_root" \
  INSTALL_CONFIG_PATH="$config_path" \
  INSTALL_SOCKET_PATH="$socket_path" \
  INSTALL_BROWSER_SOCKET_DIRECTORY="$browser_socket_directory" \
  INSTALL_RUNTIME_PATH="$runtime_path" \
  INSTALL_STDOUT_PATH="$launchd_stdout_path" \
  INSTALL_STDERR_PATH="$launchd_stderr_path" \
    "$python" - "$source_root/launchd/hermes-meal-planner.plist" "$launch_agent_path" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

value = plistlib.loads(Path(sys.argv[1]).read_bytes())
markers = {
    "@LAUNCHD_LABEL@": os.environ["INSTALL_LAUNCHD_LABEL"],
    "@SOURCE_ROOT@": os.environ["SOURCE_ROOT"],
    "@HERMES_HOME@": os.environ["INSTALL_HERMES_HOME"],
    "@HERMES_PYTHON@": os.environ["INSTALL_HERMES_PYTHON"],
    "@AGENT_BROWSER@": os.environ["INSTALL_AGENT_BROWSER"],
    "@BROWSER_EXECUTABLE@": os.environ["INSTALL_BROWSER_EXECUTABLE"],
    "@PRIVATE_ROOT@": os.environ["INSTALL_PRIVATE_ROOT"],
    "@CONFIG_PATH@": os.environ["INSTALL_CONFIG_PATH"],
    "@SOCKET_PATH@": os.environ["INSTALL_SOCKET_PATH"],
    "@BROWSER_SOCKET_DIRECTORY@": os.environ["INSTALL_BROWSER_SOCKET_DIRECTORY"],
    "@RUNTIME_PATH@": os.environ["INSTALL_RUNTIME_PATH"],
    "@STDOUT_PATH@": os.environ["INSTALL_STDOUT_PATH"],
    "@STDERR_PATH@": os.environ["INSTALL_STDERR_PATH"],
}

def replace(item):
    if isinstance(item, str):
        for marker, replacement in markers.items():
            item = item.replace(marker, replacement)
        return item
    if isinstance(item, list):
        return [replace(child) for child in item]
    if isinstance(item, dict):
        return {key: replace(child) for key, child in item.items()}
    return item

with Path(sys.argv[2]).open("wb") as handle:
    plistlib.dump(replace(value), handle, sort_keys=False)
PY
  chmod 600 "$launch_agent_path"
  plutil -lint "$launch_agent_path" >/dev/null
fi

mcp_state="$("$python" - "$hermes_home/config.yaml" "$python" "$source_root/mcp_server.py" "$socket_path" <<'PY'
from pathlib import Path
import sys
import yaml

path = Path(sys.argv[1])
value = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
server = ((value or {}).get("mcp_servers") or {}).get("meal_planner")
if server is None:
    print("add")
elif (
    server.get("command") == sys.argv[2]
    and server.get("args") == [sys.argv[3]]
    and (server.get("env") or {}).get("MEAL_PLANNER_SOCKET") == sys.argv[4]
):
    print("present")
else:
    raise SystemExit("an existing meal_planner MCP server uses a different command, source or socket")
PY
)"
if [[ "$mcp_state" == "add" ]]; then
  printf 'y\n' | hermes mcp add meal_planner \
    --command "$python" \
    --connect-timeout 10 \
    --env "MEAL_PLANNER_SOCKET=$socket_path" \
    --args "$source_root/mcp_server.py"
fi
"$python" - "$hermes_home/config.yaml" "$python" "$source_root/mcp_server.py" "$socket_path" <<'PY'
from pathlib import Path
import sys
import yaml

value = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
server = (value.get("mcp_servers") or {}).get("meal_planner") or {}
if server.get("command") != sys.argv[2] or server.get("args") != [sys.argv[3]]:
    raise SystemExit("Hermes did not save the meal_planner MCP server")
if (server.get("env") or {}).get("MEAL_PLANNER_SOCKET") != sys.argv[4]:
    raise SystemExit("Hermes did not save the meal_planner socket")
if server.get("enabled") is not True:
    raise SystemExit("Hermes saved meal_planner disabled; resolve MCP discovery before continuing")
PY

if [[ "$service_manager" == "systemd" ]]; then
  systemctl --user daemon-reload
  if [[ "$existing_install" == true ]]; then
    systemctl --user start hermes-meal-planner.service
  fi
else
  if [[ "$existing_install" == true ]]; then
    launchctl bootstrap "$launchd_domain" "$launch_agent_path"
  fi
fi

if [[ "$existing_install" == true ]]; then
  status_verified=false
  for _attempt in {1..40}; do
    if MEAL_PLANNER_SOCKET="$socket_path" "$python" - <<'PY'
from mcp_server import rpc

status = rpc("status")
if status.get("state_version") != 7 or "product_favorites_count" not in status or "favorites" in status:
    raise SystemExit("meal-planner status does not expose canonical v7 state")
PY
    then
      status_verified=true
      break
    fi
    sleep 0.5
  done
  if [[ "$status_verified" != true ]]; then
    echo "the restarted meal-planner service did not expose canonical v7 status" >&2
    exit 1
  fi
fi

mcp_probe="$(hermes mcp test meal_planner 2>&1)"
if [[ "$mcp_probe" != *"meal_planner_product_favorites"* || "$mcp_probe" == *"meal_planner_favorites"* ]]; then
  echo "Hermes MCP discovery did not expose only meal_planner_product_favorites" >&2
  printf '%s\n' "$mcp_probe" >&2
  exit 1
fi

if [[ "$service_manager" == "systemd" ]]; then
  cat <<EOF
Installed the meal planner for $household with provider $provider.

Next:
  1. Complete provider login with the exact browser command printed below.
  2. systemctl --user enable --now hermes-meal-planner.service
  3. hermes mcp test meal_planner
  4. Restart Hermes, then ask: "Show my meal-planner status."

Resolved runtime:
  Hermes Python: $python
  agent-browser: $agent_browser
  Chromium: $chromium
EOF
else
  cat <<EOF
Installed the meal planner for $household with provider $provider.

Next:
  1. Complete provider login with the exact browser command printed below.
  2. launchctl bootstrap $launchd_domain "$launch_agent_path"
  3. hermes mcp test meal_planner
  4. Restart Hermes, then ask: "Show my meal-planner status."

Lifecycle:
  Status:  launchctl print $launchd_domain/$launchd_label
  Restart: launchctl kickstart -k $launchd_domain/$launchd_label
  Stop:    launchctl bootout $launchd_domain/$launchd_label
  Start:   launchctl bootstrap $launchd_domain "$launch_agent_path"
  Logs:    "$launchd_stdout_path" and "$launchd_stderr_path"

Resolved runtime:
  Hermes Python: $python
  agent-browser: $agent_browser
  Chromium: $chromium
EOF
fi
printf '  Login command: %q --user-data-dir=%q %q\n' \
  "$chromium" \
  "$private_root/browser/profile" \
  "$([[ "$provider" == "oda" ]] && printf '%s' 'https://oda.com/no/' || printf '%s' 'https://meny.no/')"
