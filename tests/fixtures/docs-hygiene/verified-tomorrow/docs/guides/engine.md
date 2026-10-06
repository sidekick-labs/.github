---
description: How the engine turns input into output.
type: guide
status: current
covers:
  [
    src/core/**,
    src/lib/util.js, # trailing comment
  ]
verified: {{UTC_TODAY+1}}
---

# Engine

Code: `src/core/engine.js:12`, a glob `src/**/*.js`, the module dir `src/lib/`,
an extensionless module `src/lib/util`, and a relative link to the
[reference](../reference.md#section). Build output lands in `dist/bundle.js`
(gitignored), env in `.env.local`. Gradle-style shorthand: `app/di/AppModule.kt`.
The skill is at `.claude/skills/ship/SKILL.md` (a symlinked dir).

Either `src/core/engine.js` or `src/core/engine.ts` works.
The engine is called by harness `src/pipeline/caller.ts` in another repo.
We no longer ship `src/legacy/old.js`; it was removed.
The template is `drizzle/meta/NNNN_snapshot.json`. Commands: `npm run build`,
MIME `application/json`, repo `sidekick-labs/octo-brain`, route `/api/v1/things`,
sibling `sidekick-web/app/models/user.rb`, `../other-repo/README.md`.
Not a path: `src/runtime/only.pem` <!-- docs-hygiene-ignore -->
External: [site](https://example.com) and [anchor](#engine).
Pins live in `config/attestation_roots/*.pem` (repo allowlist).
The old resolver `src/core/claim_resolver.rb` is named on purpose.
<!-- docs-hygiene: allow-missing src/core/claim_resolver.rb -->
