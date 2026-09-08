"""Hosted Cloud SQL catalog; local mode keeps the filesystem source of truth."""

import json

from .. import config


class MetadataError(RuntimeError):
    pass


def enabled():
    return config.METADATA_BACKEND == "cloud_sql"


def _dsn():
    dsn = config.METADATA_DATABASE_URL or config.DATABASE_URL
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _connect():
    try:
        import asyncpg

        return await asyncpg.connect(_dsn())
    except Exception as exc:
        raise MetadataError("Cloud SQL metadata connection unavailable") from exc


async def initialize():
    if not enabled():
        return
    connection = await _connect()
    try:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orpheus_projects (
                project_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                document JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS orpheus_projects_owner_updated
                ON orpheus_projects(owner_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS orpheus_receipts (
                project_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                receipt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(project_id, receipt_id)
            );
            CREATE INDEX IF NOT EXISTS orpheus_receipts_owner_project
                ON orpheus_receipts(owner_id, project_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS orpheus_runs (
                project_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                run_key TEXT NOT NULL,
                status TEXT NOT NULL,
                operation TEXT,
                cancel_requested BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(project_id, run_key)
            );
            CREATE INDEX IF NOT EXISTS orpheus_runs_active
                ON orpheus_runs(owner_id, project_id, updated_at DESC)
                WHERE status IN ('submitted', 'running');
            CREATE UNIQUE INDEX IF NOT EXISTS orpheus_runs_one_active
                ON orpheus_runs(project_id)
                WHERE status IN ('submitted', 'running');
            """
        )
    except Exception as exc:
        raise MetadataError("Cloud SQL metadata schema unavailable") from exc
    finally:
        await connection.close()


async def sync_project(document, owner_id):
    if not enabled():
        return
    if not isinstance(document, dict) or not isinstance(owner_id, str) or not owner_id:
        raise MetadataError("Invalid project metadata")
    connection = await _connect()
    try:
        await connection.execute(
            """
            INSERT INTO orpheus_projects(project_id, owner_id, document)
            VALUES($1, $2, $3::jsonb)
            ON CONFLICT(project_id) DO UPDATE SET
                owner_id=EXCLUDED.owner_id,
                document=EXCLUDED.document,
                updated_at=now()
            """,
            document["id"],
            owner_id,
            json.dumps(document, allow_nan=False),
        )
    except Exception as exc:
        raise MetadataError("Cloud SQL project write unavailable") from exc
    finally:
        await connection.close()


async def sync_receipt(project_id, owner_id, kind, receipt_id, payload):
    if not enabled():
        return
    connection = await _connect()
    try:
        await connection.execute(
            """
            INSERT INTO orpheus_receipts(project_id, owner_id, receipt_id, kind, payload)
            VALUES($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT(project_id, receipt_id) DO UPDATE SET
                owner_id=EXCLUDED.owner_id,
                kind=EXCLUDED.kind,
                payload=EXCLUDED.payload
            """,
            project_id,
            owner_id,
            receipt_id,
            kind,
            json.dumps(payload, allow_nan=False),
        )
    except Exception as exc:
        raise MetadataError("Cloud SQL receipt write unavailable") from exc
    finally:
        await connection.close()


async def sync_turn(project_id, turn, owner_id):
    await sync_receipt(
        project_id,
        owner_id,
        "turn",
        turn["id"],
        turn,
    )


async def owns(project_id, owner_id):
    if not enabled():
        return True
    connection = await _connect()
    try:
        return bool(
            await connection.fetchval(
                "SELECT 1 FROM orpheus_projects WHERE project_id=$1 AND owner_id=$2",
                project_id,
                owner_id,
            )
        )
    except Exception as exc:
        raise MetadataError("Cloud SQL ownership lookup unavailable") from exc
    finally:
        await connection.close()


async def list_projects(owner_id):
    if not enabled():
        return []
    connection = await _connect()
    try:
        rows = await connection.fetch(
            "SELECT document FROM orpheus_projects WHERE owner_id=$1 ORDER BY updated_at DESC",
            owner_id,
        )
        return [json.loads(row["document"]) for row in rows]
    except Exception as exc:
        raise MetadataError("Cloud SQL project listing unavailable") from exc
    finally:
        await connection.close()


async def begin_run(project_id, owner_id, run_key):
    if not enabled():
        return {"created": True, "status": "submitted", "run_key": run_key}
    if not all(isinstance(value, str) and value for value in (project_id, owner_id, run_key)):
        raise MetadataError("Invalid run metadata")
    connection = await _connect()
    try:
        async with connection.transaction():
            active = await connection.fetchrow(
                """
                SELECT run_key, status, operation, cancel_requested
                FROM orpheus_runs
                WHERE project_id=$1 AND owner_id=$2 AND status IN ('submitted', 'running')
                ORDER BY updated_at DESC LIMIT 1
                """,
                project_id,
                owner_id,
            )
            if active:
                return {"created": False, **dict(active)}
            row = await connection.fetchrow(
                """
                INSERT INTO orpheus_runs(project_id, owner_id, run_key, status)
                VALUES($1, $2, $3, 'submitted')
                ON CONFLICT DO NOTHING
                RETURNING run_key, status, operation, cancel_requested
                """,
                project_id,
                owner_id,
                run_key,
            )
            if row:
                return {"created": True, **dict(row)}
            active = await connection.fetchrow(
                """
                SELECT run_key, status, operation, cancel_requested
                FROM orpheus_runs
                WHERE project_id=$1 AND owner_id=$2 AND status IN ('submitted', 'running')
                ORDER BY updated_at DESC LIMIT 1
                """,
                project_id,
                owner_id,
            )
            return {"created": False, **dict(active)} if active else {"created": False}
    except Exception as exc:
        raise MetadataError("Cloud SQL run state unavailable") from exc
    finally:
        await connection.close()


async def update_run(project_id, owner_id, run_key, status, operation=None, cancel_requested=None):
    if not enabled():
        return
    connection = await _connect()
    try:
        await connection.execute(
            """
            UPDATE orpheus_runs
            SET status=$4,
                operation=COALESCE($5, operation),
                cancel_requested=COALESCE($6, cancel_requested),
                updated_at=now()
            WHERE project_id=$1 AND owner_id=$2 AND run_key=$3
            """,
            project_id,
            owner_id,
            run_key,
            status,
            operation,
            cancel_requested,
        )
    except Exception as exc:
        raise MetadataError("Cloud SQL run update unavailable") from exc
    finally:
        await connection.close()


async def cancel_run(project_id, owner_id, run_key):
    if not enabled():
        return None
    connection = await _connect()
    try:
        return await connection.fetchrow(
            """
            UPDATE orpheus_runs SET status='canceled', cancel_requested=true, updated_at=now()
            WHERE project_id=$1 AND owner_id=$2 AND run_key=$3 AND status IN ('submitted', 'running')
            RETURNING operation
            """,
            project_id,
            owner_id,
            run_key,
        )
    except Exception as exc:
        raise MetadataError("Cloud SQL cancellation update unavailable") from exc
    finally:
        await connection.close()


async def run_canceled(project_id, owner_id, run_key):
    if not enabled() or not run_key:
        return False
    connection = await _connect()
    try:
        return bool(
            await connection.fetchval(
                "SELECT cancel_requested FROM orpheus_runs WHERE project_id=$1 AND owner_id=$2 AND run_key=$3",
                project_id,
                owner_id,
                run_key,
            )
        )
    except Exception as exc:
        raise MetadataError("Cloud SQL cancellation lookup unavailable") from exc
    finally:
        await connection.close()


def sync_project_now(document, owner_id):
    if enabled():
        import asyncio

        asyncio.run(sync_project(document, owner_id))


def sync_receipt_now(project_id, owner_id, kind, receipt_id, payload):
    if enabled():
        import asyncio

        asyncio.run(sync_receipt(project_id, owner_id, kind, receipt_id, payload))


def begin_run_now(project_id, owner_id, run_key):
    if enabled():
        import asyncio

        return asyncio.run(begin_run(project_id, owner_id, run_key))
    return {"created": True, "status": "submitted", "run_key": run_key}


def update_run_now(project_id, owner_id, run_key, status, operation=None, cancel_requested=None):
    if enabled():
        import asyncio

        asyncio.run(update_run(project_id, owner_id, run_key, status, operation, cancel_requested))


def cancel_run_now(project_id, owner_id, run_key):
    if enabled():
        import asyncio

        return asyncio.run(cancel_run(project_id, owner_id, run_key))
    return None
