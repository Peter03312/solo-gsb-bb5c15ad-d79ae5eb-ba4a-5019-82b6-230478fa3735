"""Idempotent demo data used to initialise an empty database.

Enabled by the ``SEED_DEMO`` setting. Running it repeatedly is safe: each
entity is first looked up and reused, and change replays go through the same
service path as client submissions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .database import Database
from .service import LicenseService

logger = logging.getLogger("license_service.seed")

DEMO: list[dict[str, object]] = [
    {
        "version_id": "demo-root-a",
        "parent_ids": [],
        "note": "示例根版本 A",
        "changes": [
            {
                "change_id": "demo-chg-a-1",
                "effective_at": "2026-01-01T00:00:00Z",
                "allowed_uses": ["research", "review"],
            },
            {
                "change_id": "demo-chg-a-2",
                "effective_at": "2026-06-01T00:00:00Z",
                "allowed_uses": ["research"],
            },
        ],
    },
    {
        "version_id": "demo-root-b",
        "parent_ids": [],
        "note": "示例根版本 B",
        "changes": [
            {
                "change_id": "demo-chg-b-1",
                "effective_at": "2026-01-01T00:00:00Z",
                "allowed_uses": ["research", "commercial"],
            }
        ],
    },
    {
        # Child of both roots: allowed uses are the intersection.
        # research  => allowed; review/commercial => blocked.
        "version_id": "demo-derived-1",
        "parent_ids": ["demo-root-a", "demo-root-b"],
        "note": "示例派生版本（双父版本，用途交集）",
        "changes": [
            {
                "change_id": "demo-chg-d1-1",
                "effective_at": "2026-02-01T00:00:00Z",
                "allowed_uses": ["research", "review", "commercial"],
            }
        ],
    },
    {
        # Registered with no change at all: queries are blocked with
        # no_valid_license until a change is submitted.
        "version_id": "demo-no-license",
        "parent_ids": [],
        "note": "示例：尚未登记任何许可变更的版本",
        "changes": [],
    },
    {
        # Grandchild on the DAG (parents both roots transitively through
        # demo-derived-1). Used as the later target of the demo slot
        # publications, which must move along strict descendant edges.
        "version_id": "demo-derived-2",
        "parent_ids": ["demo-derived-1"],
        "note": "示例派生版本二代（版本槽发布推进目标）",
        "changes": [
            {
                "change_id": "demo-chg-d2-1",
                "effective_at": "2026-04-01T00:00:00Z",
                "allowed_uses": ["research", "review", "commercial"],
            }
        ],
    },
]

# Version-slot publications: two immutable releases published as a chain.
# Each slot's first release claims revision 0; the second release claims the
# slot's NEXT revision (1), targets a strict descendant and becomes effective
# strictly later. The effective instants make historical resolution observable
# (resolving "demo-derived" at 2026-03 vs 2026-06 returns different revisions).
DEMO_RELEASES: list[dict[str, object]] = [
    {
        "release_id": "demo-rel-1",
        "effective_at": "2026-02-15T00:00:00Z",
        "slots": [
            {"slot_name": "demo-slot-a", "target_version_id": "demo-root-a",
             "expected_revision": 0},
            {"slot_name": "demo-derived", "target_version_id": "demo-derived-1",
             "expected_revision": 0},
        ],
    },
    {
        "release_id": "demo-rel-2",
        "effective_at": "2026-05-01T00:00:00Z",
        "slots": [
            {"slot_name": "demo-slot-a", "target_version_id": "demo-derived-1",
             "expected_revision": 1},
            {"slot_name": "demo-derived", "target_version_id": "demo-derived-2",
             "expected_revision": 1},
        ],
    },
]


def seed_demo_data(db: Database) -> bool:
    """Populate demo data into an empty database.

    Returns ``True`` when data was inserted, ``False`` when the database
    already contained versions.
    """
    service = LicenseService(db)
    if db.count_versions() > 0:
        logger.info("数据库非空，跳过示例数据")
        return False

    logger.info("写入示例数据集…")
    for item in DEMO:
        version_id = str(item["version_id"])
        if db.get_version(version_id) is None:
            service.register_version(
                version_id=version_id,
                parent_ids=list(item["parent_ids"]),  # type: ignore[arg-type]
                note=str(item["note"]),
            )
        for change in item["changes"]:  # type: ignore[union-attr]
            service.submit_change(
                change_id=str(change["change_id"]),
                version_id=version_id,
                effective_at=datetime.fromisoformat(str(change["effective_at"])).replace(
                    tzinfo=timezone.utc
                ),
                allowed_uses=list(change["allowed_uses"]),  # type: ignore[index]
            )

    # Version-slot publications (idempotent via the same service path).
    for release in DEMO_RELEASES:
        if db.get_release(str(release["release_id"])) is not None:
            continue
        service.publish_release(
            release_id=str(release["release_id"]),
            effective_at=datetime.fromisoformat(
                str(release["effective_at"])
            ).replace(tzinfo=timezone.utc),
            slots=[dict(slot) for slot in release["slots"]],  # type: ignore[union-attr]
        )
    logger.info(
        "示例数据写入完成：%d 个版本，%d 次版本槽发布",
        len(DEMO),
        len(DEMO_RELEASES),
    )
    return True
