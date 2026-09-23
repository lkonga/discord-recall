# Discord Recall — web UI

React 19 + Vite + TypeScript + Tailwind v4 + shadcn/ui front end for Discord Recall.

The whole point of this UI is a **two-level picker that never asks for an id**: pick a
Discord **server**, pick a **channel** from that server, look at that channel's **per-day
message activity**, choose a date range that actually contains messages, then **Capture**
and **Generate digest**, and finally **read the digests**.

## Flow

1. **Servers sidebar** (`src/components/servers-sidebar.tsx`) — every server from
   `GET /api/servers`, grouped and foldable by capture state (has captured messages /
   channels known but nothing captured / no channels stored). Each row shows channel,
   captured and digest counts as `Badge`s, plus a filter box and a total line.
2. **Channel combobox** (`src/components/channel-combobox.tsx`) — a searchable
   `Popover` + `Command` (cmdk) over `GET /api/channels?server=<id>&q=<text>`. Channels
   with captured messages are listed before empty ones. Search text is sent to the API
   (debounced), so nothing has to be preloaded.
3. **Activity bars** (`src/components/activity-chart.tsx`) — `GET /api/channel/<id>/activity?days=N`
   rendered as one bar per calendar day (missing days are drawn as faint stubs). Clicking
   a day starts a range, clicking a second day closes it; the panel reports how many
   captured messages that window holds, so a useless range is obvious before you run
   anything. Windows: 30 / 90 / 180 / 365 days / 5 years.
4. **Range & run** (`src/components/run-panel.tsx`) — from/to calendars, quick ranges
   built from real activity ("last 7 active days", "last 30 active days", "whole captured
   span", "today"), digest period, a `force` toggle, max-messages for capture, and the
   `Capture messages` / `Generate digest` buttons with spinners and sonner toasts that
   show the `status` string returned by the API.
5. **Digests** (`src/components/digest-browser.tsx`) — `GET /api/channel/<id>/digests`,
   newest first, rendered as markdown (`react-markdown` + `remark-gfm`) in cards with an
   `Index` table tab and a period filter. Long digests are collapsed to a paragraph-safe
   preview.
6. **Ask** (`src/components/ask-card.tsx`) — `POST /api/ask`, either scoped to the
   selected channel or to everything captured.
7. **Refresh channel list from Discord** (header) — `POST /api/discover`, then reloads
   the server and channel lists.

Dark theme is the default (`<html class="dark">`); `sonner` toasts are rendered with
`theme="dark"`.

## API client

`src/lib/api.ts` is the only module that talks to the backend. It is typed end to end and
defensive about the responses:

- all ids are `string` (Discord snowflakes exceed `Number.MAX_SAFE_INTEGER`, so they are
  never parsed as numbers anywhere in the app),
- relative `/api/...` URLs, so the built bundle works same-origin behind the FastAPI app,
- `GET /api/channels` is called with `server` and `q`; `POST /api/digest` sends the
  `from` key (not `start`), because that is what the endpoint accepts,
- every response is normalised (`asList`/`asId`/`asNumber`/`asNullableString`) so a list
  wrapped in an object, a numeric id or a missing field degrades instead of crashing,
- failures raise `ApiError` with `status` and `detail`, which the UI surfaces as a toast.

## Scripts

```bash
npm install
npm run dev            # Vite dev server, /api proxied to VITE_API_PROXY_TARGET (default http://127.0.0.1:8000)
npm run build          # tsc -b && vite build -> dist/ with base "./"
npm run preview        # serve dist/ with the same /api proxy
npm run lint           # eslint . --max-warnings 0
npm run format         # prettier --write .
npm run format:check   # prettier --check .
npm run typecheck      # tsc -b
```

## Deployment

`npm run build` emits `web/dist`. The Discord Recall FastAPI app mounts `web/dist`
(`WEB_DIST` env var overrides the location) and serves it as static files, so
`npm run build` is enough for the deployed container image.

Note: the legacy server-rendered pages still own the `/` route, so the built SPA is
served at `/index.html` (and any unmatched path) until that route is retired. See the
"API contract notes" section of the hand-off report for the exact behaviour observed.

## Notes on lint configuration

ESLint 10 flat config (`eslint.config.js`) with `typescript-eslint`, `react-hooks`
(rules-of-hooks + exhaustive-deps) and `react-refresh`, plus Prettier. The React Compiler
enforcement rules that ship with `eslint-plugin-react-hooks` v7 are intentionally not
enabled: they reject the standard async-fetch-in-effect pattern this app uses. The
vendored shadcn primitives under `src/components/ui/` keep the `react-refresh`
only-export-components rule disabled; one unused `React` import was dropped from
`scroll-area.tsx` so `tsc -b` (with `noUnusedLocals`) stays clean.
