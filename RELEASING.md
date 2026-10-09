# Releasing

The version has one home: `__version__` in `src/matview/__init__.py`. Hatchling reads the
distribution's version from it, and the release workflow refuses a tag that disagrees,
so there is no second place to forget.

What is **not** released this way is the fingerprint specification. `SPEC.md` carries its
own `fingerprint_spec_version`, recorded in every database's `meta` table; bumping it
invalidates every existing cache, so it moves on its own schedule and never as a side
effect of a release.

## One-time setup

These need repository or account access, so they are not things the automation account
can do. Nothing below has to be repeated per release.

- [ ] Make the repository public.
- [x] Rename the default branch to `main` (done 2026-10; PR #1 merged on it). The docs
      workflow deploys only from `main`, `mkdocs.yml` points `edit_uri` at `edit/main`,
      and the `Changelog` URL in `pyproject.toml` points at `blob/main`.
- [ ] **Settings → Pages → Source: GitHub Actions.** Until this is set the docs build
      passes and the deploy step fails, which is easy to misread as a broken site build.
- [ ] Install [`ops/workflows/release.yml`](ops/workflows/release.yml) as
      `.github/workflows/release.yml`. It is staged rather than installed because
      pushing a file under `.github/workflows/` needs a token scope the automation
      account does not have.
- [ ] Add `"3.14"` to the matrix in `.github/workflows/ci.yml`. Verified locally: the
      full suite with every codec extra passes on 3.14, and the golden codec fixtures
      match, so this is a metadata catch-up rather than a port.
- [ ] Create the `pypi` and `testpypi` environments under **Settings → Environments**.
      The release workflow names them, and an environment is what lets you require a
      manual approval before an upload if you ever want one.
- [ ] Register the trusted publisher on <https://pypi.org/manage/account/publishing/>:
      owner `tresoldi`, repository `matview`, workflow `release.yml`, environment `pypi`.
      Repeat on <https://test.pypi.org> with environment `testpypi`. This is what makes
      publishing credential-free; there is no API token to store or rotate. The
      registration is bound to the workflow *filename*, so renaming the file breaks it.
- [ ] **Settings → Security → enable private vulnerability reporting.** `SECURITY.md`
      tells people to use it, and with it switched off that link is a dead end.
- [ ] Authorize Zenodo for the repository on <https://zenodo.org/account/settings/github/>
      before the first tag, since Zenodo only archives releases made after the switch is
      flipped. After the first release, put the DOI in `CITATION.cff` and its badge in
      `README.md`.

## Cutting a release

1. `make check` — quality, both test passes, the codec floor.
2. Bump `__version__` in `src/matview/__init__.py`.
3. `CHANGELOG.md`: retitle `## [Unreleased]` to `## [x.y.z] — YYYY-MM-DD` and open a
   fresh empty `## [Unreleased]` above it. The release notes are taken verbatim from this
   section, and the workflow fails if there is no section matching the tag — so read the
   section as a reader of the release page would, and drop any "not yet published"
   framing left in its body.
4. `CITATION.cff`: set `version` and `date-released`.
5. `make release-check` — builds the sdist and wheel, validates the metadata the way an
   index does, and installs the wheel in isolation to confirm it imports, ships
   `py.typed` and resolves its console script. The test suite cannot catch those: it
   imports an editable install.
6. Commit, then `git tag -a vx.y.z -m "matview x.y.z"` and
   `git push origin main --follow-tags`.
7. The tag runs the release workflow: checks, the tag-versus-`__version__` guard, build,
   publish, and a GitHub release whose notes are the changelog section.
8. Confirm the result from the outside: `uv run --no-project --with matview==x.y.z
   matview --version` installs from the index rather than the working tree.

## Dry run

Run the **Release** workflow manually with `index: testpypi`. It exercises the whole path
against TestPyPI without spending a version number on the real index. TestPyPI needs its
own trusted-publisher registration and rejects a version it has already seen, so treat a
dry run as a one-shot per version too.
