#!/usr/bin/env node
// drift-issue: file the weekly docs drift report as ONE issue per repo in the
// owning brain (docs-shape rule 5: "opens or refreshes one issue per repo in
// the owning brain").
//
// Spec: rule 5 of
// https://github.com/sidekick-labs/octo-brain/blob/main/.claude/conventions/docs-shape.md
//
// Input is the JSON that `docs-hygiene.mjs --drift --json-out FILE` writes.
//
//   drift > 0  open issue titled `[docs-drift] <repo>: N docs changed since
//              verified`, labelled `docs-drift`, with a table of the drifted
//              docs. If one is already open it is refreshed in place (title and
//              body), never duplicated. Extra open duplicates are closed.
//   drift = 0  close the open one (if any) with a comment.
//
// The issue is found by label `docs-drift` plus the title prefix
// `[docs-drift] <repo>:` among OPEN issues, so several repos can share one
// brain without colliding, and a closed issue is never reopened (a new drift
// opens a fresh one).
//
// Report-only, like the drift scan itself: every reporter failure (no token,
// API error) degrades to a ::warning:: and exit 0, so the reporting path never
// becomes the outage. One rule is load-bearing: a failed READ of the brain's
// issues means "could not tell", never "there is no open issue", so it stops
// rather than filing a duplicate. Precedent: the failure sink in
// sidekick-system-tests' system-tests.yml.
//
// Exit codes: 0 always for a run (even when filing failed; see above),
// 1 self-test failure, 2 usage error.
//
// Usage
// -----
//   GH_TOKEN=... node drift-issue.mjs --report FILE --repo OWNER/NAME --brain OWNER/NAME [--run-url URL]
//   node drift-issue.mjs --report FILE --repo OWNER/NAME --brain OWNER/NAME --dry-run
//       prints the would-be title, body and action; with GH_TOKEN set it also
//       reads the brain to say whether it would create, refresh or close.
//   node drift-issue.mjs --self-test
//
// Zero dependencies: node:fs, node:url and the global fetch.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const LABEL = 'docs-drift';
const LABEL_COLOR = 'c5def5';
const LABEL_DESCRIPTION = 'Weekly docs-shape drift report: docs whose covered code changed since verified';
const MAX_ROWS = 200; // an issue body caps at 65536 chars
const CONVENTION = 'https://github.com/sidekick-labs/octo-brain/blob/main/.claude/conventions/docs-shape.md';

// ---------------------------------------------------------------- rendering

function shortName(repo) {
  return repo.split('/').pop();
}

export function titlePrefix(repo) {
  return `[docs-drift] ${shortName(repo)}:`;
}

export function issueTitle(repo, n) {
  return `${titlePrefix(repo)} ${n} doc${n === 1 ? '' : 's'} changed since verified`;
}

// Table-cell safe: no pipes, no newlines, no stray backticks.
function cell(s) {
  return String(s ?? '').replace(/\r?\n/g, ' ').replace(/\|/g, '\\|');
}
function code(s) {
  return `\`${String(s).replace(/`/g, "'").replace(/\|/g, '\\|')}\``;
}

