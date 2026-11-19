# JSM Assets Backup & Restore

A single-file Python tool that **backs up** a Jira Service Management (JSM) Assets
workspace to local CSV/JSON files and **restores** those files back into Jira.
It reads the structure from your Jira site, so it works with any schema, object
type and attribute set. Nothing about your data model, site or IDs is hard-coded.

| | |
|---|---|
| Backup | All objects in all schemas (or chosen schema keys) → CSV + JSON + a metadata file describing types, attributes and relationships |
| Restore | Recreates schemas / object types / attributes (optional), creates objects, re-links references |
| Storage | Local folder by default; optional upload to Azure Blob Storage |
| Safety | `--dry-run`, confirmation prompt, no secrets in code, owner-only file permissions |

> **Test restore on a sandbox site or throwaway schema first, and run it with
> `--dry-run` before every real run.** See [Limitations](#limitations).

---

## 1. Requirements

- Python **3.10 or newer** (`python3 --version`)
- Jira Cloud with Assets (JSM Premium or Enterprise)
- An Atlassian **API token** for an account that can
  - *backup*: view the Assets schemas and objects
  - *restore*: create/edit Assets objects (and schemas/types if you use `--create-structure`)

Create a token at <https://id.atlassian.com/manage-profile/security/api-tokens>.

## 2. Install

```bash
git clone <this-repo-url> jsm-assets-backup   # or download and unzip
cd jsm-assets-backup

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Only if you want Azure uploads:
pip install -r requirements-azure.txt
```

## 3. Configure (`.env`)

```bash
cp .env.example .env
chmod 600 .env                       # keep your token private
```

Edit `.env`. Minimum configuration:

```env
ATLASSIAN_SITE=mycompany.atlassian.net
ATLASSIAN_EMAIL=you@example.com
ATLASSIAN_API_TOKEN=paste-your-api-token-here
```

| Variable | Required | Description |
|----------|----------|-------------|
| `ATLASSIAN_SITE` | Yes | Host name of your Jira Cloud site, e.g. `mycompany.atlassian.net`. Must end in `.atlassian.net`; anything else is refused so credentials can't be sent elsewhere. |
| `ATLASSIAN_EMAIL` | Yes | Email of the account that owns the token |
| `ATLASSIAN_API_TOKEN` | Yes | The API token |
| `ATLASSIAN_CLOUD_ID` | No | UUID. Skips auto-discovery of the Jira cloud ID |
| `ATLASSIAN_WORKSPACE_ID` | No | UUID. Skips auto-discovery of the Assets workspace ID |
| `AZURE_STORAGE_CONNECTION_STRING` | Azure only | Connection string used by `--azure` |
| `AZURE_STORAGE_ACCOUNT_URL` | Azure only | Alternative to the connection string, e.g. `https://acct.blob.core.windows.net`; authenticates with `DefaultAzureCredential` (`az login`, managed identity, …) |
| `AZURE_STORAGE_CONTAINER` | No | Default `jsm-assets-backups` (must already exist) |
| `AZURE_BLOB_PREFIX` | No | Default `jsm-assets-backup` |

On first run the tool looks up your **cloud ID** (from
`https://<site>/_edge/tenant_info`, no credentials sent) and **Assets workspace ID**
(from the Atlassian API) automatically, so you never enter them. Environment
variables set in your shell take precedence over `.env`.

---

## 4. Backup

```bash
# Everything → ./jsm-assets-backups/<UTC timestamp>/
python3 jsm_assets.py backup

# Custom folder, only the schemas with keys ASSET and VEND
python3 jsm_assets.py backup --output ~/assets-backups --schema ASSET --schema VEND

# Also upload the finished snapshot to Azure Blob Storage
python3 jsm_assets.py backup --azure
```

| Flag | Default | Description |
|------|---------|-------------|
| `--output DIR` | `./jsm-assets-backups` | Where snapshots are written |
| `--schema KEY` | all | Only this schema key (repeatable). Keys are shown in Assets, e.g. `ASSET` |
| `--azure` | off | Upload the snapshot after it is written locally |
| `--delay SECONDS` | `0.3` | Pause between API calls, to stay under rate limits |

Every run writes a **new** timestamped snapshot, so earlier ones are never
overwritten (delete old ones yourself when you no longer need them). Exit codes:
`0` success, `1` fatal error, `2` some object types failed to export (details in
`_backup_summary.json`).

### Snapshot layout

```
jsm-assets-backups/
  20261119T143000Z/               one folder per run (UTC)
    <SCHEMA_KEY>/
      <Object type>.csv           UTF-8 with BOM; opens correctly in Excel
      <Object type>.json          array of records; this is what restore reads
      _schema_metadata.json       object types, attributes, references, cardinality
    _backup_summary.json          site, object counts per type, any errors
```

Each record begins with `_Key`, `_ObjectType`, `_Created`, `_Updated`, then one
column per attribute, in the order defined in Assets.

- **References** are stored as the referenced object's key (e.g. `PEOP-35`).
- **Multi-valued attributes** are joined with ` | `.
- **Dates / date-times** are exported as ISO values so they can be restored.
- **Object types with no objects** have no CSV/JSON but are still in the metadata.
- **CSV only:** values starting with `=`, `+`, `-` or `@` get a leading `'` so
  Excel can't run them as formulas. The JSON files are exact.

With `--azure` the same tree lands at `<container>/<prefix>/<timestamp>/…`.

---

## 5. Restore

```bash
# 1. Preview: lists what exists and what would be created. Changes nothing.
python3 jsm_assets.py restore ./jsm-assets-backups/20261119T143000Z --dry-run

# 2. Load objects into schemas that already exist in the target (matched by key)
python3 jsm_assets.py restore ./jsm-assets-backups/20261119T143000Z

# 3. Also recreate missing schemas, object types and attributes
python3 jsm_assets.py restore ./jsm-assets-backups/20261119T143000Z --create-structure
```

`BACKUP_DIR` is a snapshot folder, or a folder containing snapshots (the newest
is used). Download Azure backups first (portal or `azcopy`); restore reads local
files only. The **target site is whichever site `.env` points at**, so you can
restore into a different Jira site than you backed up from. Before writing,
restore prints the target site and asks you to type `yes`.

| Flag | Description |
|------|-------------|
| `--schema KEY` | Only restore this schema key (repeatable) |
| `--create-structure` | Create missing schemas, object types (parents first) and attributes |
| `--overwrite` | Update objects whose key already exists in the target. Default: skip them |
| `--dry-run` | Report only; makes no changes and needs no confirmation |
| `--yes` | Skip the confirmation prompt (for scripts) |
| `--delay SECONDS` | Pause between API calls |

### What restore does, in order

1. Finds each target schema by **key**; with `--create-structure` it creates it if missing.
2. Matches object types and attributes by **name**; with `--create-structure` it
   creates the missing ones.
3. Indexes objects already present in the target.
4. Creates objects, referenced types first. A reference to an object that doesn't
   exist yet is skipped on creation and linked in a second pass, so circular
   references (A→B→A) work.
5. Writes `_restore_keymap_<timestamp>.json` into the snapshot: original key → key in
   the target.

**Keys change.** Jira assigns object keys itself, so a created object gets a new key
(`PEOP-35` may become `PEOP-1`). References between restored objects are re-linked
for you. Anything outside Jira that stores old keys must be updated from the keymap.

### Limitations

- **Not restored:** user, group, status, version, project and Confluence attributes.
  They are exported as display names, which the API can't accept back. The CSV/JSON
  still contains them for reference; fill them in afterwards or via Assets' own import.
- Multi-valued text is split on ` | `; a value that itself contains ` | ` splits wrongly.
- **Not backed up:** attachments, comments, object history, automation rules, import configurations.
- Recreated reference link types and select-attribute options are best-effort; verify
  them in the schema editor after `--create-structure`.
- Objects are matched by key. If the target has *different* objects with the same
  keys, they are skipped (or, with `--overwrite`, modified).
- Restore calls the endpoints documented by Atlassian but has not been verified against
  every Assets configuration. Treat it as you would any restore: sandbox first.

---

## 6. Security

- **No secrets or IDs in code.** Credentials come from `.env` / environment only and are never logged.
- **Credentials only go to `api.atlassian.com`.** `ATLASSIAN_SITE` must match
  `*.atlassian.net`; cloud/workspace IDs must be UUIDs; redirects are not followed.
- **Backups are sensitive.** Snapshot folders are created `0700` and files `0600`
  (owner only). Store, encrypt and share them accordingly.
- **Output is path-safe.** Schema and type names are sanitised, so a hostile name can't write outside the snapshot folder.
- **CSV formula injection** is neutralised (see above).
- **Restore needs confirmation** and supports `--dry-run`.
- **Dependencies** are minimal (`requests`, `urllib3`, `python-dotenv`; plus
  `azure-storage-blob`, `azure-identity` if you use `--azure`), with minimum
  versions set to releases that had no known vulnerabilities when last audited with
  [`pip-audit`](https://pypi.org/project/pip-audit/) (November 2026). Re-check any time:

  ```bash
  pip install pip-audit
  pip-audit -r requirements.txt -r requirements-azure.txt
  ```

- Never commit `.env` or backup folders (both are in `.gitignore`). Use a dedicated
  Atlassian account/token for backups where you can, and revoke the token if it leaks.

## 7. Troubleshooting

| Message | Cause / fix |
|---------|-------------|
| `Missing required env var: ATLASSIAN_SITE` | `.env` missing or not in the folder you run from |
| `ATLASSIAN_SITE must look like mycompany.atlassian.net` | Remove `https://`/paths, use your `*.atlassian.net` host |
| `401` / `403` from the API | Wrong email/token, or the account lacks Assets permissions |
| `No Assets workspace found` | Assets isn't enabled on the site, or the account can't see it |
| `429` messages | Rate limited; the tool retries automatically. Raise `--delay` |
| `schema key X not found in target` | Re-run restore with `--create-structure` |
| Exit code `2` on backup | Some types failed; see `errors` in `_backup_summary.json`, then re-run |

## 8. Files

```
jsm_assets.py            the tool (backup + restore)
requirements.txt         requests, urllib3, python-dotenv
requirements-azure.txt   azure-storage-blob, azure-identity (optional)
.env.example             copy to .env and fill in
.gitignore
```
