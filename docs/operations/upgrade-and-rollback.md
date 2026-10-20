# Upgrade and rollback

## Upgrading

```sh
cd /opt/celius
make upgrade                  # follow the current branch
make upgrade REV=v1.3.0       # or a tag / commit
```

`infra/scripts/upgrade.sh`:

1. Refuses to run if the checkout has local changes.
2. Shows the commits it is about to apply.
3. Takes a **pre-upgrade backup** (`celius-…-pre-upgrade.tar.enc`). If it fails, nothing
   else happens. A failed off-host copy is tolerated: the local archive is enough to
   roll back.
4. Tags the running images `celius-api:rollback` and `celius-sandbox:rollback`.
5. Records what to go back to in `backups/last-upgrade.env`.
6. Checks out the new revision, builds, and runs `docker compose up -d`. The `migrate` step
   applies new migrations before the api starts.
7. Waits up to three minutes for `/api/v1/health/ready`.

Read the release notes before upgrading: anything that needs a new `.env` setting is
listed there. `.env.example` always has every setting with its default.

## Rolling back

```sh
make rollback                 # infra/scripts/rollback.sh [--yes]
```

This goes back to the previous revision and its saved images (or rebuilds them). Then:

- **The upgrade added no migrations**: the data is kept as it is. Nothing is lost.
- **The upgrade changed the schema**: the database and files are restored from the
  pre-upgrade backup, so anything written since the upgrade is lost. The script says so
  and asks first.

Database downgrades (`alembic downgrade`) exist for every migration and are tested
(`tests/test_migrations.py`). The rollback still restores the backup instead, because
restoring is the path that is always known to give back exactly the data from before.

## If an upgrade fails halfway

| Symptom | What to do |
| --- | --- |
| `migrate` exited with an error | `docker compose -f infra/compose.yaml logs migrate`. The migration ran in one transaction, so the database is unchanged: fix the cause or `make rollback` |
| api not ready | `… logs api`. Often a missing setting; compare `.env` with `.env.example` |
| Build failed (network) | Nothing has changed yet except the checkout: `git checkout <FROM_REVISION from backups/last-upgrade.env>` and `make up` |

## Tested

The CI release gate (`infra/scripts/test-clean-deployment.sh`) runs the rollback after a
backup and restore, and checks that the data survives it. The pre-upgrade backup is an
ordinary backup, covered by the same restore tests.
