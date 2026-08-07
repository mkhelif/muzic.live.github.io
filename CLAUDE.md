# CLAUDE.md

Guidance for working in this repository. Read this before editing content or templates.

## Project

**Muzic.Live** (`muzic.live`) is a French-language webzine covering live music: news, concert reviews (chroniques), album reviews, festivals, venues and artists. It is a **Hugo** static site deployed to **GitHub Pages**. All published content is in **French**.

## Tech stack

- **Hugo extended** `0.163.2` (uses the modern `layouts/` template system: `page.html`, `section.html`, `term.html`, `taxonomy.html`, `_partials/`, `_shortcodes/`, `_markup/`).
- **Dart Sass** for SCSS (`assets/scss/`), **Bootstrap 5.3.8** and **Font Awesome 7** vendored under `assets/scss/` and `assets/js/`.
- **Pagefind** `1.5.2` for client-side search (built against `public/` after the Hugo build).
- The **site** has no `package.json`; its JS deps are vendored and Pagefind runs via `npx`. The only npm
  project is `worker/` (TypeScript, wrangler) — keep it that way.

## Build, deploy & local dev

- CI: `.github/workflows/hugo.yml` builds on push to `main`, on manual dispatch, and **daily at 02:00 Europe/Paris**. Build command: `hugo --minify --baseURL "https://muzic.live/" --buildFuture`, then `npx pagefind@1.5.2 --site public`, then deploy to Pages.
- `--buildFuture` is significant: **future-dated content is published**. Events and reviews dated in the future (e.g. upcoming festivals) will appear on the live site.
- Local preview: `hugo server`. Note: `hugo server` does **not** produce the Pagefind index, so search returns nothing locally unless you run `hugo` then `npx pagefind --site public` and serve `public/`.
- Do **not** commit or push unless explicitly asked.

## Repository layout

```
content/        Markdown content, one folder per section (see Content model)
layouts/        Hugo templates
  page.html, section.html, term.html, taxonomy.html, 404.html, home.html, baseof.html
  _partials/    Per-section partials (artists/, albums/, festivals/, venues/, events/, news/, reports/, home/, social/, format/, calendar/, seo/)
  _shortcodes/  carousel.html (image grid + lightbox), youtube.html
  _markup/      render-link, render-heading, render-blockquote hooks
assets/         scss/ (main.scss + partials), js/ (search.js, gallery.js, utilities.js + vendored libs), authors.json, fonts/
static/         Static passthrough (logos, favicon, cover-empty.jpg, icons/)
hugo.toml       Site config
public/         Build output (generated; also holds the committed Pagefind index)
```

## Content model

Content lives under `content/<section>/`. Sizes are large (~2.9k artists, ~3.6k venues, ~3.7k events), so **restrict globs** when scanning (e.g. `content/artists/*/index.md`) — a full `content/**` scan can time out.

### The UUID cross-reference system (core concept)

Entities reference each other by a stable **`id` (UUID)**, never by path or slug. Each artist/album/festival/venue fiche declares `id: "<uuid>"` in its front matter. Other content points to it by that UUID (e.g. an event's `artists:` list, a report's `venue:`).

Resolution happens through cached lookup partials:

- `layouts/_partials/lookup.html` builds a single cached map `section → id → Page` over `site.Pages` for sections `artists`, `venues`, `festivals`, `albums`.
- Per-section resolvers `layouts/_partials/<section>/lookup.html` return the page for an id, e.g. `{{ partial "artists/lookup.html" $someUuid }}` → the artist Page (or nil). O(1).
- `layouts/_partials/artists/member-of-lookup.html` is a cached reverse index: person id → the bands they are/were a member of.

**Taxonomies mirror sections.** `hugo.toml` defines taxonomies `album/artist/festival/venue/new` whose terms are the same UUIDs. This means a page can render both as a section single (`page.html`) and as a taxonomy term (`term.html`). Guard template code that reads `.Params.id` on term pages — on a taxonomy term `.Params.id` is not a string, so string-keyed lookups must be guarded (see `member-of.html` / `reports.html`).

### Front matter by type

Prose in content files is **hard-wrapped (~120 cols)**. A linter/formatter frequently reruns and may reorder keys or reflow — re-read a file right before editing, and don't fight the formatter.

**Artist** — `content/artists/<slug>/index.md`
```yaml
id: "<uuid>"
title: "<Name>"
socials: { facebook, instagram, tiktok, x, youtube, web, email, spotify, deezer, apple, tidal, ... }
members:                     # optional — only for bands
  - id: "<person-uuid>"
    roles: [sing, guitar, bass, drums, keys, other]   # always an array
    periods: # ordered from most recent to older entries
      - start: <year>
        end: <year>          # omit for current members
todo: [ ... ]
```

