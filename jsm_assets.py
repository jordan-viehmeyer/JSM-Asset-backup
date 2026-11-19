#!/usr/bin/env python3
"""
Back up and restore Jira Service Management (JSM) Assets.

    python3 jsm_assets.py backup  [--output DIR] [--schema KEY ...] [--azure]
    python3 jsm_assets.py restore BACKUP_DIR [--schema KEY ...] [--create-structure]
                                  [--overwrite] [--dry-run]

backup   Exports every object of every Assets schema (or the ones you pick)
         to CSV and JSON, plus a _schema_metadata.json describing object
         types and attributes.  Files go to a local timestamped snapshot
         folder; optionally the snapshot is also uploaded to Azure Blob
         Storage.

restore  Reads a snapshot and writes it back to Jira through the Assets
         REST API: optionally recreating schema / object types /
         attributes, creating objects, then re-linking references.

Required environment variables (or a .env file)
-----------------------------------------------
ATLASSIAN_SITE        e.g. mycompany.atlassian.net
ATLASSIAN_EMAIL       Atlassian account email
ATLASSIAN_API_TOKEN   Atlassian API token

Optional
--------
ATLASSIAN_CLOUD_ID, ATLASSIAN_WORKSPACE_ID   skip auto-discovery
AZURE_STORAGE_CONNECTION_STRING              for `backup --azure`
AZURE_STORAGE_ACCOUNT_URL                    alternative to the above
                                             (uses DefaultAzureCredential)
AZURE_STORAGE_CONTAINER                      default: jsm-assets-backups
AZURE_BLOB_PREFIX                            default: jsm-assets-backup
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is a convenience, not a requirement
    pass

log = logging.getLogger("jsm-assets")

PAGE_SIZE = 500
META_FILE = "_schema_metadata.json"
SUMMARY_FILE = "_backup_summary.json"
MULTI_SEP = " | "

SYSTEM_ATTRS = {"Key", "Created", "Updated"}

# Attribute `type` values (Assets REST API): 0 default, 1 object reference,
# 2 user, 3 confluence, 4 group, 5 version, 6 project, 7 status.
ATTR_TYPE_DEFAULT = 0
ATTR_TYPE_REFERENCE = 1
# Values for these are exported as display names, which the API cannot
# accept back on restore, so restore skips them.
UNRESTORABLE_ATTR_TYPES = {2, 3, 4, 5, 6, 7}
# Default-type ids whose raw ISO `value` is exported instead of the
# locale-formatted display value: 4 date, 5 time, 6 date-time.
RAW_VALUE_DEFAULT_TYPES = {4, 5, 6}


# ── Helpers ─────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    """Make a string safe to use as a single file/folder name (no path
    separators, no leading dots so '..' can never escape the output dir)."""
    cleaned = re.sub(r"[^\w\-.]", "_", name).lstrip(".")
    return cleaned or "_"


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_bytes(path: Path, data: bytes):
    """Write owner-only (0600) files in owner-only (0700) folders:
    backups can contain sensitive asset data."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.atlassian\.net$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @, tab, CR) in
    CSV output.  JSON, which restore reads, is left untouched."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def _default_type_id(attr_def: dict):
    dt = attr_def.get("defaultType")
    return dt.get("id") if isinstance(dt, dict) else None


# ── Configuration / discovery ───────────────────────────────────────

class Config:
    def __init__(self):
        try:
            self.site = (
                os.environ["ATLASSIAN_SITE"]
                .removeprefix("https://")
                .removeprefix("http://")
                .strip("/")
            )
            self.email = os.environ["ATLASSIAN_EMAIL"]
            self.token = os.environ["ATLASSIAN_API_TOKEN"]
        except KeyError as e:
            raise SystemExit(f"Missing required env var: {e.args[0]}")
        # Credentials are only ever sent to api.atlassian.com, so refuse
        # anything that doesn't look like an Atlassian Cloud site.
        if not _SITE_RE.match(self.site.lower()):
            raise SystemExit(
                "ATLASSIAN_SITE must look like mycompany.atlassian.net"
            )
        self.site = self.site.lower()
        self.cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID") or None
        self.workspace_id = os.environ.get("ATLASSIAN_WORKSPACE_ID") or None
        for label, val in (("ATLASSIAN_CLOUD_ID", self.cloud_id),
                           ("ATLASSIAN_WORKSPACE_ID", self.workspace_id)):
            if val and not _UUID_RE.match(val):
                raise SystemExit(f"{label} must be a UUID")


