# Public Release Checklist

## 0. Choose the publication topology — BLOCKING DECISION

The topology is **Path A**: Private remains the ongoing canonical development
repository; the distribution repository receives periodic reviewed snapshots.

Preparation checkpoint (2026-10-04): the development repository is
`akiaki524/FLOP-private`. The distribution repository `akiaki524/FLOP` has been
created PRIVATE, with an initial clean root and reviewed incremental updates
from Private. It remains PRIVATE at this checkpoint; apply the publication gates
before any separately approved Public visibility change. Check GitHub for the
current visibility. Local folder names are separate.

There are two different release paths. Do not mix their gates.

### Path A — clean Public repository from a sanitized tree (preferred)

Use this when the goal includes keeping historical personal metadata, old
Private Issues / PRs, branch history, and historical Actions logs out of public
reach.

1. keep the existing repository Private as the ongoing canonical development
   repository, retaining its engineering history;
2. prepare the approved Public file set and final clean root as described in
   section 2.1; `scripts/export_clean_public_snapshot.sh` creates only an
   intermediate full-source candidate;
3. publish that one-root-commit repository initially, then append reviewed
   snapshot updates to its Public history (section 2.2);
4. recreate only publication-safe Issues / summaries that are actually useful;
5. the Private rename and distribution repository creation are complete;
   verify remaining trusted writers before making the distribution Public.

GitHub warns that reusing the old repository name stops redirects from the old
location to the renamed repository. Therefore, before reusing
`akiaki524/FLOP`, update every trusted Private clone, automation, and maintainer
remote, including Codex environment targets, to the new Private URL and verify
no operational writer still targets the old URL. Otherwise an old clone can start addressing the new Public
repository instead of the Private development repository.

Because old history is **not copied** in Path A, the Private repository's
historical commit email, PR refs, abandoned branches, and Actions history do
**not** need to be rewritten or deleted merely to publish the clean repository.

### Path B — in-place Private → Public visibility change

Use this only when preserving the existing GitHub history / Issues / PRs is more
important than completely separating the old Private metadata.

Path B requires the additional full-history, Issue / PR, branch, and Actions
gates in section 3 and explicit Human acceptance of residual references that a
history rewrite may not make unreachable.

## 1. Common release-tree gates — BLOCKING FOR BOTH PATHS

- `Public Hygiene CI` passes on the exact release commit.
- No personal workstation username, personal email, private Claude session URL,
  or private Notion page URL is present in the tracked release tree.
- Public protocol identities may remain where they are required for exact
  implementation / signature / policy binding. Historical operational docs
  should use placeholders when the exact identity is not needed.
- No committed private-key PEM, GitHub token, OpenAI/Anthropic-style secret
  token, or AWS access-key pattern is present in the tracked release tree.
- Runtime / Production evidence kept in GitHub uses publication-safe
  placeholders for personal machine paths and identity values unless the value
  is intentionally public and necessary to understand the implementation.
- Root and path-specific licensing notices accurately describe the rights that
  are actually granted; changing visibility must not silently relicense code.
- Public-facing README / routing docs do not require access to Private Issue /
  PR history. Historical numbers may remain as provenance identifiers when
  clearly labeled as such.

Run:

~~~bash
python3 -B scripts/check_public_hygiene.py
~~~

### Owner-only personalized gate

Default CI runs generic checks only and explicitly reports personalized checks
DISABLED. Its PASS does not establish absence of owner-specific identifiers.
Before publication the owner must run the personalized checks using the PRIVATE,
ignored file `.local/public-check-identifiers.json`. It contains nonempty
`usernames` and `emails` lists plus optional `owner_names`; never put its
values in tracked files, CI settings, secrets, logs, or exports. A requested
missing/invalid configuration fails rather than falling back to generic checks.

~~~bash
python3 -B scripts/check_public_hygiene.py --private-config .local/public-check-identifiers.json
python3 -B scripts/check_public_commit_metadata.py --private-config .local/public-check-identifiers.json HEAD
~~~

For a later exported checkout, explicitly pass the absolute PRIVATE source config
path; relative paths change meaning across checkouts. Do not copy the input.
Path B additionally requires personalized history scanning via the same option.
The initial exporter runs generic gates; it does not fulfill this owner gate.

