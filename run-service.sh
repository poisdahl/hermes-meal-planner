#!/usr/bin/env bash
set -Eeuo pipefail

source_root="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$source_root"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
private_root="${MEAL_CONCIERGE_HOME:-$hermes_home/meal-concierge}"
config_path="${MEAL_CONCIERGE_CONFIG:-$private_root/config.json}"
state_path="${MEAL_CONCIERGE_STATE:-$private_root/state}"
socket_path="${MEAL_CONCIERGE_SOCKET:-$private_root/service.sock}"
browser_home="${MEAL_CONCIERGE_BROWSER_HOME:-$private_root/browser}"
browser_profile="${MEAL_CONCIERGE_BROWSER_PROFILE:-$browser_home/profile}"
browser_socket_directory="${MEAL_CONCIERGE_BROWSER_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}/meal-concierge-$(id -u)}"

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
  echo "Hermes managed Python was not found; set HERMES_PYTHON to its venv/bin/python" >&2
  return 1
}

find_agent_browser() {
  local candidate
  if [[ -n "${MEAL_CONCIERGE_AGENT_BROWSER:-}" && -x "$MEAL_CONCIERGE_AGENT_BROWSER" ]]; then
    printf '%s\n' "$MEAL_CONCIERGE_AGENT_BROWSER"
    return 0
  fi
  if command -v agent-browser >/dev/null 2>&1; then
    command -v agent-browser
    return 0
  fi
  candidate="$HOME/.local/lib/meal-concierge/node_modules/.bin/agent-browser"
  if [[ -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  candidate="$hermes_home/node/bin/agent-browser"
  if [[ -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  echo "agent-browser was not found; install it before starting the meal concierge" >&2
  return 1
}

find_chromium() {
  local candidate
  if [[ -n "${MEAL_CONCIERGE_BROWSER_EXECUTABLE:-}" && -x "$MEAL_CONCIERGE_BROWSER_EXECUTABLE" ]]; then
    printf '%s\n' "$MEAL_CONCIERGE_BROWSER_EXECUTABLE"
    return 0
  fi
  for candidate in chromium chromium-browser google-chrome-stable google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  if [[ "$(uname -s)" == "Darwin" ]]; then
    for candidate in \
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      "/Applications/Chromium.app/Contents/MacOS/Chromium" \
      "$HOME/Applications/Chromium.app/Contents/MacOS/Chromium"; do
      if [[ -x "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
  fi
  echo "Chromium or Google Chrome was not found; set MEAL_CONCIERGE_BROWSER_EXECUTABLE" >&2
  return 1
}

python="$(find_hermes_python)"
agent_browser="$(find_agent_browser)"
chromium="$(find_chromium)"
provider="$("$python" -c 'from pathlib import Path; from service import config; import sys; print(config(Path(sys.argv[1]))["provider"])' "$config_path")"

umask 077
mkdir -p "$private_root" "$state_path" "$browser_home" "$browser_profile" "$browser_socket_directory" "$(dirname -- "$socket_path")"
chmod 700 "$private_root" "$state_path" "$browser_home" "$browser_profile" "$browser_socket_directory"

service_args=(
  "$python" "$source_root/service.py"
  --config "$config_path"
  --state "$state_path"
  --tokens "$hermes_home/mcp-tokens"
  --socket "$socket_path"
  --agent-uid "$(id -u)"
  --socket-group "$(id -g)"
  --browser-binary "$agent_browser"
  --browser-executable "$chromium"
  --browser-profile "$browser_profile"
  --browser-home "$browser_home"
  --browser-socket-directory "$browser_socket_directory"
  --browser-uid "$(id -u)"
  --browser-gid "$(id -g)"
)

exec "${service_args[@]}"
