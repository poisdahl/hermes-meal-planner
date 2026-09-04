# Meal Concierge

Meal Concierge adds weekly meal planning and grocery ordering to Hermes
Agent for a single household. It stores a private searchable recipe bank,
household preferences, weekly menus, product favorites, and recurring items locally,
discovers recipes through that bank, Oda, MENY, TheMealDB and Wikibooks
Cookbook, and uses either Oda or MENY for product search, cart management,
delivery, and orders. By default, it plans
seven different dinners per week for two people. Adjust the household profile
through Hermes Agent or with `profile_overrides` in your private configuration.

## Provider support

| Capability | Oda | MENY |
|---|---:|---:|
| Product and recipe search | MCP | Logged-in browser |
| Read, change and active-menu sync cart | MCP | Logged-in browser |
| Product favorites, recurring items and menus | Local | Local |
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
The meal concierge deliberately uses Hermes's managed Python runtime; a system
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
mkdir -p "$HOME/.local/lib/meal-concierge"
node_bin="${HERMES_HOME:-$HOME/.hermes}/node/bin/node"
npm_bin="${HERMES_HOME:-$HOME/.hermes}/node/bin/npm"
if [ ! -x "$node_bin" ] || [ ! -x "$npm_bin" ]; then
  node_bin="$(command -v node)"
  npm_bin="$(command -v npm)"
fi
"$node_bin" -e 'if (Number(process.versions.node.split(".")[0]) < 24) { console.error("Node.js 24+ is required"); process.exit(1) }'
PATH="$(dirname "$node_bin"):$PATH" "$npm_bin" install \
  --prefix "$HOME/.local/lib/meal-concierge" \
  agent-browser@0.33.1
export MEAL_CONCIERGE_AGENT_BROWSER="$HOME/.local/lib/meal-concierge/node_modules/.bin/agent-browser"
```

Ubuntu's transitional `chromium` package installs a strictly confined snap
that cannot use the private profile below `~/.hermes`; the installer rejects
it. Use a non-snap Chromium/Chrome package and, if needed, set
`MEAL_CONCIERGE_BROWSER_EXECUTABLE` to its exact executable.

Clone the repository to a stable path:

```sh
mkdir -p "$HOME/.local/share"
git clone https://github.com/poisdahl/meal-concierge.git \
  "$HOME/.local/share/meal-concierge"
cd "$HOME/.local/share/meal-concierge"
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
`MEAL_CONCIERGE_VIPPS_PHONE_NUMBER` only for the installer process.

```sh
./install.sh --provider meny --household "My household"
```

The installer verifies Hermes's managed Python, `agent-browser`, Chromium and
the platform's user service manager; creates one private config,
provider-bound state, browser profile and Unix socket under
`$HERMES_HOME/meal-concierge` (normally `~/.hermes/meal-concierge`); installs the
single skill; registers the local MCP bridge with `hermes mcp add`; and installs
a user-level systemd service on Linux or LaunchAgent on macOS. It does not start a provider session or overwrite
an existing household/provider config. If the machine uses non-standard paths,
set `HERMES_PYTHON`, `MEAL_CONCIERGE_AGENT_BROWSER` or
`MEAL_CONCIERGE_BROWSER_EXECUTABLE` while running the installer; their resolved
values are saved in the private service definition.

Clean installations create household state v11 with only the
`product_favorites` list and expose the
`meal_concierge_product_favorites` tool. When rerun for an existing installation,
the installer stops only the meal-concierge service, creates the non-overwriting
private migration backups, including `state-v6.backup.json` immediately before
the v6→v7 delivery-preference migration and `state-v7.backup.json` before
renaming saved email automation identities, migrates state atomically, refreshes
the installed skill and MCP registration, restarts the service, and verifies
both status and the new tool schema. This also starts an existing installation
that was stopped before the update. If migration fails, the old state and its
backup remain usable and the service stays stopped. Existing v6 households gain
`delivery.strategy="keep_selected"`; clean state and newly replaced delivery
preferences default to `"cheapest"`. Restore the matching backup before running
older code; older code must not read a v8 state file.

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

## First-run configuration

The first interactive menu or recipe-discovery request returns one setup
question with the current household, provider, people, portions, diet,
confirmation policy, weekly-menu fields and five recipe-source switches. Keep
all values once, or send only the values to change. The operation is
idempotent, and `setup rerun` makes the same review available later. It never
asks for or returns provider credentials, API keys, Vipps details or recipient
addresses. A non-interactive weekly run proceeds with the saved/default values
and leaves an explicit `needs_review` status instead of blocking automation.

All five recipe sources are enabled by default. Disable a source with the setup
tool, the profile tool, or a private `profile_overrides.recipes.sources` value.
Provider selection and confirmation policy remain config-bound and require a
separate state/service change rather than an in-place setup edit.

## Product observations and bounded selection

Product search normalizes Oda and MENY results into one bounded observation
shape while preserving the provider's exact `product_id`/`product_ref`. Each
product carries availability, an observation timestamp, display-only provider
text and, only when established exactly, package mass/volume/count and purchase
options. Merchandise price, a `fra` lower bound, mandatory product-level pant
and total payable are separate integer-øre fields. An exact displayed price or
unit price is therefore not necessarily an exact payable total: missing pant,
variable weight, unknown member eligibility and unsupported offer syntax all
stay unresolved. Comparable merchandise unit prices use exact fractions within
mass, volume or count; the display value is rounded to two decimal øre per
canonical unit using round-half-even. The current Oda `product_search` fixture
exposes exact merchandise price and availability but no product-level deposit
or offer-term field, so its `total_payable_ore` and automatic offer use remain
unavailable. MENY search cards likewise do not prove an exact pant amount or
that pant is absent. For each bounded, available exact-price result the MENY
adapter therefore verifies the same current price against the linked product's
single primary price block. Exactly no pant marker establishes zero product
pant; a `+ pant` marker without its amount remains unknown. This lets ordinary
no-pant products expose an exact payable total without treating card absence as
evidence. The captured public `Tilbud` current/original-price pair and strict
`N for M` tag establish the only automatic MENY discount forms. `Fra N,NN kr`
is always a lower bound (including `fra 0`), never payable; no such product card
was present in the bounded verification capture. Other lower-bound, promotional
and package grammars remain display-only. Because the captured campaigns do not
state repetition limits, one discounted unit or one exact multi-buy threshold is
the largest decision-bearing quantity; larger quantities stay unresolved. No
observation is stored as durable price truth.

`meal_concierge_products prepare` binds one read-only proposal to the exact
active saved-menu identity or one complete deterministic planner handoff and to
the configured provider. It aggregates only identical, scalable ingredients
with exactly convertible units; raw or non-scalable quantities stay unresolved.
For a menu saved before shopping requirements carried `scalable`, the flag is
recovered only when the requirement still exactly matches the same-index frozen
ingredient's identity, scaled quantity, unit and option/pantry flags.
The first call exposes at most five relevance-ranked search
results per requirement and returns `needs_input`; search order, names and
promotional text never establish substitution safety. A subsequent current user
choice must name the exact approved candidate refs for each exact requirement.
Only confirmed availability, package evidence, offer eligibility and complete
product-level payable amounts enter the bounded combination search.
Configured allergy/sensitivity and avoid rules are also hard at the product
boundary. The current provider observations contain no authoritative product
safety evidence, so a non-empty rule keeps preparation at `needs_input`; an
exact candidate-ref approval cannot override it.

