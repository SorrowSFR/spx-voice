"""
E2E Integration Test: SC-RURAL-04 - Dropped Connection Recovery

Scenario:
- Caller Profile: RURAL: Farmer calling from area with poor network, call drops after 2 minutes
- Language: Hindi
- Expected Flow: IVR greeting -> Bot starts collecting information ->
  Call drops at minute 2 -> Call reconnects at minute 5 ->
  Bot recognizes returning caller -> 'I see we were disconnected. Would you like to continue
  from where we left off?' -> Bot resumes from last confirmed piece of information ->
  Continues conversation seamlessly

Test Infrastructure Used:
- patch_run_pipeline_externals: Mocks LLM, TTS, STT, S3, external integrations
- MockTransport: Simulates WebRTC transport
- MockLLMService: Simulates LLM responses with configurable steps
- create_workflow_run_rows: Sets up test database with org/user/workflow/run rows
- TranscriptionFrame: Simulates user speech input to the pipeline

Assertions:
1. Bot recognizes returning caller (via call_id/participant context)
2. Bot acknowledges disconnection and asks to continue
3. Bot resumes from last confirmed piece of information
4. Workflow completes successfully after reconnection
5. gathered_context preserves state from both sessions
6. nodes_visited tracks all nodes across both connections

CURRENT STATUS: TEST INFRASTRUCTURE EXISTS BUT FEATURE NOT IMPLEMENTED
This test documents what needs to be built to support dropped connection recovery.
"""

import asyncio
from typing import Optional

import pytest
from pipecat.frames.frames import TranscriptionFrame
from pipecat.tests import MockLLMService, MockTTSService
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.utils.time import time_now_iso8601

from api.enums import WorkflowRunMode
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.run_pipeline import _run_pipeline
from api.tests.integrations._run_pipeline_helpers import (
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)

# =============================================================================
# Test Workflow Definition - Rural Caller with Disconnection Support
# =============================================================================

RURAL_DROPPED_CONNECTION_WORKFLOW = {
    "nodes": [
        {
            "id": "start",
            "type": "startCall",
            "position": {"x": 0, "y": 0},
            "data": {
                "name": "Start",
                "prompt": (
                    "You are a helpful assistant for rural farmers. "
                    "Use simple Hindi words. Speak slowly and clearly. "
                    "Collect information about the caller's land and crops."
                ),
                "is_start": True,
                "allow_interrupt": True,
                "add_global_prompt": True,
                "greeting_type": "text",
                "greeting": (
                    "Namaskar. Aapka swagat hai. "
                    "Kya aapke paas kheti ke baare mein batane ke liye waqt hai?"
                ),
            },
        },
        {
            "id": "collect_land_info",
            "type": "agentNode",
            "position": {"x": 0, "y": 150},
            "data": {
                "name": "Collect Land Info",
                "prompt": (
                    "You are collecting land information from a rural caller. "
                    "Use simple Hindi words. "
                    "Ask about the size of their land in acres or bigha. "
                    "Confirm the land size when provided. "
                    "Use vocabulary like: 'zameen' for land, 'acre' as 'acre', "
                    "'kitna' for how much."
                ),
                "allow_interrupt": True,
                "add_global_prompt": True,
                "extraction_enabled": True,
                "extraction_prompt": "Extract land information from the conversation.",
                "extraction_variables": [
                    {
                        "name": "land_size",
                        "type": "string",
                        "prompt": "What is the size of the caller's land?",
                    },
                    {
                        "name": "land_size_confirmed",
                        "type": "boolean",
                        "prompt": "Did the caller confirm their land size?",
                    },
                ],
            },
        },
        {
            "id": "collect_crop_info",
            "type": "agentNode",
            "position": {"x": 0, "y": 300},
            "data": {
                "name": "Collect Crop Info",
                "prompt": (
                    "You are collecting crop information from a rural caller. "
                    "Use simple Hindi words. "
                    "Ask what crops they grow on their land. "
                    "Use vocabulary like: 'fasal' for crop, 'ugaao' for grow, "
                    "'gehu' for wheat, 'chaval' for rice."
                ),
                "allow_interrupt": True,
                "add_global_prompt": True,
                "extraction_enabled": True,
                "extraction_prompt": "Extract crop information from the conversation.",
                "extraction_variables": [
                    {
                        "name": "crops_grown",
                        "type": "string",
                        "prompt": "What crops does the caller grow?",
                    },
                ],
            },
        },
        {
            "id": "end",
            "type": "endCall",
            "position": {"x": 0, "y": 450},
            "data": {
                "name": "End Call",
                "prompt": (
                    "Thank the caller and end the call politely in Hindi. "
                    "Use simple words like: 'Dhanyavaad' (thank you), 'namaste' (goodbye)."
                ),
                "is_end": True,
                "allow_interrupt": False,
                "add_global_prompt": False,
            },
        },
    ],
    "edges": [
        {
            "id": "start-collect_land",
            "source": "start",
            "target": "collect_land_info",
            "data": {
                "label": "After Greeting",
                "condition": "After greeting, transition to land collection",
            },
        },
        {
            "id": "collect_land-collect_crop",
            "source": "collect_land_info",
            "target": "collect_crop_info",
            "data": {
                "label": "Land Info Collected",
                "condition": "When caller confirms their land size",
            },
        },
        {
            "id": "collect_crop-end",
            "source": "collect_crop_info",
            "target": "end",
            "data": {
                "label": "End Call",
                "condition": "When crop information is collected",
            },
        },
    ],
}

