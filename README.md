# Hermes Meal Planner

Hermes Meal Planner adds weekly meal planning and grocery ordering to Hermes
Agent for a single household. It stores a private searchable recipe bank,
household preferences, weekly menus, favorites, and recurring items locally,
while using either Oda or MENY for
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
is not supported. The private config's `vipps_phone_number` is entered only on
Vipps's own handoff page; it is never returned by status, written to state or
included in application logs.

Before the payment click, each MENY line is bound to its exact product path.
MENY's completed-order view omits those paths, so reconciliation uses the
unique displayed product-and-package identity plus quantity and fails closed
if two different paths share that identity.

## One-agent installation

The supported path is a standard non-root Hermes Agent 0.20.5 or newer on a
systemd-based Linux host or Apple Silicon macOS, plus Chromium or Google Chrome and
[`agent-browser`](https://github.com/vercel-labs/agent-browser).
The meal planner deliberately uses Hermes's managed Python runtime; a system
`python3` normally does not contain Hermes's MCP and OAuth modules. There is no
external database service, web app, scheduler service or multi-agent controller;
the private recipe bank is one local SQLite file.

On Linux, install a non-snap Chromium or Google Chrome with the distribution's
package manager. On macOS, install the normal Google Chrome or Chromium app in
`/Applications` or `~/Applications`; the installer discovers those app bundles.
Hermes uses a supported Node.js runtime; use its npm, or the supported Node on
`PATH`, to install the tested browser adapter under your home:

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

For MENY, run the installer from an interactive terminal; it prompts privately
for the eight-digit Vipps mobile number. For non-interactive installation, set
`MEAL_PLANNER_VIPPS_PHONE_NUMBER` only for the installer process.

```sh
./install.sh --provider meny --household "My household"
```

The installer verifies Hermes's managed Python, `agent-browser`, Chromium and
the platform's user service manager; creates one private config,
provider-bound state, browser profile and Unix socket under
`$HERMES_HOME/meal-planner` (normally `~/.hermes/meal-planner`); installs the
single skill; registers the local MCP bridge with `hermes mcp add`; and installs
a user-level systemd service on Linux or LaunchAgent on macOS. It does not start a provider session or overwrite
an existing household/provider config. If the machine uses non-standard paths,
set `HERMES_PYTHON`, `MEAL_PLANNER_AGENT_BROWSER` or
`MEAL_PLANNER_BROWSER_EXECUTABLE` while running the installer; their resolved
values are saved in the private service definition.

New installations use `"confirmation_policy": "fresh"`: Hermes prepares the
exact checkout or cancellation summary and asks once before dispatch. An owner
who wants a standing authorization can set the private config to
`"confirmation_policy": "standing"` and restart the service. A clear current
request to order, pay, check out or cancel then proceeds without another Hermes
confirmation, including when the freshly prepared amount changes. Requests to
preview or prepare remain read-only. This setting does not bypass a provider,
Vipps, bank, device or platform approval, and an uncertain result is never
retried automatically.
Each standing submit/cancel intent uses one explicit idempotency key. Reuse it
only to recover that same lost response; use a new key for a later user intent.

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

If MENY reports that the selected delivery reservation has expired, list the
same date and select the same window once to renew it before preparing checkout
again. Selecting an already chosen window uses MENY's explicit keep-delivery
control and verifies the renewed selection before payment.

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
confirming; the meal planner never stores full payment data.

On systemd Linux, start and verify the service:

```sh
systemctl --user enable --now hermes-meal-planner.service
systemctl --user status hermes-meal-planner.service
hermes mcp test meal_planner
```

On macOS, the installer prints the exact LaunchAgent path and label. With the
defaults, start and verify it with:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.hermes-agent.meal-planner.plist"
launchctl print "gui/$(id -u)/com.hermes-agent.meal-planner"
hermes mcp test meal_planner
```

Restart with `launchctl kickstart -k
"gui/$(id -u)/com.hermes-agent.meal-planner"`. Stop and unload it with
`launchctl bootout "gui/$(id -u)/com.hermes-agent.meal-planner"`; start it
again with the `bootstrap` command above. Standard output and errors go to
`~/Library/Logs/com.hermes-agent.meal-planner.out.log` and `.err.log`.

Restart the Hermes CLI or gateway after adding the MCP server. Current Hermes
registers the tools as `mcp__meal_planner__meal_planner_*` and makes them
available to ordinary natural-language turns. A vanilla config needs no
toolset edit. If a platform is explicitly restricted under
`platform_toolsets.<platform>`, add the raw server name `meal_planner` to that
platform's list.

The systemd unit or LaunchAgent restarts the service on failure. For MENY, `agent-browser`
owns the headless Chromium instance privately and launches it from the exact
persistent profile when needed; the service never attaches to a fixed or
pre-existing remote-debugging endpoint. The service reports `awaiting_login`
when the MENY session is absent or expired and
`unavailable` when a required dependency or provider is unreachable. On an
unattended server, enable the user's systemd
lingering according to the distribution's policy so user services start before
interactive login.

For rollback, stop the service, keep a private copy of the config/state/profile,
check out the previously working public commit in the stable clone, rerun the
installer, and start the service again. For uninstall, stop and disable/unload
the service, run `hermes mcp remove meal_planner`, and move its service
definition aside before removing the installed skill, stable clone and private
meal-planner directory. The private directory contains provider state and must
not be deleted before its backup is verified.

Status reports pending checkout, cancellation and order-change status
explicitly without exposing their private payloads, so an uncertain protected
operation cannot be mistaken for an idle household.

The socket is mode `0660`, assigned to the configured group, and placed under
the private meal-planner directory by the installer. Config, OAuth tokens,
state and browser profiles stay outside Git. Use `service.py --help` only for
an advanced manual layout with separate service and agent users.

State is bound to the provider selected at first initialization. Do not switch
an existing private state directory in place: pending checkout, cancellation,
order-change, schedule, favorite and recurring records are provider-specific.
Use a separate private installation and MCP registration for another provider.

## Private recipe bank

The recipe bank is household-bound SQLite at
`$HERMES_HOME/meal-planner/state/recipes.sqlite3`. It is opened only by recipe
operations, so a missing or damaged bank cannot block provider status, cart,
delivery or order reconciliation. Recipe saves and imports require explicit
source and rights metadata. Full recipes have structured ingredients and
positive portions; quantities marked `scalable` are scaled deterministically
and produce provider-neutral shopping requirements. Product matching still
happens later through the configured provider, and provider product IDs are
never stored in a recipe.

The bank distinguishes exact source identities from looser content matches.
An exact source identity is idempotent, while a similar name and ingredient set
is retained as a separate recipe with a duplicate warning. Updates use the
returned recipe revision. Archive is reversible at the data level because all
recipe revisions remain in SQLite; ordinary search hides archived recipes.

Source URLs must be credential-free HTTPS. Query strings and fragments are
discarded before persistence. Original Oda or MENY recipe text is stored only
as a `link_only` record; a full stored version must be explicitly identified as
`adapted` or `inspired_by`.

A native full-recipe record looks like this:

```json
{
  "name": "Vegetargryte",
  "portions": 4,
  "ingredients": [
    {"quantity": 400, "unit": "g", "item": "kikerter", "scalable": true},
    {"raw": "salt etter smak", "item": "salt", "scalable": false, "pantry": true}
  ],
  "steps": ["Kok gryten.", "Smak til."],
  "tags": ["middag", "vegetar"],
  "source": {
    "kind": "user",
    "publisher": "Familien",
    "external_id": "vegetargryte-1",
    "relationship": "user_supplied"
  },
  "rights": {"storage": "full", "credit": "Familieoppskrift"}
}
```

Import one JSON object, a JSON array, or JSONL. The command validates the whole
batch inside one transaction; a bad record rolls everything back. Always run a
dry run first. The first committed import creates the bank without a backup:

```sh
python3 import_recipes.py recipes.jsonl \
  --state-directory "${HERMES_HOME:-$HOME/.hermes}/meal-planner/state" \
  --dry-run
python3 import_recipes.py recipes.jsonl \
  --state-directory "${HERMES_HOME:-$HOME/.hermes}/meal-planner/state"
```

On subsequent imports into an existing bank, add
`--backup "${HERMES_HOME:-$HOME/.hermes}/meal-planner/state/recipes-before-import.sqlite3"`.

The import is bounded to 64 MiB, 10,000 records and the normal per-recipe
limits. Reimporting identical native records is idempotent. Keep the private
state and SQLite database together in backups; neither belongs in Git.

Menus carry a server-owned `menu_id`, revision and content digest. Bank recipes
are materialized from an exact recipe ID/revision and optional portion count.
Updating or clearing a menu requires its current returned `menu_id` and
`expected_revision`, so a stale request cannot replace newer work.
The default six-week repeat cooldown includes planned, ordered and explicitly
marked-cooked use. A deliberate repeat needs the exact returned recipe key and
a reason. Cancellation does not pretend a meal was cooked, and `not cooked`
must be recorded explicitly against the matching menu. Confirmed orders and
their recipe-email jobs keep immutable menu, recipient, subject and HTML
snapshots even if the current menu or recipient later changes.

## Natural-language workflow

After restart, use the normal Hermes CLI or messaging path. Useful smoke
requests, in a safe order, are:

- “Show my meal-planner status and household profile.”
- “Change dinner portions to four,” then “Reset dinner portions.”
- “Plan next week's seven dinners,” then answer its continuation question and
  save the final complete menu.
- “Search my recipe bank for vegetarian dinners,” “save this family recipe,”
  “scale that recipe to six portions,” or “archive recipe …”. Search excludes
  recipes still inside the cooldown unless the user asks to see repeats.
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
  action. Test email never consumes the due job. A due email gets a short
  pre-dispatch claim; `begin_send` must accept that token immediately before
  sender invocation, and `mark_sent` uses it only after successful delivery.
  Release is safe only after a definite no-send failure. A moved
  delivery returns the replacement one-shot action.

New, moved and upgraded delivery-day jobs expose
`automation_update_required`. Apply the exact `cron_prompt` to that one Hermes
automation, then call the returned `automation_ack`; acknowledgement records
protocol 3 only after the external update succeeds. After an upgrade, call
email `automation_plan`, replace every listed legacy prompt, and acknowledge
each result. Until then the old prompt safely declines to send rather than
using an unbound payload.

Each job snapshots its provider as well as its menu and recipient. A MENY
instance can therefore finish an older Oda delivery email when its normal Oda
token directory is still available, without changing the household's active
provider or sharing provider state.

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

The service stores atomic household/menu state in one private JSON file and
recipe documents and revisions in one household-bound SQLite file, plus a
local Unix socket. Reversible cart changes follow clear user requests. Checkout,
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
