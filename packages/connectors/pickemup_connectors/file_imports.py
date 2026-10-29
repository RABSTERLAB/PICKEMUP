"""CSV / JSONL file-import connectors with provenance + validation.

Accepted CSV columns (header required): post_id, username, account_id, text,
created_at (ISO8601), hashtags (semicolon-sep), urls (semicolon-sep),
mentions, repost_of, lang. JSONL: one object per line with the same keys
(nested lists allowed).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone

from pickemup_analysis.domain import AccountObs, PostObs

from . import Connector, ConnectorManifest, IngestBatch


def _ts(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).lstrip("#@") for x in v if str(x).strip()]
    return [s.lstrip("#@") for s in str(v).split(";") if s.strip()]


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class FileImportConnector(Connector):
    dev_only = True
    def __init__(self, fmt: str):
        assert fmt in ("csv", "jsonl")
        self.fmt = fmt
        self.manifest = ConnectorManifest(
            name=f"import_{fmt}", platform="import", capabilities=["import"],
            auth_required=False, rate_limit_desc="n/a", cost_model="free",
            coverage_gaps=["whatever the file contains; no enrichment"],
            retention_rule="source file stays under the analyst's control",
            data_freshness="file-based")

    def available(self):
        return True, "local file import always available"

    def run(self, params: dict, budget=None) -> IngestBatch:
        path = params["path"]
        digest = _sha(path)
        rows = []
        parse_errors: list[str] = []
        if self.fmt == "csv":
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
        else:
            # one bad line must not poison the file: skip + report per line
            with open(path, encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        parse_errors.append(f"line {ln}: skipped malformed JSON ({e.msg})")
        accounts: dict[str, AccountObs] = {}
        posts: list[PostObs] = []
        errors: list[str] = list(parse_errors)
        seen: set[str] = set()
        for i, r in enumerate(rows):
            try:
                aid = r.get("account_id") or f"acct-{r.get('username', 'unknown')}"
                uname = r.get("username") or aid
                if aid not in accounts:
                    accounts[aid] = AccountObs(account_id=aid, username=uname,
                                               display_name=r.get("display_name") or uname,
                                               origin_mode="fixture")
                pid = r.get("post_id") or f"imp-{i}"
                if pid in seen:
                    continue
                seen.add(pid)
                posts.append(PostObs(
                    post_id=pid, author_id=aid, text=r.get("text") or "",
                    created_at=_ts(r.get("created_at")), collected_at=self.now(),
                    lang=r.get("lang"), hashtags=_list(r.get("hashtags")),
                    urls=_list(r.get("urls")), mentions=_list(r.get("mentions")),
                    repost_of=r.get("repost_of"), quote_of=r.get("quote_of"),
                    origin_mode="fixture", source_url=r.get("source_url")))
            except Exception as e:  # keep ingesting; report bad rows
                errors.append(f"row {i}: {e}")
        return IngestBatch(accounts=list(accounts.values()), posts=posts, origin_mode="fixture",
                           provenance={"source_kind": f"import_{self.fmt}", "file": path,
                                       "file_sha256": digest, "rows_in": len(rows),
                                       "rows_deduped": len(rows) - len(posts) - len(errors),
                                       "row_errors": errors[:20],
                                       "imported_at": self.now().isoformat()})