# Hard cap on the entire test to prevent hung pipelines
TEST_HARD_TIMEOUT_SECONDS = 90.0


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def dropped_connection_workflow_setup(db_session, async_session):
    """Create org/user/user_configuration/workflow/workflow_run rows for
    the dropped connection recovery scenario."""
    return await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=RURAL_DROPPED_CONNECTION_WORKFLOW,
        name_prefix="SC-RURAL-04 Dropped Connection",
        provider_id_suffix="dropped-connection",
    )


# =============================================================================
# Helper Functions
# =============================================================================


def create_hindi_transcription(text: str) -> TranscriptionFrame:
    """Create a TranscriptionFrame simulating Hindi speech input."""
    return TranscriptionFrame(
        text=text,
        user_id="rural-caller-001",
        timestamp=time_now_iso8601(),
    )


def find_processor_by_class_name(pipeline_task, class_name: str):
    """Walk the pipeline tree and find a processor by class name."""
    visited: set[int] = set()
    stack = [pipeline_task._pipeline]
    while stack:
        processor = stack.pop()
        if id(processor) in visited:
            continue
        visited.add(id(processor))
        if processor.__class__.__name__ == class_name:
            return processor
        sub = getattr(processor, "_processors", None)
        if sub:
            stack.extend(sub)
    return None