**Album (review)** — `content/albums/<artist-slug>/<album-slug>/index.md`
```yaml
id, date, title, subtitle: "N titres, MM:SS", author, rank, artists: ["<uuid>"], socials
```

**Event** — `content/events/YYYY/MM/DD/<slug>.md` (flat files, **not** page bundles; usually body-less)
```yaml
date, festival: "<uuid>"(optional), venue: "<uuid>", artists: ["<uuid>", ...], tickets:{web}(opt), full:(opt)
```
A festival "day" is one event file (e.g. `aluna-festival.md`) listing that day's whole lineup, tied to the festival + its venue.

**Report (chronique)** — `content/reports/YYYY/MM/DD-<slug>/index.md`
```yaml
date, festival(opt), venue: "<uuid>", title, cover, author, rank, artists: ["<uuid>", ...]
```

**News (actualité)** — `content/news/YYYY/MM/DD-<slug>/index.md`
```yaml
date, title, category, artists: ["<uuid>"], festival|album|venue (optional)
```
`category` must be one of the values handled by `_partials/news/category.html` / `category-badge.html`: `album, concert, event, festival, metal, tour` (unknown values render an empty badge). A band breakup / generic item → `event`.

**Festival** — `content/festivals/<slug>/index.md`: `id, title, socials`, then a detailed French description.

**Venue** — `content/venues/<country>/<city>/<slug>/index.md`: `id`, optional parent `venue: "<uuid>"`, `coordinates:{latitude,longitude,zoom}`, `socials`.

- `rank` is on a **/10 scale**, rendered as 5 stars (`_partials/rank.html` divides by 2; supports halves).
- `author` keys map to `assets/authors.json` (e.g. `mkhelif`).

## Search

Custom full-screen modal driven by Pagefind.

- Trigger: `#search-open` button in `_partials/header.html`. Modal markup: `_partials/search-results.html` (`#search-blackbox` overlay + `#search-modal` dialog), included once from `baseof.html`.
- Logic: `assets/js/search.js` — opens the modal, live-queries Pagefind, and **groups results by section**. `CATEGORIES` is an object `{ sectionKey: "Label" }` (e.g. `artists → "Artistes"`); iterate with `Object.entries`/`Object.keys`, not array methods.
- Grouping relies on `page.html` emitting `data-pagefind-filter="section:{{ .Section }}"`. In JS, read the facet as `result.data().filters.section[0]`. **Grouping only works after a fresh Pagefind index build.**
- Styling is Bootstrap utility classes + the theme's `primary` color; keep custom CSS in `_search.scss` minimal.

## Styling

- Tokens in `assets/scss/_variables.scss`: `$primary: #c5f853` (lime), `$dark: #0b0b0b`, dark theme. Fonts: Noto Sans (body), Londrina Solid (display), Space Mono (mono) — `_fonts.scss`.
- Prefer **Bootstrap utility classes** over new custom classes; use `text-primary`/`border-primary`/`bg-primary bg-opacity-10` etc. rather than hard-coding the lime hex.
- `main.scss` imports the component partials; `data-pagefind-body` is on `<main>` in `baseof.html`, and list/section/home bodies are wrapped in `data-pagefind-ignore="all"` so only single content pages get indexed.

## Conventions for content work

- **Accuracy first.** Never fabricate members, roles, dates, tracklists, or facts. Verify via web search / official sources before writing reviews, news, or lineups. Prefer year-level precision over false precision.
- **When creating artist fiches: do not invent socials and do not write a description.** Create a minimal fiche (`id`, `title`, `socials: { facebook: "", instagram: "", ... }`, `todo:` with "Add description/picture/socials"). Descriptions and socials are filled later (a daily Cowork task enriches artist descriptions).
- Do not reproduce copyrighted material (lyrics, full articles). Summarize and cite.
- Cite sources when content is based on the web (the site's editorial process expects verifiable facts).
- Generate a fresh UUID (`python3 -c "import uuid;print(uuid.uuid4())"`) for every new entity; reference existing entities by their existing UUID (grep `content/<section>` by title first to avoid duplicates).
- After creating/editing content, validate: YAML parses, and every referenced UUID (`artists`, `venue`, `festival`, `album`) resolves to an existing fiche.

*Full Hugo builds can't run in every environment; validate SCSS with `dart-sass`/`npx sass` against the Bootstrap load path, and JS with `node --check`.*