def _discover_ids(cfg: Config):
    if not cfg.cloud_id:
        # Unauthenticated: no credentials are sent to the site itself.
        r = requests.get(f"https://{cfg.site}/_edge/tenant_info", timeout=30,
                         allow_redirects=False)
        r.raise_for_status()
        cfg.cloud_id = r.json()["cloudId"]
        if not _UUID_RE.match(cfg.cloud_id):
            raise SystemExit("Unexpected cloud id returned by the site")
        log.info(f"Discovered cloud id {cfg.cloud_id}")
    if not cfg.workspace_id:
        r = requests.get(
            f"https://api.atlassian.com/ex/jira/{cfg.cloud_id}"
            "/rest/servicedeskapi/assets/workspace",
            auth=(cfg.email, cfg.token),
            headers={"Accept": "application/json"},
            timeout=30,
            allow_redirects=False,
        )
        r.raise_for_status()
        values = r.json().get("values", [])
        if not values:
            raise SystemExit(
                "No Assets workspace found for this site "
                "(is Assets enabled? Jira Service Management Premium/Enterprise)."
            )
        cfg.workspace_id = values[0]["workspaceId"]
        if not _UUID_RE.match(cfg.workspace_id):
            raise SystemExit("Unexpected workspace id returned by the API")
        log.info(f"Discovered workspace id {cfg.workspace_id}")


# ── Atlassian Assets REST client ────────────────────────────────────

class AssetsAPI:
    def __init__(self, cfg: Config, delay: float = 0.3):
        _discover_ids(cfg)
        self.base = (
            f"https://api.atlassian.com/ex/jira/{cfg.cloud_id}"
            f"/jsm/assets/workspace/{cfg.workspace_id}/v1"
        )
        self.delay = delay
        s = requests.Session()
        s.auth = (cfg.email, cfg.token)
        s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        retry = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT"],
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        self._s = s

    def _call(self, method, path, **kw):
        r = self._s.request(method, f"{self.base}{path}", timeout=120,
                            allow_redirects=False, **kw)
        if not r.ok:
            raise requests.HTTPError(
                f"{r.status_code} {method} {path}: {r.text[:500]}", response=r
            )
        time.sleep(self.delay)
        return r.json() if r.content else {}

    @staticmethod
    def _as_list(data, *keys):
        if isinstance(data, list):
            return data
        for k in keys:
            if data.get(k):
                return data[k]
        return []

    # reads
    def list_schemas(self):
        return self._as_list(
            self._call("GET", "/objectschema/list"), "objectschemas", "values"
        )

    def list_object_types(self, schema_id):
        return self._as_list(
            self._call("GET", f"/objectschema/{schema_id}/objecttypes/flat"),
            "values",
        )

    def get_type_attributes(self, ot_id):
        return self._as_list(
            self._call("GET", f"/objecttype/{ot_id}/attributes"), "values"
        )

    def list_global_icons(self):
        return self._as_list(self._call("GET", "/icon/global"), "values")

    def aql_all(self, ql: str):
        objects: list = []
        start_at = 0
        while True:
            data = self._call(
                "POST", "/object/aql",
                json={"qlQuery": ql, "includeAttributes": True},
                params={"startAt": start_at, "maxResults": PAGE_SIZE},
            )
            batch = data.get("values", [])
            objects.extend(batch)
            if not batch or data.get("isLast", True):
                break
            start_at += len(batch)
            log.info(f"    {len(objects)}/{data.get('total', '?')}")
        return objects

    # writes (restore)
    def create_schema(self, body):
        return self._call("POST", "/objectschema/create", json=body)

    def create_object_type(self, body):
        return self._call("POST", "/objecttype/create", json=body)

    def create_attribute(self, ot_id, body):
        return self._call("POST", f"/objecttypeattribute/{ot_id}", json=body)

    def create_object(self, body):
        return self._call("POST", "/object/create", json=body)

    def update_object(self, obj_id, body):
        return self._call("PUT", f"/object/{obj_id}", json=body)