The resulting claim is only the lowest verified total payable amount among the
explicitly approved, exactly priced candidates observed in those bounded
provider searches—never the cheapest item in the store. It excludes delivery,
bags, cart-level fees and checkout drift. Selection ranks exact payable amount,
then the rational per-requirement excess score, package count and stable product
refs. The complete canonical result and `product_plan_digest` are carried by the
caller; display fields and timestamps do not grant freshness or apply authority.

Apply needs that complete unchanged result and digest plus a clear current user
request to change the cart. It repeats the same provider searches and ranking,
stops without a cart write on menu, candidate, package, availability, offer or
price drift, and otherwise hands the exact whole-package quantities to the
existing guarded, restart-safe cart sync. A verified exact product-line amount
is compared with the prepared exact total; a post-write difference is reported
without rollback. MENY's current cart DOM does not establish a semantic line
total, so its post-write price verification is explicitly unavailable rather
than inferred from presentation markup. Product selection never authorizes
delivery, checkout, ordering or payment, and the provider checkout summary
remains the final price authority.
MENY applies larger selections as sequential, acknowledged batches of at most
two UI quantity changes. Immediately before each batch it rereads the cart;
an unexpected intermediate cart stops the remaining batches and returns the
normal reconciliation boundary. Reconciliation uses the same bounded batches.

## Delivery prices and selection

Delivery list returns the same seven-field slot object for each provider:
`slot_ref`, unchanged provider ID or `null`, complete offset-aware start/end
timestamps, integer `price_ore` or `null`, `price_kind` (`exact`, `from` or
`unavailable`) and the provider-selected flag. Price presentation is derived
only from that state as `49 kr`, `fra 49 kr` or `pris ikke tilgjengelig`.
`fra 0` is never reported as free. A parser is enabled only for sanitized
provider-fixture shapes; unknown shapes stop instead of guessing price or units.
The current Oda fixture establishes integer IDs, RFC 3339 `openDatetime` /
`closeDatetime`, and exact `kr` + NBSP + integer-kroner prices, including
confirmed `kr 0`; other Oda price syntax remains unavailable. Full or
provider-unavailable Oda rows are not offered as candidates. The current MENY
fixture establishes only its duplicated `fra N kr fra N kroner` label form.
MENY list results retain that bounded original ARIA label in the outer
`display[slot_ref]` metadata map while excluding it from the seven-field slot
identity, so price wording can change without changing `slot_ref`.

Select using the exact returned `slot_ref`. A provider ID remains unchanged;
MENY currently has no verified stable slot ID, so its reference is derived only
from date/start/end and a live click requires exactly one semantic match. This
keeps the identity stable when price wording changes. Local selection provenance
is only an observation and is checked against a fresh provider read.

`schedule.delivery.strategy` is `keep_selected` or `cheapest`. Hard weekday/date
and latest-end limits are applied before price. Cheapest requires every eligible
candidate to have an exact price, then ranks by price, end nearest
`preferred_end`, earlier start and bytewise `slot_ref`. Mixed exact/from/missing
prices stop in `cart_ready`/`needs_input`, and explicit or externally selected
windows are never replaced. Protected checkout rereads the provider selection
and price. A strategy-owned window may be reselected/reprepared once after
drift; a second drift stops before payment. MENY still ends unattended runs in
`cart_ready` and keeps its manual/Vipps boundary.

The scheduler invokes checkout `auto` for both `cart_ready` and
`auto_checkout`. A `cart_ready` run may reserve the verified cheapest slot but
never enters checkout or submits payment. Its returned occurrence is part of
the delivery provenance and must be passed unchanged to a later manual checkout
`prepare`; an unrelated or omitted occurrence is rejected while that scheduled
selection is active.

Checkout amount maps contain only independently supplied values. Oda's current
MCP cart establishes the provider total; the read-only expanded checkout
summary independently supplies its aggregate item-count/product-total,
discount, discounted `Delsum`, delivery, delivery-packaging, named other-fee
and total rows, while the selected-slot price is also verified against the
fresh slot listing. The aggregate label is bound to the exact cart or order
product count. The sanitized fixture retains the exact observed labels and
formatted amount strings used by that parser.
MENY's cart and checkout
surfaces establish their own totals. Neither provider reconstructs a missing
subtotal, delivery, discount, deposit, bag or other fee by subtraction.

## Manual cart goods during a weekly menu

An active weekly menu uses one small provider- and menu-revision-bound cart
plan. Cart `sync` receives the complete required quantity `R` for each exact
provider product ID. On its first run it snapshots the live starting quantity
`B`; different packages, brands and substitutions remain different products.
By default an existing same-SKU quantity counts toward the requirement, so the
target is `max(B,R)` and the service adds only the verified shortfall. If the
owner explicitly says a starting product is extra, its target is `B+R`.

The plan stores only `B`, `R`, the verified quantity Meal Concierge added, the
last verified live product quantities/digest and an optional owner-approved
digest. It survives restart, and repeated sync is idempotent, including after
an explicit exclusion or accepted shortfall on an unchanged digest. Immediately
before every batch write, the provider cart is reread; any concurrent manual
change stops the write for reconciliation. A successful write is read back and
must exactly match the safe merge. Price, bags, deposits, fees and delivery are
not classified as manual product provenance; the normal fresh checkout review
continues to bind those values.

Checkout rereads the live product quantities, including immediately before the
final provider click. Extras, missing menu quantities
and unresolved starting goods are presented in one combined question bound to
that exact cart digest. “Keep the current cart” is only a suggested default;
silence and timeout are not approval. The owner may name exact exclusions,
restore missing menu products, or explicitly accept named missing quantities.
An exclusion cannot reduce below `R` unless that shortfall is accepted in the
same decision. The cart is reread before applying the decision and again before
checkout. Any later product or quantity change invalidates the approved digest.
Scheduled checkout stops in `cart_ready`/`needs_input` while anything is
unresolved and never treats the suggested default as automatic consent.

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
private profile even when `HERMES_HOME` or `MEAL_CONCIERGE_HOME` is customized.
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
confirming; the meal concierge never stores full payment data.

On systemd Linux, start and verify the service:

```sh
systemctl --user enable --now meal-concierge.service
systemctl --user status meal-concierge.service
hermes mcp test meal_concierge
```

