# Staged workflow files

These belong under `.github/workflows/` and are kept here only because the automation
account that develops this repository has no `workflow` OAuth scope — GitHub rejects any
push that creates or updates a file in that directory.

To install them:

```bash
git mv ops/workflows/ci.yml   .github/workflows/ci.yml
git mv ops/workflows/docs.yml .github/workflows/docs.yml
```

`ci.yml` replaces the one already installed. The difference is that it calls `make`
targets instead of spelling the commands out itself, so that after this one move,
changing *how* something is checked is an edit to the `Makefile` — a file the automation
account can push — rather than another round trip through here.

`docs.yml` additionally needs GitHub Pages enabled for the repository with **Source: GitHub
Actions** (Settings → Pages). Until that is set, the deploy step will fail while the
build step still passes.