### Approved public-only treatment of quoted evaluation evidence

Retain the original seven quoted evaluation JSON records in Private:

- `empirical-evaluation-20260919.json`
- `empirical-evaluation-batch5-20260919.json`
- `empirical-evaluation-batch6-20260919.json`
- `export-reliability-saturation-batch15-20260919.json`
- `intake-reliability-batch13-20260919.json`
- `material-fetch-efficiency-batch14-20260919.json`
- `material-intake-gap-analysis-batch11-20260919.json`

All are under `components/collaboration/agent/docs/`. The approved treatment
keeps these originals in Private and excludes them from the eventual Public
file set. At release packaging, provide separately named derived summaries
without quoted text, retaining source locators, selectors, original-evidence
digests and evaluation results. Label them explicitly as quote-omitted Public
summaries, not valid outcomes or reproducible replay fixtures. A retained digest
describes the original evidence, not the sanitized summary's JSON bytes.

Use a separate summary schema with the explicit top-level marker
`"kind": "quote_omitted_public_summary"`. Its field allowlist is structured
source IDs/locators, selectors, original-evidence/bundle/material digests,
status/verdict, numeric counts and metrics. Select these fields explicitly;
do not copy whole result objects. Exclude `quote`, `value`, `result`,
`batch3`/`batch4`/`batch5` (and other copied batch results), `context`,
`ask`, `done`, free-text `reason`, previews and other text bodies by default.
Review the allowed fields' contents as well so copied text is not repackaged
inside them. No original quote key or nested response body belongs in this
derived schema.

The empirical replay in `components/collaboration/agent/tests/evaluate_empirical.py`
requires Private originals and local evidence; it remains Private-only.
Do not substitute summaries at the original JSON paths: the replay compares
complete results, and `validate_outcome` requires and verifies citation quotes.
Preserve that validator and the synthetic Component tests. Summary generation
and Public file-set preparation happen at release packaging, not during this
local remediation; do not modify or delete Private evidence.

## 2. Path A — initial publication and periodic snapshot updates

### 2.1 Initial clean-root publication

The existing exporter is initial-only: it rejects an existing destination and
creates exactly one root commit. Do not rerun it on an existing Public checkout.
It copies the entire tracked source tree and does not implement file exclusions
or derived-summary generation.

Create an intermediate full-source candidate from the reviewed release commit:

~~~bash
scripts/export_clean_public_snapshot.sh ../FLOP-public <release-commit>
~~~

For this intermediate candidate, verify before release packaging:

- the export has exactly one root commit;
- the root commit uses the Project owner's approved no-reply identity and is
  created without inherited commit signing / hooks;
- the intermediate exported tree SHA exactly matches the reviewed source tree;
  this checks the exporter, not final Public release suitability;
- the export has no remote;
- `scripts/check_public_hygiene.py` passes inside the export;
- the expected source files, workflows, path-specific LICENSE / NOTICE files,
  and public documentation are present;
- no Private Issue / PR / Actions / branch history is present;
- any public Issues / project-status summaries to be recreated have been
  selected and sanitized separately.

Before any remote is added or push occurs, prepare the approved Public file set
with the seven original JSON files excluded and the reviewed derived summaries
included. Verify that final file set, modes/types and contents against the pinned
source revision and approved transformations, rather than requiring blind
full-source tree SHA equality. Record exclusions and source mapping in Private.

Intermediate full-source candidates and earlier unsafe exports are local-only:
never add remotes to them or use their Git histories as the final Public base.
Preserve existing folders in this remediation. Create the final clean root
separately from the approved file set; any later disposal is a separate action.

The final initial Public root must contain only that approved release content.
Do not append a deletion/redaction commit to the full-source candidate: its
original quoted blobs would remain reachable through the first commit.
The current exporter cannot create this transformed final root; a separately
reviewed packaging/root-creation step is still required before publication.
No such packaging or final-root creation is performed by this local fix.

The export leaves the Private development repository unchanged and Private;
ongoing development continues there.

Repository rename / creation / first Public push are Human Publish Gates.

### 2.2 Subsequent incremental Public snapshot updates

