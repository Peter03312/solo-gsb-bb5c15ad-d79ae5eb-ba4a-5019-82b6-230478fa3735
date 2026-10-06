"""End-to-end tests for version-slot unified releases (publishing + resolution)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    settings = Settings(
        db_path=db_path,
        init_db=True,
        seed_demo=False,
        echo=False,
        signing_keys_dir=str(tmp_path / "keys"),
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _iso(y, m, d, hh=0):
    return datetime(y, m, d, hh, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _register(client, version_id, parents=None):
    resp = client.post(
        "/versions",
        json={"version_id": version_id, "parent_ids": parents or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _publish(client, release_id, at, slots, expected=None):
    resp = client.post(
        "/releases",
        json={"release_id": release_id, "effective_at": at, "slots": slots},
    )
    if expected is not None:
        assert resp.status_code == expected, resp.text
    return resp


def _chain(client):
    """Register a->b->c versions with a diamond fork d(a,b)."""
    _register(client, "a")
    _register(client, "b", ["a"])
    _register(client, "c", ["b"])
    _register(client, "d", ["a", "b"])
    # An unrelated version that is NOT a descendant of a.
    _register(client, "z")


# -- first release ---------------------------------------------------------


def test_first_release_slots_get_revision_zero(client):
    _chain(client)
    resp = _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [
            {"slot_name": " s1 ", "target_version_id": "a", "expected_revision": 0},
            {"slot_name": "s2", "target_version_id": "b", "expected_revision": 0},
        ],
        expected=201,
    )
    body = resp.json()
    assert body["replayed"] is False
    release = body["release"]
    assert release["release_id"] == "rel-1"
    assert release["effective_at"].endswith("Z")
    # Entries are sorted by the normalised slot name; whitespace is trimmed.
    assert [s["slot_name"] for s in release["slots"]] == ["s1", "s2"]
    assert all(s["revision"] == 0 for s in release["slots"])
    assert release["slots"][0]["target_version_id"] == "a"


def test_first_release_is_retrievable_immutable_and_listed(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
        expected=201,
    )
    got = client.get("/releases/rel-1")
    assert got.status_code == 200
    assert got.json()["slots"][0]["revision"] == 0

    listed = client.get("/releases").json()
    assert [r["release_id"] for r in listed] == ["rel-1"]

    # Slot current pointer matches the first release.
    state = client.get("/slots/s1").json()
    assert state["current_revision"] == 0
    assert state["current_version_id"] == "a"
    assert state["current_release_id"] == "rel-1"


def test_first_release_revision_must_be_zero(client):
    _chain(client)
    resp = _publish(
        client,
        "rel-x",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 1}],
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    assert resp.json()["details"]["slot_name"] == "s1"
    # Nothing was published.
    assert client.get("/releases/rel-x").status_code == 404
    assert client.get("/slots/s1").status_code == 404


# -- later releases --------------------------------------------------------


def test_later_release_advances_revision_by_one(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
        expected=201,
    )
    resp = _publish(
        client,
        "rel-2",
        _iso(2026, 2, 1),
        [{"slot_name": "s1", "target_version_id": "b", "expected_revision": 1}],
        expected=201,
    )
    assert resp.json()["release"]["slots"][0]["revision"] == 1
    state = client.get("/slots/s1").json()
    assert state["current_revision"] == 1
    assert state["current_version_id"] == "b"

    # Third publication claims revision 2.
    resp = _publish(
        client,
        "rel-3",
        _iso(2026, 3, 1),
        [{"slot_name": "s1", "target_version_id": "c", "expected_revision": 2}],
        expected=201,
    )
    assert resp.json()["release"]["slots"][0]["revision"] == 2
    assert client.get("/slots/s1").json()["current_revision"] == 2

    # The immutable earlier records keep their revisions.
    assert client.get("/releases/rel-1").json()["slots"][0]["revision"] == 0
    assert client.get("/releases/rel-2").json()["slots"][0]["revision"] == 1


def test_expected_revision_must_be_next_revision(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
        expected=201,
    )
    # Re-claiming the already-assigned revision 0 is rejected.
    resp = _publish(
        client,
        "rel-stale",
        _iso(2026, 2, 1),
        [{"slot_name": "s1", "target_version_id": "b", "expected_revision": 0}],
    )
    assert resp.status_code == 409
    assert resp.json()["details"]["submitted_expected_revision"] == "0"
    assert resp.json()["details"]["required_expected_revision"] == "1"
    # Skipping ahead is rejected.
    resp = _publish(
        client,
        "rel-skip",
        _iso(2026, 2, 1),
        [{"slot_name": "s1", "target_version_id": "b", "expected_revision": 5}],
    )
    assert resp.status_code == 409
    assert resp.json()["details"]["required_expected_revision"] == "1"
    assert client.get("/slots/s1").json()["current_revision"] == 0


def test_target_must_be_strict_descendant_of_previous(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
        expected=201,
    )
    # Same version is not a strict descendant.
    resp = _publish(
        client,
        "rel-same",
        _iso(2026, 2, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 1}],
    )
    assert resp.status_code == 409
    # An unrelated version is not a descendant.
    resp = _publish(
        client,
        "rel-unrel",
        _iso(2026, 2, 1),
        [{"slot_name": "s1", "target_version_id": "z", "expected_revision": 1}],
    )
    assert resp.status_code == 409
    # A transitive descendant skipping one generation is still a descendant,
    # but it must also be paired with a strictly later instant.
    resp = _publish(
        client,
        "rel-skip",
        _iso(2026, 2, 1),
        [{"slot_name": "s1", "target_version_id": "c", "expected_revision": 1}],
    )
    assert resp.status_code == 201
    assert resp.json()["release"]["slots"][0]["revision"] == 1


def test_effective_at_must_be_strictly_later_than_slot_previous(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [
            {"slot_name": "s1", "target_version_id": "a", "expected_revision": 0},
        ],
        expected=201,
    )
    # Equal effective instant -> conflict even with a descendant target.
    resp = _publish(
        client,
        "rel-eq",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "b", "expected_revision": 1}],
    )
    assert resp.status_code == 409
    assert "严格晚于" in resp.json()["message"]
    # Earlier instant -> conflict.
    resp = _publish(
        client,
        "rel-earlier",
        _iso(2025, 12, 1),
        [{"slot_name": "s1", "target_version_id": "b", "expected_revision": 1}],
    )
    assert resp.status_code == 409
    assert client.get("/slots/s1").json()["current_revision"] == 0


def test_slots_advance_independently_per_slot(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [
            {"slot_name": "s1", "target_version_id": "a", "expected_revision": 0},
            {"slot_name": "s2", "target_version_id": "b", "expected_revision": 0},
        ],
        expected=201,
    )
    # Advance only s1 (it claims next revision 1); s2 is untouched.
    resp = _publish(
        client,
        "rel-2",
        _iso(2026, 2, 1),
        [
            {"slot_name": "s1", "target_version_id": "b", "expected_revision": 1},
        ],
        expected=201,
    )
    entry = {s["slot_name"]: s for s in resp.json()["release"]["slots"]}
    assert entry["s1"]["revision"] == 1
    # s2 untouched.
    assert client.get("/slots/s2").json()["current_revision"] == 0


def test_one_failing_slot_aborts_the_whole_release(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [
            {"slot_name": "s1", "target_version_id": "a", "expected_revision": 0},
            {"slot_name": "s2", "target_version_id": "b", "expected_revision": 0},
        ],
        expected=201,
    )
    resp = _publish(
        client,
        "rel-2",
        _iso(2026, 2, 1),
        [
            {"slot_name": "s1", "target_version_id": "b", "expected_revision": 1},
            # z is unrelated to b (not a descendant) -> whole release rejected.
            {"slot_name": "s2", "target_version_id": "z", "expected_revision": 1},
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["details"]["slot_name"] == "s2"
    # No immutable record exists and neither pointer moved.
    assert client.get("/releases/rel-2").status_code == 404
    assert client.get("/slots/s1").json()["current_version_id"] == "a"
    assert client.get("/slots/s2").json()["current_version_id"] == "b"
    assert client.get("/slots/s2").json()["current_revision"] == 0


# -- request validation ----------------------------------------------------


def test_duplicate_slot_names_after_trim_conflict(client):
    _chain(client)
    resp = _publish(
        client,
        "rel-dup",
        _iso(2026, 1, 1),
        [
            {"slot_name": " s1 ", "target_version_id": "a", "expected_revision": 0},
            {"slot_name": "s1", "target_version_id": "b", "expected_revision": 0},
        ],
    )
    assert resp.status_code == 409
    assert "不可重复" in resp.json()["message"]
    assert client.get("/releases").json() == []


def test_unknown_target_version_is_404(client):
    _register(client, "a")
    resp = _publish(
        client,
        "rel-404",
        _iso(2026, 1, 1),
        [
            {"slot_name": "s1", "target_version_id": "a", "expected_revision": 0},
            {"slot_name": "s2", "target_version_id": "ghost", "expected_revision": 0},
        ],
    )
    assert resp.status_code == 404
    assert "ghost" in resp.json()["details"]["missing_version_ids"]
    assert client.get("/releases").json() == []
    assert client.get("/slots/s1").status_code == 404


def test_empty_slots_and_naive_time_are_422(client):
    resp = client.post(
        "/releases",
        json={
            "release_id": "rel-empty",
            "effective_at": _iso(2026, 1, 1),
            "slots": [],
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/releases",
        json={
            "release_id": "rel-naive",
            "effective_at": "2026-01-01T00:00:00",
            "slots": [
                {"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}
            ],
        },
    )
    assert resp.status_code == 422


def test_blank_slot_name_is_422(client):
    _chain(client)
    resp = _publish(
        client,
        "rel-blank",
        _iso(2026, 1, 1),
        [{"slot_name": "   ", "target_version_id": "a", "expected_revision": 0}],
    )
    assert resp.status_code == 422


def test_negative_expected_revision_is_422(client):
    _chain(client)
    resp = _publish(
        client,
        "rel-neg",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": -1}],
    )
    assert resp.status_code == 422


def test_unknown_release_and_slot_are_404(client):
    assert client.get("/releases/nope").status_code == 404
    assert client.get("/slots/nope").status_code == 404


# -- idempotency and conflict ----------------------------------------------


def test_same_release_id_same_content_replays_first(client):
    _chain(client)
    first = _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [
            {"slot_name": "s2", "target_version_id": "b", "expected_revision": 0},
            {"slot_name": " s1 ", "target_version_id": "a", "expected_revision": 0},
        ],
        expected=201,
    ).json()

    # Same content: slots reordered, names padded with whitespace, same instant
    # expressed with a +00:00 offset -> canonical content identical.
    retry = client.post(
        "/releases",
        json={
            "release_id": "rel-1",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "slots": [
                {"slot_name": "s1", "target_version_id": "a", "expected_revision": 0},
                {"slot_name": "s2", "target_version_id": "b", "expected_revision": 0},
            ],
        },
    )
    assert retry.status_code == 200
    body = retry.json()
    assert body["replayed"] is True
    assert body["release"] == first["release"]
    # The replay did not advance revisions.
    assert client.get("/slots/s1").json()["current_revision"] == 0


def test_same_release_id_different_content_conflicts(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
        expected=201,
    )
    # Different effective instant.
    resp = _publish(
        client,
        "rel-1",
        _iso(2026, 2, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
    )
    assert resp.status_code == 409
    # Different slot list (different expected revision).
    resp = _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 9}],
    )
    assert resp.status_code == 409
    # Different slot set.
    resp = _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s2", "target_version_id": "a", "expected_revision": 0}],
    )
    assert resp.status_code == 409
    assert client.get("/slots/s1").json()["current_revision"] == 0
    assert client.get("/slots/s2").status_code == 404


# -- resolution ------------------------------------------------------------


def _setup_chain_releases(client):
    _chain(client)
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
        expected=201,
    )
    _publish(
        client,
        "rel-2",
        _iso(2026, 3, 1),
        [{"slot_name": "s1", "target_version_id": "b", "expected_revision": 1}],
        expected=201,
    )
    _publish(
        client,
        "rel-3",
        _iso(2026, 5, 1),
        [{"slot_name": "s1", "target_version_id": "c", "expected_revision": 2}],
        expected=201,
    )


def test_resolve_returns_latest_target_effective_at_or_before(client):
    _setup_chain_releases(client)

    # Before the second release: the first target (a, revision 0).
    r = client.get(
        "/slots/s1/resolve", params={"use": "research", "at": _iso(2026, 2, 15)}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == {
        "release_id": "rel-1",
        "revision": 0,
        "target_version_id": "a",
        "release_effective_at": "2026-01-01T00:00:00Z",
    }

    # Exactly at rel-2's effective instant (inclusive): b, revision 1.
    r = client.get(
        "/slots/s1/resolve", params={"use": "research", "at": _iso(2026, 3, 1)}
    )
    body = r.json()
    assert body["resolved"]["revision"] == 1
    assert body["resolved"]["target_version_id"] == "b"
    assert body["resolved"]["release_id"] == "rel-2"

    # After rel-3: c, revision 2.
    r = client.get(
        "/slots/s1/resolve", params={"use": "research", "at": _iso(2026, 6, 1)}
    )
    body = r.json()
    assert body["resolved"]["revision"] == 2
    assert body["resolved"]["target_version_id"] == "c"


def test_resolve_decision_uses_existing_permission_rules(client):
    _setup_chain_releases(client)
    # Give a research-only license at 2026-01-01 across the chain.
    for vid, cid in (("a", "lc-a"), ("b", "lc-b"), ("c", "lc-c")):
        resp = client.post(
            "/license-changes",
            json={
                "change_id": cid,
                "version_id": vid,
                "effective_at": _iso(2026, 1, 1),
                "allowed_uses": ["research"],
            },
        )
        assert resp.status_code == 201, resp.text

    r = client.get(
        "/slots/s1/resolve", params={"use": "research", "at": _iso(2026, 6, 1)}
    )
    body = r.json()
    assert body["decision"]["version_id"] == "c"
    assert body["decision"]["allowed"] is True
    assert body["decision"]["allowed_uses_intersection"] == ["research"]

    r = client.get(
        "/slots/s1/resolve", params={"use": "commercial", "at": _iso(2026, 6, 1)}
    )
    body = r.json()
    assert body["decision"]["allowed"] is False
    blockers = {(b["version_id"], b["reason"]) for b in body["decision"]["blockers"]}
    assert all(reason == "use_not_allowed" for _, reason in blockers)


def test_resolve_slot_never_published_is_explicit_404(client):
    _chain(client)
    r = client.get(
        "/slots/ghost/resolve", params={"use": "research", "at": _iso(2026, 6, 1)}
    )
    assert r.status_code == 404
    assert r.json()["details"]["reason"] == "slot_not_published"


def test_resolve_before_first_effective_is_explicit_404(client):
    _setup_chain_releases(client)
    r = client.get(
        "/slots/s1/resolve", params={"use": "research", "at": _iso(2025, 12, 1)}
    )
    assert r.status_code == 404
    assert r.json()["details"]["reason"] == "not_effective_at"
    assert r.json()["details"]["first_release_id"] == "rel-1"


def test_resolve_naive_time_is_422(client):
    _setup_chain_releases(client)
    r = client.get(
        "/slots/s1/resolve",
        params={"use": "research", "at": "2026-03-01T00:00:00"},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_time"


def test_resolution_is_historical_after_revocation(client):
    _setup_chain_releases(client)
    client.post(
        "/license-changes",
        json={
            "change_id": "lc-a",
            "version_id": "a",
            "effective_at": _iso(2026, 1, 1),
            "allowed_uses": ["research"],
        },
    )
    client.post(
        "/license-changes",
        json={
            "change_id": "lc-b",
            "version_id": "b",
            "effective_at": _iso(2026, 1, 1),
            "allowed_uses": ["research"],
        },
    )
    client.post(
        "/license-changes",
        json={
            "change_id": "lc-c",
            "version_id": "c",
            "effective_at": _iso(2026, 1, 1),
            "allowed_uses": ["research"],
        },
    )
    # Revoking lc-c denies resolution at the latest target c for any later time,
    # while resolution at an earlier instant still uses b and remains allowed.
    rev = client.post(
        "/license-changes/revocations",
        json={"change_id": "lc-c", "revocation_id": "rev-lc-c", "reason": "x"},
    )
    assert rev.status_code == 201, rev.text

    latest = client.get(
        "/slots/s1/resolve", params={"use": "research", "at": _iso(2026, 6, 1)}
    ).json()
    assert latest["decision"]["allowed"] is False
    assert latest["resolved"]["target_version_id"] == "c"
    assert any(
        b["version_id"] == "c" and b["reason"] == "no_valid_license"
        for b in latest["decision"]["blockers"]
    )

    earlier = client.get(
        "/slots/s1/resolve", params={"use": "research", "at": _iso(2026, 3, 1)}
    ).json()
    assert earlier["resolved"]["target_version_id"] == "b"
    assert earlier["decision"]["allowed"] is True


# -- atomicity / concurrency ------------------------------------------------


def test_concurrent_same_release_id_commits_exactly_once(client):
    _chain(client)
    slots = [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}]
    results: list = []
    lock = threading.Lock()

    def worker(i):
        resp = client.post(
            "/releases",
            json={
                "release_id": "rel-race",
                "effective_at": _iso(2026, 1, 1),
                "slots": slots,
            },
        )
        with lock:
            results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    created = [r for r in results if r[0] == 201]
    replayed = [r for r in results if r[0] == 200]
    assert len(created) == 1
    assert len(replayed) == 7
    assert all(r[1]["replayed"] is True for r in replayed)
    # Every response carries the identical frozen record.
    frozen = created[0][1]["release"]
    assert all(r[1]["release"] == frozen for r in replayed)
    # Exactly one immutable record and one slot entry.
    assert client.get("/releases/rel-race").status_code == 200
    state = client.get("/slots/s1").json()
    assert state["current_revision"] == 0
    assert state["current_release_id"] == "rel-race"


def test_concurrent_advancing_releases_never_skip_or_duplicate_revision(client):
    _chain(client)
    # Publish revision 0 first.
    _publish(
        client,
        "rel-0",
        _iso(2026, 1, 1),
        [{"slot_name": "s1", "target_version_id": "a", "expected_revision": 0}],
        expected=201,
    )
    statuses: list[int] = []
    lock = threading.Lock()

    def worker(i):
        # All contenders claim the NEXT revision 1; at most one can win, the
        # rest must 409 (revision mismatch) — never silently overwrite or
        # skip, never leave two entries at the same revision.
        resp = _publish(
            client,
            f"rel-cont-{i}",
            _iso(2026, 2, 1 + i),
            [{"slot_name": "s1", "target_version_id": "b", "expected_revision": 1}],
        )
        with lock:
            statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses).count(201) == 1
    assert sorted(statuses).count(409) == 5
    state = client.get("/slots/s1").json()
    assert state["current_revision"] == 1
    assert state["current_version_id"] == "b"

    # The unique (slot, revision) constraint means only one entry at revision 1;
    # the failed contenders inserted nothing.
    releases = client.get("/releases").json()
    winners = [
        r for r in releases
        if any(s["slot_name"] == "s1" and s["revision"] == 1 for s in r["slots"])
    ]
    assert len(winners) == 1


def test_multi_slot_release_is_atomic_under_validation_failure(client):
    _chain(client)
    # First release establishes three slots at revision 0.
    _publish(
        client,
        "rel-1",
        _iso(2026, 1, 1),
        [
            {"slot_name": "s1", "target_version_id": "a", "expected_revision": 0},
            {"slot_name": "s2", "target_version_id": "a", "expected_revision": 0},
            {"slot_name": "s3", "target_version_id": "b", "expected_revision": 0},
        ],
        expected=201,
    )
    # s3->c is valid, but s1->z fails the descendant rule: nothing advances.
    resp = _publish(
        client,
        "rel-2",
        _iso(2026, 2, 1),
        [
            {"slot_name": "s1", "target_version_id": "z", "expected_revision": 1},
            {"slot_name": "s2", "target_version_id": "b", "expected_revision": 1},
            {"slot_name": "s3", "target_version_id": "c", "expected_revision": 1},
        ],
    )
    assert resp.status_code == 409
    for name in ("s1", "s2", "s3"):
        state = client.get(f"/slots/{name}").json()
        assert state["current_revision"] == 0
    assert [r["release_id"] for r in client.get("/releases").json()] == ["rel-1"]


def test_seeded_demo_publishes_and_resolves(tmp_path):
    from app.seed import seed_demo_data
    from app.database import Database

    db_path = str(tmp_path / "demo.db")
    db = Database(db_path)
    db.init_schema()
    assert seed_demo_data(db) is True
    db.close()

    settings = Settings(
        db_path=db_path,
        init_db=False,
        seed_demo=False,
        echo=False,
        signing_keys_dir=str(tmp_path / "keys"),
    )
    app = create_app(settings)
    with TestClient(app) as c:
        releases = c.get("/releases").json()
        assert [r["release_id"] for r in releases] == ["demo-rel-1", "demo-rel-2"]
        # 2026-03 resolves the first publication of "demo-derived" (revision 0).
        body = c.get(
            "/slots/demo-derived/resolve",
            params={"use": "research", "at": _iso(2026, 3, 1)},
        ).json()
        assert body["resolved"]["revision"] == 0
        assert body["resolved"]["target_version_id"] == "demo-derived-1"
        # 2026-06 resolves the second (revision 1).
        body = c.get(
            "/slots/demo-derived/resolve",
            params={"use": "research", "at": _iso(2026, 6, 1)},
        ).json()
        assert body["resolved"]["revision"] == 1
        assert body["resolved"]["target_version_id"] == "demo-derived-2"