On macOS, the installer prints the exact LaunchAgent path and label. With the
defaults, start and verify it with:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.hermes-agent.meal-concierge.plist"
launchctl print "gui/$(id -u)/com.hermes-agent.meal-concierge"
hermes mcp test meal_concierge
```

Restart with `launchctl kickstart -k
"gui/$(id -u)/com.hermes-agent.meal-concierge"`. Stop and unload it with
`launchctl bootout "gui/$(id -u)/com.hermes-agent.meal-concierge"`; start it
again with the `bootstrap` command above. Standard output and errors go to
`~/Library/Logs/com.hermes-agent.meal-concierge.out.log` and `.err.log`.

Restart the Hermes CLI or gateway after adding the MCP server. Current Hermes
registers the tools as `mcp__meal_concierge__meal_concierge_*` and makes them
available to ordinary natural-language turns. A vanilla config needs no
toolset edit. If a platform is explicitly restricted under
`platform_toolsets.<platform>`, add the raw server name `meal_concierge` to that
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
the service, run `hermes mcp remove meal_concierge`, and move its service
definition aside before removing the installed skill, stable clone and private
meal-concierge directory. The private directory contains provider state and must
not be deleted before its backup is verified.

Status reports pending checkout, cancellation and order-change status
explicitly without exposing their private payloads, so an uncertain protected
operation cannot be mistaken for an idle household.

The socket is mode `0660`, assigned to the configured group, and placed under
the private meal-concierge directory by the installer. Config, OAuth tokens,
state and browser profiles stay outside Git. Use `service.py --help` only for
an advanced manual layout with separate service and agent users.

State is bound to the provider selected at first initialization. Do not switch
an existing private state directory in place: pending checkout, cancellation,
order-change, schedule, product-favorite and recurring records are provider-specific.
Use a separate private installation and MCP registration for another provider.

## Personal recipe-library connections

The built-in bank always exists as exact `library_id="builtin"` and is the
zero-configuration primary. The Mealie and RecipeSage adapters are included.
Merely adding either connection never selects it or blocks the built-in path.
Connections live only in the private config and use stable IDs:

```json
{
  "primary_recipe_library_id": "builtin",
  "recipe_libraries": [
    {"library_id": "builtin", "provider": "builtin", "read_only": false},
    {"library_id": "family-mealie", "provider": "mealie", "base_url": "https://recipes.example", "read_only": false}
  ]
}
```

An ID matches `[a-z][a-z0-9-]{0,62}`, is unique case-insensitively and never
changes with its URL, display name, credential or primary status. Search/save
without a library ID uses the primary; a per-call exact `library_id` overrides
only that call. Provider names are not selectors: if “save in Mealie” matches
zero or several connections, ask for one exact connection instead of choosing
by order. Cross-library search requires an explicit list of exact IDs. Its
continuation cursor is a map keyed by those IDs, so one provider's cursor is
never sent to another connection. Treat every returned cursor as opaque and
return it unchanged; the service binds it to the exact library and preserves
provider page size plus any partially consumed page while favorite and cooldown
filters fill a result page.

`base_url` is one credential-free origin. HTTPS is the default; loopback HTTP
is allowed for local hosting. Any other HTTP origin requires the setup helper's
explicit local warning confirmation and is never configurable through MCP.
Authenticated adapter calls compare scheme, host and port before attaching a
credential and never follow redirects.

Credentials are separate from config at
`$MEAL_CONCIERGE_HOME/secrets/recipe-libraries/<library_id>.json`, with directory
mode `0700` and file mode `0600`, owned by the service user. They never belong
in command arguments, state, SQLite, logs, MCP traffic, fixtures or Git. After
the provider-specific adapter is installed, use the interactive local helper:

```sh
home="${MEAL_CONCIERGE_HOME:-${HERMES_HOME:-$HOME/.hermes}/meal-concierge}"
python3 recipe_library_setup.py --config "$home/config.json" --home "$home" \
  add --library-id family-mealie --provider mealie --base-url https://recipes.example
python3 recipe_library_setup.py --config "$home/config.json" --home "$home" \
  test --library-id family-mealie
python3 recipe_library_setup.py --config "$home/config.json" --home "$home" \
  set-primary --library-id family-mealie
```

For Mealie, enter exactly `{"token":"<long-lived Mealie API token>"}` at the
hidden credential prompt. The adapter supports Mealie `3.24.0` and newer when
the probed response schemas remain compatible. Its read-only connection test
checks `/api/app/about`, authenticated `/api/users/self`, one bounded recipe
list page, the favorite-read response shape and one bounded organizer-tag page.
It reports `search`, `get`, `create_from_discovery`, `reconcile_create`,
`delete`, `reconcile_delete`,
`favorite_read`, `favorite_write_desired_state`, `favorite_reconcile`,
`label_read` and, when the authenticated user may organize and the connection
is writable, `label_create`. Native favorite writes
use Mealie's exact recipe UUID add/remove routes, then read the exact native
state back. Mealie exposes no ETag or favorite revision for these routes, so
`favorite_conditional_write` is false and `expected_favorite_revision` is
rejected before dispatch. A configured `read_only` connection retains
search/get, native favorite reads and delete reconciliation but reports no
create, delete or favorite-write capability and cannot be selected as a
writable primary.

Mealie search is paginated and returns the immutable provider UUID in the
namespaced `library_recipe_ref`; the current slug is display-only
`provider_slug` metadata. Exact get always addresses the UUID, and `updatedAt`
is the version token when Mealie supplies it. Saving uses only the already
frozen discovery document. The create flow installs native Mealie fields plus a
deterministic attribution block and private `hermes_origin`/`hermes_recipe`
extras. Frozen discovery tags stay in `hermes_recipe`; saving a recipe never
creates or changes Mealie organizer tags. Organizer tags are exposed separately
through exact label operations described below. `link_only` sends only the title, original HTTPS source link,
attribution/rights metadata and operation metadata—never ingredients, steps or
notes. A definite rejection before Mealie creates anything is failed. Any lost
response or failure after possible creation is uncertain, is never retried or
redirected to `builtin`, and can become confirmed only when a unique marker
search, origin record, snapshot digest, source identity and normalized content
all agree. A copied marker or partial stub is not enough.

The sanitized fixture in `tests/fixtures/mealie/v3.24.0.json` records the
stable v3.24.0 shapes checked against the official current Mealie OpenAPI
surface without retaining a token, user/household identity, private recipe or
internal hostname. Optional live coverage runs only when an operator explicitly
supplies a test connection; it creates one uniquely marked recipe and removes
only the exact confirmed or reconciled provider UUID.

Mealie search and get include the authenticated account's current native
`is_favorite`; `favorites_only=true` is accepted because the connection reports
`favorite_read`. Meal Concierge serializes its own desired-state writes for a
connection, reads before dispatch to avoid a redundant add/remove, and reads
back afterward. This prevents conflicting Meal Concierge calls from racing each
other, but it does not detect an out-of-band change without a provider
conditional token. A lost response is never followed by another write: an
exact authenticated read may confirm the desired state because Mealie reports
`favorite_reconcile`; otherwise the operation remains `uncertain`.

For RecipeSage, configure the API origin: `https://api.recipesage.com` for the
hosted service or, for the official self-host proxy, the site origin such as
`https://recipes.example` without its `/api` suffix. The adapter verifies the
OpenAPI document and automatically selects the official direct or `/api`
prefix. The hosted API is documented for personal, non-commercial use only;
this integration does not imply broader authorization. Self-hosted use follows
the configured instance and the RecipeSage license.

At the hidden credential prompt, enter exactly
`{"token":"<RecipeSage bearer session token>"}`. Obtain or renew that token
through an explicit local RecipeSage sign-in. Do not provide the RecipeSage
email/password, Google token or Google authorization code to Meal Concierge, MCP,
the command line or logs; the helper persists only the returned session token
in the mode-`0600` credential file. A revoked or expired session reports
`needs_auth` and is never silently replaced. Install a renewed token with the
same hidden `update-credential` action.