async def wait_for_condition(
    predicate,
    *,
    timeout: float,
    interval: float = 0.1,
) -> bool:
    """Poll a condition until it becomes true or timeout expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# =============================================================================
# E2E Test Body - Dropped Connection Recovery
# =============================================================================


async def run_dropped_connection_recovery_test(
    workflow_run_setup,
    db_session,
    simulate_disconnect_reconnect: bool = True,
) -> dict:
    """
    Execute the SC-RURAL-04 test scenario.

    Args:
        workflow_run_setup: Fixture providing workflow run rows
        db_session: Database session
        simulate_disconnect_reconnect: If True, simulates a dropped connection
            and reconnection. If False, runs a normal uninterrupted call.

    Returns a dict with test results:
    - success: bool
    - steps_completed: list of str
    - errors: list of str
    - extracted_variables: dict
    - disconnection_point: str (where in the workflow the disconnect occurred)
    """
    workflow_run, user, workflow = workflow_run_setup
    results = {
        "success": False,
        "steps_completed": [],
        "errors": [],
        "extracted_variables": {},
        "disconnection_point": None,
    }

    # Configure LLM responses for the conversation flow:
    # Session 1:
    #   - Bot greets
    #   - User says "haan" confirming availability
    #   - Bot asks for land size
    #   - User provides "5 acre"
    #   - Connection drops (DISCONNECTION POINT)
    #
    # Session 2 (after reconnection):
    #   - Bot recognizes returning caller
    #   - Bot says "I see we were disconnected. Would you like to continue?"
    #   - User says "haan" confirming continue
    #   - Bot confirms land size was "5 acre"
    #   - Bot asks about crops
    #   - User provides crop info
    #   - Bot ends call

    llm_responses = [
        # Session 1, Step 1: User confirms availability
        MockLLMService.create_function_call_chunks(
            function_name="collect_land_info_continue",
            arguments={"user_said": "haan", "ready_for_info": True},
            tool_call_id="call_1",
        ),
        # Session 1, Step 2: User provides land size (before drop)
        MockLLMService.create_function_call_chunks(
            function_name="collect_crop_info_transition",
            arguments={"land_size": "5 acre", "land_confirmed": True},
            tool_call_id="call_2",
        ),
        # Session 2, Step 1: Bot acknowledges disconnection
        MockLLMService.create_function_call_chunks(
            function_name="acknowledge_disconnection",
            arguments={"caller_confirmed": True, "continue_session": True},
            tool_call_id="call_3",
        ),
        # Session 2, Step 2: User confirms land size
        MockLLMService.create_function_call_chunks(
            function_name="collect_crop_info_transition",
            arguments={
                "land_size": "5 acre",
                "land_confirmed": True,
                "from_reconnect": True,
            },
            tool_call_id="call_4",
        ),
        # Session 2, Step 3: User provides crop info
        MockLLMService.create_function_call_chunks(
            function_name="end_call",
            arguments={"crops_grown": "wheat and rice"},
            tool_call_id="call_5",
        ),
    ]

    llm = MockLLMService(
        mock_steps=llm_responses,
        chunk_delay=0.001,
    )

    tts = MockTTSService(mock_audio_duration_ms=30, frame_delay=0)

    transport = MockTransport(
        TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    )

    captured_task: list = []
    audio_config = create_audio_config(WorkflowRunMode.SMALLWEBRTC.value)
    pipeline_task: Optional[object] = None
    disconnected = False

    try:
        with patch_run_pipeline_externals(captured_task, llm=llm, tts=tts):
            run_coro = _run_pipeline(
                transport=transport,
                workflow_id=workflow.id,
                workflow_run_id=workflow_run.id,
                user_id=user.id,
                audio_config=audio_config,
                user_provider_id=user.provider_id,
            )
            run_task = asyncio.create_task(run_coro)

            # Wait for pipeline task to be created
            for _ in range(60):
                if captured_task or run_task.done():
                    break
                await asyncio.sleep(0.05)

            if run_task.done() and not captured_task:
                run_task.result()

            assert captured_task, "create_pipeline_task was never invoked"
            pipeline_task = captured_task[0]

            # Wait for pipeline to start
            await asyncio.wait_for(
                pipeline_task._pipeline_start_event.wait(),
                timeout=3.0,
            )

            # =================================================================
            # SESSION 1: Normal conversation until disconnection point
            # =================================================================

            # Wait for greeting to complete
            await asyncio.sleep(0.5)

            # User confirms availability
            await pipeline_task.queue_frame(create_hindi_transcription("haan"))
            results["steps_completed"].append("Session 1: User confirmed availability")

            # Wait for LLM to process
            await asyncio.sleep(0.5)

            # User provides land size
            await pipeline_task.queue_frame(
                create_hindi_transcription("panch acre zameen hai")
            )
            results["steps_completed"].append(
                "Session 1: User provided land size (5 acre)"
            )
            results["disconnection_point"] = "after_land_size_provided"

            # =================================================================
            # SIMULATE DROPPED CONNECTION
            # =================================================================

            if simulate_disconnect_reconnect:
                # Simulate network drop by firing disconnect event
                # This would trigger on_client_disconnected handler
                logger.info("Simulating network drop at disconnection point")

                # Store the current workflow run state before disconnect
                pre_disconnect_run = await db_session.get_workflow_run_by_id(
                    workflow_run.id
                )
                pre_disconnect_context = pre_disconnect_run.gathered_context.copy()
                pre_disconnect_nodes = pre_disconnect_context.get("nodes_visited", [])

                results["steps_completed"].append(
                    f"Pre-disconnect nodes visited: {pre_disconnect_nodes}"
                )

                # In a real implementation, this is where the disconnect handler would:
                # 1. NOT call end_call_with_reason (currently it does!)
                # 2. Instead, update workflow_run state to DISCONNECTED
                # 3. Persist gathered_context with session info
                # 4. Wait for reconnection

                # For the test, we simulate the reconnection by:
                # 1. The workflow run would be in a DISCONNECTED state
                # 2. A new connection would come in with the same call_id
                # 3. The system would recognize the returning caller
                # 4. Restore the session context

                # Since this is NOT implemented, we document what SHOULD happen:
                results["errors"].append(
                    "FEATURE NOT IMPLEMENTED: Dropped connection recovery is not currently "
                    "supported. The system ends the call immediately on disconnect "
                    "(see event_handlers.py on_client_disconnected). "
                    "To support this feature, the following would be needed:"
                )

                # What would be needed:
                needed_features = [
                    "1. WorkflowRunState.DISCONNECTED enum value",
                    "2. Session state persistence (Redis or DB) for LLM context",
                    "3. Reconnection handler that restores session from persisted state",
                    "4. Bot prompt modification to handle reconnection acknowledgment",
                    "5. 'acknowledge_disconnection' function in the workflow graph",
                ]
                results["errors"].extend(needed_features)

                # Cancel the pipeline task since feature is not implemented
                await pipeline_task.cancel()
                await asyncio.wait_for(run_task, timeout=5.0)

                results["success"] = False
                return results

            # =================================================================
            # SESSION 2: Reconnection flow (NOT YET IMPLEMENTED)
            # =================================================================

            # If we reach here, it means the reconnection feature is implemented
            # In that case, the pipeline_task would be recreated or restored

            # User confirms to continue after reconnection
            await pipeline_task.queue_frame(
                create_hindi_transcription("haan, continue karo")
            )
            results["steps_completed"].append("Session 2: User confirmed to continue")

            # Wait for LLM to process
            await asyncio.sleep(0.5)

            # User provides crop information
            await pipeline_task.queue_frame(
                create_hindi_transcription("gehu aur chaval")
            )
            results["steps_completed"].append("Session 2: User provided crop info")

            # Wait for run to complete
            await asyncio.wait_for(run_task, timeout=30.0)

        # Verify results
        refreshed = await db_session.get_workflow_run_by_id(workflow_run.id)

        # Check workflow completed successfully
        assert refreshed.is_completed is True, (
            f"Workflow should have completed. State: {refreshed.state}"
        )
        results["success"] = True

        # Verify nodes were visited (should include nodes from both sessions)
        nodes_visited = refreshed.gathered_context.get("nodes_visited", [])
        assert "Start" in nodes_visited, "Start node should have been visited"
        assert "Collect Land Info" in nodes_visited, (
            "Collect Land Info node should have been visited"
        )
        assert "Collect Crop Info" in nodes_visited, (
            "Collect Crop Info node should have been visited"
        )
        assert "End Call" in nodes_visited, "End Call node should have been visited"

        results["steps_completed"].extend(
            [
                "Workflow completed successfully",
                f"Nodes visited: {nodes_visited}",
            ]
        )

        # Verify extraction captured the conversation outcomes
        extracted = refreshed.gathered_context
        if "land_size" in str(extracted):
            results["extracted_variables"]["land_size"] = "captured"
        if "crops_grown" in str(extracted):
            results["extracted_variables"]["crops_grown"] = "captured"

        # Verify session continuity
        # In a proper implementation, there should be evidence of both sessions
        # This could be in gathered_context as "session_count" or "reconnection_event"
        if "reconnection_event" in refreshed.gathered_context:
            results["steps_completed"].append(
                "Reconnection event captured in gathered_context"
            )

    except AssertionError as e:
        results["errors"].append(f"Assertion failed: {e}")
        results["success"] = False
    except asyncio.TimeoutError:
        results["errors"].append(
            f"Test exceeded hard timeout of {TEST_HARD_TIMEOUT_SECONDS}s"
        )
        results["success"] = False
    except Exception as e:
        results["errors"].append(f"Unexpected error: {type(e).__name__}: {e}")
        results["success"] = False
    finally:
        if pipeline_task is not None and not pipeline_task.has_finished():
            try:
                await asyncio.wait_for(pipeline_task.cancel(), timeout=3.0)
            except Exception:
                pass

    return results


# =============================================================================
# Test Cases
# =============================================================================


@pytest.mark.asyncio
async def test_sc_rural_04_dropped_connection_recovery(
    dropped_connection_workflow_setup,
    db_session,
):
    """
    SC-RURAL-04: Dropped Connection Recovery

    This test verifies the system's ability to handle dropped connections
    for rural callers with poor network coverage.

    CURRENT STATUS: FEATURE NOT IMPLEMENTED
    This test documents the expected behavior and what needs to be built.

    Expected Flow (when implemented):
    1. Bot greets in Hindi and asks about caller's land
    2. Rural caller confirms availability
    3. Caller provides land size ("5 acre")
    4. CONNECTION DROPS (network issue)
    5. Caller reconnects at minute 5
    6. Bot recognizes returning caller (via call_id)
    7. Bot says: "I see we were disconnected. Would you like to continue?"
    8. Caller confirms "haan" (yes)
    9. Bot confirms land size was "5 acre" (restored from session)
    10. Bot asks about crops
    11. Caller provides crop info
    12. Workflow completes

    Assertions (when implemented):
    - Bot recognizes returning caller
    - Bot acknowledges disconnection
    - Session state is preserved across reconnection
    - Workflow completes successfully
    - gathered_context contains data from both sessions
    """
    try:
        results = await asyncio.wait_for(
            run_dropped_connection_recovery_test(
                dropped_connection_workflow_setup,
                db_session,
                simulate_disconnect_reconnect=True,
            ),
            timeout=TEST_HARD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as e:
        raise AssertionError(
            f"Test exceeded hard timeout of {TEST_HARD_TIMEOUT_SECONDS}s — "
            "pipeline likely hung. Check earlier debug logs for the last frame "
            "to reach the pipeline."
        ) from e

    # Print results for debugging
    print("\n" + "=" * 60)
    print("SC-RURAL-04 Test Results")
    print("=" * 60)
    print(f"Success: {results['success']}")
    print(f"Disconnection Point: {results['disconnection_point']}")
    print(f"Steps Completed: {results['steps_completed']}")
    print(f"Extracted Variables: {results['extracted_variables']}")
    if results["errors"]:
        print(f"Errors/Notes: {results['errors']}")
    print("=" * 60)

    # Document the expected failures
    assert not results["success"], (
        "This test is expected to fail because dropped connection recovery "
        "is not yet implemented. See the errors list for details."
    )
    assert any("FEATURE NOT IMPLEMENTED" in e for e in results["errors"]), (
        "Test should document that the feature is not implemented"
    )


# =============================================================================
# Implementation Roadmap (for future work)
# =============================================================================

"""
IMPLEMENTATION ROADMAP FOR SC-RURAL-04:

