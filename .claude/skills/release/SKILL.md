---
name: release
description: Cut a tagged GitHub release for collide-logging-py after a feature PR merges. Use when the user asks to "do a release", "cut a release", "release the patch", or "tag and publish" the package. Handles squash-merge, main sync, tag creation, and the release-notes format.
disable-model-invocation: true
argument-hint: [PR number] [vX.Y.Z]
allowed-tools: Bash(gh pr view:*), Bash(gh pr checks:*), Bash(gh pr merge:*), Bash(gh release create:*), Bash(gh release view:*), Bash(git checkout:*), Bash(git pull:*), Bash(git log:*), Bash(git fetch:*)
---

# Cut a release

Releases are cut from `main` after the feature PR merges. The version bump (`pyproject.toml` + `src/collide_logging/__init__.py`) and the `CHANGELOG.md` entry land **in the feature PR**, not here — so by the time you run this, the repo already carries the new version. Confirm that before publishing; if the bump or CHANGELOG entry is missing, stop and fix the PR first.

`$ARGUMENTS` may contain the PR number and/or the target version (`vX.Y.Z`). Infer whichever is missing from the merge target and the version already in `pyproject.toml`.

## Procedure

1. **Confirm the PR is green and mergeable.** `gh pr checks <n>` (lint, test, and `check-issue-link` must all pass) and `gh pr view <n> --json mergeStateStatus,mergeable`. Note `check-issue-link` only re-fires on push, not on `gh pr edit` — if it's stale, the PR body must be fixed and pushed, not just edited.

2. **Confirm the version is bumped and CHANGELOG has an entry** for the target version. Read `pyproject.toml`, `src/collide_logging/__init__.py`, and the top of `CHANGELOG.md`. All three must already reflect `vX.Y.Z`. If not, do not release — the bump belongs in the PR.

3. **Squash-merge and delete the branch:** `gh pr merge <n> --squash --delete-branch`. Squash is the repo convention — GitHub appends the PR number, producing `<subject> (#<n>)` commits on `main`.

4. **Sync local main:** `git checkout main && git pull --ff-only`. Confirm the squash commit is at `HEAD`.

5. **Create the GitHub release, which also creates the `vX.Y.Z` tag:** `gh release create vX.Y.Z --target main --title "vX.Y.Z — <short description>" --notes "..."`. Tags are `vX.Y.Z`; the release title is `vX.Y.Z — <short description>`.

## Release-notes shape

Mirror the CHANGELOG entry. `gh release view v0.5.0` is the canonical example to copy. The body is:

- A one-line summary of what the release does.
- The install snippet: the `uv add "git+https://github.com/collide-ai/collide-logging-py.git@vX.Y.Z"` line in a fenced bash block.
- `## Fixed` / `## Added` / `## Known limitation` / `## Compatibility` sections, only those that apply.
- A `**Full prior history:**` footer pointing at the CHANGELOG / prior releases.

## After publishing

Report the release URL and confirm the tag exists (`git fetch --tags && git tag | grep vX.Y.Z`). The package is internal-only (not on PyPI) — there is no publish step beyond the GitHub release; consumers install via the tag.
