# CC_REPORT: f-odysseus-salvage-resilient-search

**Type:** operator-directed, out-of-band (NOT the active `NEXT_TASK.md`, which
remains `f-position-identity-phase-5i-post-rename-soak` / STATUS: PENDING — that
soak is observation-only and was not touched or closed here).

**Operator request (2026-06-01):** review
[`pewdiepie-archdaemon/odysseus`](https://github.com/pewdiepie-archdaemon/odysseus)
and salvage what's worth salvaging to improve CHILI. After assessment, the
operator selected "Build win #1 now": resilient multi-provider search +
source-content fetcher, additive, zero behavior change without config, one commit.

## What shipped

- **New `app/search_providers.py`** — resilient multi-provider web/news search
  cascade (SearXNG, Brave, Tavily, Serper, Google PSE, DuckDuckGo) plus an
  SSRF-hardened page-content fetcher with a bounded in-process TTL cache.
  Salvaged + adapted (MIT) from odysseus `services/search/{providers,content}.py`;
  odysseus-specific coupling (its settings/cache/analytics/constants modules)
  stripped and replaced with `config.settings`, stdlib `logging`, and a
  max-size+TTL cache per CLAUDE.md.
- **`app/web_search.py` rewired.** `search()` and `news_search()` now route
  through the cascade. The historical return contracts are preserved exactly
  (`search` → `{title,href,body}`; `news_search` → `{title,url,publisher,date}`).
  `news_search` still tries DDG news first (richest publisher/date fields) and
  only falls back to the cascade's news category on empty/rate-limit.
- **New opt-in APIs** `web_search.fetch_source(url)` and
  `web_search.search_with_sources(query, max_fetch)` — close the "snippets only"
  gap by reading full article text. No existing caller uses them, so no current
  flow changes latency.
- **Config** (`app/config.py`): `search_provider_order` (default keeps DDG as the
  backstop), `searxng_url`, `brave_api_key`, `tavily_api_key`, `serper_api_key`,
  `google_pse_key`, `google_pse_cx`, `search_request_timeout`,
  `search_content_cache_ttl_sec`. **All keyed providers self-skip when unset**, so
  with no config the effective provider is DuckDuckGo — identical to before.
- **requirements.txt:** declared `beautifulsoup4>=4.12` (already in chili-env;
  now used on the core fetch path). `pdfminer.six` left optional (PDF extraction
  degrades to "unavailable" when absent — guarded import).
- **Backlog:** `docs/STRATEGY/QUEUED/f-odysseus-salvage-backlog.md` — the
  remaining ranked salvage candidates (P1 wire-source-into-research, P2 visual
  report, P3 MCP client, P4 teacher-skill escalation) + explicit rejections.

Files touched: 4 modified (`app/config.py`, `app/web_search.py`,
`requirements.txt`, `tests/test_web_search.py`), 3 added
(`app/search_providers.py`, `tests/test_search_providers.py`, the backlog doc).
Migrations added: none (no schema change).

## Verification

- **`tests/test_search_providers.py` (new, 30+ cases):** provider order honors
  config and always backstops to DDG; keyed providers self-skip without keys (no
  network); first-non-empty-wins; failures fall through and never raise; SSRF
  guard blocks private/loopback/link-local/cloud-metadata/non-http URLs and
  re-validates every redirect hop; content fetcher extracts title+text, honors
  `max_chars`, caches with a hard max size. **All pass.**
- **`tests/test_web_search.py` (existing, updated):** `TestSearch` re-pointed to
  the new `resilient_search` seam, contract assertions preserved; intent /
  extraction / NLU / planner-schema tests unchanged. **All pass.**
- Combined: **64 passed**.
- **Consumer regression:** `test_reasoning_web_research_mechanics.py`,
  `test_research_integrity.py`, `test_phase2_research_hygiene.py` — **21 passed**.
- Total **85 passed, 0 failed**.

## Surprises / deviations

- The new tests caught a real defect on first run: `resilient_search` dispatched
  through a dict of *frozen function references* captured at import, so providers
  were un-monkeypatchable and the live DDG call hit the network during tests
  (returned real Wikipedia results for the query "q"). Fixed by resolving
  provider functions dynamically by name via `globals()` — also makes providers
  patchable at runtime. Worth keeping in mind as the intended dispatch style.
- `conda run -n chili-env` crashes on this machine (NotImplementedError on
  newline args, and an unrelated plugin crash). Ran the env interpreter directly
  at `C:/Users/rindo/miniconda3/envs/chili-env/python.exe` instead.

## Deferred

- Did **not** wire source-content fetching into the research consumers — that is
  P1 in the backlog (flag-gated, default off). Kept this commit to the search
  backend + fetcher per the selected scope ("one logical change").
- Did not salvage visual report / MCP client / teacher-escalation — backlogged.

## Open questions for Cowork

1. Approve P1 (wire `search_with_sources` into `reasoning_brain` /
   `project_brain` behind `search_fetch_sources`, default off)? It activates
   value already shipped for near-zero cost.
2. Provider preference: if the operator wants resilience live now, which key to
   provision first — Brave (cheap, good freshness) or a self-hosted SearXNG (no
   per-query cost)? Until a key/URL is set, behavior is unchanged (DDG only).
3. Should `beautifulsoup4` also be promoted from "transitively present" to a
   pinned version, consistent with the rest of requirements.txt?
