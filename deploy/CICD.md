# CI/CD: tests on every push, automatic deploy from `main`

Workflow: `.github/workflows/backend.yml`.

```
pull request ─┐
push to main ─┴─> Lint ─┐
                  Tests (SQLite, coverage ≥ 80%) ─┼─> (main only) Deploy ─> verify live
                  Tests (MySQL) ─┘
```

Pull requests run the checks and never deploy. A push to `main` deploys only when
every check passed.

## What a deploy does

1. Signs in to Google Cloud as `github-deployer` with **Workload Identity
   Federation**: GitHub hands Google a short-lived token, so no key is stored
   anywhere. Only runs of this repo on `main` are accepted.
2. Starts the VM if it is stopped (it stops 21:00–07:00 IST) and waits for SSH
   through Google's IAP tunnel.
3. Copies `deploy/ci-deploy.sh` to the VM and runs it as root with the commit:
   waits for apt, backs up the SQLite database (last 10 kept as
   `arul_motors.db.pre-deploy-*`), fast-forwards the VM's clone to exactly that
   commit (it must be on `origin/main`), runs `deploy/vm-setup.sh`.
4. The API now reports the commit in `/api/health` (`version`). The VM checks it,
   and the workflow checks it again from outside at `https://backend.arulmotors.com`.
5. If the new version is not healthy, the VM puts the previous commit back and the
   workflow fails (red). The database is never rolled back; schema changes are
   additive (`_ensure_column`), so older code still runs on it.

## One-time setup

1. **Google Cloud** (as a project owner): `bash deploy/gcp-setup-cicd.sh`. It creates the
   service account, a custom role limited to starting/reaching this VM, the
   identity pool and provider, and an IAP firewall rule. It prints two values.
2. **GitHub variables** (repo Settings → Secrets and variables → Actions → Variables),
   or with the CLI:
   ```
   gh variable set GCP_WIF_PROVIDER --body "projects/…/providers/github"
   gh variable set GCP_DEPLOY_SA    --body "github-deployer@<project>.iam.gserviceaccount.com"
   ```
3. **Environment** `production` (Settings → Environments): limit it to the `main` branch.
   Optionally add yourself as a required reviewer to approve each deploy.
4. **Recommended**: protect `main` (Settings → Branches): require the status checks
   `Lint`, `Tests (SQLite)` and `Tests (MySQL)` before merging.

## Day to day

- Work on a branch, open a pull request, merge when green: the merge deploys.
- Pushing straight to `main` also works: it is tested, and deployed only if it passes.
- Re-run or force a deploy: Actions → Backend CI/CD → Run workflow (on `main`).
- A push at night starts the VM; the existing schedule stops it again later.

## When a deploy fails

- The run's log shows the VM's output. On the VM: `journalctl -u arul-api -n 50`.
- A rolled-back deploy leaves the previous version serving; fix and push again.
- To restore data: `sudo -u arul sqlite3 /opt/arul-motors/arul_motors.db ".restore '/opt/arul-motors/arul_motors.db.pre-deploy-<time>'"`
  (stop `arul-api` and `arul-voice` first).

## What the tests guard

Beyond the feature tests: every `/api` route needs a login (walks the real route
table), an old database upgrades in place without losing rows, the agent prompts
stay within a size budget (they are re-read every turn and cost money), no
secrets, `.env`, databases or client workbooks are tracked (the repo is public),
and the shell scripts parse.

The VM runs no GitHub runner: the repository is public, and a runner on the
production VM would run code from any pull request.
