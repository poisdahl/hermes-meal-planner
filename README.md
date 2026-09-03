# Hermes Meal Planner

Hermes Meal Planner adds weekly meal planning and grocery ordering to Hermes
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

Clean installations create household state v7 with only the
`product_favorites` list and expose the
`meal_planner_product_favorites` tool. When rerun for an existing installation,
the installer stops only the meal-planner service, creates the non-overwriting
private migration backups, including `state-v6.backup.json` immediately before
the v6→v7 delivery-preference migration, migrates state atomically, refreshes
the installed skill and MCP registration, restarts the service, and verifies
both status and the new tool schema. This also starts an existing installation
that was stopped before the update. If migration fails, the old state and its
backup remain usable and the service stays stopped. Existing v6 households gain
`delivery.strategy="keep_selected"`; clean state and newly replaced delivery
preferences default to `"cheapest"`. Restore the matching backup before running
older code; older code must not read a v7 state file.

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

The plan stores only `B`, `R`, the verified quantity Meal Planner added, the
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
order-change, schedule, product-favorite and recurring records are provider-specific.
Use a separate private installation and MCP registration for another provider.

## Personal recipe-library connections

The built-in bank always exists as exact `library_id="builtin"` and is the
zero-configuration primary. The Mealie adapter is included; RecipeSage remains
a separate deliverable. Merely adding either connection never selects it or
blocks the built-in path. Connections live only in the private config and use
stable IDs:

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
never sent to another connection.

`base_url` is one credential-free origin. HTTPS is the default; loopback HTTP
is allowed for local hosting. Any other HTTP origin requires the setup helper's
explicit local warning confirmation and is never configurable through MCP.
Authenticated adapter calls compare scheme, host and port before attaching a
credential and never follow redirects.

Credentials are separate from config at
`$MEAL_PLANNER_HOME/secrets/recipe-libraries/<library_id>.json`, with directory
mode `0700` and file mode `0600`, owned by the service user. They never belong
in command arguments, state, SQLite, logs, MCP traffic, fixtures or Git. After
the provider-specific adapter is installed, use the interactive local helper:

```sh
home="${MEAL_PLANNER_HOME:-${HERMES_HOME:-$HOME/.hermes}/meal-planner}"
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
list page and the favorite-read response shape. It reports `search`, `get`,
`create_from_discovery`, `reconcile_create` and `favorite_read`; it never
reports update, archive, delete or favorite-write capabilities. A configured
`read_only` connection retains search/get and favorite reporting but suppresses
create/reconcile and cannot be selected as a writable primary.

Mealie search is paginated and returns the immutable provider UUID in the
namespaced `library_recipe_ref`; the current slug is display-only
`provider_slug` metadata. Exact get always addresses the UUID, and `updatedAt`
is the version token when Mealie supplies it. Saving uses only the already
frozen discovery document. The create flow installs native Mealie fields plus a
deterministic attribution block and private `hermes_origin`/`hermes_recipe`
extras. Frozen tags stay in `hermes_recipe`; the first version does not mutate
Mealie's shared tag organizer. `link_only` sends only the title, original HTTPS source link,
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
`$HERMES_HOME/meal-planner/state/recipes.sqlite3`. It is opened only by recipe
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
built-in search filter, so query, archive, cooldown, diet and other eligibility
rules still apply. Archiving or updating a recipe preserves its mark. An
archived favorite is visible only with `include_archived=true`, which allows it
to be inspected or unfavorited. Permanent deletion removes the mark, and a
later import with a new recipe ID begins unfavorited. External-library copies
have independent identities and no built-in favorite state. A favorite mark
never means cooked, repeat now, add to cart, or bypass any menu or rights rule.
Conditional writes compare `expected_favorite_revision` with the current value
before evaluating the desired state. An already-current desired state is an
idempotent observation, not a state-changing write, and does not consume or
increment that revision. Thus competing state-changing writes from one
revision conflict after the first commit, while a completed no-op leaves a
later real change with that still-current expected revision valid.

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

To favorite one unambiguously selected unsaved discovery, Hermes first saves
its exact `discovery_ref` to `builtin` with a stable save idempotency key, then
calls `set_favorite(true)` on the exact returned `library_recipe_ref` with a
separate stable favorite key. It asks one short clarification before either
stage when “this” is ambiguous. If only the save succeeds, Hermes reports
exactly `saved in builtin; favorite not set`; if the second outcome cannot be
known, it reports `favorite outcome uncertain`. A retry reuses the same bound
ref and both keys: it never repeats discovery, guesses by name, creates a
duplicate, deletes the saved recipe or rolls either stage back destructively.

The first write to a non-empty v1 recipe bank still creates its private v1
backup. Opening a non-empty v2 bank creates exactly one transactionally
consistent, non-overwriting `recipes-v2.backup.sqlite3` at mode `0600`, then
upgrades atomically to v3. Opening a non-empty v3 bank likewise creates one
transactionally consistent, non-overwriting `recipes-v3.backup.sqlite3` at
mode `0600`, then upgrades atomically to v4 with the separate favorite table.
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
- On the first interactive run, answer the one setup question by keeping all
  values or changing only the named values.
- “Change dinner portions to four,” then “Reset dinner portions.”
- “Plan next week's seven dinners,” then answer its continuation question and
  save the final complete menu.
- “Discover vegetarian dinners,” “search my recipe bank,” “favorite this
  recipe,” “list my favorite recipes,” “plan next week from favorite recipes,”
  “save this family recipe,” “scale that recipe to six portions,” or “archive
  recipe …”. Favorite planning uses explicit built-in `favorites_only`; search
  still excludes archived, ineligible or cooling-down recipes unless the user
  explicitly requests the existing supported override.
- “Save this product as a favorite” or “Add this every two weeks,” using an
  exact product returned by search.
- “List favorite products” uses the local provider-bound product-favorites tool
  and never changes the cart. Recipe favorites use the separate built-in recipe
  capability; never store a recipe through the product tool. If
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
