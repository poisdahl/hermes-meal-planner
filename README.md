<p align="center">
  <img src="assets/meal-concierge-banner.png" alt="Meal Concierge — a retro cooking-pot mascot delivering groceries. Plan meals. Save recipes. Shop smarter." width="1000">
</p>

<p align="center">
  <a href="#agent-support"><img src="assets/icons/hermes.png" alt="Hermes Agent" title="Hermes Agent" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/openclaw.png" alt="OpenClaw" title="OpenClaw" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/nanoclaw.png" alt="NanoClaw" title="NanoClaw" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/codex.png" alt="Codex" title="Codex" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/claude-code.png" alt="Claude Code" title="Claude Code" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/grok-bot.png" alt="Grok Bot" title="Grok Bot" width="48" height="48"></a>
</p>

<p align="center">
  <a href="#supported-stores"><img src="assets/icons/oda.png" alt="Oda" title="Oda" width="40" height="40"></a>
  <a href="#supported-stores"><img src="assets/icons/meny.png" alt="MENY" title="MENY" width="80" height="40"></a>
</p>

**Plan meals, save recipes and shop for groceries through Hermes Agent.**

Meal Concierge connects [Hermes Agent](https://github.com/NousResearch/hermes-agent)
to Oda or MENY in Norway. Tell Hermes what you want to cook, build a weekly menu,
and turn it into a grocery cart you can review and order. Your household
preferences, saved recipes and menus are stored locally.

> “Plan next week's dinners for two.”
>
> “Replace Wednesday's dinner with something vegetarian.”
>
> “Show me the groceries we need and prepare checkout.”

[Installation](#installation) · [First use](#first-use) ·
[Troubleshooting](#troubleshooting) · [Technical reference](docs/reference.md) ·
[![MIT License](assets/badges/mit.svg)](LICENSE)

## What you can do

- **Plan your week:** choose portions and preferences, replace meals, plan
  leftovers and adjust the remaining week when plans change.
- **Build your recipe collection:** discover, save, search, scale and favorite
  recipes. Use the included local recipe bank or connect Mealie or RecipeSage.
- **Prepare your shopping:** match ingredients to products, account for pantry
  items, manage your cart and remember favorite or recurring purchases.
- **Manage delivery and orders:** compare delivery windows, prepare checkout,
  add goods to supported existing orders, move delivery or cancel an order.
- **Set up recurring help:** schedule weekly planning through Hermes. Recipe
  emails additionally require a configured Hermes email account/tool.

The default is **seven different dinners for two people**. Recipe discovery
uses the local bank, Oda, MENY, TheMealDB and Wikibooks Cookbook; you can adjust
portions, preferences and enabled sources during first-use setup.

Meal Concierge runs as a local background service with a skill and an MCP
connection that gives Hermes its meal and grocery tools. You use it through
your usual Hermes chat. No separate web app or database server is required.

## Agent support

**Available now:** Hermes Agent.

**Coming soon:** OpenClaw, NanoClaw, Codex, Claude Code and Grok Bot integrations.
These integrations are planned and are not available to install yet. The
installation instructions below are for Hermes Agent.

## Supported stores

Choose one store per installation. Both support product and recipe search,
cart management, delivery selection and supported order changes.

| | Oda | MENY |
|---|---|---|
| Connection | Oda MCP service, plus a browser for protected order actions | Logged-in MENY website |
| Sign-in | Hermes OAuth **and** a browser login to the same Oda account | Persistent browser login to your MENY account |
| Checkout | A payment method already configured in Oda | Home delivery and Vipps approval on your phone |

By default, Hermes asks you to confirm the prepared summary before checkout or
cancellation. An optional standing-authorization policy is described in the
[reference](docs/reference.md#upgrade-and-confirmation-details). Provider and
payment approvals still apply, and uncertain submissions are reconciled before
any further action.

## Requirements

- **Hermes Agent 0.20.5 or newer**, installed for your normal user, with `hermes`
  available in your terminal. Set up Hermes first using its
  [installation guide](https://github.com/NousResearch/hermes-agent#quick-install).
- **Linux with a running user systemd manager**, or **Apple Silicon macOS**.
  These are the supported installation paths.
- **Git, Node.js 24+ and npm.** The steps below use Hermes's bundled Node/npm
  when available, otherwise the versions on your `PATH`.
- **Google Chrome or Chromium**, plus `agent-browser` (installed below).
- **An Oda or MENY account** that supports delivery to your address. MENY also
  needs your eight-digit Vipps mobile number.

Both stores require a one-time login in a visible browser. A remote headless
server needs a private graphical session, such as X11 forwarding or a private
remote desktop, to complete that step.

## Installation

Run these steps as the same non-root user who runs Hermes.

### 1. Install the browser and browser adapter

On **Linux**, install Git and a **non-snap** Chrome or Chromium package for your
distribution. For example, on Debian:

```sh
sudo apt-get update
sudo apt-get install -y git chromium
```

Ubuntu's Chromium package may install a snap that cannot access the private
Hermes browser profile. Use a non-snap Chrome/Chromium package there.

On **macOS**, install Google Chrome or Chromium in `/Applications` or
`~/Applications`, and make sure Git is available. The installer discovers these
normal app locations.

Then, on **either platform**, install the tested browser adapter:

```sh
mkdir -p "$HOME/.local/lib/meal-concierge"
node_bin="${HERMES_HOME:-$HOME/.hermes}/node/bin/node"
npm_bin="${HERMES_HOME:-$HOME/.hermes}/node/bin/npm"
if [ ! -x "$node_bin" ] || [ ! -x "$npm_bin" ]; then
  node_bin="$(command -v node)"
  npm_bin="$(command -v npm)"
fi
"$node_bin" -e 'if (Number(process.versions.node.split(".")[0]) < 24) { console.error("Node.js 24+ is required"); process.exit(1) }' &&
PATH="$(dirname "$node_bin"):$PATH" "$npm_bin" install \
  --prefix "$HOME/.local/lib/meal-concierge" \
  agent-browser@0.33.1
export MEAL_CONCIERGE_AGENT_BROWSER="$HOME/.local/lib/meal-concierge/node_modules/.bin/agent-browser"
```

### 2. Clone the repository

Keep the clone at this location: the installed service runs its code from here.

```sh
mkdir -p "$HOME/.local/share"
git clone https://github.com/poisdahl/meal-concierge.git \
  "$HOME/.local/share/meal-concierge"
cd "$HOME/.local/share/meal-concierge"
```

### 3. Install for your store

**For Oda**, authenticate with Hermes, disable its direct Oda tools so grocery
requests go through Meal Concierge, and install:

```sh
hermes mcp add oda-weekly --url https://oda.com/mcp --auth oauth
hermes mcp login oda-weekly
hermes config set mcp_servers.oda-weekly.enabled false
./install.sh --provider oda --household "My household"
```

Meal Concierge continues to use and refresh those private OAuth token files.

**For MENY**, run this in an interactive terminal. The installer privately
prompts for your eight-digit Vipps mobile number:

```sh
./install.sh --provider meny --household "My household"
```

Replace `My household` with your household's name. The installer checks the
required dependencies, creates private configuration and storage, registers the
Hermes skill and MCP connection, and installs the platform's background service.
For a new installation, complete login and start the service next.

### 4. Log in to your store

Run the **exact browser login command printed by the installer**. This opens
the dedicated profile Meal Concierge will use. Log in to your chosen store,
verify the delivery address, then **close that browser** so the service can use
its profile.

For Oda, use the same account as the OAuth login and make sure it already has a
payment method configured. For MENY, checkout uses home delivery and Vipps.

### 5. Start and verify

On **Linux**:

```sh
systemctl --user enable --now meal-concierge.service
systemctl --user status meal-concierge.service
hermes mcp test meal_concierge
```

On **macOS**, with the default service name and path:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.hermes-agent.meal-concierge.plist"
launchctl print "gui/$(id -u)/com.hermes-agent.meal-concierge"
hermes mcp test meal_concierge
```

If you customized paths or the LaunchAgent name, use the commands printed by
the installer. The MCP test should discover the `meal_concierge_*` tools.
Restart your Hermes CLI or gateway, then ask:

> “Show my meal-concierge status and household profile.”

Check that the household and store are correct and the service reports `ready`.
If it reports `awaiting_login` or `unavailable`, see
[troubleshooting](#troubleshooting).

## First use

Start with a planning request:

> “Plan next week's seven dinners.”

The first interactive planning or recipe-discovery request asks you to review
the household settings. Keep the defaults or change the portions, preferences
and recipe sources. Review the proposed menu and ask Hermes to save it.

Then try:

- “Find vegetarian dinners and save this recipe.”
- “Favorite this recipe and plan next week from my favorites.”
- “We already have rice. Show me what else we need.”
- “Show my cart and available delivery windows.”
- “Prepare checkout.”

Planning and preparing a checkout do not place an order. With the default
confirmation policy, review the final summary and confirm when you are ready.
For MENY, also approve the payment in Vipps when prompted.

## Configuration and optional features

The default private directory is `~/.hermes/meal-concierge` (or
`$HERMES_HOME/meal-concierge` with a custom Hermes home). It contains
`config.json`, household state, a SQLite recipe bank and the dedicated browser
profile. Oda OAuth tokens stay in Hermes's private token directory.

Use Hermes to change everyday meal preferences. See
[example-config.json](example-config.json) for configuration fields; the
installer creates the working config for you. Each installation's state is
bound to its store: use a separate installation to connect another provider.

| Optional setup | Guide |
|---|---|
| Mealie or RecipeSage recipe library | [Connect a recipe library](docs/reference.md#personal-recipe-library-connections) |
| Importing and managing local recipes | [Private recipe bank](docs/reference.md#private-recipe-bank) |
| Weekly schedules and recipe emails | [Workflow and email requirements](docs/reference.md#natural-language-workflow) |
| Custom paths, service management and uninstall | [Service lifecycle](docs/reference.md#provider-login-and-startup) |
| Planner behavior and product-price limits | [Technical reference](docs/reference.md) |

For non-standard installations, the installer accepts `HERMES_HOME`,
`HERMES_PYTHON`, `MEAL_CONCIERGE_HOME`, `MEAL_CONCIERGE_AGENT_BROWSER`,
`MEAL_CONCIERGE_BROWSER_EXECUTABLE` and `MEAL_CONCIERGE_NODE`. It uses Hermes's
managed Python with MCP/OAuth support; a system Python is not a replacement.

## Updating

Keep a private backup of the installation data before upgrading. In the stable
clone, pull the update and rerun the installer with your **existing store and
household name**; for example, for the Oda setup above:

```sh
cd "$HOME/.local/share/meal-concierge"
git pull --ff-only
./install.sh --provider oda --household "My household"
```

For MENY, use `--provider meny` instead. The installer preserves the existing
config, stops the Meal Concierge service, migrates state with private backups,
refreshes the skill and MCP registration, and restarts and verifies the service.
Restart Hermes too so it receives the updated tools.

If migration fails, the service stays stopped. Consult the
[upgrade details](docs/reference.md#upgrade-and-confirmation-details) before
restoring a backup or running older code against upgraded data.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `awaiting_login` | Sign in using the installer's dedicated browser-profile command, then close the visible browser before starting the service. |
| `unavailable` | Check the service logs and whether the configured store and browser dependencies are reachable. |
| Hermes cannot find the tools | Run `hermes mcp test meal_concierge` and restart Hermes. If you restrict `platform_toolsets`, include `meal_concierge` for that platform. |
| Hermes managed Python not found | Set `HERMES_PYTHON` to the Hermes virtual environment's `bin/python`. |
| Browser missing or snap rejected | Install non-snap Chrome/Chromium; set `MEAL_CONCIERGE_BROWSER_EXECUTABLE` if it is in a custom location. |
| MENY is waiting for payment | Check Vipps on your phone, then let Hermes reconcile the result. An uncertain result must not trigger another payment attempt. |

On Linux, inspect logs with `journalctl --user -u meal-concierge.service -n 100`.
On macOS, the default logs are in `~/Library/Logs/` as
`com.hermes-agent.meal-concierge.out.log` and
`com.hermes-agent.meal-concierge.err.log`.

## Privacy and limitations

Household data and provider sessions are stored locally, outside the source
repository. Requests still use your configured Hermes model and the relevant
store or recipe service. Keep credentials and private data out of Git and
public issue reports.

Product selection compares the candidates it can verify; it does not guarantee
the cheapest basket in the store. The checkout summary determines the final
price. Current integrations cannot establish authoritative allergen safety;
configured allergy or avoidance rules may require further input and block
product selection.

This project is not affiliated with Oda or MENY. Website changes can require
adapter updates. Email sending needs an existing Hermes email integration;
connecting an optional recipe library also requires your own account/server.

## Help and contributing

For bugs or feature requests, open an
[issue](https://github.com/poisdahl/meal-concierge/issues). Include your operating
system, Hermes version, store, repository commit (`git rev-parse --short HEAD`),
reproduction steps and a redacted error message.

Contributions are welcome through
[pull requests](https://github.com/poisdahl/meal-concierge/pulls). Read the
[technical reference](docs/reference.md) for implementation details. From the
repository root, run the test suite:

```sh
python3 -m unittest discover -s tests
```

Licensed under the [MIT License](LICENSE).