The verified contract is RecipeSage `v4.0.6` on the hosted service and the
official self-host bundle's `v4.0.3` application/config level `2026-08-16` or
newer. The self-host build reports `selfhost` rather than its application
version, so support additionally requires the same exact selected request and
response schema fingerprint, not merely matching operation names, plus
successful semantic read probes. Connection testing reads the OpenAPI
version, validates the bearer session, reads the authenticated-user shape,
lists at most one private recipe and validates the authenticated label-list
shape. It reports `search`, `get`, `create_from_discovery`, `reconcile_create`,
`label_read` and, for a writable connection, `label_create`; a configured
`read_only` connection retains search/get, label reads and delete
reconciliation, suppresses create and delete writes, and cannot be the
writable primary. RecipeSage rating is
not treated as a favorite. Native
favorite read, desired-state mutation, conditional favorite write and favorite
reconciliation are all false for the verified v4.0.6 hosted contract. Rating,
labels, folders and local shadow metadata are never used to emulate that
missing boolean, so `set_favorite` fails before any RecipeSage dispatch.
Conditional update and archive are likewise not reported or emulated. Writable
connections report exact-ID `delete`; all connections report its authenticated
exact-read `reconcile_delete` capability.

Private list/search uses the current account only. The authenticated `getMe`
UUID is cached for the connection, and every list, search, exact-get, create
readback and reconciliation result must carry that same owner UUID. Results
return immutable recipe UUIDs in `library_recipe_ref`; `updatedAt` is included as a version only when
RecipeSage supplies it. List paging uses the server offset, while full-text
search pages the provider's bounded result set locally. Exact get always sends
the UUID. Saving sends one already frozen discovery document directly to
`createRecipe`; it never refetches the source or calls RecipeSage clipping or
Discover. Native recipe fields carry the permitted content and deterministic
attribution. A versioned block at the start of the private recipe notes carries
the unguessable operation ID, exact `library_id`, snapshot digest, normalized
source identity and frozen document needed for semantic readback. Frozen
discovery tags and fields without a native RecipeSage representation stay in
that block; saving a recipe never mutates the account's label organizer.

For `link_only`, the request contains only the permitted title, original link,
attribution/rights statement and the required operation metadata—never recipe
ingredients, steps, notes, classification tags or source snapshot text. A
definite pre-dispatch rejection is failed. A timeout, lost response or any
failure after possible create is target-bound `uncertain` and is never retried
or redirected to `builtin`. Reconciliation requires exactly one private
full-text marker result whose operation ID, library, digest, source identity
and normalized native content all match; a copied marker, title/URL match,
partial metadata or duplicate remains uncertain. Generic v3 journal/mapping
rules provide same-ref/same-target idempotency and independent explicit
targets. Menu save performs one exact get and freezes it, so later RecipeSage
edits, deletion, expiry or outage cannot change the saved menu.

A definite `needs_auth` rejection from `createRecipe` releases only the local
dispatch claim and leaves the same target-bound operation pending, so an
explicit token renewal can resume the same frozen save without rediscovery. If
authentication expires while an already-uncertain operation is being
reconciled, the operation stays uncertain but reports `needs_auth`; renewal
allows reconciliation to continue and never repeats the create blindly.

### External recipe lifecycle

Lifecycle capabilities are connection-specific. The verified Mealie 3.24.0
and RecipeSage 4.0.6 contracts have exact private-recipe deletion and safe
delete reconciliation, but no provider-enforced compare-and-swap update and no
native reversible archive state. They therefore report `conditional_update`
and `archive_desired_state` false instead of approximating either feature with
a read-before-write, label, rating, folder or local shadow. A read-only
connection also reports `delete` false and sends no mutation.

An external conditional update is accepted only from an adapter that reports
`conditional_update`. It takes one exact versioned `library_recipe_ref`, the
complete normalized replacement and one stable idempotency key. The provider
must enforce the supplied version during its write. A stale version conflicts
without dispatch, and source, rights and attribution must remain unchanged.

External archive and permanent deletion always use two calls, independently of
the checkout confirmation policy. `archive_prepare` or `delete_prepare` reads
the exact provider ID and returns a confirmation bound to its library, ID,
provider origin, authenticated account and authorization scope, version,
normalized name, content digest and requested state. A separate
`archive_confirm` or `delete_confirm` must present that unchanged confirmation
ID and a stable idempotency key within ten minutes. Confirm rereads the exact
target; identity, version, content or archive-state drift fails before any
mutation. Delete preview marks the action permanent and states that immutable
active-menu, pending-checkout, confirmed-order and recipe-email snapshots are
retained.

A possibly dispatched update, archive or deletion remains `uncertain` and
blocks another mutation of that target. Retry only the same confirmation and
same idempotency key so the service performs exact reconciliation; it never
blindly repeats delete/archive. A deletion becomes confirmed only after a fresh
authentication check followed by authoritative exact-ID absence. Auth,
permission, rate-limit, malformed and ambiguous 404 responses cannot prove
absence, and a changed provider origin, authenticated account or Mealie
group/household scope cannot inherit or reconcile the old operation. Missing
recipes are not recreated, retargeted or copied into the
built-in bank. A later provider recipe with the same title or source URL but a
new ID is a distinct identity and inherits no mapping, usage or favorite state.

The sanitized fixture in `tests/fixtures/recipesage/v4.0.6.json` records exact
selected request/response schemas captured from the hosted OpenAPI and the
official self-host release provenance, with synthetic response values only. It
contains no session, real email/account ID, private recipe or internal hostname.
Optional live coverage runs only when an operator explicitly
supplies a test connection; it creates one uniquely marked temporary recipe and
removes only the exact confirmed or reconciled UUID. An uncertain cleanup is
reconciled and never repeated blindly.

The hidden prompt reads credential JSON. Add/update probes authentication and
semantic read capabilities before writing; primary changes and credential
removal require exact local confirmations. `update-credential` and `remove`
provide the other two lifecycle actions. Remove refuses a connection while its
journal has pending or uncertain work; resolve that work first. A running service is restarted only
after a successful local change. First-run conversational setup never requests
library credentials; it continues with `builtin` and only mentions this helper.
Setup mutations hold one installation-local lock across confirmation and writes,
so concurrent helpers cannot overwrite one another. Removal atomically disables
the connection in the operation journal after confirmation and succeeds only
when no pending or uncertain save exists; this closes the dispatch/removal race.
The service resolves credentials beside a conventional `HOME/state` directory
or inside a state-root container mount, without exposing either path through MCP.

## Private recipe bank

The recipe bank is household-bound SQLite at
`$HERMES_HOME/meal-concierge/state/recipes.sqlite3`. It is opened only by recipe
operations, so a missing or damaged bank cannot block provider status, cart,
delivery or order reconciliation. Recipe saves and imports require explicit
source and rights metadata. Full recipes have structured ingredients and
positive portions when the source states a serving count; quantities marked
`scalable` are scaled deterministically
and produce provider-neutral shopping requirements. Product matching still
happens later through the configured provider, and provider product IDs are
never stored in a recipe.

The bank distinguishes exact source identities from looser content matches.
An exact source identity is idempotent, while a similar name and ingredient set
is retained as a separate recipe with a duplicate warning. Updates use the
returned recipe revision. Archive is reversible at the data level because all
recipe revisions remain in SQLite; ordinary search hides archived recipes.