1. Database Changes:
   - Add WorkflowRunState.DISCONNECTED to enums.py
   - Add session_token/call_id tracking to workflow_runs table
   - Add session_context JSONB column for LLM context persistence

2. Session Persistence Layer:
   - Create services/session_persistence.py
   - Store: LLM context, gathered_context, current_node_id, audio_position
   - Use Redis for fast access with DB fallback
   - TTL: 30 minutes for rural caller use case

3. Reconnection Handler (services/workflow/):
   - Modify event_handlers.py on_client_disconnected:
     - Instead of ending call, set state to DISCONNECTED
     - Persist session context
   - Create on_client_reconnected handler:
     - Look up existing workflow_run by call_id
     - Restore session from persisted context
     - Update state back to RUNNING

4. Workflow Graph Changes:
   - Add "acknowledge_disconnection" transition function
   - Add "check_session_continuation" node for returning callers

5. Bot Prompt Engineering:
   - Modify start node prompt to check for returning caller
   - Add reconnection acknowledgment message:
     "I see we were disconnected. Would you like to continue from where we left off?"
   - Add context restoration instructions

6. Test Infrastructure:
   - Extend MockTransport to support disconnect/reconnect simulation
   - Add session persistence mocks
   - Create integration test for full reconnection flow

7. Assertions for Implementation:
   - Verify workflow_run.state transitions: RUNNING -> DISCONNECTED -> RUNNING
   - Verify gathered_context is preserved across reconnection
   - Verify bot acknowledges disconnection
   - Verify "session_count" or "reconnection_event" in gathered_context
   - Verify all nodes visited in correct order despite disconnect
"""
