# Handoff: 17f — Worktrees & repositories view

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no installs. You can
run `npm run …` with `cwd: "frontend"`. The lead commits; your report goes through `finish`.

You are a junior engineer on **Gaia**. Build exactly one frontend view, **17f: Worktrees &
repositories**, at `/worktrees`. It replaces the placeholder in `src/pages/Worktrees.tsx`.

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


## The view
Two sections on one page.

**Repositories**
- `GET /api/v1/repositories` → `RepositoryRead[]`
  (`id, name, local_path, remote_url, default_branch, setup_command, test_command, …`). Query key
  `['repositories']`.
- Show each repo as a card or row: the name, the local path in monospace, the remote URL as text,
  the default branch, and the test and setup commands. Those are argv lists: render them joined by
  spaces, in `<code>`.
- **Register repo form**, which `POST /api/v1/repositories` with `RepositoryCreate{name,
  local_path, test_command?, setup_command?}`.
  - The command inputs are single-line text, split on whitespace into an argv list. Say so in the
    field's help text ("no shell: `uv run pytest -q`, not `cd x && …`").
  - Empty means `null`.
  - On a 4xx, show the `ApiError` message inline, under the form. On success, reset the form and
    invalidate `['repositories']`.
- **Unregister:** `DELETE /api/v1/repositories/{repository_id}`, behind a `ConfirmDialog`. A **409**
  (the repo has tickets or live worktrees) is shown as the API's message. It is not a crash.

**Worktrees**
- `GET /api/v1/worktrees?repo_id=` → `WorktreeListRead{items: WorktreeItemRead[],
  unknown_paths: string[]}`. Each item is `{id, repo_id, branch, path, status, base_sha, dirty,
  exists, orphan, tampered, created_at, …}`. Query key `['worktrees', {repoId}]`.
- **A repo filter:** a `<select>` of repositories, with "All" meaning no `repo_id`.
- **Table:**
  - the branch (monospace);
  - the status (`StatusBadge`);
  - flags shown as small badges: **dirty** (amber), **orphan** (row says it exists but git doesn't
    know it, or the reverse; read what the flags mean in `backend/gaia/services/worktrees.py`,
    `WorktreeItem`), **missing** (`exists=false`), **tampered** (red);
  - the base SHA (`shortSha`);
  - the age;
  - the path, with a "Copy path" button (`navigator.clipboard.writeText`; guard for its absence)
    and an "Open in VS Code" link (`vscode://file/<path>`, with the path URI-encoded per segment).
- **Remove:** `DELETE /api/v1/worktrees/{worktree_id}?force=` behind a `ConfirmDialog`.
  - If the worktree is **dirty**, the dialog warns that uncommitted changes will be lost, sends
    `force=true`, and requires typing the branch name (`requireText={branch}`).
  - Not dirty → no typing, `force=false`.
  - Refused (409 or 422) → show the API's message.
  - Invalidate `['worktrees']` after.
- `unknown_paths` (git knows them, Gaia has no row) are listed under the table as plain text, with
  "Gaia did not create these; remove them with git if they are yours."

## Tests (`src/views/worktrees/*.test.tsx`)
- the repo list renders, with commands joined;
- the register form posts the split argv, shows an API error inline, and clears on success;
- unregister → confirm → DELETE, and a 409 message is shown;
- the worktree table shows the flags;
- removing a dirty worktree requires typing the branch and sends `force=true`;
- a clean worktree sends `force=false`;
- the repo filter sets `repo_id`;
- the loading, error and empty states for both sections.