Keep the existing Public Git history. Each snapshot update is an ordinary
incremental commit based on that history, not another root export or a merge /
cherry-pick of Private commits. This is a manual reviewed procedure; the initial
exporter does not implement updates.

1. before choosing the source revision, incorporate adopted changes originating
   in Public into Private so the next snapshot does not revert them. Explicitly
   choose one immutable Private source commit, define the intended Public file
   set from that revision, and review its release content against the common
   gates in section 1. Record the source commit, intended file set,
   and any deliberate exclusions in the Private release record. Do not
   automatically disclose Private commit IDs or operational context in Public
   commit messages;
2. verify the intended Public checkout / remote and its clean working state,
   then prepare a Public work branch based on the existing Public history;
3. compare the approved release content with the current Public tree and review
   the additions, modifications, and deletions, including dotfiles such as
   `.github/` and `.gitignore`. Apply only that reviewed delta. Check removals
   against the approved Public file set so stale or newly excluded Public files
   do not remain; unrelated Private-only files are not publication inputs.
   Preserve the Public checkout's own `.git`, refs, and remote settings;
   never copy the Private `.git`, Git history, untracked / ignored operational
   files, credentials, or secrets. Avoid copying the live working directory or
   using a blanket directory deletion / replacement;
4. in the Public checkout, inspect `git diff --name-status` and `git diff`,
   run `git diff --check` and `python3 -B scripts/check_public_hygiene.py`, and
   review the staged diff as well. Dotfiles, removals, licensing, and workflows
   are part of the release-tree review, not just the visible source files.
   Also run the mandatory owner gate in this checkout using the absolute
   Private input path (replace the placeholder; do not copy the input):

   ~~~bash
   python3 -B scripts/check_public_hygiene.py --private-config /ABSOLUTE/PRIVATE-CHECKOUT/.local/public-check-identifiers.json
   ~~~

   A generic PASS is not a substitute for this personalized gate;
5. when separately authorized to commit, create the incremental Public commit
   using the approved identity. Verify its tree matches the approved release
   content from the pinned source revision and its ancestry remains in the
   existing Public history. Exact full source-tree SHA equality applies when
   the entire tracked source tree was explicitly approved for release, as with
   an initial full-tree release. With deliberate exclusions, verify the
   approved file set, file modes / types, and contents instead; never include
   Private-only files merely to satisfy full-tree equality. Run
   `python3 -B scripts/check_public_commit_metadata.py HEAD` and satisfy the
   existing Public Hygiene CI / PR requirements on the exact commit. Also run:

   ~~~bash
   python3 -B scripts/check_public_commit_metadata.py --private-config /ABSOLUTE/PRIVATE-CHECKOUT/.local/public-check-identifiers.json HEAD
   ~~~

   This personalized metadata gate is mandatory for the owner release;
6. submit the reviewed update through the existing Public publication and
   protected-branch gates. Once PR-based main protection is enabled, future
   distribution updates must use a work branch and PR; this is the planned
   protected workflow, not a claim that protection is already enabled.
   Use normal history-preserving updates; do not replace
   Public history or force-push to distribute a snapshot. Record the resulting
   Public commit and source mapping in the Private release record.

Preparing a local snapshot does not authorize a commit, push, rename, or
publication. The Private repository remains the development source throughout.

## 3. Path B only — in-place publication gates

### 3.1 Full reachable Git history — BLOCKING

Run the full-history scanner from a trusted clone with all writable branches and
tags present:

~~~bash
python3 -B scripts/check_public_history.py --private-config /ABSOLUTE/PRIVATE-CHECKOUT/.local/public-check-identifiers.json
~~~

It scans commit metadata and reachable text blobs for configured identifier
literals and generic secret formats. Replace the absolute Private input
placeholder. An unconfigured generic PASS is not sufficient for this gate.

Known pre-cleanup condition: historical commits contain personal metadata.
The configured scan can detect matching literals, but PASS is not proof that
all historical personal content is absent. In particular, old versions of the
three public checker scripts contain split identifier constants which this
literal scanner cannot reconstruct. Path B requires explicit review and an
approved disposition of those historical blobs and other encoded identifiers.
Path A does not copy that Private history and requires no rewrite here.

If Path B is chosen:

