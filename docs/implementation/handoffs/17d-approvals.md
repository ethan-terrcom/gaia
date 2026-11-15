# Handoff: 17d — Approvals inbox

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no installs. You can
run `npm run …` with `cwd: "frontend"`. The lead commits; your report goes through `finish`.

You are a junior engineer on **Gaia**. Build exactly one frontend view, **17d: Approvals inbox**, at
`/approvals`. It replaces the placeholder in `src/pages/Approvals.tsx`. Read `docs/frontend/README.md` and
the two finished views (`src/views/pull-requests/`, `src/views/worktrees/`) first: copy their
patterns, including passing the current global `fetch` per request (recorded in doc 17).

## Common rules for every 17x view (read before the view-specific notes)
- **Read first:**
  - `docs/implementation/17-frontend-views.md` ("Shared rules");
  - `docs/frontend/README.md` (the whole guide, especially "Shared building blocks");
  - `frontend/src/{App,routes}.tsx`, `src/layout/AppLayout.tsx`, `src/api/{client,errors,queryClient}.ts`,
    `src/hooks/useEventStream.ts` (the `INVALIDATIONS` map), `src/components/*`;
  - the generated `src/api/schema.d.ts` for the exact request and response types;
  - `docs/reviews/2026-11-14-review-16.md`.
- **Data:** React Query (`useQuery`, `useMutation`, `invalidateQueries`) over the typed `api` client
  (`api.GET('/api/v1/…', { params: { query: … } })`). Unwrap `{data}` and throw on error, the way
  `fetchPendingApprovals` does, so React Query sees failures. **No hand-written fetch**, no
  absolute URLs.
- **Query keys** must match `INVALIDATIONS`, so live events refresh the view: `['pull-requests']`,
  `['worktrees']`, `['runs']`, `['approvals']`, `['costs']`. Use e.g. `['pull-requests', {state}]`
  for a filtered variant; the prefix still matches.
- **States:** every view handles loading (`LoadingState`), error (`ErrorState` with retry) and empty
  (`EmptyState`, with a sentence on what to do next). Required by doc 17.
- **Untrusted text** (ticket titles, branch names, paths, anything from a repo or an agent) is
  rendered as **plain text**: no `dangerouslySetInnerHTML` and no Markdown in these views.
- **Destructive actions** always go through `ConfirmDialog`.
- **Links out** to GitHub use `target="_blank" rel="noreferrer noopener"`, and only for URLs that
  start with `https://github.com/` (check before rendering an `<a>`).
- **Accessibility:** tables use `<table>`, `<th scope="col">`; buttons are `<button type="button">`;
  every form control has a label.
- **Tests (Vitest + Testing Library)** stub `fetch` (see `src/api/client.test.ts` for the pattern) and
  wrap renders in a `QueryClientProvider` with `makeQueryClient()` and a `MemoryRouter`. Cover the
  three states, the main interaction, and the rendered data.
- **You may touch:** your view's page file in `src/pages/`, new files under `src/views/<view>/`, and
  `src/routes.tsx` (only to point the route at your page). **You must not touch:** `src/components/`,
  `src/api/`, `src/hooks/`, `src/layout/`, `package.json`, the lockfiles, `backend/`, or other views'
  files. Another junior builds another view in parallel; if you need a new shared component, write
  it inside your view folder and mention it in the report.
- **Checks** (`cwd: "frontend"`): `npm run format`, then `npm run lint` (warnings are errors),
  `npm run typecheck`, `npm run test`, `npm run build`. All must pass.
- **Report** (pass to `finish`): Summary · Files changed · Deviations · Tests added · Open
  questions · Final check output.


## The view: where the human decides
- **Data:** `GET /api/v1/approvals?status=pending` (oldest first), query key
  `['approvals', {status:'pending'}]`. It is invalidated live by `approval.*`. A toggle shows
  recent decided ones (`status` omitted, limit 50).
- **Layout:** a list on the left (kind badge, the run's ticket if present in the payload, age) and
  the selected approval's detail on the right. **Keyboard:** `j`/`k` move the selection, `a`
  approves, `r` rejects. **Merge approvals ignore `a`**, because merge always needs typing (doc
  17). The keys do nothing while focus is in an input or textarea; reuse the guard logic from
  `src/hooks/useHotkeys.ts`, but implement these keys locally in the view.
- **Detail per kind** (read `payload_json`):
  - **`plan`:** the summary, the risks, and the steps (title, instructions, files, acceptance,
    test command). **Edit mode:** an editable form of the steps (title, instructions and test
    command as one line split to argv), validated client-side with **zod** against the same shape
    as the backend `Plan` (1–10 steps, non-empty titles). Then "Approve with edits" →
    `decision: "edit"` with `edited_payload` = the plan object. Show a 422's message inline.
  - **`command`:** the exact `argv` (each element in its own `<code>`, so spaces are visible),
    `cwd`, `tool` and the `reason`, prominently, with the warning text "This command runs inside
    the sandbox with your approval."
  - **`pr_open`:** the review summary and the diff summary from the payload (read the
    `approve_pr` node for its keys). A "View diff" link goes to `/runs/<run_id>`, the diff tab.
  - **`merge`:** the PR number and link. Approve opens a `ConfirmDialog` with
    `requireText={"merge " + pr_number}` and sends `confirm: "merge <n>"`.
  - **`budget`:** the spent and budget figures, and an optional new budget input (`budget_usd`).
  - **`escalation`:** the reason and feedback, with approve (retry) and reject (stop).
- **Deciding:** `POST /api/v1/approvals/{id}/decide {decision, comment?, edited_payload?,
  confirm?, budget_usd?}`. There is an optional comment textarea for every kind. On 200 or 202,
  invalidate `['approvals']` and `['runs']`, and move the selection to the next item. Show a 409
  (already decided, or the run is busy) or a 422 message inline.
- All payload text is untrusted model output, so render it as **plain text**.

## Tests
- Each kind's detail renders its fields.
- The plan edit validation (an empty title is blocked; 11 steps are blocked) and the edit POST
  body.
- A command shows each argv element.
- A merge needs the typed phrase and sends `confirm`; `a` does nothing on a merge.
- `j`/`k` selection and `a`/`r` sending the right decision; hotkeys are ignored inside a textarea.
- The 409 and 422 messages; the empty inbox ("Nothing needs you").

The lead adds a Playwright smoke test afterwards. Use stable `data-testid`s (`approval-list`,
`approval-detail`, `approve-button`, `reject-button`).
