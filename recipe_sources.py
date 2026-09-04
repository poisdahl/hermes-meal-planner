"""Bounded, read-only recipe discovery from public and provider sources."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core import DEFAULT_RECIPE_SOURCES, HouseholdError, RECIPE_SOURCE_IDS
from recipes import RecipeError, normalize_recipe, normalize_source_url


SOURCE_IDS = RECIPE_SOURCE_IDS
DEFAULT_SOURCES = DEFAULT_RECIPE_SOURCES
HTTP_TIMEOUT = 5.0
MAX_JSON_BYTES = 1024 * 1024
MAX_PROVIDER_RESULTS = 20
WIKIBOOKS_LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"


class RecipeSourceError(HouseholdError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.replace("\u0000", "").split())
    return cleaned[:maximum]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SameHostRedirect(HTTPRedirectHandler):
    def __init__(self, host: str):
        self.host = host

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if parsed.scheme != "https" or parsed.hostname != self.host or parsed.username or parsed.password:
            raise RecipeSourceError("recipe source redirected outside its fixed HTTPS host")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def fetch_json(url: str, params: Mapping[str, Any], *, timeout: float, maximum: int) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RecipeSourceError("recipe source URL is invalid")
    encoded = urlencode({key: str(value) for key, value in params.items()})
    request = Request(
        f"{url}?{encoded}",
        headers={"Accept": "application/json", "User-Agent": "Meal-Concierge/1.7 (private household recipe discovery)"},
    )
    try:
        with build_opener(_SameHostRedirect(parsed.hostname)).open(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if "json" not in content_type:
                raise RecipeSourceError("recipe source returned a non-JSON response")
            payload = response.read(maximum + 1)
    except RecipeSourceError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RecipeSourceError("recipe source request failed") from exc
    if len(payload) > maximum:
        raise RecipeSourceError("recipe source response is too large")
    try:
        value = json.loads(payload, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RecipeSourceError("recipe source returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RecipeSourceError("recipe source returned an invalid object")
    return value


class TheMealDBSource:
    def __init__(
        self,
        *,
        api_key: str = "1",
        transport: Callable[..., dict[str, Any]] = fetch_json,
        clock: Callable[[], str] = _now,
    ):
        if not isinstance(api_key, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", api_key) is None:
            raise RecipeSourceError("TheMealDB API key is invalid")
        self.api_key = api_key
        self.transport = transport
        self.clock = clock

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        query = _text(query, maximum=200)
        value = self.transport(
            f"https://www.themealdb.com/api/json/v1/{self.api_key}/search.php",
            {"s": query}, timeout=HTTP_TIMEOUT, maximum=MAX_JSON_BYTES,
        )
        meals = value.get("meals")
        if meals is None:
            return []
        if not isinstance(meals, list) or len(meals) > 100:
            raise RecipeSourceError("TheMealDB returned an invalid meal list")
        results = []
        for value in meals:
            try:
                recipe = self._recipe(value)
            except (RecipeError, RecipeSourceError):
                continue
            results.append(recipe)
            if len(results) == limit:
                break
        return results

    def _recipe(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RecipeSourceError("TheMealDB meal is invalid")
        meal_id = _text(value.get("idMeal"), maximum=32)
        name = _text(value.get("strMeal"), maximum=300)
        instruction_value = value.get("strInstructions")
        instructions = instruction_value.replace("\u0000", "")[:24_000] if isinstance(instruction_value, str) else ""
        if not re.fullmatch(r"[0-9]{1,20}", meal_id) or not name or not instructions.strip():
            raise RecipeSourceError("TheMealDB meal is incomplete")
        ingredients = []
        for index in range(1, 21):
            item = _text(value.get(f"strIngredient{index}"), maximum=300)
            measure = _text(value.get(f"strMeasure{index}"), maximum=100)
            if not item:
                continue
            raw = " ".join(part for part in (measure, item) if part)
            ingredients.append({
                "raw": raw or item, "amount": measure or None, "item": item,
                "quantity": None, "unit": None, "scalable": False,
            })
        steps = [_text(line, maximum=4_000) for line in re.split(r"[\r\n]+", instructions)]
        steps = [line for line in steps if line]
        if not ingredients or not steps:
            raise RecipeSourceError("TheMealDB meal is incomplete")
        tags = []
        for candidate in (value.get("strCategory"), value.get("strArea"), value.get("strTags")):
            for tag in str(candidate or "").split(","):
                cleaned = _text(tag, maximum=80)
                if cleaned and cleaned not in tags:
                    tags.append(cleaned)
        recipe = {
            "name": name,
            "language": "en",
            "portions": None,
            "ingredients": ingredients,
            "steps": steps,
            "tags": tags[:50],
            "source": {
                "kind": "themealdb", "publisher": "TheMealDB", "title": name,
                "author": None, "url": f"https://www.themealdb.com/meal/{meal_id}",
                "external_id": meal_id, "relationship": "original",
            },
            "rights": {
                "storage": "full", "license": "TheMealDB terms for private API use",
                "license_url": "https://www.themealdb.com/terms_of_use.php",
                "credit": "Recipe data from TheMealDB; fetched through the official V1 API.",
            },
            "external_snapshot": {
                "fetched_at": self.clock(), "content_hash": _hash(value),
                "source_revision_id": _text(value.get("dateModified"), maximum=100) or None,
                "permanent_url": None,
                "changes": "Normalized into Meal Concierge's structured format; artwork was not copied.",
            },
        }
        return normalize_recipe(recipe)


class _RecipeHTML(HTMLParser):
    INGREDIENT_HEADINGS = ("ingredient", "ingredients", "what you need")
    STEP_HEADINGS = ("procedure", "directions", "method", "preparation", "instructions", "steps")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section: str | None = None
        self.heading: list[str] | None = None
        self.heading_tag: str | None = None
        self.list_depth = 0
        self.list_value: list[str] = []
        self.ingredients: list[str] = []
        self.steps: list[str] = []
        self.ignored_depth = 0

    @staticmethod
    def _section(value: str) -> str | None:
        normalized = " ".join(value.casefold().split())
        normalized = re.sub(r"\s*\[?edit(?: source)?\]?\s*$", "", normalized).strip(" :–—-")
        if normalized in _RecipeHTML.INGREDIENT_HEADINGS:
            return "ingredients"
        if normalized in _RecipeHTML.STEP_HEADINGS:
            return "steps"
        return None

    def handle_starttag(self, tag: str, attrs):
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        if self.ignored_depth:
            return
        if tag in {"h2", "h3"}:
            self.heading_tag = tag
            self.heading = []
        elif tag == "li" and self.section:
            self.list_depth += 1
            if self.list_depth == 1:
                self.list_value = []

    def handle_endtag(self, tag: str):
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if self.heading is not None and tag == self.heading_tag:
            self.section = self._section(" ".join(self.heading))
            self.heading = None
            self.heading_tag = None
        elif tag == "li" and self.list_depth:
            self.list_depth -= 1
            if self.list_depth == 0:
                value = " ".join(" ".join(self.list_value).split())
                if value:
                    target = self.ingredients if self.section == "ingredients" else self.steps
                    if value not in target:
                        target.append(value)

    def handle_data(self, data: str):
        if self.ignored_depth:
            return
        if self.heading is not None:
            self.heading.append(data)
        elif self.list_depth:
            self.list_value.append(data)


class WikibooksSource:
    def __init__(
        self,
        *,
        transport: Callable[..., dict[str, Any]] = fetch_json,
        clock: Callable[[], str] = _now,
    ):
        self.transport = transport
        self.clock = clock

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        query = _text(query, maximum=200)
        params = {
            "action": "query", "list": "categorymembers", "cmtitle": "Category:Recipes",
            "cmtype": "page", "cmlimit": min(50, max(10, limit * 4)),
            "format": "json", "formatversion": 2,
        }
        if query:
            params["cmstartsortkeyprefix"] = query
        value = self.transport(
            "https://en.wikibooks.org/w/api.php", params,
            timeout=HTTP_TIMEOUT, maximum=MAX_JSON_BYTES,
        )
        members = (value.get("query") or {}).get("categorymembers") if isinstance(value.get("query"), Mapping) else None
        if not isinstance(members, list) or len(members) > 50:
            raise RecipeSourceError("Wikibooks returned an invalid category list")
        titles = []
        for member in members:
            title = _text(member.get("title") if isinstance(member, Mapping) else None, maximum=300)
            if title.startswith("Cookbook:") and title not in titles:
                titles.append(title)
        titles = titles[:min(10, max(4, limit * 2))]
        results = []
        executor = ThreadPoolExecutor(max_workers=min(4, max(1, len(titles))))
        futures = {executor.submit(self._fetch_recipe, title): title for title in titles}
        done, pending = wait(futures, timeout=HTTP_TIMEOUT + 1)
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        for future in done:
            try:
                recipe = future.result()
            except (RecipeError, RecipeSourceError):
                continue
            if recipe is not None:
                results.append(recipe)
        results.sort(key=lambda item: str(item.get("name") or "").casefold())
        return results[:limit]

    def _fetch_recipe(self, title: str) -> dict[str, Any] | None:
        value = self.transport(
            "https://en.wikibooks.org/w/api.php",
            {
                "action": "parse", "page": title, "redirects": 1,
                "prop": "text|revid|displaytitle", "format": "json", "formatversion": 2,
            },
            timeout=HTTP_TIMEOUT, maximum=MAX_JSON_BYTES,
        )
        parsed = value.get("parse")
        if not isinstance(parsed, Mapping):
            return None
        final_title = _text(parsed.get("title"), maximum=300)
        revision = parsed.get("revid")
        body = parsed.get("text")
        if (
            not final_title.startswith("Cookbook:")
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
            or not isinstance(body, str) or not body or len(body.encode()) > MAX_JSON_BYTES
        ):
            return None
        lowered = body.casefold()
        if "incomplete recipe" in lowered or "cookbook incomplete" in lowered or "class=\"incomplete" in lowered:
            return None
        parser = _RecipeHTML()
        try:
            parser.feed(body)
            parser.close()
        except (ValueError, RecursionError):
            return None
        ingredients = [_text(item, maximum=500) for item in parser.ingredients]
        steps = [_text(item, maximum=4_000) for item in parser.steps]
        ingredients = [item for item in ingredients if item]
        steps = [item for item in steps if item]
        if not ingredients or not steps:
            return None
        name = final_title.removeprefix("Cookbook:").strip()
        encoded_title = quote(final_title.replace(" ", "_"), safe=":()/,'-")
        source_url = normalize_source_url(f"https://en.wikibooks.org/wiki/{encoded_title}")
        permanent_url = normalize_source_url(f"https://en.wikibooks.org/wiki/Special:PermanentLink/{revision}")
        recipe = {
            "name": name,
            "language": "en",
            "portions": None,
            "ingredients": [
                {"raw": item, "item": item, "quantity": None, "unit": None, "scalable": False}
                for item in ingredients[:200]
            ],
            "steps": steps[:100],
            "tags": ["Wikibooks Cookbook"],
            "source": {
                "kind": "wikibooks", "publisher": "Wikibooks Cookbook", "title": name,
                "author": "Wikibooks contributors", "url": source_url,
                "external_id": str(parsed.get("pageid") or final_title), "relationship": "adapted",
            },
            "rights": {
                "storage": "full", "license": "CC BY-SA 4.0",
                "license_url": WIKIBOOKS_LICENSE_URL,
                "credit": f"Wikibooks contributors, permanent revision {revision}; normalized by Meal Concierge.",
            },
            "external_snapshot": {
                "fetched_at": self.clock(), "content_hash": _hash({"title": final_title, "revision": revision, "html": body}),
                "source_revision_id": str(revision), "permanent_url": permanent_url,
                "changes": "Ingredients and procedure were normalized; navigation and images were omitted.",
            },
        }
        return normalize_recipe(recipe)


def provider_recipe_candidates(provider: str, value: Any, limit: int) -> list[dict[str, Any]]:
    if provider not in {"oda", "meny"} or not isinstance(value, Mapping):
        raise RecipeSourceError("provider recipe response is invalid")
    rows = value.get("recipes")
    if not isinstance(rows, list) or len(rows) > MAX_PROVIDER_RESULTS:
        raise RecipeSourceError("provider recipe response is invalid")
    allowed_hosts = {"oda": {"oda.com", "www.oda.com"}, "meny": {"meny.no", "www.meny.no"}}[provider]
    results = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _text(row.get("name") or row.get("title"), maximum=300)
        external_id = _text(row.get("recipe_id") or row.get("id"), maximum=300)
        url_value = row.get("recipe_url") or row.get("url")
        try:
            url = normalize_source_url(url_value)
        except RecipeError:
            continue
        if not name or not external_id or urlsplit(url or "").hostname not in allowed_hosts:
            continue
        results.append(normalize_recipe({
            "name": name, "language": "nb-NO", "tags": [provider.upper()],
            "source": {
                "kind": provider, "publisher": provider.upper(), "title": name,
                "author": None, "url": url, "external_id": external_id, "relationship": "original",
            },
            "rights": {
                "storage": "link_only", "license": None, "license_url": None,
                "credit": f"Original recipe at {provider.upper()}; open the source link for its terms.",
            },
        }))
        if len(results) == limit:
            break
    return results


def validate_source_settings(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != set(SOURCE_IDS):
        raise RecipeSourceError("recipe sources must name exactly internal, oda, meny, themealdb and wikibooks")
    if any(not isinstance(value[source], bool) for source in SOURCE_IDS):
        raise RecipeSourceError("recipe source settings must be true or false")
    return {source: value[source] for source in SOURCE_IDS}