export function renderIssue(report, { repo, runUrl = '' }) {
  const drift = (report.findings || []).filter((f) => f.code === 'covers-drift');
  const n = drift.length;
  const base = `https://github.com/${repo}`;
  const lines = [];
  lines.push(`<!-- docs-drift-report repo=${repo} -->`);
  lines.push(
    `**${n}** living doc${n === 1 ? '' : 's'} in [${repo}](${base}) cover${n === 1 ? 's' : ''} code that gained commits after ${n === 1 ? 'its' : 'their'} \`verified:\` date ([docs-shape](${CONVENTION}) rule 5).`,
    '',
    '| Doc | Covers | Commits since verified | Verified | Last commit |',
    '|---|---|---|---|---|',
  );
  const rows = [...drift].sort((a, b) => (b.commits ?? 0) - (a.commits ?? 0) || a.file.localeCompare(b.file));
  for (const f of rows.slice(0, MAX_ROWS)) {
    const covers = Array.isArray(f.covers) && f.covers.length ? f.covers.map(code).join('<br>') : '';
    let last = '';
    if (f.latest) {
      const [sha, date, ...subject] = String(f.latest).split(' ');
      // `(#123)` in a subject means THIS repo's PR, not the brain's: qualify it.
      // Defuse @mentions so a weekly refresh never pings commit authors.
      const subj = subject.join(' ').replace(/(^|[^\w/])#(\d+)\b/g, `$1${repo}#$2`).replace(/@(?=\w)/g, '@<!-- -->');
      last = `[\`${sha.slice(0, 7)}\`](${base}/commit/${sha}) ${cell(date)} ${cell(subj)}`;
    }
    lines.push(`| [${code(f.file)}](${base}/blob/HEAD/${f.file}) | ${covers} | ${f.commits ?? ''} | ${cell(f.verified)} | ${last} |`);
  }
  if (rows.length > MAX_ROWS) lines.push('', `…and ${rows.length - MAX_ROWS} more (see the run's step summary).`);
  lines.push(
    '',
    '**To clear a row:** re-read the doc against the code its `covers:` globs name, fix whatever has gone stale, and bump `verified:` to today in the same PR. A doc that no longer describes live code should move to `status: superseded` or `archived` instead.',
    '',
    `Report-only: nothing is failing. This issue is refreshed in place by each weekly drift run and closed automatically when drift reaches zero.${runUrl ? ` Last refreshed by ${runUrl}.` : ''}`,
  );
  if (report.scanned) {
    lines.push('', `<sub>Scanned ${Object.entries(report.scanned).map(([k, v]) => `${k}=${v}`).join(', ')}.</sub>`);
  }
  return { title: issueTitle(repo, n), body: lines.join('\n') + '\n', count: n };
}

// ---------------------------------------------------------------- GitHub API

export function githubApi(token, { baseUrl = process.env.GITHUB_API_URL || 'https://api.github.com' } = {}) {
  return async (method, p, body) => {
    const res = await fetch(`${baseUrl}${p}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'sidekick-labs-docs-drift',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    return { status: res.status, data };
  };
}

const ok = (r) => r.status >= 200 && r.status < 300;

class ReadFailed extends Error {}

async function findOpen(api, brain, repo) {
  const prefix = titlePrefix(repo);
  const found = [];
  for (let page = 1; page <= 10; page++) {
    const r = await api('GET', `/repos/${brain}/issues?state=open&labels=${LABEL}&per_page=100&page=${page}`);
    if (!ok(r) || !Array.isArray(r.data)) throw new ReadFailed(`listing open ${LABEL} issues on ${brain} returned HTTP ${r.status}`);
    for (const i of r.data) if (!i.pull_request && i.title.startsWith(prefix)) found.push(i);
    if (r.data.length < 100) return found.sort((a, b) => a.number - b.number);
  }
  // Not seen to the end: an unread page could hold ours.
  throw new ReadFailed(`more than 1000 open ${LABEL} issues on ${brain}; could not read them all`);
}

async function ensureLabel(api, brain) {
  const r = await api('GET', `/repos/${brain}/labels/${LABEL}`);
  if (ok(r)) return;
  if (r.status !== 404) throw new Error(`reading label ${LABEL} on ${brain} returned HTTP ${r.status}`);
  const c = await api('POST', `/repos/${brain}/labels`, { name: LABEL, color: LABEL_COLOR, description: LABEL_DESCRIPTION });
  // 422 = created concurrently; fine.
  if (!ok(c) && c.status !== 422) throw new Error(`creating label ${LABEL} on ${brain} returned HTTP ${c.status}`);
}

// Close first, then comment: a failed close leaves no comment behind to be
// repeated on next week's retry.
async function closeIssue(api, brain, number, comment) {
  const r = await api('PATCH', `/repos/${brain}/issues/${number}`, { state: 'closed', state_reason: 'completed' });
  if (!ok(r)) throw new Error(`closing ${brain}#${number} returned HTTP ${r.status}`);
  const c = await api('POST', `/repos/${brain}/issues/${number}/comments`, { body: comment });
  if (!ok(c)) throw new Error(`closed ${brain}#${number} but commenting returned HTTP ${c.status}`);
}

// Returns { action, number?, url?, warnings[] }. Never throws for API trouble:
// that becomes a warning (report-only), see the header.
export async function upsert({ api, brain, repo, report, runUrl = '', dryRun = false }) {
  const { title, body, count } = renderIssue(report, { repo, runUrl });
  const warnings = [];
  let existing;
  try {
    existing = api ? await findOpen(api, brain, repo) : null;
  } catch (e) {
    // Could not tell whether one is open: filing now could duplicate. Stop.
    return { action: 'none', title, body, count, warnings: [`${e.message}; not filing (a failed read is not "no open issue")`] };
  }
  const [keep, ...dupes] = existing || [];
  // Zero drift closes only when the scan actually looked at something: a run
  // that considered no docs (odd checkout, frontmatter gone) is not evidence
  // the drift was fixed.
  if (count === 0 && keep && report.scanned && report.scanned.considered === 0) {
    return { action: 'none', number: keep.number, url: keep.html_url, title, body, count, warnings: [`the scan considered 0 docs, so ${brain}#${keep.number} is left open (zero drift from an empty scan is not a fix)`] };
  }
  let action;
  if (count === 0) action = keep ? 'close' : 'none';
  else action = existing === null ? 'upsert' : keep ? 'refresh' : 'create';
  if (dryRun || !api) return { action, number: keep?.number, url: keep?.html_url, title, body, count, dupes: dupes.map((d) => d.number), warnings, dryRun: true };

  try {
    if (count === 0) {
      for (const i of existing) {
        await closeIssue(api, brain, i.number, `Docs drift for ${repo} is zero: every living doc's \`covers:\` code is unchanged since it was verified.${runUrl ? ` (${runUrl})` : ''}`);
      }
      return { action, number: keep?.number, url: keep?.html_url, title, body, count, warnings };
    }
    let issue;
    if (keep) {
      const r = await api('PATCH', `/repos/${brain}/issues/${keep.number}`, { title, body });
      if (!ok(r)) throw new Error(`refreshing ${brain}#${keep.number} returned HTTP ${r.status}`);
      issue = r.data;
    } else {
      await ensureLabel(api, brain);
      const r = await api('POST', `/repos/${brain}/issues`, { title, body, labels: [LABEL] });
      if (!ok(r)) throw new Error(`creating the issue on ${brain} returned HTTP ${r.status}`);
      issue = r.data;
    }
    for (const d of dupes) {
      try {
        await closeIssue(api, brain, d.number, `Duplicate of #${issue.number}: one docs-drift issue per repo.`);
      } catch (e) {
        warnings.push(e.message);
      }
    }
    return { action, number: issue.number, url: issue.html_url, title, body, count, warnings };
  } catch (e) {
    warnings.push(e.message);
    return { action: 'failed', title, body, count, warnings };
  }
}

// ---------------------------------------------------------------- self-test

// In-memory GitHub with just the endpoints upsert() uses.
function fakeGitHub({ issues = [], labels = [], failList = false } = {}) {
  const state = { issues: issues.map((i) => ({ state: 'open', labels: [{ name: LABEL }], comments: [], ...i })), labels: [...labels], writes: [] };
  let next = Math.max(0, ...state.issues.map((i) => i.number)) + 1;
  const api = async (method, p, body) => {
    const u = new URL(`https://x${p}`);
    const parts = u.pathname.split('/').filter(Boolean); // repos o r ...
    const rest = parts.slice(3);
    if (method !== 'GET') state.writes.push(`${method} ${rest.join('/')}`);
    if (method === 'GET' && rest[0] === 'issues' && rest.length === 1) {
      if (failList) return { status: 500, data: { message: 'boom' } };
      const want = u.searchParams.get('labels');
      const st = u.searchParams.get('state');
      const per = Number(u.searchParams.get('per_page') || 30);
      const page = Number(u.searchParams.get('page') || 1);
      const all = state.issues.filter((i) => i.state === st && i.labels.some((l) => l.name === want));
      return { status: 200, data: all.slice((page - 1) * per, page * per) };
    }
    if (method === 'GET' && rest[0] === 'labels') return state.labels.includes(rest[1]) ? { status: 200, data: { name: rest[1] } } : { status: 404, data: {} };
    if (method === 'POST' && rest[0] === 'labels') {
      state.labels.push(body.name);
      return { status: 201, data: body };
    }
    if (method === 'POST' && rest[0] === 'issues' && rest.length === 1) {
      const i = { number: next++, state: 'open', title: body.title, body: body.body, labels: body.labels.map((name) => ({ name })), comments: [], html_url: `https://github.com/x/issues/${next - 1}` };
      state.issues.push(i);
      return { status: 201, data: i };
    }
    const i = state.issues.find((x) => x.number === Number(rest[1]));
    if (!i) return { status: 404, data: {} };
    if (method === 'PATCH') {
      Object.assign(i, body);
      return { status: 200, data: i };
    }
    if (method === 'POST' && rest[2] === 'comments') {
      i.comments.push(body.body);
      return { status: 201, data: {} };
    }
    return { status: 400, data: {} };
  };
  return { api, state };
}

const REPORT = {
  mode: 'drift',
  scanned: { docs: 12, considered: 9 },
  findings: [
    { check: 'drift', code: 'covers-drift', file: 'docs/a.md', line: 1, commits: 2, verified: '2026-09-01', covers: ['app/**'], latest: 'aaaaaaaaaaaa1111 2026-10-02 fix: a | b' },
    { check: 'drift', code: 'covers-drift', file: 'docs/b.md', line: 1, commits: 5, verified: '2026-08-01', covers: ['lib/**', 'config/x.yml'], latest: 'bbbbbbbbbbbb2222 2026-10-03 feat: b' },
  ],
};
const EMPTY = { mode: 'drift', scanned: { docs: 12, considered: 9 }, findings: [] };
const BRAIN = 'sidekick-labs/some-brain';
const REPO = 'sidekick-labs/sidekick-web';

export async function selfTest() {
  const failures = [];
  const check = (name, cond, detail = '') => {
    if (cond) console.log(`  ok   ${name}`);
    else {
      console.log(`  FAIL ${name}${detail ? `: ${detail}` : ''}`);
      failures.push(name);
    }
  };

  // Rendering.
  const r = renderIssue(REPORT, { repo: REPO, runUrl: 'https://run/1' });
  check('title counts docs', r.title === '[docs-drift] sidekick-web: 2 docs changed since verified', r.title);
  check('title singular', issueTitle(REPO, 1) === '[docs-drift] sidekick-web: 1 doc changed since verified');
  check('body has one row per doc', (r.body.match(/^\| \[`docs\//gm) || []).length === 2);
  check('rows sorted by commits desc', r.body.indexOf('docs/b.md') < r.body.indexOf('docs/a.md'));
  check('covers globs listed', r.body.includes('`lib/**`<br>`config/x.yml`'));
  check('commit subject pipe escaped', r.body.includes('fix: a \\| b'));
  {
    const q = renderIssue({ findings: [{ code: 'covers-drift', file: 'docs/c.md', commits: 1, verified: '2026-09-01', covers: ['x/**'], latest: 'cccc 2026-10-04 fix @alice thing (#42)' }] }, { repo: REPO });
    check('PR refs qualified to the code repo', q.body.includes(`(${REPO}#42)`), q.body);
    check('@mentions defused', q.body.includes('@<!-- -->alice'));
  }
  check('last commit linked', r.body.includes(`(https://github.com/${REPO}/commit/aaaaaaaaaaaa1111)`));

  // 1. Drift, nothing open: create (and the label).
  {
    const g = fakeGitHub();
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT });
    check('create: one issue opened', out.action === 'create' && g.state.issues.length === 1, JSON.stringify(out.warnings));
    check('create: labelled docs-drift', g.state.issues[0]?.labels.some((l) => l.name === LABEL));
    check('create: label created when missing', g.state.labels.includes(LABEL));
  }
  // 2. Drift, one open for this repo: refresh in place, no new issue.
  {
    const g = fakeGitHub({ labels: [LABEL], issues: [{ number: 7, title: '[docs-drift] sidekick-web: 1 doc changed since verified', body: 'old' }] });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT });
    check('refresh: no new issue', out.action === 'refresh' && g.state.issues.length === 1 && out.number === 7);
    check('refresh: title and body updated', g.state.issues[0].title.includes(': 2 docs') && g.state.issues[0].body.includes('docs/b.md'));
    check('refresh: idempotent on a second run', (await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT })).action === 'refresh' && g.state.issues.length === 1);
  }
  // 3. Another repo's open issue in the same brain is not ours.
  {
    const g = fakeGitHub({ labels: [LABEL], issues: [{ number: 3, title: '[docs-drift] sidekick-web-extra: 4 docs changed since verified' }, { number: 4, title: '[docs-drift] sidekick-harness: 1 doc changed since verified' }] });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT });
    check('other repos ignored (prefix includes the colon)', out.action === 'create' && g.state.issues.filter((i) => i.state === 'open').length === 3);
  }
  // 4. Duplicates: keep the oldest, close the rest.
  {
    const g = fakeGitHub({ labels: [LABEL], issues: [{ number: 9, title: '[docs-drift] sidekick-web: 3 docs changed since verified' }, { number: 5, title: '[docs-drift] sidekick-web: 1 doc changed since verified' }] });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT });
    check('dupes: oldest refreshed, newer closed', out.number === 5 && g.state.issues.find((i) => i.number === 9).state === 'closed');
  }
  // 5. Zero drift with an open issue: close with a comment.
  {
    const g = fakeGitHub({ labels: [LABEL], issues: [{ number: 7, title: '[docs-drift] sidekick-web: 1 doc changed since verified' }] });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: EMPTY, runUrl: 'https://run/2' });
    const i = g.state.issues[0];
    check('zero: open issue closed with a comment', out.action === 'close' && i.state === 'closed' && i.comments.length === 1 && i.comments[0].includes('https://run/2'));
  }
  // 5b. An empty scan (0 docs considered) never closes a real issue.
  {
    const g = fakeGitHub({ labels: [LABEL], issues: [{ number: 7, title: '[docs-drift] sidekick-web: 1 doc changed since verified' }] });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: { findings: [], scanned: { docs: 0, considered: 0 } } });
    check('empty scan: issue left open, warned', out.action === 'none' && g.state.issues[0].state === 'open' && g.state.writes.length === 0 && out.warnings.length === 1);
  }
  // 5c. More than 1000 open labelled issues: could not read them all, file nothing.
  {
    const many = Array.from({ length: 1000 }, (_, k) => ({ number: k + 1, title: `[docs-drift] repo-${k}: 1 doc changed since verified` }));
    const g = fakeGitHub({ labels: [LABEL], issues: many });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT });
    check('pagination cap: treated as a failed read', out.action === 'none' && g.state.writes.length === 0 && out.warnings.length === 1, JSON.stringify(out.warnings));
  }
  // 6. Zero drift, nothing open: no writes at all.
  {
    const g = fakeGitHub();
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: EMPTY });
    check('zero, none open: no writes', out.action === 'none' && g.state.writes.length === 0, g.state.writes.join(','));
  }
  // 7. A failed read must not be taken as "nothing open".
  {
    const g = fakeGitHub({ failList: true });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT });
    check('read failure: nothing filed, warned', out.action === 'none' && g.state.writes.length === 0 && out.warnings.length === 1);
  }
  // 8. Dry run reads but never writes.
  {
    const g = fakeGitHub({ labels: [LABEL], issues: [{ number: 7, title: '[docs-drift] sidekick-web: 1 doc changed since verified' }] });
    const out = await upsert({ api: g.api, brain: BRAIN, repo: REPO, report: REPORT, dryRun: true });
    check('dry run: plans refresh, writes nothing', out.action === 'refresh' && g.state.writes.length === 0);
  }

  if (failures.length) {
    console.error(`\ndrift-issue self-test FAILED (${failures.length}): ${failures.join('; ')}`);
    return 1;
  }
  console.log('\ndrift-issue self-test passed.');
  return 0;
}

// ---------------------------------------------------------------- cli

function parseArgs(argv) {
  const o = { dryRun: false, selfTest: false, runUrl: '' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--report') o.report = argv[++i];
    else if (a === '--repo') o.repo = argv[++i];
    else if (a === '--brain') o.brain = argv[++i];
    else if (a === '--run-url') o.runUrl = argv[++i];
    else if (a === '--dry-run') o.dryRun = true;
    else if (a === '--self-test') o.selfTest = true;
    else if (a === '-h' || a === '--help') o.help = true;
    else o.bad = a;
  }
  return o;
}

const SLUG = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

function warn(msg) {
  if (process.env.GITHUB_ACTIONS === 'true') process.stderr.write(`::warning title=docs-drift issue::${msg.replace(/%/g, '%25').replace(/\n/g, '%0A')}\n`);
  else console.error(`warning: ${msg}`);
}

async function main() {
  const o = parseArgs(process.argv.slice(2));
  if (o.selfTest) return selfTest();
  if (o.help || o.bad || !o.report || !SLUG.test(o.repo || '') || !SLUG.test(o.brain || '')) {
    if (o.bad) console.error(`unknown argument: ${o.bad}`);
    console.error('usage: drift-issue.mjs --report FILE --repo OWNER/NAME --brain OWNER/NAME [--run-url URL] [--dry-run] | --self-test');
    return o.help ? 0 : 2;
  }
  let report;
  try {
    report = JSON.parse(fs.readFileSync(o.report, 'utf8'));
  } catch (e) {
    warn(`could not read the drift report ${o.report} (${e.message}); no issue filed.`);
    return 0;
  }
  const token = process.env.GH_TOKEN || '';
  if (!token && !o.dryRun) {
    const n = (report.findings || []).length;
    warn(`no token for ${o.brain} (could not mint the release-bot App token: is SIDEKICK_RELEASE_BOT_PRIVATE_KEY shared with this repo?). ${n} drifted doc(s) reported in the step summary only; no issue filed.`);
    return 0;
  }
  const out = await upsert({ api: token ? githubApi(token) : null, brain: o.brain, repo: o.repo, report, runUrl: o.runUrl, dryRun: o.dryRun });
  for (const w of out.warnings) warn(w);
  if (o.dryRun) {
    console.log(`DRY RUN: would ${out.action}${out.number ? ` ${o.brain}#${out.number}` : ` in ${o.brain}`}${out.dupes?.length ? ` (and close duplicates ${out.dupes.map((d) => `#${d}`).join(', ')})` : ''}`);
    console.log(`title: ${out.title}\nlabel: ${LABEL}\n--- body ---\n${out.body}--- end body ---`);
  } else {
    const where = out.url || (out.number ? `${o.brain}#${out.number}` : o.brain);
    const msg = {
      create: `opened ${where}`,
      refresh: `refreshed ${where}`,
      close: `drift is zero; closed ${where}`,
      none: out.count === 0 ? 'drift is zero; no open docs-drift issue to close' : 'no issue filed (see warnings)',
      failed: `filing failed (see warnings); ${out.count} drifted doc(s) in the step summary only`,
    }[out.action];
    console.log(`docs-drift issue: ${msg}`);
    if (process.env.GITHUB_STEP_SUMMARY) {
      try {
        fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `\n**Brain issue:** ${msg}\n`);
      } catch {
        /* best effort */
      }
    }
  }
  return 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then((c) => (process.exitCode = c), (e) => {
    // An unexpected bug in the reporter is still not the outage, but a crashed
    // self-test is a failed positive control.
    warn(`drift-issue crashed: ${e.stack || e.message}`);
    process.exitCode = process.argv.includes('--self-test') ? 1 : 0;
  });
}
