import unittest
from unittest.mock import AsyncMock, patch

from orpheus.server import metadata


class MetadataChecks(unittest.IsolatedAsyncioTestCase):
    async def test_schema_and_upserts_are_parameterized(self):
        connection = AsyncMock()
        with patch.object(metadata, "enabled", return_value=True), patch.object(
            metadata, "_connect", new=AsyncMock(return_value=connection)
        ):
            await metadata.initialize()
            await metadata.sync_project({"id": "a" * 16, "status": "ready"}, "owner")
            await metadata.sync_turn("a" * 16, {"id": "b" * 12, "status": "done"}, "owner")
        self.assertGreaterEqual(connection.execute.await_count, 3)
        self.assertIn("$1", connection.execute.await_args_list[1].args[0])

    async def test_disabled_backend_does_no_io(self):
        with patch.object(metadata, "enabled", return_value=False), patch.object(
            metadata, "_connect", new=AsyncMock()
        ) as connect:
            await metadata.sync_project({"id": "a" * 16}, "owner")
        connect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
