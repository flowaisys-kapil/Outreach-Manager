#!/usr/bin/env python
"""Backfill missing profile embeddings in the raw_hub_contributions lake.

The hub snapshots ``contacts_contribution`` into
``gs://<bucket>/raw_hub_contributions/dt=YYYY-MM-DD/data.parquet`` with an
``embedding`` column of f16 bytes (``struct.pack("384e", ...)`` → 768 B/row).
Rows contributed before a client computed the vector — or whole partitions
written before the column existed — carry a NULL/absent embedding. This script
re-derives those vectors locally: scrape the profile by ``public_identifier``,
build the same profile text, embed with the same model, and write the same f16
bytes back into the parquet.

Idempotent and re-runnable: resolved vectors are cached on disk
(``data/lake_backfill/cache/``), so iterating only scrapes what's still missing.
Default is a dry run (local parquets + report, no GCS writes); pass ``--commit``
to back up the originals and upload the filled files.

Run with the project venv so Django + the LinkedIn login + fastembed are wired:

    .venv/bin/python scripts/backfill_lake_embeddings.py            # dry run
    .venv/bin/python scripts/backfill_lake_embeddings.py --commit   # upload
    .venv/bin/python scripts/backfill_lake_embeddings.py --only 2026-06-22
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import subprocess
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger("backfill_lake_embeddings")

BUCKET = os.environ.get("OUTREACH_MANAGER_BUCKET", "outreach_manager-pipeline-eu")
RAW_PREFIX = f"gs://{BUCKET}/raw_hub_contributions"
BACKUP_PREFIX = f"gs://{BUCKET}/_backup/raw_hub_contributions"
EMBEDDING_DIM = 384
EMBEDDING_BYTES = EMBEDDING_DIM * 2  # f16

WORK_DIR = Path(__file__).resolve().parent.parent / "data" / "lake_backfill"
DL_DIR = WORK_DIR / "download"
OUT_DIR = WORK_DIR / "filled"
CACHE_DIR = WORK_DIR / "cache"


# ── Django bootstrap ──────────────────────────────────────────────────────


def setup_django() -> None:
    import sys

    # Running ``scripts/foo.py`` puts scripts/ on sys.path, not the project root,
    # so the ``outreach_manager`` package isn't importable without this.
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outreach_manager.settings")
    import django

    django.setup()


# ── GCS transfer (via gsutil — the venv has no gcsfs) ─────────────────────


def _gsutil(*args: str) -> str:
    """Run gsutil and return stdout; raise on failure."""
    result = subprocess.run(
        ["gsutil", *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def list_partitions() -> list[str]:
    """Return sorted ``YYYY-MM-DD`` partition keys present in the lake."""
    out = _gsutil("ls", f"{RAW_PREFIX}/")
    keys = []
    for line in out.splitlines():
        name = line.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith("dt="):
            keys.append(name[len("dt=") :])
    return sorted(keys)


def download_partition(dt: str) -> Path | None:
    """Download one partition's parquet locally; None if the day has no file."""
    remote = f"{RAW_PREFIX}/dt={dt}/data.parquet"
    local = DL_DIR / f"{dt}.parquet"
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        _gsutil("cp", remote, str(local))
    except subprocess.CalledProcessError:
        logger.warning("No file for dt=%s (empty day) — skipping", dt)
        return None
    return local


def upload_partition(dt: str, local: Path) -> None:
    """Back up the live file, then overwrite it with the filled local copy."""
    remote = f"{RAW_PREFIX}/dt={dt}/data.parquet"
    backup = f"{BACKUP_PREFIX}/dt={dt}/data.parquet"
    _gsutil("cp", remote, backup)
    _gsutil("cp", str(local), remote)
    logger.info("Uploaded dt=%s (backup → %s)", dt, backup)


# ── Embedding resolution ──────────────────────────────────────────────────


