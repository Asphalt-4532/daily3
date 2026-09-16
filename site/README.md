# Static showcase (`site/`)

One self-contained `index.html` - no build step, no dependencies beyond the
Google Fonts link. It shows the interface and the rules the app enforces; it
is **not** the running app and talks to no backend.

## Deploy on Netlify

**Drag and drop:** open Netlify -> Sites -> drag this `site/` folder onto the
drop zone.

**From the repo:** point Netlify at the repository root and leave the build
command empty - `netlify.toml` already sets `publish = "site"`.

## Editing

Company name, address and the sample figures are hardcoded in `index.html`.
Search for `Al Manara Industries` to change them. Screenshots can go beside
this file and be referenced relatively (`<img src="screenshot-dashboard.png">`).