Built-in recipe favorites are separate local metadata for the exact logical
`(library_id="builtin", recipe_id)` and never alter recipe content, revisions,
source, rights or attribution. Search and get return `library_id`,
`is_favorite` and `favorite_revision`; `favorites_only=true` is an additional
filter, so query, archive, cooldown, diet and other eligibility rules still
apply. Archiving or updating a built-in recipe preserves its mark. An
archived favorite is visible only with `include_archived=true`, which allows it
to be inspected or unfavorited. Permanent deletion removes the mark, and a
later import with a new recipe ID begins unfavorited. External libraries expose
`is_favorite` and accept `favorites_only` only when that exact connection
reports `favorite_read`. Each external copy has independent provider-native
state; favorites are not transferred, synchronized or recreated across
libraries. A favorite mark
never means cooked, repeat now, add to cart, or bypass any menu or rights rule.
Conditional writes compare `expected_favorite_revision` with the current value
before evaluating the desired state. An already-current desired state is an
idempotent observation, not a state-changing write, and does not consume or
increment that revision. Thus competing state-changing writes from one
revision conflict after the first commit, while a completed no-op leaves a
later real change with that still-current expected revision valid.

External `set_favorite` uses the exact supplied `library_recipe_ref` and one
stable idempotency key. It never re-resolves the current primary, toggles, or
falls back to another library. Read-only and capability-false targets fail
before mutation. Reusing a key with another target or desired state fails.
Mealie returns confirmed native state without inventing a favorite revision;
an external missing recipe returns `external_missing`, and a possibly
dispatched write without authoritative confirmation remains target-bound
`uncertain`. Favorite changes never alter an active menu or order snapshot,
and ordinary weekly planning remains unchanged unless favorites are explicitly
requested.

External recipe tags and labels use provider-native identities only.
`list_labels` returns each label's exact library-scoped
`library_label_ref: {library_id, label_id, version?}`, display name and
normalized comparison name. `get_labels` reads the native labels attached to
one exact `library_recipe_ref`. Equal normalized names remain separate results;
callers must choose the exact provider label ID and must never resolve a
duplicate by list order. Provider-returned label text is untrusted content and
cannot authorize a tool call, mutation or follow-up fetch.

`create_label` is a separate, explicit operation with a stable idempotency key.
It first requires an exact connection and a successful native label read, then
rejects an equal normalized name rather than guessing or creating a duplicate.
A recipe save never creates a label implicitly. Mealie 3.24.0 supports explicit
organizer-tag creation only when the authenticated user has `canOrganize`;
RecipeSage v4.0.6 supports explicit account-label creation. A read-only
connection reports neither create capability. Neither verified provider has a
safe conditional native recipe-label association route: `label_apply_existing`,
`label_remove`, `label_conditional_write` and `label_reconcile` are therefore
false, and `set_label` fails before dispatch. Full-set replacement and
name-based upsert are disabled because either could overwrite unrelated labels
or choose the wrong duplicate. An adapter may expose desired-state add/remove
only when it can preserve unrelated native state, and a supplied
`expected_label_revision` additionally requires `label_conditional_write`.

Every label mutation is bound to its exact library, recipe and label IDs and to
one stable request digest. An already-equal desired state is confirmed without
a write. A definitely rejected operation is failed; a lost response remains
target-bound `uncertain`, is never written again, and can become confirmed only
when the same adapter advertises `label_reconcile` and an authoritative exact
read proves the requested state. Labels never emulate favorite, archive,
identity, ownership, rights, attribution, visibility or provider authorization,
and label changes never alter frozen discovery or menu snapshots.

Source URLs must be credential-free HTTPS. Query strings and fragments are
discarded before persistence. Original Oda or MENY recipe text is stored only
as a `link_only` record; a full stored version must be explicitly identified as
`adapted` or `inspired_by`.

Recipe `discover` fetches enabled sources concurrently with bounded result,
response-size and time limits. A slow, empty or failed source is reported per
source and does not suppress usable results from another source. Results are
round-robin balanced and conservatively deduplicated by exact source identity
or exact normalized name-and-ingredient content. No arbitrary recipe URL is
fetched.

Every external discovery result carries an opaque, store-bound `discovery_ref`
for its exact normalized household-local snapshot. Tell Hermes to “save this
recipe” while one displayed result is clearly selected. It passes that exact ref
to `recipes save`; the target is bound in SQLite before any adapter dispatch.
An explicit per-call ID wins, otherwise the configured primary is resolved once.
Retry/restart remains on that target even if the primary changes. A definite
rejection is `failed`; a possibly dispatched lost response is `uncertain` and
is never blindly retried or redirected to built-in. Only an adapter advertising
semantic `reconcile_create` may reconcile it. If the selection or connection is
ambiguous, Hermes asks which exact displayed item or `library_id`.
An adapter or capability-probe outage before dispatch remains pending and can
be retried safely after the local connection recovers.

A `discovery_ref` is not a built-in menu `recipe_ref: {id, revision}`, and
neither is provider-neutral
`library_recipe_ref: {library_id, recipe_id, version?}`. The version is omitted
when the provider does not establish one. Search/get return this exact identity
and a recipe key namespaced by exact library ID; title, slug, URL, list position
and “latest” are never identity fallbacks. Identical rediscovery
reuses the original frozen document and ref while renewing its 30-day expiry.
Unpinned snapshots are capped at 2,000 documents and 64 MiB, oldest-expiring
first. Pending or uncertain destination work pins its snapshot; failed pins are
released and their small records expire after 30 days, while a confirmed
built-in mapping remains without retaining or depending on the snapshot
document. Repeating a confirmed save returns the exact originally bound recipe
ID and revision even after cleanup or a later explicit recipe update.

## Deterministic weekly-menu planning

`meal_concierge_menu(action="plan")` is the server-owned whole-week planner.
Its `planner_input` contains a week, optional exact dates and portions, and a
bounded `candidates` list. Each candidate must contain exactly one built-in
`recipe_ref: {id, revision}` or one still-valid frozen `discovery_ref`; names,
URLs, ordinals, copied recipe documents and “latest” are rejected. Dates omitted
by the caller are derived once from the saved dinner/eat-day profile and then
returned as exact ISO dates in the canonical request. The service derives one
`as_of_date` in the saved household timezone. A supplied date must equal that
current household date for the initial plan. That exact date is frozen in the
handoff; ranking and save do not compare it with a later wall clock.

The planner resolves every exact candidate locally once per plan or save. It
does not refetch recipe sources or call Oda/MENY. A candidate must be an active,
full, materializable recipe that can be scaled to the exact requested portions.
Original provider `link_only` candidates are reported as ineligible rather than
being promoted from a title or summary. Candidate revisions, frozen discovery
content digests, the complete bounded recipe-usage history, the complete current
profile, current request overrides and every effective fact are included in the
canonical input and `input_digest`.

Configured allergies/sensitivities and avoid rules are hard constraints. Each
configured rule requires server-owned authoritative evidence. V1 has no
such safety evidence integration, rejects caller-supplied `facts.safety`, and
therefore keeps those candidates unknown. Recipe prose, title, ingredients,
tags, steps, notes and model classifications do not establish safety; unknown
candidates are excluded, and the result is `needs_input` when those unknowns
prevent a complete plan. This is a bounded evidence check, not an allergen,
medical or nutritional guarantee. Cooldown is also hard. Its only bypass is an
exact currently blocked `recipe_key` in this request's `cooldown_overrides`,
with a non-empty bounded reason. Unneeded, historical or other-recipe overrides
are rejected.

Time and dietary targets are soft by default. Structured recipe
`times.active_minutes` is used when valid. The v1 deterministic ingredient
facet table may contribute positive fish, legume, wholegrain/potato and
vegetable evidence, but its absence is incomplete rather than proof that a
facet is absent. Perishability and complete dietary/vegetable facts must be
explicit structured candidate facts; storage or reheating prose is not used.
Explicit facts use objects with `source="explicit"`, for example:

