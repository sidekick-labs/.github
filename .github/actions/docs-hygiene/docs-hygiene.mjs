#!/usr/bin/env node
// docs-hygiene: the enforcement half of the docs-shape convention.
//
// Why this exists
// ---------------
// Agents follow docs. The 2026-10-06 docs audit (sidekick-labs/octo-brain#573)
// found live guidance pointing agents at a superseded API contract, a
// nonexistent deploy workflow and code paths that had moved months earlier;
// 28 orphan docs nobody linked; and 0 of 122 docs carrying any machine-readable
// status. Docs-only PRs also skip Claude review, which the
// mechanical-pr-review-skip convention allows only when a deterministic gate
// gives the same guarantee. This is that gate.
//
// Spec: rule 5 ("Enforcement") of
// https://github.com/sidekick-labs/octo-brain/blob/main/.claude/conventions/docs-shape.md
// with rule 2 as the frontmatter schema.
//
// Checks (each finding carries one of these `check` names)
// --------------------------------------------------------
//   links        broken relative markdown links, and backticked repo-relative
//                paths, in CLAUDE.md, AGENTS.md, docs/**/*.md, .claude/**/*.md
//                and .agents/**/*.md
//   frontmatter  docs/**/*.md (README.md index files excepted) must carry
//                description/type/status/covers/verified with valid values;
//                every `covers` glob of a `current` doc must match >= 1 file
//   index        the always-loaded index (CLAUDE.md, or AGENTS.md when CLAUDE.md
//                is just `@AGENTS.md`) is <= 200 lines and has a
//                `| Doc | Read when |` table, or a bullet list under a heading
//                matching /read when relevant/i (the compact form)
//   orphans      every living docs/**/*.md is reachable by links (or backticked
//                paths) from CLAUDE.md / AGENTS.md / docs/README.md, transitively
//                through docs
//   drift        (--drift only) docs whose `covers` paths gained commits after
//                their `verified` date. Report-only: never fails.
//
// What the backticked-path heuristic treats as a path
// ---------------------------------------------------
// Inline code is mostly NOT a path (commands, identifiers, env vars, MIME types,
// GitHub `owner/repo` refs, API routes). A span is checked only when it has no
// whitespace or placeholder syntax, contains a `/`, is not absolute / home /
// URL / `../`-escaping / a sibling-repo reference, and its first segment is an
// existing top-level entry of the repo (or a directory beside the doc). An
// extension alone is NOT enough: in calibration those were device paths, other
// repos' files and abbreviations. It is skipped when the line, or the line
// before it, names another repo or is past-tense/negated ("removed", "not
// under"), when the doc is point-in-time (adr/report/archived/superseded), or
// when the line carries `<!-- docs-hygiene-ignore -->`, or when it is allowed by
// a doc's `<!-- docs-hygiene: allow-missing a/b c/ -->` marker or the repo's
// `.docs-hygiene.yml` `allow-missing:` list (intentionally missing paths). It resolves if it exists
// (through tracked dir symlinks too) relative to the repo root or the doc, as a
// suffix of any tracked path, as a module-rooted abbreviation (`app/di/X.kt` for
// `app/src/main/java/.../di/X.kt`), as an extensionless module (`src/lib/util`),
// or if it is gitignored (a local/generated artefact). Globs must match >= 1
// file or directory. Calibrated against 11 sidekick-labs code repos; see the PR
// that added this for the counts and the residual false positives.
//
// Exit codes
// ----------
//   0  clean (or findings in --warn-only / --drift mode)
//   1  findings, blocking mode
//   2  usage error
//   3  could not tell: root missing/unreadable, not a git work tree, git failed,
//      or (--drift) a shallow clone whose history cannot answer the question.
//      An unreadable probe is UNKNOWN, never a clean bill of health.
//
// Positive control
// ----------------
// `--self-test` runs every fixture under tests/fixtures/docs-hygiene/ (each a
// tiny repo, materialised as a throwaway git repo) and asserts the EXACT set of
// findings in its expected.json: `clean` (and any fixture marked
// `"negative": true`) must produce none, every bad fixture
// exactly its own. A check that has lost the ability to go red fails there.
// https://github.com/sidekick-labs/octo-brain/blob/main/.claude/conventions/check-positive-controls.md
//
// Zero dependencies: node:fs, node:path, node:child_process, node:os, node:url.
//
// Usage
// -----
//   node docs-hygiene.mjs [--root DIR] [--warn-only] [--json]
//   node docs-hygiene.mjs --drift [--root DIR] [--json] [--json-out FILE]
//   node docs-hygiene.mjs --self-test [--fixtures DIR]
//   node docs-hygiene.mjs --materialise FIXTURE --out DIR   (test helper)

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const MAX_INDEX_LINES = 200;
export const TYPES = ['guide', 'reference', 'adr', 'runbook', 'report'];
export const STATUSES = ['current', 'proposed', 'superseded', 'archived'];
export const REQUIRED_KEYS = ['description', 'type', 'status', 'covers', 'verified'];

class Unknown extends Error {}

// ---------------------------------------------------------------- git / files

function git(root, args, { allowFail = false } = {}) {
  const r = spawnSync('git', ['-C', root, ...args], { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 });
  if (r.error || (r.status !== 0 && !allowFail)) {
    throw new Unknown(`git ${args.join(' ')} failed: ${r.error?.message || r.stderr.trim()}`);
  }
  return r;
}

function assertRepoRoot(root) {
  let st;
  try {
    st = fs.statSync(root);
  } catch {
    throw new Unknown(`root ${root} does not exist or is unreadable`);
  }
  if (!st.isDirectory()) throw new Unknown(`root ${root} is not a directory`);
  const r = git(root, ['rev-parse', '--show-toplevel'], { allowFail: true });
  if (r.status !== 0) throw new Unknown(`${root} is not inside a git work tree`);
  if (fs.realpathSync(r.stdout.trim()) !== fs.realpathSync(root)) {
    throw new Unknown(`${root} is not the top level of its git work tree (${r.stdout.trim()})`);
  }
}

