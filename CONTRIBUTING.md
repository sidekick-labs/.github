# Contributing to sidekick-labs

Welcome — this file is the default `CONTRIBUTING.md` for every repository in the
[sidekick-labs](https://github.com/sidekick-labs) organization (it inherits from
this `.github` repo). Individual repos may override it.

## Getting set up

Most repos use git worktrees + devcontainers for development. The repo's own
`CLAUDE.md` (or `README.md`) has the specifics. The pattern across the org:

```bash
git worktree add .worktrees/<feature-name> -b <branch-name> origin/main
cd .worktrees/<feature-name>
# work, commit, push
```

## Branching

- Branch off `origin/main`. Use a descriptive name — Linear ticket ID if available
  (`<initials>/<TICKET>-<slug>`), or a task slug (`fix/<slug>`, `chore/<slug>`,
  `feat/<slug>`).
- One PR per concern. If your branch grows too big to review, split it.

## Commits

- **Signed commits are required on `main`.** Branch protection rejects unsigned
  commits at push time. Configure GPG or SSH signing once and forget about it —
  see your repo's `CLAUDE.md` for the signing setup it expects.
- Conventional-commit prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `test:`,
  `refactor:`, `ci:`) are encouraged but not strictly enforced.
- Co-author tags (`Co-Authored-By:`) are welcome — many repos in this org use
  AI-pair-programming and surface the assist via co-authorship.

## Pull requests

- Open against `main` (default branch). Branch protection requires:
  - All required CI checks green (varies per repo — see the repo's `.github/workflows/`)
  - Signed commits on every commit in the branch
- The Claude Code Review action will leave automated feedback on your PR. Address
  blocking issues; nits are optional.
- Force-pushes to feature branches are fine. Force-pushes to `main` are blocked.
- After merge, the head branch is auto-deleted.

## Testing

Each repo has its own test suite — see the repo's `CLAUDE.md` or `README.md`.
The org-wide convention is that PRs run lint + tests via GitHub Actions; these
checks must be green for branch protection to allow merge.

## Deploying

Production deploys run through the shared
[`deploy-production.yml`](https://github.com/sidekick-labs/.github/blob/main/.github/workflows/deploy-production.yml)
workflow. Each app has its own caller config that wires DigitalOcean App
Platform. See the per-repo deployment runbook in `docs/` if present.

## Security

If you discover a security vulnerability, see [SECURITY.md](SECURITY.md) — please
do not open public issues for vulnerabilities.

## Questions?

Open an issue on the relevant repo, or `@`-mention `@jaryl` in a PR comment.