# ── Value extraction ────────────────────────────────────────────────

def _attr_val(attr_values: list, prefer_raw: bool = False) -> str:
    if not attr_values:
        return ""
    parts: list[str] = []
    for av in attr_values:
        ref = av.get("referencedObject")
        if ref and ref.get("objectKey"):
            parts.append(ref["objectKey"])
        elif av.get("status"):
            parts.append(
                av["status"].get("name") or str(av.get("displayValue", ""))
            )
        elif prefer_raw and av.get("value") is not None:
            parts.append(str(av["value"]))
        elif av.get("displayValue") is not None:
            parts.append(str(av["displayValue"]))
        elif av.get("value") is not None:
            parts.append(str(av["value"]))
    return MULTI_SEP.join(parts)


def _columns(attr_defs: list) -> list[tuple[str, str]]:
    ordered = sorted(attr_defs, key=lambda a: a.get("position", 0))
    return [
        (str(a["id"]), a["name"])
        for a in ordered
        if a["name"] not in SYSTEM_ATTRS
    ]


def _obj_vals(obj: dict, raw_ids: set[str]) -> dict[str, str]:
    vals: dict[str, str] = {}
    for attr in obj.get("attributes", []):
        aid = str(
            attr.get("objectTypeAttributeId")
            or attr.get("objectTypeAttribute", {}).get("id", "")
        )
        if aid:
            vals[aid] = _attr_val(
                attr.get("objectAttributeValues", []), aid in raw_ids
            )
    return vals


def _records(objects: list, attr_defs: list) -> tuple[list[str], list[dict]]:
    columns = _columns(attr_defs)
    raw_ids = {
        str(a["id"]) for a in attr_defs
        if _default_type_id(a) in RAW_VALUE_DEFAULT_TYPES
    }
    headers = ["_Key", "_ObjectType", "_Created", "_Updated"]
    headers += [name for _, name in columns]
    records = []
    for obj in objects:
        vals = _obj_vals(obj, raw_ids)
        rec = {
            "_Key": obj.get("objectKey", ""),
            "_ObjectType": obj.get("objectType", {}).get("name", ""),
            "_Created": obj.get("created", ""),
            "_Updated": obj.get("updated", ""),
        }
        for aid, name in columns:
            rec[name] = vals.get(aid, "")
        records.append(rec)
    return headers, records