class EmbeddingResolver:
    """Resolve a ``public_identifier`` to its 768-byte f16 embedding.

    Order of attempts: on-disk cache → local ``Lead.embedding`` → live scrape.
    Resolved vectors and known failures are cached so reruns don't re-scrape.
    """

    def __init__(self, session, *, min_delay: float, max_delay: float):
        self.session = session
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.cache_file = CACHE_DIR / "embeddings.npz"
        self.fail_file = CACHE_DIR / "failures.txt"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, bytes] = self._load_cache()
        self._failures: set[str] = self._load_failures()
        self.scraped = 0
        self.from_lead = 0
        self.from_cache = 0

    def _load_cache(self) -> dict[str, bytes]:
        if not self.cache_file.exists():
            return {}
        npz = np.load(self.cache_file, allow_pickle=False)
        return {k: npz[k].tobytes() for k in npz.files}

    def _save_cache(self) -> None:
        arrays = {k: np.frombuffer(v, dtype=np.uint8) for k, v in self._cache.items()}
        np.savez(self.cache_file, **arrays)

    def _load_failures(self) -> set[str]:
        if not self.fail_file.exists():
            return set()
        return set(self.fail_file.read_text().split())

    @staticmethod
    def _to_f16_bytes(arr: np.ndarray) -> bytes:
        """384-dim float vector → 768 f16 bytes, matching the hub's struct.pack."""
        b = np.asarray(arr, dtype=np.float16).tobytes()
        if len(b) != EMBEDDING_BYTES:
            raise ValueError(f"expected {EMBEDDING_BYTES} bytes, got {len(b)}")
        return b

    def _from_local_lead(self, public_identifier: str) -> bytes | None:
        from outreach_manager.crm.models import Lead

        lead = (
            Lead.objects.filter(public_identifier=public_identifier)
            .exclude(embedding=None)
            .first()
        )
        if lead is None:
            return None
        return self._to_f16_bytes(lead.embedding_array)

    def _scrape(self, public_identifier: str) -> bytes | None:
        from linkedin_cli.api.client import PlaywrightLinkedinAPI
        from linkedin_cli.exceptions import ProfileInaccessibleError
        from outreach_manager.linkedin.ml.embeddings import embed_text
        from outreach_manager.linkedin.ml.profile_text import build_profile_text

        self.session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=self.session)
        try:
            profile, _raw = api.get_profile(public_identifier=public_identifier)
        except ProfileInaccessibleError:
            return None
        if not profile:
            return None

        text = build_profile_text({"profile": profile})
        if not text.strip():
            logger.warning("Empty profile text for %s — embedding anyway", public_identifier)
        return self._to_f16_bytes(embed_text(text))

    def resolve(self, public_identifier: str) -> bytes | None:
        if public_identifier in self._cache:
            self.from_cache += 1
            return self._cache[public_identifier]

        local = self._from_local_lead(public_identifier)
        if local is not None:
            self.from_lead += 1
            self._cache[public_identifier] = local
            return local

        emb = self._scrape(public_identifier)
        # Pace scrapes the way enrichment does, to stay under LinkedIn's radar.
        time.sleep(random.uniform(self.min_delay, self.max_delay))
        if emb is None:
            self._failures.add(public_identifier)
            return None
        self.scraped += 1
        self._cache[public_identifier] = emb
        return emb

    def flush(self) -> None:
        self._save_cache()
        self.fail_file.write_text("\n".join(sorted(self._failures)))


# ── Parquet rewrite ───────────────────────────────────────────────────────


