# Backup and restore

Everything Celius knows lives in four places, and a backup captures all four together:

| Part | Where | In the archive |
| --- | --- | --- |
| Database (chats, projects, memories, settings, audit log) | `postgres-data` volume | `db.dump` (`pg_dump -Fc`) |
| Stored files (uploads, exports, thumbnails) | `app-data` volume | `app-data.tar.gz` |
| Tool workspaces (files the model made in its sandbox) | `workspaces` volume | `workspaces.tar.gz` (`BACKUP_INCLUDE_WORKSPACES`) |
| Settings | `.env` | `config/env`, plus `config/compose.yaml` and `config/git-revision` |

`manifest.json` lists a SHA-256 for every part and a census of the database: rows per
table and the schema revision. A restore is checked against it.

## Setting up

In `.env`:

```sh
BACKUP_PASSPHRASE=…            # openssl rand -base64 32. Keep a copy OFF this machine.
BACKUP_DIR=../backups          # relative to infra/, or absolute
BACKUP_RETENTION_DAYS=30
BACKUP_OFFSITE_TARGET=backup@nas:/volume1/celius     # rsync over SSH, or
# BACKUP_OFFSITE_TARGET=rclone:b2:my-bucket/celius   # any rclone remote
```

Without the passphrase a backup can't be decrypted, and the passphrase is inside the
backup's own copy of `.env`. Store it in a password manager.

For an rsync target, give root on the host a key the NAS accepts (`ssh-copy-id`). For
rclone, run `rclone config` as root. Then schedule it nightly:

```cron
15 3 * * *  cd /opt/celius && infra/scripts/backup.sh >>backups/backup.log 2>&1
```

## Taking a backup

```sh
make backup                 # or: infra/scripts/backup.sh [--label NAME] [--no-offsite]
```

1. The api and worker are paused, usually for a few seconds. This gives a database and file
   store from the same moment. A reply in progress is resumed by the worker afterwards;
   open apps reconnect.
2. `pg_dump`, the file store and workspaces are archived. Temporary files, uploads in progress
   and status files are left out.
3. The manifest is written, and the api and worker start again.
4. Everything is packed into `celius-<UTC time>.tar.enc` with AES-256-CBC and PBKDF2
   (200 000 iterations). A `.sha256` file goes next to it.
5. The archive is copied off-host, archives older than the retention period are pruned,
   and `last-success.json` is updated.

Exit codes: 0 means OK; 2 means the archive was made but the off-host copy failed; anything
else means failure. On failure the services are restarted either way.

The **Account → System status** panel, `make status` and the alert webhook show the
backup's age. They warn after 26 h and go critical after 72 h. They also warn while
backups only exist on this machine.

## Proving a backup works: the restore drill

```sh
make restore-drill                              # the newest archive
infra/scripts/restore-drill.sh backups/celius-….tar.enc [--keep]
```

The drill restores into a separate compose project, `celius-drill`, with its own database
and volumes on port 18099, next to the live deployment, which it doesn't touch. It runs
the full verification, including re-hashing every stored file, and checks that the accounts
are there. Then it deletes itself. The worker isn't started, so scheduled tasks don't run
twice. Run it monthly, and after changing anything about backups.

## Restoring

On the same machine, after data loss or a bad change:

```sh
make restore ARCHIVE=backups/celius-20261020T031500Z.tar.enc
```

On a new machine (the old one is gone):

```sh
git clone … /opt/celius && cd /opt/celius
git checkout <the revision in the backup, or newer>
# copy the archive (and its .sha256) here, from the NAS / rclone remote
BACKUP_PASSPHRASE='…' infra/scripts/restore.sh /path/to/celius-….tar.enc
```

`restore.sh` (`--yes` skips the confirmation, `--deep` re-hashes files):

1. Checks the archive against its `.sha256`, decrypts it, and checks every part against
   the manifest. A wrong passphrase or a damaged archive stops here, with nothing touched.
2. If there is no `.env`, it uses the backup's. An existing `.env` is kept; pass
   `--with-config` to replace it, and the old one is saved as `.env.before-restore`.
   Images are built if missing.
3. Asks for confirmation, then stops the app, **drops and recreates the database** from
   the dump, and replaces the stored files and workspaces.
4. Verifies: every table has the backup's row count, and every stored file's content is
   present at the right size.
5. Applies any newer migrations (restoring an old backup into newer code), starts the stack
   and waits for the API to be ready.

Afterwards everyone is still signed in on their devices; sessions are part of the
database. Anything written after the backup was taken is gone. That is the point of the
confirmation.

## What is not in a backup

- The model files and llama-server's configuration (they're on the GPU machine).
- Redis (it holds nothing durable).
- The images; they're rebuilt from the checkout at the revision in `config/git-revision`.
- Whatever the reverse proxy and TLS certificates need.