1. make a fresh trusted clone while the repository is still Private;
2. record all branch / tag heads;
3. use current `git-filter-repo` with sensitive-data-removal mode where
   appropriate;
4. replace personal machine identifiers in historical text;
5. rewrite personal commit email metadata to an approved no-reply identity;
6. rerun the configured history command above and complete the explicit
   historical split-identifier review; a literal scan alone is insufficient;
7. inspect changed refs and affected pull-request refs before any force push;
8. force-push rewritten writable refs only after Human review.

Do not squash the entire engineering history merely to make this gate pass.

### 3.2 GitHub Issues / PRs — BLOCKING

Before an in-place visibility flip:

- editable Issue / PR bodies and comments must not contain personal home paths,
  personal email addresses, private workspace/session URLs, or private Notion
  links;
- operational identity values should use publication-safe placeholders unless
  the exact value is intentionally part of the public protocol record;
- preserve engineering findings and lessons; remove only unnecessary personal /
  private-environment linkage.

### 3.3 Branches — BLOCKING

- unmerged / divergent branches require an explicit disposition;
- abandoned branches containing superseded signer or production paths must not
  remain as the only live branch ref to that code;
- do not delete an active reviewed PR branch solely for cosmetic cleanup.

### 3.4 Actions history / artifacts — BLOCKING

Changing an existing Private repository to Public exposes existing Actions
history and logs.

- review historical workflow runs that executed Real / Host /
  credential-adjacent tests;
- delete runs or artifacts that contain unnecessary private operational
  information;
- retain only logs that are safe to make public.

### 3.5 Residual historical reachability — HUMAN ACCEPTANCE

A history rewrite does not guarantee that every old commit disappears from
pull-request refs or cached views. Path B may therefore retain residual
historical exposure even after writable refs are rewritten. Record that
acceptance explicitly before the visibility flip.

## 4. Workflow posture on the release tree — BLOCKING

Public PR workflows should retain:

- GitHub-hosted runners unless a separately reviewed boundary requires
  otherwise;
- least-privilege permissions such as `contents: read`;
- immutable action commit pins;
- `persist-credentials: false` for PR-controlled checkout paths;
- no `pull_request_target` execution of untrusted PR code.

## 5. Publication action — HUMAN GATE

### Path A — initial publication

1. verify the pinned source commit, approved Public file set and final clean root;
2. verify the completed Private rename and the intended new repository name;
3. before reusing the old name, update and verify all trusted Private remotes /
   automation / Codex environment targets so none still targets
   `akiaki524/FLOP`;
4. when authorized, create the new distribution repository PRIVATE first;
5. add the new Public remote only to the final approved clean repository;
6. when separately authorized, push the approved one-root-commit `main` while
   the destination remains PRIVATE;
7. verify the uploaded tree, root ancestry and release gates while PRIVATE;
8. configure and verify the section 6 protections and contributor controls
   while PRIVATE where available; resolve any Public-only controls before
   accepting external contributions;
9. only after explicit Human authorization, change visibility to PUBLIC;
10. verify the Public repository from a logged-out / unauthenticated view;
11. verify a trusted Private clone still fetches from the renamed development
   repository and cannot accidentally address the new Public repository through
   the old remote URL.

### Path B

1. verify every Path B gate above;
2. verify the exact rewritten release refs;
3. change repository visibility from Private to Public;
4. verify the repository from a logged-out / unauthenticated view.

## 6. Immediate post-public controls — BLOCKING FOLLOW-UP

Immediately after either path:

- enable protection / rulesets for `main`;
- require PR-based changes and required CI as appropriate;
- prohibit force-push / deletion of the protected default branch;
- configure Actions approval to require maintainer approval for **all external
  contributors** before fork PR workflows run;
- verify Secret Scanning / dependency / security settings available to the
  Public repository;
- verify no unexpected workflow, artifact, branch, Issue, PR, or release is
  public.

Record the final Public commit and completed checks in the controlling release
record.

## 7. GitHub rename / redirect reference

GitHub's repository-rename documentation states that the old repository URL
normally redirects to the renamed repository, but that creating a new
repository under the old name stops that redirect. This is why Path A treats
trusted-clone remote migration as a blocking step before reusing
`akiaki524/FLOP`.

Reference:
https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository
