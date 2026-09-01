# Hermes Meal Planner

Hermes Meal Planner adds weekly meal planning and grocery ordering to Hermes
Agent for a single household. It stores household preferences, weekly menus,
favorites, and recurring items locally, while using either Oda or MENY for
product search, cart management, delivery, and orders. By default, it plans
seven different dinners per week for two people. Adjust the household profile
through Hermes Agent or with `profile_overrides` in your private configuration.

## Provider support

| Capability | Oda | MENY |
|---|---:|---:|
| Product and recipe search | MCP | Logged-in browser |
| Read and change cart | MCP | Logged-in browser |
| Favorites, recurring items and menus | Local | Local |
| Delivery, orders and cancellation | Yes | Yes |
| Add goods / move an existing order | Yes | Yes |
| Protected checkout | Fresh or standing authorization, reconcile | Fresh or standing authorization, Vipps mobile approval, reconcile |

MENY does not document a public customer API or MCP service. Its adapter uses
the logged-in website's visible controls and exact `meny.no` product paths
rather than private web endpoints. The service requires a persistent MENY
login so store, lists, offers, cart and orders all belong to the intended
account. MENY checkout requires home delivery and Vipps: prepare verifies the
unchanged cart, final reserved amount, delivery window and selected Vipps
method; confirm starts one Vipps request; the user approves it on the phone;
and reconcile verifies the exact new or updated MENY order. Anonymous MENY mode
is not supported.

Before the payment click, each MENY line is bound to its exact product path.
MENY's completed-order view omits those paths, so reconciliation uses the
unique displayed product-and-package identity plus quantity and fails closed
if two different paths share that identity.

## One-agent installation

The supported path is a standard non-root Hermes Agent 0.20.5 or newer on a
systemd-based Linux host, plus non-snap Chromium and
[`agent-browser`](https://github.com/vercel-labs/agent-browser).
The meal planner deliberately uses Hermes's managed Python runtime; a system
`python3` normally does not contain Hermes's MCP and OAuth modules. There is no
database, web app, scheduler service or multi-agent controller.

Install a non-snap Chromium or Google Chrome with the Linux distribution's
package manager. Hermes Agent 0.20.5 installs its supported Node.js 26 runtime
under `HERMES_HOME`; use that exact npm to install the tested browser adapter
under your home rather than Debian's older Node.js:

```sh
sudo apt-get update
sudo apt-get install -y git chromium
mkdir -p "$HOME/.local/lib/hermes-meal-planner"
node_bin="${HERMES_HOME:-$HOME/.hermes}/node/bin/node"
npm_bin="${HERMES_HOME:-$HOME/.hermes}/node/bin/npm"
if [ ! -x "$node_bin" ] || [ ! -x "$npm_bin" ]; then
  node_bin="$(command -v node)"
  npm_bin="$(command -v npm)"
fi
"$node_bin" -e 'if (Number(process.versions.node.split(".")[0]) < 24) { console.error("Node.js 24+ is required"); process.exit(1) }'
PATH="$(dirname "$node_bin"):$PATH" "$npm_bin" install \
  --prefix "$HOME/.local/lib/hermes-meal-planner" \
  agent-browser@0.33.1
export MEAL_PLANNER_AGENT_BROWSER="$HOME/.local/lib/hermes-meal-planner/node_modules/.bin/agent-browser"
```

Ubuntu's transitional `chromium` package installs a strictly confined snap
that cannot use the private profile below `~/.hermes`; the installer rejects
it. Use a non-snap Chromium/Chrome package and, if needed, set
`MEAL_PLANNER_BROWSER_EXECUTABLE` to its exact executable.

Clone the repository to a stable path:

```sh
mkdir -p "$HOME/.local/share"
git clone https://github.com/poisdahl/hermes-meal-planner.git \
  "$HOME/.local/share/hermes-meal-planner"
cd "$HOME/.local/share/hermes-meal-planner"
```

For Oda, first let native Hermes OAuth create the normal private token files.
The final command keeps the raw Oda MCP tools out of conversations; the meal
planner continues to refresh and use the same private OAuth files.

```sh
hermes mcp add oda-weekly --url https://oda.com/mcp --auth oauth
hermes mcp login oda-weekly
hermes config set mcp_servers.oda-weekly.enabled false
./install.sh --provider oda --household "My household"
```

For MENY:

```sh
./install.sh --provider meny --household "My household"
```

The installer verifies Hermes's managed Python, `agent-browser`, non-snap
Chromium and a running user systemd manager; creates one private config,
provider-bound state, browser profile and Unix socket under
`$HERMES_HOME/meal-planner` (normally `~/.hermes/meal-planner`); installs the
single skill; registers the local MCP bridge with `hermes mcp add`; and installs
a user-level systemd service. It does not start a provider session or overwrite
an existing household/provider config. If the machine uses non-standard paths,
set `HERMES_PYTHON`, `MEAL_PLANNER_AGENT_BROWSER` or
`MEAL_PLANNER_BROWSER_EXECUTABLE` while running the installer; their resolved
values are saved in the private systemd unit.

New installations use `"confirmation_policy": "fresh"`: Hermes prepares the
exact checkout or cancellation summary and asks once before dispatch. An owner
who wants a standing authorization can set the private config to
`"confirmation_policy": "standing"` and restart the service. A clear current
request to order, pay, check out or cancel then proceeds without another Hermes
confirmation, including when the freshly prepared amount changes. Requests to
preview or prepare remain read-only. This setting does not bypass a provider,
Vipps, bank, device or platform approval, and an uncertain result is never
retried automatically.

Only the checkout tool's bound `submit` or `reconcile` result can establish a
successful order. A generic order list or order read after checkout returned an
error is not proof that the current attempt succeeded. A dispatched click that
was not acknowledged stays uncertain even after its summary expires; only an
acknowledged, unapproved Vipps request may expire into an explicitly retryable
state.
When a known pre-dispatch check stops checkout, the error states that no payment
was dispatched. Under standing authorization, the agent may perform exactly one
fresh submit for the same current request; this is distinct from retrying an
uncertain dispatched action.

## Provider login and startup

Before starting the service, run the exact provider-login command printed by
the installer. It uses the resolved Chromium/Chrome executable and the actual
private profile even when `HERMES_HOME` or `MEAL_PLANNER_HOME` is customized.
Open that profile in the visible browser and log in to the selected account. A remote headless host therefore
needs a private graphical session such as X11 forwarding or a private remote
desktop for this one-time step; do not copy cookies or credentials between
machines.

Close visible Chromium after login so the supervised browser can own that
profile. For Oda protected checkout, verify that this profile shows the
same account used for `oda-weekly`, the intended delivery address and an
already configured provider-side payment method. Every new-cart or add-to-order
protected Oda summary includes the browser-verified address and only the
payment method's last four digits so the user can catch a wrong profile before
confirming; the meal planner never stores full payment data. Start and verify
the service:

```sh
systemctl --user enable --now hermes-meal-planner.service
systemctl --user status hermes-meal-planner.service
hermes mcp test meal_planner
```

Restart the Hermes CLI or gateway after adding the MCP server. Current Hermes
registers the tools as `mcp__meal_planner__meal_planner_*` and makes them
available to ordinary natural-language turns. A vanilla config needs no
toolset edit. If a platform is explicitly restricted under
`platform_toolsets.<platform>`, add the raw server name `meal_planner` to that
platform's list.

The systemd unit restarts the service on failure. For MENY, `agent-browser`
owns the headless Chromium instance privately and launches it from the exact
persistent profile when needed; the service never attaches to a fixed or
pre-existing remote-debugging endpoint. The service reports `awaiting_login`
when the MENY session is absent or expired and
`unavailable` when a required dependency or provider is unreachable. On an
unattended server, enable the user's systemd
lingering according to the distribution's policy so user services start before
interactive login.

The socket is mode `0660`, assigned to the configured group, and placed under
the private meal-planner directory by the installer. Config, OAuth tokens,
state and browser profiles stay outside Git. Use `service.py --help` only for
an advanced manual layout with separate service and agent users.

State is bound to the provider selected at first initialization. Do not switch
an existing private state directory in place: pending checkout, cancellation,
order-change, schedule, favorite and recurring records are provider-specific.
Use a separate private installation and MCP registration for another provider.

## Natural-language workflow

After restart, use the normal Hermes CLI or messaging path. Useful smoke
requests, in a safe order, are:

- “Show my meal-planner status and household profile.”
- “Change dinner portions to four,” then “Reset dinner portions.”
- “Plan next week's seven dinners,” then answer its continuation question and
  save the final complete menu.
- “Save this product as a favorite” or “Add this every two weeks,” using an
  exact product returned by search.
- “Schedule a weekly Thursday draft,” or explicitly configure a guarded
  scheduled-checkout maximum and delivery preference. The due run follows the
  configured confirmation policy after its amount and delivery guards pass.
- “Search for broccoli,” “Find a salmon recipe,” “Show my cart,” and “Add one
  of that exact broccoli to this week's order.”
- “List delivery windows,” select one exact window, then list, inspect or track
  orders.
- “Add one of that product to order …” or move that order's delivery. The agent
  begins the exact order change before using the ordinary cart or delivery
  tool.
- “Prepare checkout” or “Prepare cancellation for order …” only prepares a
  summary. Under `fresh`, review it and confirm in the next message. Under
  `standing`, a direct “order/pay/check out/cancel” request submits without a
  second Hermes question. MENY then waits for one Vipps approval and
  reconciliation; an expired or uncertain result is never blindly retried.
- “Send a test recipe email for order …” or run the returned delivery-day
  action. Test email never consumes the due job, and due email is marked sent
  only after successful delivery.

Email delivery requires an existing Hermes email account/tool; a vanilla
Hermes install has none. Without one, the integration can safely prepare the
masked-recipient subject and HTML payload but cannot send it. Set
`email_automation_profile` only when that named private automation profile is
already configured for the intended sender.

Weekly and delivery-day wakeups are ordinary Hermes cron jobs created from the
exact action returned by the integration. Provider selection remains in the
private config below the skill; natural language never selects a household or
account.

## State and safety

The service stores one private JSON state file with atomic writes and a local
Unix socket. Reversible cart changes follow clear user requests. Checkout,
payment-bearing changes to one exact existing order, and cancellation always
bind the action to one freshly prepared summary. The `fresh` policy also
requires its exact confirmation ID; the `standing` policy lets the same clear
request authorize immediate submission. An uncertain final action is
reconciled and never retried automatically. Adding goods preserves
the existing order identity. Moving a delivery window is kept separate from an
Oda addition cart so the target cannot become ambiguous. A scheduled checkout
dispatches only after its configured total and delivery guards, and only under
standing authorization; MENY always requires the user's Vipps approval.

Hermes cron owns weekly wakeups and delivery-day email wakeups; this package
only stores settings and returns the next cron or email action. The email flow
is bound to one confirmed provider order and marks delivery only after a successful
send. The package does not store payment data or implement payment itself.

For multiple agents, run the same source once per agent with different config,
state, profile and socket paths. That is the entire multi-agent model; a normal
single-agent installation pays no coordination overhead.

## Tests

In the public repository:

```sh
python3 -m unittest discover -s tests
```

The public repository contains the same provider/core sources and focused
tests, without private operational configuration or private Git history.

This project is not affiliated with Oda or MENY. Website-backed behavior may
need a small adapter update when a provider changes its accessible page
structure.

## License

MIT