// Tracked files plus untracked-but-not-ignored ones (so a doc added locally and
// not yet staged is still checked). Filtered to what actually exists on disk.
function listFiles(root) {
  const r = git(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard']);
  const files = [...new Set(r.stdout.split('\0').filter(Boolean))].sort();
  return files.filter((f) => {
    try {
      fs.statSync(path.join(root, f));
      return true;
    } catch {
      return false;
    }
  });
}

class Repo {
  constructor(root) {
    this.root = root;
    this.files = listFiles(root);
    this.fileSet = new Set(this.files);
    this.dirSet = new Set();
    for (const f of this.files) {
      let d = path.posix.dirname(f);
      while (d !== '.' && !this.dirSet.has(d)) {
        this.dirSet.add(d);
        d = path.posix.dirname(d);
      }
    }
    this.topLevel = new Set(this.files.map((f) => f.split('/')[0]));
    // Tracked symlinks to directories inside the repo (e.g. `.claude/skills ->
    // ../.agents/skills`) make `.claude/skills/x/SKILL.md` a real path. Alias
    // the target's files under the link so resolution sees them; the aliases
    // are NOT added to the scan scope (that would double-report).
    this.aliases = [];
    for (const f of this.files) {
      const abs = path.join(root, f);
      let target;
      try {
        if (!fs.lstatSync(abs).isSymbolicLink() || !fs.statSync(abs).isDirectory()) continue;
        target = path.relative(fs.realpathSync(root), fs.realpathSync(abs)).split(path.sep).join('/');
      } catch {
        continue;
      }
      if (target.startsWith('..')) continue;
      for (const g of this.files) if (g.startsWith(`${target}/`)) this.aliases.push(`${f}/${g.slice(target.length + 1)}`);
    }
    for (const a of this.aliases) {
      let d = path.posix.dirname(a);
      while (d !== '.' && !this.dirSet.has(d)) {
        this.dirSet.add(d);
        d = path.posix.dirname(d);
      }
    }
    this.aliasSet = new Set(this.aliases);
    this.name = repoName(root);
    this._suffix = null;
    this._text = new Map();
    this._ignored = new Map();
  }
  exists(rel) {
    const p = rel.replace(/\/+$/, '');
    return p === '' || this.fileSet.has(p) || this.dirSet.has(p) || this.aliasSet.has(p);
  }
  // Gitignored paths are local/generated artefacts (build outputs, `.env.local`,
  // credentials, `dist/`): a doc naming one is describing it, not linking it.
  isIgnored(rel) {
    if (!this._ignored.has(rel)) {
      // Also ask with a trailing slash: a `dir/` ignore pattern only matches a
      // path git knows is a directory, and a missing path gives it no way to know.
      const p = rel.replace(/\/+$/, '');
      const r = git(this.root, ['check-ignore', '--no-index', '--', p, `${p}/`], { allowFail: true });
      if (r.status > 1) throw new Unknown(`git check-ignore failed for ${p}: ${r.stderr.trim()}`);
      this._ignored.set(rel, r.status === 0);
    }
    return this._ignored.get(rel);
  }
  // `.storybook/preview` names a module; any `.storybook/preview.*` satisfies it.
  existsWithAnyExt(rel) {
    const p = `${rel}.`;
    return this.files.some((f) => f.startsWith(p) && !f.slice(p.length).includes('/'));
  }
  isDir(rel) {
    return this.dirSet.has(rel.replace(/\/+$/, ''));
  }
  hasSuffix(rel) {
    if (!this._suffix) {
      this._suffix = new Set();
      for (const p of [...this.files, ...this.aliases, ...this.dirSet]) {
        const parts = p.split('/');
        for (let i = 1; i < parts.length; i++) this._suffix.add(parts.slice(i).join('/'));
      }
    }
    return this._suffix.has(rel.replace(/\/+$/, ''));
  }
  read(rel) {
    if (!this._text.has(rel)) {
      try {
        this._text.set(rel, fs.readFileSync(path.join(this.root, rel), 'utf8'));
      } catch (e) {
        throw new Unknown(`cannot read ${rel}: ${e.message}`);
      }
    }
    return this._text.get(rel);
  }
  // Matches files AND directories: `.claude/skills/*` names the skill dirs.
  glob(pattern) {
    const re = globToRegExp(pattern);
    return [...this.files, ...this.aliases, ...this.dirSet].filter((f) => re.test(f));
  }
}

function repoName(root) {
  const r = spawnSync('git', ['-C', root, 'remote', 'get-url', 'origin'], { encoding: 'utf8' });
  const url = r.status === 0 ? r.stdout.trim() : '';
  const base = url ? url.replace(/\.git$/, '').split(/[/:]/).pop() : path.basename(root);
  return base.replace(/^sidekick-/, '');
}

// ---------------------------------------------------------------------- globs

export function isGlob(s) {
  return /[*?[{]/.test(s);
}

export function globToRegExp(glob) {
  let g = glob.replace(/^\.\//, '');
  // A bare directory (or trailing slash) covers everything beneath it.
  const dirLike = g.endsWith('/');
  if (dirLike) g += '**';
  let re = '';
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === '*') {
      if (g[i + 1] === '*') {
        i++;
        if (g[i + 1] === '/') {
          i++;
          re += '(?:.*/)?';
        } else {
          re += '.*';
        }
      } else {
        re += '[^/]*';
      }
    } else if (c === '?') {
      re += '[^/]';
    } else if (c === '{') {
      const end = g.indexOf('}', i);
      if (end === -1) {
        re += '\\{';
        continue;
      }
      const alts = g.slice(i + 1, end).split(',').map((a) => globToRegExp(a).source.slice(1, -1));
      re += `(?:${alts.join('|')})`;
      i = end;
    } else if (c === '[') {
      const end = g.indexOf(']', i + 1);
      if (end === -1) {
        re += '\\[';
        continue;
      }
      re += `[${g.slice(i + 1, end).replace(/^!/, '^').replace(/\\/g, '\\\\')}]`;
      i = end;
    } else {
      re += c.replace(/[.+^$()|\\]/g, '\\$&');
    }
  }
  // `src/pipeline` (no glob chars) should also cover files beneath it.
  if (!isGlob(glob) && !dirLike) re += '(?:/.*)?';
  return new RegExp(`^${re}$`);
}

// ---------------------------------------------------------------- frontmatter

// Parses the small YAML subset docs frontmatter uses: `key: scalar`,
// `key: [flow, list]`, `key:` + `- block` items, `key: >`/`|` block scalars,
// quoted strings and `#` comments. Anything else is kept as a raw string.
export function parseFrontmatter(text) {
  const lines = text.split(/\r?\n/);
  if (lines[0] !== '---') return null;
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i] === '---' || lines[i] === '...') {
      end = i;
      break;
    }
  }
  if (end === -1) return { error: 'frontmatter opened with `---` but never closed', data: {}, endLine: 0 };
  const data = {};
  const errors = [];
  const body = lines.slice(1, end);
  for (let i = 0; i < body.length; i++) {
    const line = body[i];
    if (/^\s*(#.*)?$/.test(line)) continue;
    const m = line.match(/^([A-Za-z0-9_-]+):(?:\s+(.*))?$/);
    if (!m) {
      if (!/^\s/.test(line)) errors.push(`unparseable line ${i + 2}: ${line}`);
      continue;
    }
    const key = m[1];
    let rawVal = stripComment(m[2] ?? '').trim();
    // A flow list may start on the next line and/or span several lines (the
    // shape Prettier writes for a long `covers:`).
    if (rawVal === '' && i + 1 < body.length && /^\s+\[/.test(body[i + 1])) {
      i++;
      rawVal = stripComment(body[i]).trim();
    }
    if (rawVal.startsWith('[')) {
      while (!rawVal.endsWith(']') && i + 1 < body.length && /^\s/.test(body[i + 1])) {
        i++;
        rawVal += ` ${stripComment(body[i]).trim()}`;
      }
    }
    if (rawVal === '') {
      const items = [];
      while (i + 1 < body.length && /^\s*-\s/.test(body[i + 1])) {
        i++;
        items.push(unquote(stripComment(body[i].replace(/^\s*-\s+/, '')).trim()));
      }
      // `key:` followed by nothing is null; followed by `- x` lines is a list.
      data[key] = items.length ? items : null;
    } else if (/^[>|][+-]?$/.test(rawVal)) {
      const parts = [];
      while (i + 1 < body.length && (/^\s+\S/.test(body[i + 1]) || body[i + 1] === '')) {
        i++;
        parts.push(body[i].trim());
      }
      data[key] = parts.join(rawVal.startsWith('>') ? ' ' : '\n').trim();
    } else if (rawVal.startsWith('[')) {
      if (!rawVal.endsWith(']')) {
        errors.push(`unterminated flow list for \`${key}\``);
        data[key] = rawVal;
      } else {
        const inner = rawVal.slice(1, -1).trim();
        data[key] = inner === '' ? [] : splitFlow(inner).map((s) => unquote(s.trim()));
      }
    } else {
      data[key] = unquote(rawVal);
    }
  }
  return { data, errors, endLine: end + 1 };
}

function stripComment(s) {
  let q = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q) {
      if (c === q) q = null;
    } else if (c === '"' || c === "'") {
      q = c;
    } else if (c === '#' && (i === 0 || /\s/.test(s[i - 1]))) {
      return s.slice(0, i);
    }
  }
  return s;
}

