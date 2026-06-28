"""Reject smallwebrtc runs that reach the telephony WebSocket.

Ported from upstream Dograh PR #468. A smallwebrtc (browser) run must connect
through the WebRTC signaling endpoint, not the telephony websocket. If one is
misrouted here it should be closed without running the agent.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.enums import WorkflowRunMode, WorkflowRunState
from api.routes.telephony import _handle_telephony_websocket


@pytest.mark.asyncio
async def test_smallwebrtc_run_reaching_telephony_websocket_closes_without_running():
    websocket = AsyncMock()
    workflow_run = SimpleNamespace(
        id=501,
        workflow_id=33,
        mode=WorkflowRunMode.SMALLWEBRTC.value,
        state=WorkflowRunState.INITIALIZED.value,
        initial_context={"provider": WorkflowRunMode.SMALLWEBRTC.value},
        gathered_context={},
    )
    workflow = SimpleNamespace(id=33, organization_id=11)
    provider_lookup = AsyncMock()

    with (
        patch("api.routes.telephony.db_client") as mock_db,
        patch(
            "api.routes.telephony.get_telephony_provider_for_run",
            new=provider_lookup,
        ),
    ):
        mock_db.get_workflow_run = AsyncMock(return_value=workflow_run)
        mock_db.get_workflow = AsyncMock(return_value=workflow)
        mock_db.update_workflow_run = AsyncMock()

        await _handle_telephony_websocket(websocket, 33, 99, 501)

    websocket.close.assert_awaited_once_with(
        code=4400,
        reason=(
            "smallwebrtc runs connect through the WebRTC signaling endpoint, "
            "not the telephony websocket"
        ),
    )
    assert provider_lookup.await_count == 0