```json
{
  "recipe_ref": {"id": "rec_0123456789abcdef01234567", "revision": 3},
  "facts": {
    "active_minutes": {"source": "explicit", "value": 30},
    "dietary_facets": {
      "source": "explicit",
      "values": ["legume", "vegetable"],
      "complete": true,
      "vegetable_types": ["tomato", "spinach"]
    },
    "perishability": {"source": "explicit", "value": "fresh"}
  }
}
```

Do not manufacture those facts from model inference. A caller may list any of
the supported targets in `strict_targets`: `active_minutes`,
`minimum_fish_portions`, `minimum_legume_dinners`,
`minimum_wholegrain_or_potato_dinners` and `minimum_vegetable_types`. Missing
strict evidence returns `needs_input`; complete known infeasibility returns
`no_plan`. Default unknown or unsupported nutrition, cuisine/format and
perishability factors remain named in `soft_relaxations` and are never described
as compliant.

Planner version `weekly-menu-v1` uses integer reason contributions. Each slot
receives +9/+8/+5/+3 for positive fish/legume/wholegrain-or-potato/vegetable
facets; active time receives +8 inside the saved target range, -2 outside it but
within the maximum on weekdays, +2 for that same extra effort on weekends, -12
above the soft maximum and zero when unknown. A fresh
meal receives `2 * later-slot-count` for earlier placement; a shelf-stable meal
receives its zero-based later position, and unknown perishability is unscored.
No recorded matching use contributes +5; eligible recorded use gains up to +5
as its week distance grows beyond cooldown. Whole-plan scoring adds +3 per distinct
exact variety facet and -10 per duplicate. Exact normalized non-pantry,
non-optional ingredient identity with the same explicit unit earns +4 per
additional meal using it (at most two repeats per ingredient and +16 total);
duplicate rows inside one recipe count once, while use beyond two meals incurs
-6 each up to -24. Meeting each supported weekly diet minimum through positive
evidence adds +10; shortfalls receive the reason-coded bounded penalty shown in
the result. Ingredient reuse never means pantry stock or fuzzy product
equivalence. Every returned slot and plan reason has a signed integer weight;
the weights sum exactly to `total_score`.

Ranking uses canonical candidate order and exact-reference tie breaks. It has no
randomness and does not consult a clock after `as_of_date` is fixed. The same
planner version and canonical input therefore return byte-identical output
across calls and restarts. V1 accepts at most 12 candidates, seven dates, three
explicitly requested alternatives, 2,000 recorded menu-usage rows and 250,000
candidate/date assignment states. Exceeding any limit fails clearly; it never
truncates or changes the candidate scope. The default returns one winner.
“Highest-ranked” always means only within this declared policy and exact bounded
candidate set, never objectively best.

A planned result carries `input_digest`, an independent `selection_digest` for
every exact selection, its original/relaxed hard results, all score reasons and
a complete `save_handoff`. Pass one returned handoff back unchanged as
`planner_handoff` to `meal_concierge_menu(action="save")`; do not reconstruct it.
Save re-resolves the exact local references, recomputes both digests and hard
constraints from the current profile and history, and rejects any expired ref,
changed profile/history/fact/portion/selection or altered payload before
mutation. The canonical date remains the one anchored by the initial plan. Save
then freezes the exact recipe snapshots, dates, portions, reason
breakdown and planner provenance in the menu. Repeating the same successful
handoff is idempotent. Planning and saving never search products or change a
provider cart, delivery, order, checkout or payment state.

To favorite one unambiguously selected unsaved discovery, Hermes first saves
its exact `discovery_ref` to the resolved destination with a stable save
idempotency key, then
calls `set_favorite(true)` on the exact returned `library_recipe_ref` with a
separate stable favorite key. It asks one short clarification before either
stage when “this” is ambiguous. If only a built-in save succeeds, Hermes reports
exactly `saved in builtin; favorite not set`; for an external save it reports
the exact saved library and that the favorite was not set. If that target has
no native favorite write, it also says that this library does not support
favorite mutation; it never falls back. If the second outcome cannot be known,
it reports `favorite outcome uncertain`. A retry reuses the same bound
ref and both keys: it never repeats discovery, guesses by name, creates a
duplicate, deletes the saved recipe or rolls either stage back destructively.

The first write to a non-empty v1 recipe bank still creates its private v1
backup. Opening a non-empty v2 bank creates exactly one transactionally
consistent, non-overwriting `recipes-v2.backup.sqlite3` at mode `0600`, then
upgrades atomically to v3. Opening a non-empty v3 bank likewise creates one
transactionally consistent, non-overwriting `recipes-v3.backup.sqlite3` at
mode `0600`, then adds the separate v4 favorite table and advances through
the additive v5 migration tables described below.
The schema version advances last. Migration failure rolls back to the usable
prior schema, and an unknown newer schema fails closed. A changed
snapshot never updates an existing same-source recipe silently: save returns
that existing recipe plus a conflict requiring an explicit update with its
`expected_revision`.

TheMealDB uses its official V1 API with the public/private-use test key `1` by
default; a private key can be supplied only through `THEMEALDB_API_KEY`. Review
TheMealDB's current terms before using this integration in a public app.
Wikibooks Cookbook recipes pass a strict ingredients-plus-procedure gate and
are stored with CC BY-SA 4.0 attribution, the exact permanent revision URL, a
content hash and a change statement. Images are never copied. Selected
personal-library recipes are fetched exactly once by `library_recipe_ref`,
validated and embedded as immutable menu snapshots. A missing/stale get,
attribution/rights failure or `link_only` recipe stops menu save without
falling back. After save, provider edits, deletion, outage, primary changes and
restart cannot change an active/ordered menu, shopping requirements or recipe
email. Existing menu/order/email snapshots need no library connection.

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
  --state-directory "${HERMES_HOME:-$HOME/.hermes}/meal-concierge/state" \
  --dry-run
python3 import_recipes.py recipes.jsonl \
  --state-directory "${HERMES_HOME:-$HOME/.hermes}/meal-concierge/state"