function splitFlow(s) {
  const out = [];
  let cur = '';
  let q = null;
  let depth = 0;
  for (const c of s) {
    if (q) {
      if (c === q) q = null;
      cur += c;
    } else if (c === '"' || c === "'") {
      q = c;
      cur += c;
    } else if (c === '{' || c === '[') {
      depth++;
      cur += c;
    } else if (c === '}' || c === ']') {
      depth--;
      cur += c;
    } else if (c === ',' && depth === 0) {
      out.push(cur);
      cur = '';
    } else {
      cur += c;
    }
  }
  if (cur.trim() !== '') out.push(cur);
  return out;
}

function unquote(s) {
  if (s.length >= 2 && ((s[0] === '"' && s.endsWith('"')) || (s[0] === "'" && s.endsWith("'")))) return s.slice(1, -1);
  return s;
}

// `verified:` may be at most this many days past the UTC date (time zones).
export const VERIFIED_TOLERANCE_DAYS = 1;

// YYYY-MM-DD of the UTC date `offsetDays` from now.
export function utcDay(offsetDays = 0, now = Date.now()) {
  return new Date(now + offsetDays * 86400000).toISOString().slice(0, 10);
}

export function isValidDate(s) {
  if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

// ------------------------------------------------------------------- markdown

// Returns the lines with fenced code blocks, HTML comments and frontmatter
// blanked out (line numbers preserved), so references are only read from prose.
export function proseLines(text) {
  const lines = text.split(/\r?\n/);
  const out = new Array(lines.length).fill('');
  let start = 0;
  if (lines[0] === '---') {
    const end = lines.findIndex((l, i) => i > 0 && (l === '---' || l === '...'));
    if (end > 0) start = end + 1;
  }
  let fence = null;
  let inComment = false;
  for (let i = start; i < lines.length; i++) {
    let line = lines[i];
    if (fence) {
      if (new RegExp(`^\\s{0,3}${fence[0]}{${fence.length},}\\s*$`).test(line)) fence = null;
      continue;
    }
    const f = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (f) {
      fence = f[1];
      continue;
    }
    // Indented code blocks are not stripped: docs nest bullets 4 deep, and the
    // false-negative cost of skipping them outweighs the rare indented sample.
    if (inComment) {
      const e = line.indexOf('-->');
      if (e === -1) continue;
      line = line.slice(e + 3);
      inComment = false;
    }
    line = line.replace(/<!--.*?-->/g, '');
    const s = line.indexOf('<!--');
    if (s !== -1) {
      line = line.slice(0, s);
      inComment = true;
    }
    out[i] = line;
  }
  return out;
}

const SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

// Markdown link / image / reference-definition targets, and `@path` imports.
export function extractLinks(line) {
  const out = [];
  const noCode = line.replace(/`[^`]*`/g, (m) => ' '.repeat(m.length));
  const inline = /!?\[(?:[^\]\\]|\\.)*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?\s*\)/g;
  let m;
  while ((m = inline.exec(noCode))) out.push({ target: m[1].replace(/^<|>$/g, ''), col: m.index + 1 });
  const def = noCode.match(/^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)/);
  if (def) out.push({ target: def[1].replace(/^<|>$/g, ''), col: 1 });
  // Claude Code `@path` imports (whole-token, path-like).
  const imp = /(?:^|\s)@((?:\.{1,2}\/)?[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*\.md)\b/g;
  while ((m = imp.exec(noCode))) out.push({ target: m[1], col: m.index + 1, kind: 'import' });
  return out;
}

export function extractCodeSpans(line) {
  const out = [];
  const re = /(`+)([^`]+?)\1(?!`)/g;
  let m;
  while ((m = re.exec(line))) out.push({ text: m[2].trim(), col: m.index + 1, start: m.index, end: m.index + m[0].length });
  return out;
}

// Sibling repos / brains referenced by name, e.g. `sidekick-web/app/...`.
const SIBLING_REPO = /^(sidekick-[a-z0-9-]+|[a-z0-9-]+-brain|octo-brain|workspace-config|claude-plugins|agent-estate|rarebit-[a-z0-9-]+)$/;
// Names a line can use to say "this path is in ANOTHER repo". Measured: most
// unresolvable backticked paths in calibration were another repo's file named
// on a line that said whose it was ("web's `config/routes.rb`", "harness
// `src/tools/metadata.ts`"). Bare `ui`/`protocol` are too common to use.
const REPO_WORDS = ['web', 'harness', 'inference', 'companion-kit', 'admin-kit', 'rdp-client', 'glasses-app', 'glasses-test', 'system-tests'];
// Lines that talk about a path in the past tense or the negative: "the former
// `x` classes", "we do not ship a `config/x.rb`", "removed `y`".
const HISTORICAL = /\b(former(ly)?|previously|removed|deleted|renamed|no longer|used to|legacy|deprecated|do(es)? not (ship|exist|have)|don't (ship|exist|have)|doesn't exist|was moved|were moved|moved (to|from)|before \(|instead of|not (under|in|at|from)|(was|were) once|originally)\b/i;
// Template placeholders: `drizzle/meta/NNNN_snapshot.json`, `YYYY-MM-DD.md`.
const PLACEHOLDER = /NNN|XXX|YYYY|<|>|\{\{|\.\.\.|…/;

export function lineMentionsOtherRepo(line, ownName) {
  if (/\b[a-z0-9-]+-brain\b|\bbrain repos?\b/i.test(line)) return true;
  for (const w of REPO_WORDS) {
    if (w === ownName) continue;
    if (new RegExp(`(^|[^a-z0-9/._-])(sidekick-)?${w}(?![a-z0-9_-])`, 'i').test(line)) return true;
  }
  return false;
}

// Decide whether an inline-code span is a repo-relative path we should check.
// Returns the normalised path, or null when it is not one (see header).
export function pathCandidate(raw, repo, fromFile = '') {
  let s = raw.trim();
  if (!s || /\s/.test(s)) return null;
  if (SCHEME.test(s) || s.includes('://')) return null;
  if (/^[~/$@\-:=+]/.test(s)) return null;
  if (/[()"'|;,`\\]/.test(s) || PLACEHOLDER.test(s)) return null;
  if (/\$\{|\$[A-Z_]|%[sd]/.test(s)) return null;
  // Strip trailing line refs / anchors / punctuation: `a/b.rb:42`, `x.md#sec`.
  s = s.replace(/#.*$/, '').replace(/:\d+(?::\d+|-\d+)?$/, '').replace(/[.,:;!?]+$/, '');
  if (!s.includes('/')) return null;
  if (s.startsWith('../')) return null; // another repo, or outside this one
  const norm = s.replace(/^\.\//, '');
  const segs = norm.split('/').filter(Boolean);
  if (segs.length === 0) return null;
  if (SIBLING_REPO.test(segs[0]) && !repo.topLevel.has(segs[0])) return null;
  // Only spans rooted in something this repo actually has (a top-level entry,
  // or a directory next to the doc) are treated as paths. An extension alone
  // is not enough: calibration found those were overwhelmingly device paths,
  // other repos' files and abbreviations, i.e. false positives.
  if (repo.topLevel.has(segs[0]) || repo.aliasSet.has(segs[0])) return norm;
  const docDir = path.posix.dirname(fromFile);
  if (docDir !== '.' && repo.isDir(path.posix.join(docDir, segs[0]))) return norm;
  return null;
}

// Resolve a markdown link target relative to `fromFile`. Returns
// { rel } (repo-relative, may not exist), { skip: reason } or null for non-local.
export function resolveLink(target, fromFile) {
  let t = target.trim();
  if (!t || t.startsWith('#')) return null;
  if (SCHEME.test(t) || t.startsWith('//')) return null;
  t = t.replace(/[?#].*$/, '');
  try {
    t = decodeURIComponent(t);
  } catch {
    /* keep raw */
  }
  if (!t) return null;
  const rel = t.startsWith('/')
    ? path.posix.normalize(t.slice(1))
    : path.posix.normalize(path.posix.join(path.posix.dirname(fromFile), t));
  if (rel === '..' || rel.startsWith('../')) return { skip: 'outside repo' };
  return { rel: rel === '.' ? '' : rel };
}

function resolveCodePath(cand, fromFile, repo) {
  if (isGlob(cand)) return repo.glob(cand).length > 0 ? cand : null;
  const fromRoot = path.posix.normalize(cand);
  if (repo.exists(fromRoot)) return fromRoot;
  const fromDoc = path.posix.normalize(path.posix.join(path.posix.dirname(fromFile), cand));
  if (!fromDoc.startsWith('../') && repo.exists(fromDoc)) return fromDoc;
  if (repo.hasSuffix(fromRoot)) return fromRoot;
  // Module-rooted abbreviation: `app/di/AppModule.kt` for
  // `app/src/main/java/com/x/di/AppModule.kt` (Gradle/KMP docs do this a lot).
  const slash = fromRoot.indexOf('/');
  if (slash > 0) {
    const [head, tail] = [fromRoot.slice(0, slash + 1), `/${fromRoot.slice(slash + 1).replace(/\/+$/, '')}`];
    if ([...repo.files, ...repo.dirSet].some((f) => f.startsWith(head) && f.endsWith(tail))) return fromRoot;
  }
  if (!fromRoot.endsWith('/') && !/\.[A-Za-z0-9]+$/.test(fromRoot) && repo.existsWithAnyExt(fromRoot)) return fromRoot;
  return null;
}

// --------------------------------------------------------------------- checks

export function scopeFiles(repo) {
  const md = repo.files.filter((f) => f.endsWith('.md'));
  const linkScope = md.filter(
    (f) =>
      f === 'CLAUDE.md' ||
      f === 'AGENTS.md' ||
      f.startsWith('docs/') ||
      f.startsWith('.claude/') ||
      f.startsWith('.agents/'),
  );
  const docs = md.filter((f) => f.startsWith('docs/'));
  return { linkScope, docs };
}

function isIndexReadme(f) {
  return path.posix.basename(f).toLowerCase() === 'readme.md';
}

function finding(check, code, file, line, message) {
  return { check, code, file, line: line ?? null, message };
}

// Intentionally-missing paths (a removed file a doc names on purpose, a
// directory that exists only at deploy time, a local secret location):
//   * per doc: `<!-- docs-hygiene: allow-missing config/x/ path/b.rb -->`
//   * per repo: `.docs-hygiene.yml` with `allow-missing: [config/x/, ...]`
// An entry matches the exact path, anything under it when it ends in `/`, or
// as a glob. Prefer the narrowest scope that works; each entry is reviewed
// prose, not a blanket switch.
export function allowList(text) {
  const out = [];
  for (const m of text.matchAll(/<!--\s*docs-hygiene:\s*allow-missing\s+([\s\S]*?)-->/g)) {
    out.push(...m[1].split(/\s+/).filter(Boolean));
  }
  return out;
}

export function repoAllowList(repo) {
  const f = ['.docs-hygiene.yml', '.docs-hygiene.yaml'].find((x) => repo.fileSet.has(x));
  if (!f) return [];
  const fm = parseFrontmatter(`---\n${repo.read(f)}\n---\n`);
  const v = fm?.data?.['allow-missing'];
  if (fm?.error || fm?.errors?.length || (v != null && !Array.isArray(v))) {
    throw new Unknown(`${f} is not understood (expected \`allow-missing:\` as a list): ${fm?.error || fm?.errors?.join('; ') || typeof v}`);
  }
  return v || [];
}

export function isAllowed(p, allow) {
  const n = p.replace(/^\.\//, '');
  return allow.some((a) => {
    const e = a.replace(/^\.\//, '');
    if (n === e || n.replace(/\/+$/, '') === e.replace(/\/+$/, '')) return true;
    if (e.endsWith('/') && n.startsWith(e)) return true;
    return isGlob(e) && globToRegExp(e).test(n.replace(/\/+$/, ''));
  });
}

export function checkLinks(repo, files, meta = new Map(), stats = {}, repoAllow = []) {
  const out = [];
  stats.linksChecked = 0;
  stats.pathsChecked = 0;
  for (const file of files) {
    const raw = repo.read(file).split(/\r?\n/);
    const allow = [...repoAllow, ...allowList(raw.join('\n'))];
    const lines = proseLines(raw.join('\n'));
    // ADRs, reports and archived/superseded docs describe a point in time: the
    // code paths they name are allowed to have moved. Their markdown links are
    // still checked (a dead link is dead whatever the doc's age).
    const m = meta.get(file);
    const pointInTime = m
      ? ['adr', 'report'].includes(m.type) || ['archived', 'superseded'].includes(m.status)
      : POINT_IN_TIME_DIR.test(file); // no frontmatter yet: infer from the folder
    lines.forEach((line, idx) => {
      if (!line) return;
      // Escape hatch for a reviewed exception, e.g. a path that exists only at
      // runtime: `<!-- docs-hygiene-ignore -->` on the line itself.
      if (raw[idx].includes('docs-hygiene-ignore')) return;
      for (const l of extractLinks(line)) {
        const r = resolveLink(l.target, file);
        if (!r || r.skip) continue;
        stats.linksChecked++;
        if (!repo.exists(r.rel) && !repo.isIgnored(r.rel) && !isAllowed(r.rel, allow)) {
          out.push(finding('links', 'broken-link', file, idx + 1, `link target \`${l.target}\` does not exist (resolved to \`${r.rel}\`)`));
        }
      }
      if (pointInTime) return;
      // Context is the line plus the one before it, because prose wraps: "It
      // was once a vendored tarball\nunder `vendor/`, refreshed by `bin/x`".
      // Past-tense/negated ("removed", "not under") or naming another repo
      // ("harness `src/x.ts`") means the path is not a claim about this tree.
      const ctx = `${idx > 0 ? lines[idx - 1] : ''}\n${line}`;
      if (HISTORICAL.test(ctx) || lineMentionsOtherRepo(ctx, repo.name)) return;
      const spans = extractCodeSpans(line);
      const resolved = spans.map((sp) => {
        const cand = pathCandidate(sp.text, repo, file);
        return cand ? { cand, ok: Boolean(resolveCodePath(cand, file, repo)) || isAllowed(cand, allow) || repo.isIgnored(cand) } : null;
      });
      spans.forEach((span, i) => {
        const r = resolved[i];
        if (!r) return;
        stats.pathsChecked++;
        if (r.ok) return;
        // "`db/schema.rb` or `db/structure.sql`": alternatives, one is enough.
        const alt = (j) => {
          if (!resolved[j]?.ok) return false;
          const [a, b] = j < i ? [spans[j], span] : [span, spans[j]];
          return /^\s*(or|\/)\s*$/i.test(line.slice(a.end, b.start));
        };
        if (alt(i - 1) || alt(i + 1)) return;
        const glob = isGlob(r.cand);
        out.push(
          finding(
            'links',
            glob ? 'broken-path-glob' : 'broken-path',
            file,
            idx + 1,
            glob
              ? `backticked glob \`${span.text}\` matches no file`
              : `backticked path \`${span.text}\` does not exist (tried repo root, the doc's directory, and as a suffix of any tracked path)`,
          ),
        );
      });
    });
  }
  return out;
}

const POINT_IN_TIME_DIR = /(^|\/)(adrs?|decisions|reports)\//i;

export function checkFrontmatter(repo, docs) {
  const out = [];
  const meta = new Map();
  for (const file of docs) {
    if (isIndexReadme(file)) continue;
    const fm = parseFrontmatter(repo.read(file));
    if (!fm) {
      out.push(finding('frontmatter', 'frontmatter-missing', file, 1, 'no YAML frontmatter (rule 2: description, type, status, covers, verified)'));
      continue;
    }
    if (fm.error) {
      out.push(finding('frontmatter', 'frontmatter-unparseable', file, 1, fm.error));
      continue;
    }
    for (const e of fm.errors) out.push(finding('frontmatter', 'frontmatter-unparseable', file, 1, e));
    const d = fm.data;
    meta.set(file, d);
    const missing = REQUIRED_KEYS.filter((k) => !(k in d) || d[k] === null || d[k] === '');
    for (const k of missing) out.push(finding('frontmatter', 'frontmatter-key-missing', file, 1, `frontmatter is missing \`${k}\``));
    if ('type' in d && d.type !== null && d.type !== '' && !TYPES.includes(d.type)) {
      out.push(finding('frontmatter', 'frontmatter-bad-type', file, 1, `type \`${d.type}\` is not one of ${TYPES.join(' | ')}`));
    }
    if ('status' in d && d.status !== null && d.status !== '' && !STATUSES.includes(d.status)) {
      out.push(finding('frontmatter', 'frontmatter-bad-status', file, 1, `status \`${d.status}\` is not one of ${STATUSES.join(' | ')}`));
    }
    if ('verified' in d && d.verified !== null && d.verified !== '') {
      if (!isValidDate(d.verified)) {
        out.push(finding('frontmatter', 'frontmatter-bad-verified', file, 1, `verified \`${d.verified}\` is not a YYYY-MM-DD date`));
      } else if (d.verified > utcDay(VERIFIED_TOLERANCE_DAYS)) {
        // One day of slack: a verifier east of UTC (Singapore, UTC+8) writes
        // their local date while UTC is still on the day before.
        out.push(finding('frontmatter', 'frontmatter-bad-verified', file, 1, `verified \`${d.verified}\` is in the future (later than UTC today + ${VERIFIED_TOLERANCE_DAYS} day)`));
      }
    }
    if ('covers' in d && d.covers !== null && d.covers !== '') {
      if (!Array.isArray(d.covers)) {
        out.push(finding('frontmatter', 'frontmatter-bad-covers', file, 1, `covers must be a list (use [] when the doc describes no code), got \`${d.covers}\``));
      } else if (d.status === 'current') {
        // Only current docs: a proposed doc may describe code not yet written,
        // and superseded/archived docs legitimately outlive their code.
        for (const g of d.covers) {
          if (repo.glob(g).length === 0) {
            out.push(finding('frontmatter', 'covers-unmatched', file, 1, `covers glob \`${g}\` matches no file`));
          }
        }
      }
    }
  }
  return { findings: out, meta };
}

export function indexFile(repo) {
  if (repo.fileSet.has('CLAUDE.md')) {
    const t = repo.read('CLAUDE.md');
    const meaningful = t.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith('<!--'));
    if (meaningful.length > 0 && meaningful.every((l) => /^@\S+$/.test(l)) && meaningful.includes('@AGENTS.md') && repo.fileSet.has('AGENTS.md')) {
      return 'AGENTS.md';
    }
    return 'CLAUDE.md';
  }
  if (repo.fileSet.has('AGENTS.md')) return 'AGENTS.md';
  return null;
}

export function hasReadWhenTable(text) {
  return proseLines(text).some((l) => {
    if (!/^\s*\|/.test(l)) return false;
    const cells = l.split('|').map((c) => c.trim().replace(/[*_`]/g, '').toLowerCase());
    return cells.includes('doc') && (cells.includes('read when') || cells.includes('when'));
  });
}

// The compact form (docs-shape rule 1): a bullet list (`- path — when`) under a
// heading matching /read when relevant/i. Only bullets in that heading's own
// section count, up to the next heading of the same or a higher level.
export function hasReadWhenList(text) {
  const lines = proseLines(text);
  for (let i = 0; i < lines.length; i++) {
    const h = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(lines[i]);
    if (!h || !/read when relevant/i.test(h[2])) continue;
    const level = h[1].length;
    for (let j = i + 1; j < lines.length; j++) {
      const next = /^\s{0,3}(#{1,6})\s/.exec(lines[j]);
      if (next && next[1].length <= level) break;
      if (/^\s*[-*+]\s+\S/.test(lines[j])) return true;
    }
  }
  return false;
}

export function hasReadWhenIndex(text) {
  return hasReadWhenTable(text) || hasReadWhenList(text);
}

export function checkIndex(repo) {
  const f = indexFile(repo);
  if (!f) return [finding('index', 'index-missing', 'CLAUDE.md', null, 'no CLAUDE.md or AGENTS.md at the repo root')];
  const out = [];
  const text = repo.read(f);
  const n = text.replace(/\n$/, '').split(/\r?\n/).length;
  if (n > MAX_INDEX_LINES) {
    out.push(finding('index', 'index-too-long', f, MAX_INDEX_LINES + 1, `${f} is ${n} lines (max ${MAX_INDEX_LINES}); move reference/history to docs/ (rule 1)`));
  }
  if (!hasReadWhenIndex(text)) {
    out.push(finding('index', 'read-when-table-missing', f, null, `${f} has no "Read when relevant" index (a \`| Doc | Read when |\` table, or a bullet list under a "Read when relevant" heading)`));
  }
  return out;
}

// All .md files a file points at (links + backticked paths + @imports).
function referencedDocs(repo, file) {
  const refs = new Set();
  const add = (rel) => {
    if (repo.fileSet.has(rel)) {
      refs.add(rel);
    } else if (repo.isDir(rel)) {
      const dir = rel.replace(/\/+$/, '');
      const readme = repo.files.find((f) => f.toLowerCase() === `${dir}/readme.md`.replace(/^\//, ''));
      if (readme) refs.add(readme);
      else for (const f of repo.files) if (path.posix.dirname(f) === dir && f.endsWith('.md')) refs.add(f);
    }
  };
  // An index README often lists docs as a file tree in a code block, or as bare
  // `**name.md**` words. Both are real indexes an agent reads, so they count.
  if (isIndexReadme(file)) {
    const dir = path.posix.dirname(file);
    // A listed FILE is indexed. A listed DIRECTORY indexes only its own
    // README: listing `guides/` must not vouch for every doc inside it.
    const tryAdd = (p) => {
      for (const c of [path.posix.join(dir, p), p]) {
        const n = path.posix.normalize(c);
        if (n.startsWith('../')) continue;
        if (repo.fileSet.has(n)) return refs.add(n);
        if (repo.isDir(n)) {
          const readme = repo.files.find((f) => f.toLowerCase() === `${n}/readme.md`);
          if (readme) refs.add(readme);
          return;
        }
      }
    };
    for (const p of treePaths(repo.read(file), dir)) tryAdd(p);
    for (const line of proseLines(repo.read(file))) {
      for (const m of line.matchAll(/(?:^|[\s*_(])((?:[\w.-]+\/)*[\w.-]+\.md)(?=$|[\s*_),:;])/g)) tryAdd(m[1]);
    }
  }
  for (const line of proseLines(repo.read(file))) {
    if (!line) continue;
    for (const l of extractLinks(line)) {
      const r = resolveLink(l.target, file);
      if (r && !r.skip) add(r.rel);
    }
    for (const span of extractCodeSpans(line)) {
      const cand = pathCandidate(span.text, repo, file);
      if (!cand) continue;
      if (isGlob(cand)) {
        for (const g of repo.glob(cand)) if (g.endsWith('.md')) refs.add(g);
        continue;
      }
      const r = resolveCodePath(cand, file, repo);
      if (r) add(r);
    }
  }
  return refs;
}

// Paths named by `tree`-style listings inside fenced code blocks:
//   docs/
//   ├── api/
//   │   └── backend-handoff.md   # comment
// Nesting is taken from the column where each name starts.
export function treePaths(text, readmeDir) {
  const out = [];
  let inFence = false;
  let stack = [];
  for (const raw of text.split(/\r?\n/)) {
    if (/^\s{0,3}(`{3,}|~{3,})/.test(raw)) {
      inFence = !inFence;
      stack = [];
      continue;
    }
    if (!inFence) continue;
    const line = raw.replace(/\s#.*$/, '').replace(/\s+$/, '');
    const m = line.match(/^([\s│├└─|`+\\-]*)([\w.@-][^\s]*)$/);
    if (!m) continue;
    const col = [...m[1]].length;
    const name = m[2];
    while (stack.length && stack[stack.length - 1].col >= col) stack.pop();
    let parts = [...stack.map((e) => e.name), name];
    // A root line naming the README's own folder (`docs/`) is the README dir.
    if (parts[0] === `${path.posix.basename(readmeDir)}/` && readmeDir !== '.') parts = parts.slice(1);
    const p = parts.join('').replace(/\/+$/, '');
    if (p) out.push(p);
    if (name.endsWith('/')) stack.push({ col, name });
  }
  return out;
}

export function checkOrphans(repo, docs, meta) {
  const seeds = ['CLAUDE.md', 'AGENTS.md', 'docs/README.md'].filter((f) => repo.fileSet.has(f));
  const reached = new Set(seeds);
  const queue = [...seeds];
  while (queue.length) {
    const f = queue.shift();
    for (const r of referencedDocs(repo, f)) {
      if (reached.has(r)) continue;
      reached.add(r);
      // Traverse only through the index files and docs/ (not e.g. root README).
      if (r.startsWith('docs/') || r === 'AGENTS.md' || r === 'CLAUDE.md') queue.push(r);
    }
  }
  const out = [];
  for (const d of docs) {
    if (reached.has(d)) continue;
    const st = meta.get(d)?.status;
    if (st === 'archived' || st === 'superseded') continue; // not living docs
    out.push(finding('orphans', 'orphan-doc', d, null, `not reachable from ${seeds.join(' / ') || '(no index file)'} via links or backticked paths`));
  }
  return out;
}

export function runChecks(root) {
  assertRepoRoot(root);
  const repo = new Repo(root);
  const { linkScope, docs } = scopeFiles(repo);
  const fm = checkFrontmatter(repo, docs);
  const stats = {};
  const findings = [
    ...checkLinks(repo, linkScope, fm.meta, stats, repoAllowList(repo)),
    ...fm.findings,
    ...checkIndex(repo),
    ...checkOrphans(repo, docs, fm.meta),
  ];
  return { root, scanned: { linkScope: linkScope.length, docs: docs.length, ...stats }, findings };
}

// ---------------------------------------------------------------------- drift

export function runDrift(root) {
  assertRepoRoot(root);
  const shallow = git(root, ['rev-parse', '--is-shallow-repository']).stdout.trim();
  if (shallow === 'true') throw new Unknown('shallow clone: history cannot answer "commits since verified" (check out with fetch-depth: 0)');
  const repo = new Repo(root);
  const { docs } = scopeFiles(repo);
  const findings = [];
  let considered = 0;
  for (const file of docs) {
    if (isIndexReadme(file)) continue;
    const fm = parseFrontmatter(repo.read(file));
    const d = fm?.data;
    if (!d || !isValidDate(d.verified) || !Array.isArray(d.covers) || d.covers.length === 0) continue;
    if (d.status === 'archived' || d.status === 'superseded') continue;
    if (d.type === 'report') continue; // point-in-time by definition: drift is expected
    considered++;
    // Strictly after the verified DAY: commits on the verified date itself are
    // assumed to be what was checked. `verified:` is a date with no zone, so the
    // day is compared against each commit's date in its COMMITTER's zone (%cs),
    // not UTC: a 20:00 commit in UTC-8 is still that local day, and a 06:00
    // commit in UTC+8 is already the next one. No `--since`: git stops that
    // walk early on non-monotonic commit dates (rebases, cherry-picks, mixed
    // zones), which would silently undercount drift. The pathspec keeps the
    // full walk cheap.
    const specs = d.covers.map((g) => `:(glob)${g.replace(/^\.\//, '').replace(/\/$/, '/**')}`);
    const r = git(root, ['log', '--format=%H %cs %s', 'HEAD', '--', ...specs]);
    const commits = r.stdout.split('\n').filter(Boolean).filter((l) => l.split(' ')[1] > d.verified);
    if (commits.length > 0) {
      findings.push({
        ...finding('drift', 'covers-drift', file, 1, `${commits.length} commit(s) touched its covers since verified ${d.verified}`),
        commits: commits.length,
        verified: d.verified,
        covers: d.covers,
        latest: commits[0],
      });
    }
  }
  return { root, scanned: { docs: docs.length, considered }, findings };
}

// --------------------------------------------------------------------- output

function esc(s) {
  return String(s).replace(/%/g, '%25').replace(/\r/g, '%0D').replace(/\n/g, '%0A');
}
function escProp(s) {
  return esc(s).replace(/:/g, '%3A').replace(/,/g, '%2C');
}

export function counts(findings) {
  const c = { links: 0, frontmatter: 0, index: 0, orphans: 0, drift: 0 };
  for (const f of findings) c[f.check] = (c[f.check] || 0) + 1;
  return c;
}

function renderHuman(result, { mode }) {
  const lines = [];
  const c = counts(result.findings);
  lines.push(`docs-hygiene (${mode}) ${result.root}`);
  lines.push(`scanned: ${Object.entries(result.scanned).map(([k, v]) => `${k}=${v}`).join(' ')}`);
  const byCheck = {};
  for (const f of result.findings) (byCheck[f.check] ||= []).push(f);
  for (const [check, fs_] of Object.entries(byCheck)) {
    lines.push('', `## ${check} (${fs_.length})`);
    for (const f of fs_) lines.push(`  ${f.file}${f.line ? `:${f.line}` : ''}  [${f.code}]  ${f.message}`);
  }
  lines.push('', `totals: ${Object.entries(c).filter(([k]) => mode === 'drift' ? k === 'drift' : k !== 'drift').map(([k, v]) => `${k}=${v}`).join(' ')}`);
  return lines.join('\n');
}

function stepSummary(result, { mode, blocking }) {
  const c = counts(result.findings);
  const out = [];
  const title = mode === 'drift' ? 'Docs drift report' : 'Docs hygiene';
  const verdict = result.findings.length === 0 ? 'clean' : mode === 'drift' ? 'report only' : blocking ? 'FAILED' : 'warn-only (not blocking)';
  out.push(`### ${title}: ${verdict}`, '');
  // Only claim the positive control when the caller (the composite action)
  // says it ran and passed earlier in this job; the scan cannot know that.
  const control = process.env.DOCS_HYGIENE_SELF_TESTED === '1'
    ? ' The `--self-test` positive control passed earlier in this job.'
    : ' (No self-test ran in this job: this result has no positive control.)';
  out.push(`Scanned ${Object.entries(result.scanned).map(([k, v]) => `\`${k}\`=${v}`).join(', ')}.${control}`, '');
  out.push('| Check | Findings |', '|---|---|');
  for (const [k, v] of Object.entries(c)) if (mode === 'drift' ? k === 'drift' : k !== 'drift') out.push(`| ${k} | ${v} |`);
  if (result.findings.length) {
    out.push('', '| File | Line | Code | Message |', '|---|---|---|---|');
    for (const f of result.findings.slice(0, 200)) {
      out.push(`| \`${f.file}\` | ${f.line ?? ''} | ${f.code} | ${String(f.message).replace(/\|/g, '\\|')} |`);
    }
    if (result.findings.length > 200) out.push('', `…and ${result.findings.length - 200} more (see the job log).`);
  }
  if (mode !== 'drift') {
    out.push('', 'Convention: [docs-shape](https://github.com/sidekick-labs/octo-brain/blob/main/.claude/conventions/docs-shape.md).');
  }
  return out.join('\n') + '\n';
}

function emit(result, opts) {
  const { json, jsonOut, mode, blocking } = opts;
  // --json-out keeps the human log AND hands machine output to a later step
  // (drift-issue.mjs reads it to file the brain issue).
  if (jsonOut) fs.writeFileSync(jsonOut, JSON.stringify({ mode, blocking, ...result, counts: counts(result.findings) }, null, 2) + '\n');
  if (json) {
    process.stdout.write(JSON.stringify({ mode, blocking, ...result, counts: counts(result.findings) }, null, 2) + '\n');
  } else {
    process.stdout.write(renderHuman(result, opts) + '\n');
  }
  if (process.env.GITHUB_ACTIONS === 'true') {
    const level = blocking && mode !== 'drift' ? 'error' : 'warning';
    for (const f of result.findings) {
      const loc = `file=${escProp(f.file)}${f.line ? `,line=${f.line}` : ''}`;
      process.stderr.write(`::${level} ${loc},title=${escProp(`docs-hygiene ${f.code}`)}::${esc(f.message)}\n`);
    }
  }
  if (process.env.GITHUB_STEP_SUMMARY) {
    try {
      fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, stepSummary(result, opts));
    } catch {
      /* summary is best effort */
    }
  }
}

// ------------------------------------------------------------------ self-test

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_FIXTURES = path.resolve(HERE, '../../../tests/fixtures/docs-hygiene');

// Materialise a fixture as a throwaway git repo. expected.json may name a
// `base` fixture to copy first (so a bad fixture is a minimal delta from
// `clean`) and `remove` paths to delete from it. `history.json` (optional) is
// {baseDate, commits:[{date, files:{path:content}}]} applied AFTER the fixture
// tree is committed, for the drift fixture. The global/system git config is
// ignored so the developer's hooks/signing settings never touch the temp repo.
function materialise(fixtureDir, expected = {}, dest = null) {
  const tmp = dest ?? fs.mkdtempSync(path.join(os.tmpdir(), 'docs-hygiene-'));
  if (dest) fs.mkdirSync(dest, { recursive: true });
  if (expected.base) fs.cpSync(path.join(path.dirname(fixtureDir), expected.base), tmp, { recursive: true, verbatimSymlinks: true });
  fs.cpSync(fixtureDir, tmp, { recursive: true, verbatimSymlinks: true, force: true });
  for (const meta of ['expected.json', 'history.json']) fs.rmSync(path.join(tmp, meta), { force: true });
  for (const r of expected.remove || []) fs.rmSync(path.join(tmp, r), { recursive: true, force: true });
  const env = {
    ...process.env,
    GIT_CONFIG_GLOBAL: os.devNull,
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_CONFIG_COUNT: '0',
    GIT_AUTHOR_NAME: 'fixture',
    GIT_AUTHOR_EMAIL: 'fixture@example.invalid',
    GIT_COMMITTER_NAME: 'fixture',
    GIT_COMMITTER_EMAIL: 'fixture@example.invalid',
  };
  const g = (args, extraEnv = {}) => {
    const r = spawnSync('git', ['-C', tmp, ...args], { encoding: 'utf8', env: { ...env, ...extraEnv } });
    if (r.status !== 0) throw new Error(`fixture git ${args.join(' ')}: ${r.stderr}`);
  };
  // `{{UTC_TODAY+N}}` in a fixture's markdown becomes the UTC date N days from
  // now, for checks relative to today (the `verified:` future tolerance).
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory() && e.name !== '.git') walk(p);
      else if (e.isFile() && p.endsWith('.md')) {
        const t = fs.readFileSync(p, 'utf8');
        if (t.includes('{{UTC_TODAY')) fs.writeFileSync(p, t.replace(/\{\{UTC_TODAY([+-]\d+)?\}\}/g, (_, n) => utcDay(Number(n || 0))));
      }
    }
  };
  walk(tmp);
  g(['init', '-q', '-b', 'main']);
  g(['add', '-A']);
  const historyPath = path.join(fixtureDir, 'history.json');
  if (fs.existsSync(historyPath)) {
    const base = JSON.parse(fs.readFileSync(historyPath, 'utf8'));
    // `at` (full ISO 8601 with a zone offset) overrides the noon-UTC default,
    // for fixtures about which local day a commit falls on.
    const at = (d, ts) => ({ GIT_AUTHOR_DATE: ts || `${d}T12:00:00Z`, GIT_COMMITTER_DATE: ts || `${d}T12:00:00Z` });
    g(['commit', '-q', '-m', 'fixture base'], at(base.baseDate));
    for (const c of base.commits) {
      for (const [p, content] of Object.entries(c.files)) {
        fs.mkdirSync(path.dirname(path.join(tmp, p)), { recursive: true });
        fs.writeFileSync(path.join(tmp, p), content);
      }
      g(['add', '-A']);
      g(['commit', '-q', '-m', c.message || 'fixture change'], at(c.date, c.at));
    }
  }
  return tmp;
}

function key(f) {
  return `${f.code} ${f.file}`;
}

export function selfTest(fixturesDir = DEFAULT_FIXTURES) {
  let dirs;
  try {
    dirs = fs.readdirSync(fixturesDir, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort();
  } catch (e) {
    console.error(`self-test: cannot read fixtures at ${fixturesDir}: ${e.message}`);
    return 3;
  }
  const failures = [];
  const codesSeen = new Set();
  if (!dirs.includes('clean')) failures.push('no `clean` fixture (the negative control)');
  for (const name of dirs) {
    const dir = path.join(fixturesDir, name);
    let expected;
    try {
      expected = JSON.parse(fs.readFileSync(path.join(dir, 'expected.json'), 'utf8'));
    } catch (e) {
      failures.push(`${name}: unreadable expected.json (${e.message})`);
      continue;
    }
    let tmp;
    try {
      tmp = materialise(dir, expected);
      if (expected.unknown) {
        // This fixture must make the check give up (exit 3), never pass.
        try {
          expected.mode === 'drift' ? runDrift(tmp) : runChecks(tmp);
          failures.push(`${name}: expected UNKNOWN, got a result`);
          console.log(`  FAIL ${name}`);
        } catch (e) {
          if (!(e instanceof Unknown)) throw e;
          console.log(`  ok   ${name.padEnd(28)} UNKNOWN as expected: ${e.message}`);
        }
        continue;
      }
      const result = expected.mode === 'drift' ? runDrift(tmp) : runChecks(tmp);
      const got = result.findings.map(key).sort();
      const want = (expected.findings || []).map(key).sort();
      for (const f of result.findings) codesSeen.add(f.code);
      const ok = got.length === want.length && got.every((g, i) => g === want[i]);
      if (name !== 'clean' && !expected.negative && want.length === 0 && !expected.unknown) failures.push(`${name}: a bad fixture must expect at least one finding (or set "negative": true for an extra negative control)`);
      if (!ok) {
        failures.push(`${name}: expected [${want.join('; ')}] got [${got.join('; ')}]`);
        console.log(`  FAIL ${name}`);
        for (const f of result.findings) console.log(`       ${f.file}:${f.line ?? ''} [${f.code}] ${f.message}`);
      } else {
        console.log(`  ok   ${name.padEnd(28)} ${want.length ? `red as expected: ${want.join('; ')}` : 'clean (negative control)'}`);
      }
    } catch (e) {
      failures.push(`${name}: ${e instanceof Unknown ? 'UNKNOWN ' : ''}${e.message}`);
    } finally {
      if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
    }
  }
  // Every finding code the script can emit must be proven able to fire.
  const allCodes = [
    'broken-link', 'broken-path', 'broken-path-glob',
    'frontmatter-missing', 'frontmatter-unparseable', 'frontmatter-key-missing', 'frontmatter-bad-type',
    'frontmatter-bad-status', 'frontmatter-bad-verified', 'frontmatter-bad-covers', 'covers-unmatched',
    'index-missing', 'index-too-long', 'read-when-table-missing', 'orphan-doc', 'covers-drift',
  ];
  for (const c of allCodes) if (!codesSeen.has(c)) failures.push(`no fixture ever produced \`${c}\`: that check is unverified`);
  // Exit-code contract: a non-repo directory is UNKNOWN (3), never clean.
  const notRepo = fs.mkdtempSync(path.join(os.tmpdir(), 'docs-hygiene-norepo-'));
  try {
    runChecks(notRepo);
    failures.push('a non-git directory did not raise UNKNOWN');
  } catch (e) {
    if (!(e instanceof Unknown)) failures.push(`non-git directory raised ${e.message} instead of UNKNOWN`);
    else console.log('  ok   not-a-repo                   UNKNOWN as expected');
  } finally {
    fs.rmSync(notRepo, { recursive: true, force: true });
  }
  if (failures.length) {
    console.error(`\nself-test FAILED (${failures.length}):`);
    for (const f of failures) console.error(`  - ${f}`);
    return 1;
  }
  console.log(`\nself-test passed: ${dirs.length} fixtures, every finding code fired at least once.`);
  return 0;
}

// ------------------------------------------------------------------------ cli

function parseArgs(argv) {
  const o = { root: '.', json: false, warnOnly: false, drift: false, selfTest: false, fixtures: DEFAULT_FIXTURES };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--root') o.root = argv[++i];
    else if (a === '--json') o.json = true;
    else if (a === '--json-out') o.jsonOut = argv[++i];
    else if (a === '--warn-only') o.warnOnly = true;
    else if (a === '--drift') o.drift = true;
    else if (a === '--self-test') o.selfTest = true;
    else if (a === '--fixtures') o.fixtures = argv[++i];
    else if (a === '--materialise') o.materialise = argv[++i];
    else if (a === '--out') o.out = argv[++i];
    else if (a === '-h' || a === '--help') o.help = true;
    else {
      o.bad = a;
    }
  }
  return o;
}

function main() {
  const o = parseArgs(process.argv.slice(2));
  if (o.help || o.bad || (o.root ?? '') === '') {
    if (o.bad) console.error(`unknown argument: ${o.bad}`);
    console.error('usage: docs-hygiene.mjs [--root DIR] [--warn-only] [--json] [--json-out FILE] | --drift [--root DIR] [--json] [--json-out FILE] | --self-test [--fixtures DIR]');
    return o.help ? 0 : 2;
  }
  if (o.selfTest) return selfTest(o.fixtures);
  // Test-harness helper: write fixture NAME out as a git repo at --out DIR, so a
  // workflow can drive the real action against a known-clean / known-bad tree.
  if (o.materialise) {
    if (!o.out) return 2;
    const dir = path.join(o.fixtures, o.materialise);
    const expected = JSON.parse(fs.readFileSync(path.join(dir, 'expected.json'), 'utf8'));
    materialise(dir, expected, path.resolve(o.out));
    return 0;
  }
  const root = path.resolve(o.root);
  try {
    if (o.drift) {
      const r = runDrift(root);
      emit(r, { json: o.json, jsonOut: o.jsonOut, mode: 'drift', blocking: false });
      return 0; // report-only, by design
    }
    const r = runChecks(root);
    const blocking = !o.warnOnly;
    emit(r, { json: o.json, jsonOut: o.jsonOut, mode: blocking ? 'block' : 'warn', blocking });
    return r.findings.length > 0 && blocking ? 1 : 0;
  } catch (e) {
    if (e instanceof Unknown) {
      const msg = `docs-hygiene could not tell: ${e.message}. Result is UNKNOWN, not clean.`;
      if (process.env.GITHUB_ACTIONS === 'true') process.stderr.write(`::error title=docs-hygiene UNKNOWN::${esc(msg)}\n`);
      console.error(msg);
      return 3;
    }
    throw e;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