def records_to_csv(headers: list[str], records: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow({k: _csv_safe(str(v)) for k, v in rec.items()})
    return buf.getvalue()


# ── Schema metadata ─────────────────────────────────────────────────

def _build_attr_meta(a: dict) -> dict:
    ref_type = a.get("referenceObjectType")
    return {
        "id": a["id"],
        "name": a["name"],
        "type_id": a.get("type"),
        "default_type_id": _default_type_id(a),
        "label": bool(a.get("label")),
        "system": bool(a.get("system")),
        "description": a.get("description"),
        "unique": bool(a.get("uniqueAttribute")),
        "options": a.get("options"),
        "is_reference": bool(a.get("referenceObjectTypeId")),
        "reference_type_id": a.get("referenceObjectTypeId"),
        "reference_type_name": (
            ref_type["name"]
            if isinstance(ref_type, dict) and "name" in ref_type
            else None
        ),
        "minimum_cardinality": a.get("minimumCardinality", 0),
        "maximum_cardinality": a.get("maximumCardinality", -1),
    }


def _build_type_meta(ot: dict, attr_defs: list) -> dict:
    parent = ot.get("parentObjectTypeId")
    icon = ot.get("icon") or {}
    return {
        "id": str(ot["id"]),
        "name": ot["name"],
        "description": ot.get("description"),
        "icon_id": str(icon["id"]) if icon.get("id") else None,
        "parent_object_type_id": str(parent) if parent else None,
        "attributes": [_build_attr_meta(a) for a in attr_defs],
    }


# ── Azure (optional) ────────────────────────────────────────────────

def _azure_upload_tree(local_root: Path, ts: str):
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        raise SystemExit(
            "Azure upload needs extra packages: "
            "pip install -r requirements-azure.txt"
        )

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        svc = BlobServiceClient.from_connection_string(conn)
    else:
        url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
        if not url:
            raise SystemExit(
                "Set AZURE_STORAGE_CONNECTION_STRING or "
                "AZURE_STORAGE_ACCOUNT_URL to use --azure"
            )
        from azure.identity import DefaultAzureCredential

        svc = BlobServiceClient(url, credential=DefaultAzureCredential())

    container = os.environ.get("AZURE_STORAGE_CONTAINER", "jsm-assets-backups")
    prefix = os.environ.get("AZURE_BLOB_PREFIX", "jsm-assets-backup")
    files = [p for p in local_root.rglob("*") if p.is_file()]
    log.info(f"Uploading {len(files)} file(s) to {container}/{prefix}/{ts}/")
    for p in files:
        name = f"{prefix}/{ts}/{p.relative_to(local_root).as_posix()}"
        svc.get_blob_client(container=container, blob=name).upload_blob(
            p.read_bytes(), overwrite=True
        )
    log.info("Azure upload complete")


# ── Backup ──────────────────────────────────────────────────────────

def run_backup(cfg: Config, *, output: str, only: list[str], azure: bool,
               delay: float) -> int:
    api = AssetsAPI(cfg, delay)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = Path(output) / ts
    log.info(f"Backup run {ts} -> {snap}")

    schemas = api.list_schemas()
    if only:
        wanted = {k.upper() for k in only}
        schemas = [
            s for s in schemas
            if (s.get("objectSchemaKey") or s.get("key") or "").upper() in wanted
        ]
    if not schemas:
        log.error("No matching schemas found")
        return 1
    log.info(f"{len(schemas)} schema(s)")

    summary: dict = {}
    errors: list[str] = []

    for schema in schemas:
        sid = str(schema["id"])
        sname = schema["name"]
        skey = schema.get("objectSchemaKey") or schema.get("key") or f"S{sid}"
        sdir = snap / _safe(skey)

        log.info(f"\n{'─' * 50}\nSchema  {sname}  ({skey})")
        obj_types = api.list_object_types(sid)
        log.info(f"  {len(obj_types)} object type(s)")

        meta: dict = {
            "schema_id": sid,
            "name": sname,
            "key": skey,
            "description": schema.get("description"),
            "exported_at": ts,
            "object_types": [],
        }
        counts: dict[str, int] = {}

        for ot in obj_types:
            name = ot["name"]
            attr_defs = api.get_type_attributes(str(ot["id"]))
            meta["object_types"].append(_build_type_meta(ot, attr_defs))

            ql = (
                f'objectType = "{_esc(name)}"'
                f' AND objectSchema = "{_esc(sname)}"'
            )
            log.info(f"  {name} ...")
            try:
                objects = api.aql_all(ql)
            except requests.HTTPError as exc:
                log.warning(f"  FAIL {name}: {exc}")
                errors.append(f"{skey}/{name}: {exc}")
                continue

            counts[name] = len(objects)
            if not objects:
                continue

            headers, records = _records(objects, attr_defs)
            safe = _safe(name)
            _write_bytes(
                sdir / f"{safe}.csv",
                records_to_csv(headers, records).encode("utf-8-sig"),
            )
            _write_bytes(
                sdir / f"{safe}.json",
                json.dumps(records, indent=2, ensure_ascii=False).encode("utf-8"),
            )
            log.info(f"  {name}  {len(objects)} object(s)")

        _write_bytes(
            sdir / META_FILE,
            json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
        )
        summary[skey] = {"name": sname, "types": counts}

    _write_bytes(
        snap / SUMMARY_FILE,
        json.dumps(
            {"timestamp": ts, "site": cfg.site, "schemas": summary,
             "errors": errors},
            indent=2, ensure_ascii=False,
        ).encode("utf-8"),
    )

    log.info(f"\n{'─' * 50}\nBackup complete: {snap}")
    for k, v in summary.items():
        total = sum(v["types"].values())
        non_empty = sum(1 for c in v["types"].values() if c)
        log.info(f"  {v['name']} ({k}): {total} objects, "
                 f"{non_empty}/{len(v['types'])} types non-empty")

    if azure:
        _azure_upload_tree(snap, ts)

    if errors:
        log.error(f"{len(errors)} object type(s) failed to export:")
        for e in errors:
            log.error(f"  {e}")
        return 2
    return 0


# ── Restore ─────────────────────────────────────────────────────────

def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_dirs(root: Path) -> list[Path]:
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and (d / META_FILE).exists()
    )


