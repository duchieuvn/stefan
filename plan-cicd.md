# Auto-deploy `main` to the server, one step at a time

## Context

Goal: every push to `main` updates the Pi and restarts the backend services on the new code, without anyone logging in.

Chosen approach:

- A GitHub Actions self-hosted runner on the Pi triggers the deploy.
- The deploy waits while the water pump is mid-run.
- The deploy rolls itself back if the new code fails to start.

The user wants this built in **small steps**. Each step ends in a working state with its own "done when" check, and we only move on after that check passes on the real Pi.

Placeholders: `DEPLOY_DIR` is the Pi checkout (e.g. `/home/pi/kernbild_01`), `SERVICE_USER` is the user running the services, and `SERVICES_PYTHON` is the python of the `stefan` env on the Pi.

Every step is useful on its own. If we stop after step 3, you already have services that start at boot and restart if they crash. After step 5, you have a one-command deploy.

---

## Step 1: Get the new manager onto `main` and the Pi (by hand)

- **I do:**
  - Commit the manager refactor (`manage_services.py`, `service_runtime.py`, the four services, `.gitignore`) on a branch and open a PR.
  - Add the missing `pymodbus` and `pyserial` to `backend/requirements.txt`. You give me the versions from `pip freeze` on the Pi.
- **You do:** merge the PR. On the Pi:
  - `python manage_services.py stop-all` stops the old-style services.
  - `git pull`.
  - `python manage_services.py start-all`.
- **Done when:** on the Pi,
  - `status` shows all 4 running,
  - the TUI works,
  - `run-now pump` followed by `stop pump` switches the real pump ON then OFF,
  - the sensor log shows rounds with real RSSI values.

## Step 2: The manager runs under systemd instead of cron

- **I do:**
  - Add `manage_services.py daemon --start-all`, so the daemon starts all services itself.
  - Add `deploy/stefan-services.service`, a systemd user unit: `Restart=always`, `KillMode=mixed`, `TimeoutStopSec=60`.
  - Add a short `deploy/README.md` with the install commands.
- **You do on the Pi:**
  - `manage_services.py shutdown`.
  - Remove the `@reboot` cron line.
  - Copy the unit to `~/.config/systemd/user/` and fill in the paths.
  - `sudo loginctl enable-linger $SERVICE_USER`.
  - `systemctl --user enable --now stefan-services`.
- **Done when:**
  - `systemctl --user restart stefan-services` restarts all 4 services cleanly. The pump goes OFF if it was running, and the daemon log shows an orderly stop.
  - `kill -9 <daemon pid>` brings everything back within about 10 s.
  - After `sudo reboot`, everything is running without logging in.

## Step 3: Machine-readable status

- **I do:** add `manage_services.py status --json`, which prints the daemon's raw status. It's a small addition that the later steps rely on.
- **You do:** pull on the Pi, then `systemctl --user restart stefan-services`. This is the manual deploy, for now.
- **Done when:** `status --json` on the Pi prints valid JSON that includes the pump's `activity`.

## Step 4: Deploy script, run by hand, basic version

- **I do:** add `deploy/deploy.sh`, run on the Pi with `DEPLOY_DIR` and `SERVICES_PYTHON` set. It does five things:
  - Refuses to run if tracked files have local changes.
  - Fast-forwards to `origin/main`.
  - Runs `pip install -r` only if `requirements.txt` changed.
  - Runs a smoke test (`compileall`, then `import manage_services`).
  - Runs `systemctl --user restart stefan-services`.
- **I test first:** locally, against a throwaway clone with a stub `systemctl`.
- **You do on the Pi:** push a trivial commit (e.g. a log message), then run `deploy/deploy.sh`.
- **Done when:**
  - The Pi is on the new commit and the service logs show a fresh start header.
  - Running it again with no new commit is a harmless no-op.
  - With a deliberately edited tracked file, it refuses and changes nothing.

## Step 5: The deploy script gets safe (pump wait, health check, rollback)

- **I do:** extend `deploy.sh` in three ways:
  - Before restarting, wait until the pump is idle. It polls `status --json` every 30 s and gives up after `PUMP_WAIT_MAX_MIN`, default 90.
  - After restarting, check that all services are `running` and none are `restarting`.
  - If that check fails, roll back: `git reset --hard` to the previous commit, restart, and exit 1.
- **I test first:** locally, with the fake-hardware setup.
- **You do on the Pi:** test each behaviour in turn.
- **Done when:**
  - `run-now pump` then `deploy.sh` makes the script wait, and it restarts only after the pump run ends.
  - A commit that makes a service crash on start is rolled back automatically: the services run on the old commit and the script exits non-zero. We then revert that commit.

## Step 6: GitHub can reach the Pi (runner only, no deploy yet)

- **I do:** add `.github/workflows/deploy.yml` with a single harmless job: `runs-on: [self-hosted, stefan-server]` that prints `hostname`, `whoami` and `git -C $DEPLOY_DIR log -1`. Triggers: push to `main` plus a manual button.
- **You do:**
  - On GitHub: Settings → Actions → Runners → New self-hosted runner (Linux ARM64).
  - On the Pi as `SERVICE_USER`: `./config.sh … --labels stefan-server`, then `sudo ./svc.sh install $SERVICE_USER && sudo ./svc.sh start`.
  - Add repo variables `DEPLOY_DIR` and `SERVICES_PYTHON`.
  - If the repo is public: require approval for workflows from fork PRs.
- **Done when:**
  - A push shows a green run in the Actions tab, and its log prints the Pi's hostname and user.
  - The runner survives a Pi reboot.

## Step 7: The workflow runs the deploy

- **I do:**
  - Change the job to check out the commit (`fetch-depth: 0`) and run `bash deploy/deploy.sh`.
  - The script fetches from the runner's checkout, so the Pi clone needs no GitHub credentials.
  - Add `concurrency: deploy-server` so deploys queue instead of overlapping.
  - Set `timeout-minutes: 120` to cover the pump wait.
- **Done when:**
  - A trivial push is live on the Pi within about a minute, with a green run.
  - Pushing while the pump runs shows "waiting for pump" in the job log.
  - A crashing commit gives a red run and an automatic rollback.

## Step 8 (optional): Check the code before it reaches the Pi

- **I do:** add a `check` job on `ubuntu-latest` (compile, plus pyflakes on the manager and the services). The deploy job gets `needs: check`. Optionally, branch protection on `main`.
- **Done when:** a push with a syntax error fails at `check`, and the Pi is never touched.
