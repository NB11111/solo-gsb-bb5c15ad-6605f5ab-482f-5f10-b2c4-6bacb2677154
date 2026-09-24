"""摘要列迁移测试：旧库启动时自动补列回填，迁移可重复，新写入同事务落摘要。"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.digest import run_digest
from app.main import app
from app.schemas import EnzymeIn

ECORI = {"name": "EcoRI", "pattern": "GAATTC", "cut_top": 1, "cut_bottom": 5}

# 迁移前的旧表结构：results 只有完整结果 JSON，没有摘要列
LEGACY_SCHEMA = """
CREATE TABLE submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence    TEXT NOT NULL,
    topology    TEXT NOT NULL CHECK (topology IN ('linear', 'circular')),
    created_at  TEXT NOT NULL
);
CREATE TABLE enzymes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    pattern       TEXT NOT NULL,
    cut_top       INTEGER NOT NULL,
    cut_bottom    INTEGER NOT NULL
);
CREATE TABLE results (
    submission_id INTEGER PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    result_json   TEXT NOT NULL
);
CREATE INDEX idx_enzymes_submission ON enzymes(submission_id);
"""


def _real_result(sequence, topology, enzymes):
    return run_digest(sequence, topology, [EnzymeIn(**e) for e in enzymes])


def _create_legacy_db(path):
    """按迁移前的旧表结构建库，插入两条历史记录（1 切口 2 片段 / 0 切口 1 片段）。"""
    result1 = _real_result("AAAAGAATTCTTTT", "linear", [ECORI])
    result2 = _real_result("GCGCGCGC", "circular", [ECORI])
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO submissions (id, sequence, topology, created_at)"
        " VALUES (1, 'AAAAGAATTCTTTT', 'linear', '2024-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO enzymes (submission_id, name, pattern, cut_top, cut_bottom)"
        " VALUES (1, 'EcoRI', 'GAATTC', 1, 5)"
    )
    conn.execute(
        "INSERT INTO results (submission_id, result_json) VALUES (1, ?)",
        (json.dumps(result1, ensure_ascii=False),),
    )
    conn.execute(
        "INSERT INTO submissions (id, sequence, topology, created_at)"
        " VALUES (2, 'GCGCGCGC', 'circular', '2024-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO results (submission_id, result_json) VALUES (2, ?)",
        (json.dumps(result2, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()
    return result1, result2


def _summary_rows(path):
    """直接读取 results 表的 (submission_id, cut_count, fragment_count)，按 id 升序。"""
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT submission_id, cut_count, fragment_count FROM results"
        " ORDER BY submission_id"
    ).fetchall()
    conn.close()
    return rows


@pytest.fixture()
def legacy_client(tmp_path, monkeypatch):
    """先造一个旧结构的库，再让应用在其上启动（触发迁移）。"""
    db_path = tmp_path / "legacy.db"
    results = _create_legacy_db(str(db_path))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    with TestClient(app) as client:
        yield client, str(db_path), results


def test_migration_backfills_legacy_rows(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    result1, result2 = _create_legacy_db(db_path)

    Database(db_path)  # 启动时建表并迁移

    assert _summary_rows(db_path) == [
        (1, len(result1["cuts"]), len(result1["fragments"])),
        (2, len(result2["cuts"]), len(result2["fragments"])),
    ]


def test_migration_is_repeatable(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    _create_legacy_db(db_path)

    Database(db_path)
    first = _summary_rows(db_path)
    Database(db_path)  # 重复执行：不再 ALTER、无重复回填，结果不变
    Database(db_path)

    assert _summary_rows(db_path) == first


def test_migration_preserves_full_results(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    result1, result2 = _create_legacy_db(db_path)

    Database(db_path)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT submission_id, result_json FROM results ORDER BY submission_id"
    ).fetchall()
    conn.close()
    assert json.loads(rows[0][1]) == result1  # 完整结果 JSON 不被迁移改动
    assert json.loads(rows[1][1]) == result2


def test_list_uses_backfilled_summary(legacy_client):
    client, _, _ = legacy_client
    resp = client.get("/digests")
    assert resp.status_code == 200
    items = resp.json()
    assert [i["id"] for i in items] == [1, 2]  # 按 id 升序
    assert items[0] == {
        "id": 1,
        "topology": "linear",
        "sequence_length": 14,
        "enzyme_count": 1,
        "cut_count": 1,
        "fragment_count": 2,
        "created_at": "2024-01-01T00:00:00+00:00",
    }
    assert items[1]["cut_count"] == 0
    assert items[1]["fragment_count"] == 1  # 无切口保留整条分子
    assert items[1]["enzyme_count"] == 0


def test_full_record_still_available_after_migration(legacy_client):
    client, _, (result1, _) = legacy_client
    got = client.get("/digests/1")
    assert got.status_code == 200
    assert got.json()["cuts"] == result1["cuts"]
    assert got.json()["fragments"] == result1["fragments"]


def test_new_submission_after_migration_writes_summary(legacy_client):
    client, db_path, _ = legacy_client
    resp = client.post(
        "/digests",
        json={
            "sequence": "AAAAGAATTCTTTT",
            "topology": "linear",
            "enzymes": [ECORI],
        },
    )
    assert resp.status_code == 201
    new_id = resp.json()["id"]

    items = client.get("/digests").json()
    assert [i["id"] for i in items] == [1, 2, new_id]  # 新旧记录混排仍按 id 升序
    assert items[-1]["cut_count"] == 1
    assert items[-1]["fragment_count"] == 2

    # 摘要列与完整结果在同一事务落库
    assert _summary_rows(db_path)[-1] == (new_id, 1, 2)


def test_fresh_db_new_submission_persists_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    with TestClient(app) as client:
        resp = client.post(
            "/digests",
            json={
                "sequence": "AAAAGAATTCTTTT",
                "topology": "linear",
                "enzymes": [ECORI],
            },
        )
        assert resp.status_code == 201
    assert _summary_rows(str(db_path)) == [(1, 1, 2)]


def test_failed_save_rolls_back_summary_and_result(tmp_path):
    database = Database(str(tmp_path / "fresh.db"))
    with pytest.raises(KeyError):
        database.save_submission("ACGT", "linear", [], {"no_cuts": True})
    conn = sqlite3.connect(database.path)
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    conn.close()