def resolve_snapshot(path: str) -> Path:
    """Accept a snapshot folder, or a parent of snapshots (newest is used)."""
    root = Path(path).expanduser()
    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")
    if _schema_dirs(root):
        return root
    snaps = sorted(d for d in root.iterdir() if d.is_dir() and _schema_dirs(d))
    if not snaps:
        raise SystemExit(f"No backup found in {root} (no {META_FILE} files)")
    log.info(f"Using newest snapshot: {snaps[-1].name}")
    return snaps[-1]


def _order_types(types: list[dict]) -> list[dict]:
    """Referenced types first, so references resolve in one pass when
    possible.  Cycles are broken arbitrarily; leftovers use pass 2."""
    by_name = {t["name"]: t for t in types}
    seen: set[str] = set()
    out: list[dict] = []

    def visit(t):
        if t["name"] in seen:
            return
        seen.add(t["name"])
        for a in t["attributes"]:
            dep = by_name.get(a.get("reference_type_name") or "")
            if a.get("is_reference") and dep:
                visit(dep)
        out.append(t)

    for t in types:
        visit(t)
    return out


class Restorer:
    def __init__(self, api: AssetsAPI, *, create_structure: bool,
                 overwrite: bool):
        self.api = api
        self.create_structure = create_structure
        self.overwrite = overwrite
        self.keymap: dict[str, dict[str, str]] = {}  # schema key -> old -> new
        self.stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0,
                      "unrestorable_values": 0, "unresolved_refs": 0}
        self._icon: str | None = None
        self._schemas: list | None = None

    # structure ------------------------------------------------------
    def _find_schema(self, key: str):
        if self._schemas is None:
            self._schemas = self.api.list_schemas()
        for s in self._schemas:
            if (s.get("objectSchemaKey") or s.get("key")) == key:
                return s
        return None

    def _fallback_icon(self) -> str:
        if self._icon is None:
            icons = self.api.list_global_icons()
            if not icons:
                raise RuntimeError("No global icons available for new types")
            self._icon = str(icons[0]["id"])
        return self._icon

    def _ensure_types(self, sid, meta_types, tmap):
        by_src = {t["id"]: t for t in meta_types}

        def ensure(t):
            if t["name"] in tmap:
                return
            body = {
                "name": t["name"],
                "description": t.get("description") or "",
                "iconId": t.get("icon_id") or self._fallback_icon(),
                "objectSchemaId": str(sid),
            }
            parent = by_src.get(t.get("parent_object_type_id") or "")
            if parent:
                ensure(parent)
                body["parentObjectTypeId"] = str(tmap[parent["name"]]["id"])
            try:
                tmap[t["name"]] = self.api.create_object_type(body)
            except requests.HTTPError:
                # Stale icon id (different instance): retry with a global one.
                body["iconId"] = self._fallback_icon()
                tmap[t["name"]] = self.api.create_object_type(body)
            log.info(f"  created object type {t['name']}")

        for t in meta_types:
            ensure(t)

    def _ensure_attributes(self, t, tmap) -> dict[str, dict]:
        target = tmap[t["name"]]
        existing = {a["name"]: a
                    for a in self.api.get_type_attributes(str(target["id"]))}
        added = False
        for a in t["attributes"]:
            if a["name"] in existing or a["name"] in SYSTEM_ATTRS or a.get("system"):
                continue
            body = {
                "name": a["name"],
                "type": a.get("type_id") or 0,
                "label": a.get("label", False),
                "description": a.get("description") or "",
                "minimumCardinality": a.get("minimum_cardinality", 0),
                "maximumCardinality": a.get("maximum_cardinality", -1),
                "uniqueAttribute": a.get("unique", False),
            }
            if body["type"] == ATTR_TYPE_DEFAULT:
                body["defaultTypeId"] = a.get("default_type_id") or 0
                if a.get("options"):
                    body["options"] = a["options"]
            elif body["type"] == ATTR_TYPE_REFERENCE:
                ref = tmap.get(a.get("reference_type_name") or "")
                if not ref:
                    log.warning(f"  skip attribute {t['name']}.{a['name']}: "
                                "referenced type not found in target")
                    continue
                body["typeValue"] = str(ref["id"])
            try:
                self.api.create_attribute(str(target["id"]), body)
                added = True
                log.info(f"  created attribute {t['name']}.{a['name']}")
            except requests.HTTPError as exc:
                log.warning(f"  FAIL attribute {t['name']}.{a['name']}: {exc}")
        if added:
            existing = {a["name"]: a
                        for a in self.api.get_type_attributes(str(target["id"]))}
        return existing

    # objects --------------------------------------------------------
    def restore_schema(self, sdir: Path, dry_run: bool):
        meta = _load_json(sdir / META_FILE)
        skey, sname = meta["key"], meta["name"]
        log.info(f"\n{'─' * 50}\nSchema {sname} ({skey})")

        target = self._find_schema(skey)
        if not target:
            if not self.create_structure:
                log.error(f"  schema key {skey} not found in target; "
                          "re-run with --create-structure to create it")
                return
            if dry_run:
                log.info("  [dry-run] would create schema, types, attributes")
                target = {"id": "?", "name": sname}
            else:
                target = self.api.create_schema({
                    "name": sname, "objectSchemaKey": skey,
                    "description": meta.get("description") or "",
                })
                self._schemas.append(target)
                log.info(f"  created schema {sname}")

        types = _order_types(meta["object_types"])
        tmap: dict[str, dict] = {}
        if target["id"] != "?":
            tmap = {t["name"]: t
                    for t in self.api.list_object_types(target["id"])}

        if dry_run:
            self._dry_run_report(sdir, types, tmap)
            return

        if self.create_structure:
            self._ensure_types(target["id"], meta["object_types"], tmap)
        amap = {}
        for t in types:
            if t["name"] in tmap:
                amap[t["name"]] = (
                    self._ensure_attributes(t, tmap) if self.create_structure
                    else {a["name"]: a for a in self.api.get_type_attributes(
                        str(tmap[t["name"]]["id"]))}
                )

        # Index objects already in the target so we can skip / overwrite
        # them and resolve references to them.
        keymap = self.keymap.setdefault(skey, {})
        existing: dict[str, dict[str, str]] = {}
        for t in types:
            if t["name"] not in tmap:
                continue
            ql = (f'objectType = "{_esc(t["name"])}"'
                  f' AND objectSchema = "{_esc(target["name"])}"')
            existing[t["name"]] = {
                o["objectKey"]: o["id"] for o in self.api.aql_all(ql)
            }
            for k in existing[t["name"]]:
                keymap.setdefault(k, k)

        # Pass 1: create / update objects. References to objects not yet
        # restored are deferred to pass 2.
        pending: list[tuple[str, str, dict[str, list[str]]]] = []
        for t in types:
            name = t["name"]
            path = sdir / f"{_safe(name)}.json"
            if not path.exists():
                continue
            if name not in tmap:
                log.warning(f"  {name}: type missing in target, skipped "
                            "(use --create-structure)")
                continue
            records = _load_json(path)
            log.info(f"  {name}: {len(records)} record(s)")
            self._restore_type(t, records, tmap[name], amap[name],
                               existing[name], keymap, pending)

        # Pass 2: link deferred references.
        if pending:
            log.info(f"  linking deferred references on {len(pending)} object(s)")
        for obj_id, ot_id, refs in pending:
            attrs = []
            for aid, old_keys in refs.items():
                new_keys = [keymap[k] for k in old_keys if k in keymap]
                self.stats["unresolved_refs"] += len(old_keys) - len(new_keys)
                if new_keys:
                    attrs.append(self._attr_body(aid, new_keys))
            if not attrs:
                continue
            try:
                self.api.update_object(
                    obj_id, {"objectTypeId": ot_id, "attributes": attrs})
            except requests.HTTPError as exc:
                self.stats["failed"] += 1
                log.warning(f"  FAIL linking references on object {obj_id}: {exc}")

    @staticmethod
    def _attr_body(attr_id, values):
        return {
            "objectTypeAttributeId": str(attr_id),
            "objectAttributeValues": [{"value": v} for v in values],
        }

    def _restore_type(self, t, records, ttype, tattrs, existing, keymap,
                      pending):
        src = {a["name"]: a for a in t["attributes"]}
        ot_id = str(ttype["id"])

        for rec in records:
            old_key = rec.get("_Key", "")
            obj_id = existing.get(old_key)
            if obj_id and not self.overwrite:
                self.stats["skipped"] += 1
                continue

            attrs: list[dict] = []
            deferred: dict[str, list[str]] = {}
            for name, raw in rec.items():
                if name.startswith("_") or raw in ("", None):
                    continue
                sa, ta = src.get(name), tattrs.get(name)
                if not sa or not ta:
                    continue
                if sa.get("type_id") in UNRESTORABLE_ATTR_TYPES:
                    self.stats["unrestorable_values"] += 1
                    continue
                if sa.get("is_reference") or sa.get("type_id") == ATTR_TYPE_REFERENCE:
                    keys = [k for k in str(raw).split(MULTI_SEP) if k]
                    ready = [keymap[k] for k in keys if k in keymap]
                    later = [k for k in keys if k not in keymap]
                    if ready:
                        attrs.append(self._attr_body(ta["id"], ready))
                    if later:
                        # Pass 2 sets the full list, so keep the resolved ones.
                        deferred[str(ta["id"])] = keys
                    continue
                if sa.get("maximum_cardinality", -1) != 1:
                    values = str(raw).split(MULTI_SEP)
                else:
                    values = [str(raw)]
                attrs.append(self._attr_body(ta["id"], values))

            body = {"objectTypeId": ot_id, "attributes": attrs}
            try:
                if obj_id:
                    self.api.update_object(obj_id, body)
                    self.stats["updated"] += 1
                    new_id, new_key = obj_id, old_key
                else:
                    res = self.api.create_object(body)
                    self.stats["created"] += 1
                    new_id, new_key = str(res["id"]), res["objectKey"]
            except requests.HTTPError as exc:
                self.stats["failed"] += 1
                log.warning(f"  FAIL {old_key}: {exc}")
                continue
            keymap[old_key] = new_key
            if deferred:
                pending.append((new_id, ot_id, deferred))

    def _dry_run_report(self, sdir, types, tmap):
        for t in types:
            path = sdir / f"{_safe(t['name'])}.json"
            n = len(_load_json(path)) if path.exists() else 0
            status = "exists" if t["name"] in tmap else (
                "would create" if self.create_structure else "MISSING (skipped)")
            log.info(f"  [dry-run] {t['name']}: {n} record(s), "
                     f"target type {status}")