def fill_partition(dt: str, local: Path, resolver: EmbeddingResolver) -> dict:
    """Fill NULL embeddings in one parquet; write a local filled copy.

    Returns a per-partition stat dict. The ``embedding`` column is always
    written as ``pa.binary()`` (even if every value is NULL) so it never
    serialises as a null-typed column and breaks DuckDB's ``dt=*`` glob.
    """
    from tqdm import tqdm

    table = pq.read_table(local)
    n = table.num_rows
    names = table.column_names
    pids = table.column("public_identifier").to_pylist()
    emb = table.column("embedding").to_pylist() if "embedding" in names else [None] * n

    todo = [i for i in range(n) if emb[i] is None]
    needed = len(todo)
    filled = 0
    for i in tqdm(todo, desc=f"dt={dt}", unit="row", leave=True):
        vec = resolver.resolve(pids[i])
        if vec is not None:
            emb[i] = vec
            filled += 1

    emb_field = pa.field("embedding", pa.binary())
    emb_array = pa.array(emb, type=pa.binary())
    if "embedding" in names:
        table = table.set_column(names.index("embedding"), emb_field, emb_array)
    else:
        # Insert at the canonical position (right after ``origin``) when present.
        pos = names.index("origin") + 1 if "origin" in names else len(names)
        table = table.add_column(pos, emb_field, emb_array)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / local.name
    pq.write_table(table, out)

    remaining = needed - filled
    return {
        "rows": n,
        "needed": needed,
        "filled": filled,
        "remaining": remaining,
        "out": out,
    }


# ── CLI ───────────────────────────────────────────────────────────────────


def resolve_session():
    """The single active LinkedIn login from the local DB, ready to scrape."""
    from outreach_manager.linkedin.browser.registry import (
        get_first_active_profile,
        get_or_create_session,
    )

    profile = get_first_active_profile()
    if profile is None:
        raise SystemExit("No active LinkedInProfile in the local DB — cannot scrape.")
    return get_or_create_session(profile)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Upload filled parquets to GCS (default: dry run, local files only).",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        metavar="YYYY-MM-DD",
        help="Restrict to these partition dates (default: all).",
    )
    parser.add_argument("--min-delay", type=float, default=6.0)
    parser.add_argument("--max-delay", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    setup_django()

    partitions = list_partitions()
    if args.only:
        partitions = [p for p in partitions if p in set(args.only)]
    logger.info("Partitions: %s", ", ".join(partitions))

    resolver = EmbeddingResolver(
        resolve_session(), min_delay=args.min_delay, max_delay=args.max_delay
    )

    stats: dict[str, dict] = {}
    try:
        for dt in partitions:
            local = download_partition(dt)
            if local is None:
                continue
            st = fill_partition(dt, local, resolver)
            stats[dt] = st
            logger.info(
                "dt=%s rows=%d needed=%d filled=%d remaining=%d",
                dt, st["rows"], st["needed"], st["filled"], st["remaining"],
            )
    finally:
        resolver.flush()

    if args.commit:
        for dt, st in stats.items():
            if st["needed"]:
                upload_partition(dt, st["out"])

    _print_summary(stats, resolver, committed=args.commit)


def _print_summary(stats: dict, resolver: EmbeddingResolver, *, committed: bool) -> None:
    total_needed = sum(s["needed"] for s in stats.values())
    total_filled = sum(s["filled"] for s in stats.values())
    total_remaining = sum(s["remaining"] for s in stats.values())

    print("\n── Backfill summary ──────────────────────────────")
    print(f"mode               : {'COMMIT (uploaded)' if committed else 'DRY RUN (local only)'}")
    print(f"rows needing fill  : {total_needed}")
    print(f"  filled           : {total_filled}")
    print(f"  still NULL        : {total_remaining}")
    print(f"sources            : scraped={resolver.scraped} "
          f"local_lead={resolver.from_lead} cache={resolver.from_cache}")
    print(f"unresolved profiles: {len(resolver._failures)}")
    if total_remaining == 0 and stats:
        print("\nAll targeted partitions are fully embedded.")
        print("→ Safe to remove the downstream null-coercion / union_by_name shim.")
    elif total_remaining:
        print("\nSome rows are still NULL (inaccessible/deleted profiles).")
        print(f"See {resolver.fail_file} — re-run to retry; keep the shim until zero.")


if __name__ == "__main__":
    main()