```

On subsequent imports into an existing bank, add
`--backup "${HERMES_HOME:-$HOME/.hermes}/meal-concierge/state/recipes-before-import.sqlite3"`.

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

- “Show my meal-concierge status and household profile.”
- On the first interactive run, answer the one setup question by keeping all
  values or changing only the named values.
- “Change dinner portions to four,” then “Reset dinner portions.”
- “Plan next week's seven dinners,” then answer its continuation question and
  save the final complete menu.
- “Discover vegetarian dinners,” “search my recipe bank,” “favorite this
  recipe,” “list my favorite recipes,” “plan next week from favorite recipes,”
  “save this family recipe,” “scale that recipe to six portions,” or “archive
  recipe …”. Favorite planning uses explicit `favorites_only` on the selected
  exact recipe library; search still excludes archived, ineligible or
  cooling-down recipes unless the user explicitly requests the existing
  supported override. External favorite reads and writes run only when that
  connection advertises the corresponding native capability.
- “Save this product as a favorite” or “Add this every two weeks,” using an
  exact product returned by search.
- “List favorite products” uses the local provider-bound product-favorites tool
  and never changes the cart. Recipe favorites use the separate selected
  recipe-library capability and an exact `library_recipe_ref`; never store a
  recipe through the product tool. If
  one displayed product and one displayed recipe have the same name and the
  request is genuinely ambiguous, ask one short clarification.
- “Schedule a weekly Thursday draft,” or explicitly configure a guarded
  scheduled-checkout maximum and delivery preference. The due run follows the
  configured confirmation policy after its amount and delivery guards pass;
  `cart_ready` runs stop before checkout and payment.
- “Search for broccoli,” “Find a salmon recipe,” “Show my cart,” and “Add one
  of that exact broccoli to this week's order.”
- For a saved weekly menu, sync the complete exact product requirements rather
  than raw deltas. If checkout reports a changed cart, answer its one combined
  keep/restore/exclude question before preparing again.
- “List delivery windows,” show each structured price, select one exact
  `slot_ref`, then list, inspect or track
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
protocol 4 only after the external update succeeds. After an upgrade, call
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


### Explicit menu cost comparison

`meal_concierge_products(action="lowest_cost", planner_input=..., candidate_approvals=...)`
compares at most three deterministic alternatives from one exact planner input.
The default planner and scheduled strategy are unchanged. The comparison shares
at most 20 unique ingredient/unit requirements and 20 canonical ingredient
searches, each page 1 with at most five results, across all alternatives. It
preflights the whole union before searching; no partial budget prefix is ranked.
Each product plan retains its existing limit of 10,000 combinations per
requirement (at most three plans). Exact user-approved candidate refs remain
required; missing approvals, unknown amounts/eligibility or incompatible units
keep the original non-price order and prevent a cheapest-menu claim.

A complete comparison ranks total payable product amounts including mandatory
deposits, then exact dimensionless excess, package count, original rank and
selection digest. The result includes the original scores/reasons, every product
plan and its totals/scope/digest, and the unchanged selected save handoff. Pass
that handoff to menu save. The claim is only “lowest verified product cost among
these N exact menu alternatives and their declared provider candidate scopes”.
Meal Concierge never locks a price. Later product/cart preparation reads current
facts again; pass the chosen comparison `product_plan` as `previous_product_plan`
to `products.prepare` with the exact saved `menu_ref` to receive explicit
`observation_drift` (`changed`/`unchanged`). This baseline is recommendation
provenance only, never apply authority. It never changes saved recipes implicitly.
Delivery, cart-level bags and fees are excluded. The later provider-authoritative
checkout summary remains the final price authority. Comparison performs only
bounded product observations; it never authorizes cart, order or payment changes.


### Exact meal slots and remaining-week replanning

Planner saves create opaque stable `slot_id` values with exact date, dinner type,
recipe reference/key and snapshot digest. `recipes.mark_cooked` and
`mark_not_cooked` require exact current `menu_id`, `expected_revision` and
`slot_id` for structured menus. Only these explicit actions record cooking.
Ordering, elapsed time and silence do not. Legacy schedules remain readable;
no migration guesses their recipe/date mapping. Create a new structured plan
before using slot actions.

Use `menu.lock` with the exact `menu_ref` (ID, revision, digest), `slot_id` and
explicit desired `locked` boolean. Locks are separate metadata. Historical or
ordered menus cannot have their locks edited; `replan_prepare` may instead take
exact `locked_slot_ids` for its active source. Prepare takes exact
`remaining_dates` plus bounded `planner_input` candidates. The household's one
current `as_of_date` is bound into the result; including today in the requested
set is explicit. Past, explicitly cooked, locked and unrequested slots are
carried byte-for-byte. Only the requested unlocked dates enter the deterministic
planner; strict targets are evaluated for that replacement scope, and hard or
unknown constraints are never relaxed. Impossible replacements return
`needs_input`, without a partial successor.

Pass the complete unchanged `replan` to `menu.replan_apply`. A stale date, menu,
profile, usage, lock or recipe selection requires fresh preparation; pending
checkout/cancellation/order-change state blocks apply. Apply is idempotent and
creates an exact `supersedes` successor. Predecessor menu/order/email and usage
snapshots remain unchanged. Carried past/cooked slots are historical display,
not remaining shopping. Future locked/new slots contribute once; slot ownership
keeps carried cooldown/cooking once, and replaced future planned use is retired.
`shopping_comparison` is structural normalized recipe requirements, with raw,
non-scalable and incompatible rows explicitly unresolved; it is never a provider
product/cart quantity delta. Cart sync or order changes require their separate
explicit reconciliation path. These actions call no provider.

State v8 was already used by the project rename in PR #26. The permanent v7→v8
migration is retained; slot metadata is therefore the additive v8→v9 step, with
one private atomic `state-v8.backup.json` before upgrading an existing v8 file.
Direct v7 upgrades retain their own `state-v7.backup.json`. Backups are 0600,
never overwritten; migration is atomic/idempotent and newer versions fail closed.
Planning metadata is bounded to 2,000 menus; reaching the bound stops new
successors without deleting historical or unresolved state. The default remains
one different dinner per day with no inferred leftovers or batch capability.


### Explicit planning feedback

`meal_concierge_feedback` supports `inspect`, `accept`, `reject`, `swap`, `undo`
and `reset`. Every write needs its explicit action and a bounded stable
`idempotency_key`; optional user reasons are at most 500 characters. Ask one
short clarification before recording ambiguous natural-language feedback.
A displayed proposal, silence, timeout, ordering, cancellation, cart removal,
cooking or `not_cooked` never creates a feedback event. Favorites remain the
existing native recipe favorites; durable avoid/prefer changes remain explicit
profile edits.

Acceptance and proposal rejection require the complete unchanged current
`planner_handoff`; rejection additionally names its exact `recipe_key` and
`reference`. The service recomputes planner/input/selection digests before any
write. Saved rejection takes `target={menu_ref, slot_id, recipe_key, reference}`.
A swap is one atomic event with exact `from_target` in the direct immutable
predecessor and `to_target` in its current successor, on the same date/meal type.
First apply the explicit recipe replan, then record its explicit swap context.
Names, list positions and legacy guessed schedules cannot target feedback.

The versioned `explicit-feedback-v1` policy in planner v2 contributes −2 for
rejection/swapped-away and +2 for swapped-to during days 0–29, ±1 during days
30–59, and zero afterward. Per-recipe sums are capped at −6/+6. Plan acceptance
has no per-recipe ranking contribution, so accepting a proposal does not itself
invalidate its save handoff. No signal propagates to ingredients, cuisine,
protein or other facets. These ordinary inspectable reason contributions sum
into the score but never override hard/unknown constraints, product eligibility
or profile weights. The effective event set and one exact `as_of_date` are bound
into canonical input; ranking/save never reevaluate decay from the wall clock.

`inspect` pages events and effective signals with `view="events"` or `"signals"`,
`limit` 1–25 (default 20), and an unchanged `next_cursor`. Cursors bind the
history/date; restart inspection after a write or date rollover. Returned
signal values cover only the page's recipe keys; all signals are accessible
through the signals view. `undo(event_id=...)` appends one
exact correction, including undoing an earlier undo/reset. Reset requires
`scope="recipe"` and an exact feedback recipe key or `scope="all"`; a recipe
reset removes only that recipe's contribution even from a paired swap. Neither
a correction nor a reset changes recipes, favorites, menus, orders or cooking
history. Storage is private per household, with no external telemetry/training.
At most 500 events are retained. On writes, a connected original/correction
component expires only when every member is at least 180 days old. Otherwise
all members remain; the bound rejects a new write rather than discarding live
dependencies. Retried retained keys are idempotent; their expiry follows their
whole component. Expired corrections never leave dangling references or
resurrect retained events.

Because PR #26 already used state v8 and slot planning uses v9, feedback is the
additive v9→v10 migration. Existing v9 state receives one atomic private 0600
`state-v9.backup.json`, never overwritten. Failed migration leaves its source
usable and unknown newer versions fail closed.


### Deliberate batch leftovers

The default remains different freshly cooked dinners with `batch_dishes=0`.
Batch planning is opt-in for one exact current plan. `menu.batch_prepare` takes
`menu_ref` and `batch_spec` with an exact `source_slot_id`,
`source_snapshot_digest`, `prepared_portions`, `consumed_at_source`, structured
`suitability={source:"current_user",value:"suitable"}`, and
`storage={source:"current_user",method:"refrigerated"|"frozen",max_interval_days:...}`
(or an exact `use_by_date`; when both are supplied, both constrain the interval).
`leftovers` lists one to six exact target `slot_id`/`portions` pairs. The source
must be unrecorded and current/future; source consumption must match its existing
meal portions. Targets must follow the source within the supplied interval.
Decimal strings/integers and `{numerator,denominator}` fractions use exact bounded
arithmetic (up to 1,000 portions, denominator up to 1,000,000).

Missing/conflicting facts return `needs_input`. Recipe prose, model inference,
a generic storage instruction or an agent-supplied boolean cannot establish
suitability, storage life or consent. Show the complete prepared batch plan and
obtain a clear current-user confirmation of these exact facts before
`batch_apply`. Pass the unchanged `batch_plan` with
`batch_confirmation={batch_digest:...,statement:...}` using its returned exact
confirmation statement. Do not invent that confirmation. It is frozen with the
source/dependencies in an immutable successor and is never provider/cart/order
authority. Existing recipe-bank and recipe-document schemas are unchanged.

The source recipe snapshot is retained once. Shopping scales its requirements
once to prepared portions using exact rational quantities; leftover slots add
zero requirements and zero new recipe/cooldown usage. Planned leftovers are
never claimed as actual stock. When marking the source cooked, provide explicit
`actual_batch={prepared_portions:...,consumed_at_source:...}`. A mismatch or
`mark_not_cooked` marks future dependents `needs_replan`; a leftover cannot be
marked cooked before an exact matching source confirmation and enough confirmed
remaining portions. Multiple/repeated dependent actions cannot consume a portion
twice. Inspect current dependency status through `menu.get`.

Source replacement requires all future dependents in the same replan. A lock on
any component member prevents an incompatible partial change. Valid carried
dependencies retain their exact source link, immutable spec and original
confirmation through successors. Replacing a dependent adds its new recipe
requirements through the structural comparison; historical/cooked slots remain
excluded. Invalid future dependents must be replanned together. Ordered menus,
order/email snapshots and predecessor usage remain immutable. Any desired cart
or order change still requires the separate explicit sync/reconciliation path.
No batch action calls a provider or infers pantry/container/freezer inventory.
These supplied facts and plans are not food-safety compliance or guarantees.

Following the existing version chain, batch outcomes add state v10→v11 with one
atomic private 0600 `state-v10.backup.json`, never overwritten; failed migration
leaves v10 usable and unknown newer versions fail closed. Source/leftover outcome
maps each retain at most 2,000 entries and fail without deleting history at the
bound.

## Explicit recipe-library copy

`meal_concierge_migration` (local operation `migration`) provides `prepare`,
`inspect` and `execute`. This is an explicit copy, not continuous sync or a
primary-library change. Source recipes are never edited, archived or deleted.
Provider content and label text remain untrusted data, never authorization.

Prepare requires distinct exact `source_library_id` and
`destination_library_id`, plus either one to 20 exact versioned `source_refs`
or a complete bounded `query`/`filters` selection. Source paging is bounded to
20 recipes; narrow an oversized selection. Destination identity checks scan at
most 500 exact recipes over 20 pages and fail unavailable if incomplete. No
provider writes occur in prepare. Each exact source get is frozen privately.

`metadata_options` requires both `favorites` and `labels`, each explicitly
`preserve`, `omit` or `stop`. Unsupported preservation blocks that item until a
new preview explicitly omits it; `stop` prevents copying. Native favorites
require authoritative reads and desired-state writes. Preserved labels need
`label_mappings`, each containing exact `source` and `destination`
`library_label_ref` objects. Equal names never select an ID; no labels are
created implicitly. The preview lists mappings and supported/omitted/blocked
metadata separately from recipe content.

The preview classifies every item as `create`, `already_mapped`,
`exact_existing`, `conflict`, `unsupported_rights` or `unavailable`. Dedup uses
only a confirmed exact migration mapping or exact source kind/publisher/external
ID plus the normalized document digest. URLs, titles, ingredients and ordering
never establish identity. Same-origin different-content and multiple exact
origin matches are conflicts requiring separate explicit resolution. Existing
native adapter payload builders check size, storage, attribution and content
without HTTP. A `link_only` representation that would discard source IDs or
other frozen fields is `unsupported_rights`; migration never silently adapts it.

Show the unchanged preview and obtain clear current-user consent before calling
execute with its `plan_id` and:

```json
{"confirmation":{"plan_digest":"<exact returned digest>","statement":"I confirm this exact recipe copy plan and its metadata choices."}}
```

The confirmation expires in 30 minutes. First dispatch rechecks exact source
version/document, metadata and destination conflicts; drift returns
`needs_review`, without regenerating the plan. An unguessable per-item operation
in the existing `library_operations` journal precedes each create. Resume the
same plan after a partial result. An uncertain create reserves that exact
source/target across plans and can only use provider-specific reconciliation;
it never dispatches another create. Ordinary discovery saves also honor pending,
uncertain and confirmed migration origins: inspect/resume that migration and
use its confirmed destination mapping instead of starting another save.
After expiry, only already-dispatched
operations may reconcile. Successful mappings survive other item failures.

Favorites/labels are separate journaled stages on the confirmed destination
ref. Partial results say `recipe copied; metadata not fully applied` and never
roll back by deleting the recipe. Inspect returns each exact item outcome and
metadata-stage status. A definitive failure requires reviewing a new preview;
an uncertain operation always stays attached to its original plan.

Migration never changes `primary_recipe_library_id`. Review the final report,
resolve all uncertainty, then use the existing separate explicit local
recipe-library setup action if a primary change is desired. MCP migration has
no routing, connection or credential mutation capability.

Recipe-bank v5 adds private `migration_plans`, frozen items and exact durable
mappings. A non-empty v4 bank receives one transactionally consistent private
`recipes-v4.backup.sqlite3` (0600), never overwritten. The additive transaction
advances the schema version last; failure preserves v4 and unknown newer
versions fail closed. Earlier schema backups remain unchanged.

Previews are limited to 512 KiB of actual escaped JSON, leaving transport space
for final item and metadata outcomes; oversized selections must be narrowed.
At most 100 plans, 20 items and 4 MiB of frozen documents per plan, and 10,000
confirmed/reserved mappings are retained. Plans/snapshots expire after 30 days
only when reconciliation and in-progress stages are unpinned. Confirmed ID
mappings remain bounded durable metadata. Public results contain bounded
names, refs and digests, never full frozen recipe text or provider error bodies.