def run_restore(cfg: Config, *, path: str, only: list[str],
                create_structure: bool, overwrite: bool, dry_run: bool,
                assume_yes: bool, delay: float) -> int:
    snap = resolve_snapshot(path)
    dirs = _schema_dirs(snap)
    if only:
        wanted = {k.upper() for k in only}
        dirs = [d for d in dirs if d.name.upper() in wanted]
    if not dirs:
        log.error("No matching schema folders in snapshot")
        return 1

    if not dry_run and not assume_yes:
        print(f"About to WRITE data from {snap}\n"
              f"  into Jira site {cfg.site}"
              f"{' (creating structure)' if create_structure else ''}"
              f"{' (overwriting existing keys)' if overwrite else ''}.")
        if not sys.stdin.isatty() or input("Type 'yes' to continue: ") != "yes":
            log.error("Aborted (use --yes to skip the prompt)")
            return 1

    api = AssetsAPI(cfg, delay)
    r = Restorer(api, create_structure=create_structure, overwrite=overwrite)
    log.info(f"Restoring {snap} -> {cfg.site}"
             f"{'  (dry run)' if dry_run else ''}")
    for d in dirs:
        r.restore_schema(d, dry_run)

    if dry_run:
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    km = snap / f"_restore_keymap_{ts}.json"
    km.write_text(json.dumps(r.keymap, indent=2), encoding="utf-8")
    log.info(f"\n{'─' * 50}\nRestore finished: {r.stats}")
    log.info(f"Old-key -> new-key mapping written to {km}")
    if r.stats["unrestorable_values"]:
        log.warning("User/group/status/version/project/confluence values are "
                    "not restored; set them manually or via import.")
    return 2 if r.stats["failed"] else 0


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    p = argparse.ArgumentParser(description="Back up / restore JSM Assets")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--schema", action="append", default=[], metavar="KEY",
                        help="Only this schema key (repeatable)")
        sp.add_argument("--delay", type=float, default=0.3,
                        help="Seconds to pause between API calls (default 0.3)")

    b = sub.add_parser("backup", help="Export schemas to CSV/JSON")
    common(b)
    b.add_argument("--output", default="./jsm-assets-backups", metavar="DIR",
                   help="Where to write snapshots (default ./jsm-assets-backups)")
    b.add_argument("--azure", action="store_true",
                   help="Also upload the snapshot to Azure Blob Storage")

    r = sub.add_parser("restore", help="Write a backup back into Jira")
    common(r)
    r.add_argument("path", metavar="BACKUP_DIR",
                   help="Snapshot folder (or a folder of snapshots; newest used)")
    r.add_argument("--create-structure", action="store_true",
                   help="Create missing schemas, object types and attributes")
    r.add_argument("--overwrite", action="store_true",
                   help="Update objects whose key already exists "
                        "(default: skip them)")
    r.add_argument("--dry-run", action="store_true",
                   help="Show what would be restored; change nothing")
    r.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt")

    args = p.parse_args()
    try:
        if args.cmd == "backup":
            code = run_backup(Config(), output=args.output, only=args.schema,
                              azure=args.azure, delay=args.delay)
        else:
            code = run_restore(
                Config(), path=args.path, only=args.schema,
                create_structure=args.create_structure,
                overwrite=args.overwrite, dry_run=args.dry_run,
                assume_yes=args.yes, delay=args.delay)
    except SystemExit:
        raise
    except Exception:
        log.exception(f"{args.cmd} failed")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
