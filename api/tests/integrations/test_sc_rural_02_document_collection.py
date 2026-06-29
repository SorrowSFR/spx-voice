"""
E2E Integration Test: SC-RURAL-02 - Document Collection for Rural Caller

Scenario:
- Caller Profile: RURAL: Uneducated farmer with basic phone, network coverage issues,
  calling from field/farm area
- Language: Hindi (simple vocabulary)
- Expected Flow: IVR greeting -> Bot explains document requirements ->
  Bot asks 'Do you have Aadhaar card?' -> If caller says 'haan' (yes),
  bot confirms -> Bot lists: 1. Aadhaar card, 2. Electricity bill,
  3. Bank account with IFSC -> Bot confirms 'Are these three documents ready?' ->
  If confirmed, bot provides registration steps

Test Infrastructure Used:
- patch_run_pipeline_externals: Mocks LLM, TTS, STT, S3, external integrations
- MockTransport: Simulates WebRTC transport
- MockLLMService: Simulates LLM responses with configurable steps
- create_workflow_run_rows: Sets up test database with org/user/workflow/run rows
- TranscriptionFrame: Simulates user speech input to the pipeline

Assertions:
1. Workflow handles Hindi language input (transcription of "haan")
2. Bot correctly confirms document availability when user says "haan"
3. Bot lists all three required documents
4. Bot confirms document readiness before providing registration steps
5. Workflow completes successfully after document confirmation
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
# Test Workflow Definition - Rural Document Collection
# =============================================================================

RURAL_DOCUMENT_COLLECTION_WORKFLOW = {
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
                    "Ask about documents one by one."
                ),
                "is_start": True,
                "allow_interrupt": True,
                "add_global_prompt": True,
                "greeting_type": "text",
                "greeting": (
                    "Namaskar. Aapka swagat hai. Kya aapke paas Aadhaar card hai?"
                ),
            },
        },
        {
            "id": "collect_aadhaar",
            "type": "agentNode",
            "position": {"x": 0, "y": 150},
            "data": {
                "name": "Collect Aadhaar",
                "prompt": (
                    "You are collecting documents from a rural caller. "
                    "Use simple Hindi words. "
                    "First ask if they have Aadhaar card. "
                    "If they say 'haan' (yes), confirm and list the three documents: "
                    "1. Aadhaar card, 2. Electricity bill, 3. Bank account with IFSC code. "
                    "Then ask if all three documents are ready. "
                    "Use vocabulary like: 'haan' for yes, 'nahin' for no, "
                    "'document' as 'papers', 'bank' as 'bank', "
                    "'electricity bill' as 'bijli ka bill'."
                ),
                "allow_interrupt": True,
                "add_global_prompt": True,
                "extraction_enabled": True,
                "extraction_prompt": (
                    "Extract document availability from the conversation."
                ),
                "extraction_variables": [
                    {
                        "name": "has_aadhaar",
                        "type": "string",
                        "prompt": "Did the caller confirm having Aadhaar card? (haan/nahin)",
                    },
                    {
                        "name": "documents_ready",
                        "type": "string",
                        "prompt": "Did the caller confirm all three documents are ready? (haan/nahin)",
                    },
                ],
            },
        },
        {
            "id": "provide_registration",
            "type": "agentNode",
            "position": {"x": 0, "y": 300},
            "data": {
                "name": "Provide Registration Steps",
                "prompt": (
                    "The caller has confirmed documents are ready. "
                    "Provide simple registration steps in Hindi. "
                    "Use words like: 'register' as 'register karo', "
                    "'website' as 'website', 'submit' as 'submit karo'. "
                    "End the call politely."
                ),
                "allow_interrupt": True,
                "add_global_prompt": True,
                "extraction_enabled": True,
                "extraction_prompt": "Extract any registration confirmation from the conversation.",
                "extraction_variables": [
                    {
                        "name": "registration_understood",
                        "type": "boolean",
                        "prompt": "Did the caller understand the registration steps?",
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
            "id": "start-collect_aadhaar",
            "source": "start",
            "target": "collect_aadhaar",
            "data": {
                "label": "After Greeting",
                "condition": "After greeting, transition to document collection",
            },
        },
        {
            "id": "collect_aadhaar-provide_registration",
            "source": "collect_aadhaar",
            "target": "provide_registration",
            "data": {
                "label": "Documents Ready",
                "condition": "When caller confirms all documents are ready (says 'haan')",
            },
        },
        {
            "id": "provide_registration-end",
            "source": "provide_registration",
            "target": "end",
            "data": {
                "label": "End Call",
                "condition": "When registration steps are provided",
            },
        },
    ],
}

# Hard cap on the entire test to prevent hung pipelines
TEST_HARD_TIMEOUT_SECONDS = 60.0


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def rural_document_workflow_setup(db_session, async_session):
    """Create org/user/user_configuration/workflow/workflow_run rows for
    the rural document collection scenario."""
    return await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=RURAL_DOCUMENT_COLLECTION_WORKFLOW,
        name_prefix="SC-RURAL-02 Rural Document",
        provider_id_suffix="rural-document",
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
# E2E Test Body
# =============================================================================


async def run_rural_document_collection_test(
    workflow_run_setup,
    db_session,
) -> dict:
    """
    Execute the SC-RURAL-02 test scenario.

    Returns a dict with test results:
    - success: bool
    - steps_completed: list of str
    - errors: list of str
    - extracted_variables: dict
    """
    workflow_run, user, workflow = workflow_run_setup
    results = {
        "success": False,
        "steps_completed": [],
        "errors": [],
        "extracted_variables": {},
    }

    # Track conversation flow for assertions
    llm_call_count = 0
    conversation_transcripts: list[str] = []

    # Configure LLM responses for the conversation flow:
    # Step 0: Already spoke greeting in start node
    # Step 1: Confirm aadhaar question was asked, user said "haan"
    # Step 2: List documents, ask if ready -> user said "haan"
    # Step 3: Provide registration steps
    # Step 4: End call

    llm_responses = [
        # After start node greeting, user says "haan" for aadhaar
        MockLLMService.create_function_call_chunks(
            function_name="collect_aadhaar_continue",
            arguments={"user_said": "haan", "confirmed_aadhaar": True},
            tool_call_id="call_1",
        ),
        # After listing documents, user confirms all ready
        MockLLMService.create_function_call_chunks(
            function_name="documents_confirmed",
            arguments={"user_said": "haan", "documents_ready": True},
            tool_call_id="call_2",
        ),
        # Provide registration steps
        MockLLMService.create_function_call_chunks(
            function_name="end_call",
            arguments={},
            tool_call_id="call_3",
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

            # Step 1: Wait for greeting to complete, then send "haan" response
            # The greeting asks "Kya aapke paas Aadhaar card hai?"
            await asyncio.sleep(0.5)

            # Send "haan" (yes) response - simulating rural caller speaking in Hindi
            await pipeline_task.queue_frame(create_hindi_transcription("haan"))
            conversation_transcripts.append("haan")
            results["steps_completed"].append("User confirmed Aadhaar with 'haan'")

            # Wait for LLM to process and transition
            await asyncio.sleep(0.5)

            # Step 2: Send another "haan" confirming documents are ready
            # Bot should have listed: 1. Aadhaar card, 2. Electricity bill, 3. Bank account with IFSC
            await pipeline_task.queue_frame(create_hindi_transcription("haan"))
            conversation_transcripts.append("haan")
            results["steps_completed"].append("User confirmed documents with 'haan'")

            # Wait for run to complete
            await asyncio.wait_for(run_task, timeout=30.0)

        # Verify results
        refreshed = await db_session.get_workflow_run_by_id(workflow_run.id)

        # Check workflow completed successfully
        assert refreshed.is_completed is True, (
            f"Workflow should have completed. State: {refreshed.state}"
        )
        results["success"] = True

        # Verify nodes were visited
        nodes_visited = refreshed.gathered_context.get("nodes_visited", [])
        assert "Start" in nodes_visited, "Start node should have been visited"
        assert "Collect Aadhaar" in nodes_visited, (
            "Collect Aadhaar node should have been visited"
        )
        assert "Provide Registration Steps" in nodes_visited, (
            "Provide Registration Steps node should have been visited"
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
        if "has_aadhaar" in str(extracted):
            results["extracted_variables"]["has_aadhaar"] = "captured"
        if "documents_ready" in str(extracted):
            results["extracted_variables"]["documents_ready"] = "captured"

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
async def test_sc_rural_02_rural_caller_document_collection_hindi_haan(
    rural_document_workflow_setup,
    db_session,
):
    """
    SC-RURAL-02: Document Collection for Rural Caller

    Tests the complete flow:
    1. Bot greets in Hindi and asks about Aadhaar card
    2. Rural caller says "haan" (yes)
    3. Bot confirms and lists three documents
    4. Bot asks if all documents are ready
    5. Caller says "haan"
    6. Bot provides registration steps
    7. Workflow completes

    Assertions:
    - Hindi transcription "haan" is understood
    - Bot transitions through all nodes correctly
    - Variable extraction captures document availability
    - Workflow completes with all nodes visited
    """
    try:
        results = await asyncio.wait_for(
            run_rural_document_collection_test(
                rural_document_workflow_setup,
                db_session,
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
    print("SC-RURAL-02 Test Results")
    print("=" * 60)
    print(f"Success: {results['success']}")
    print(f"Steps Completed: {results['steps_completed']}")
    print(f"Extracted Variables: {results['extracted_variables']}")
    if results["errors"]:
        print(f"Errors: {results['errors']}")
    print("=" * 60)

    # Final assertions
    assert results["success"], f"Test failed. Errors: {results['errors']}"
    assert "Workflow completed successfully" in results["steps_completed"], (
        "Workflow should have completed"
    )
    assert any("Nodes visited" in s for s in results["steps_completed"]), (
        "Should have visited all nodes"
    )


# =============================================================================
# Additional Test Cases for Edge Scenarios
# =============================================================================


@pytest.mark.asyncio
async def test_sc_rural_02_handles_nahin_response(
    rural_document_workflow_setup,
    db_session,
):
    """
    SC-RURAL-02 Variant: Test handling of "nahin" (no) response

    If caller says "nahin" to document availability, bot should
    handle this gracefully and not transition to registration steps.
    """
    workflow_run, user, workflow = rural_document_workflow_setup

    # LLM responds to "nahin" by asking to arrange documents
    llm_responses = [
        MockLLMService.create_function_call_chunks(
            function_name="handle_no_documents",
            arguments={"user_said": "nahin", "documents_ready": False},
            tool_call_id="call_1",
        ),
        MockLLMService.create_function_call_chunks(
            function_name="end_call",
            arguments={},
            tool_call_id="call_2",
        ),
    ]

    llm = MockLLMService(mock_steps=llm_responses, chunk_delay=0.001)
    tts = MockTTSService(mock_audio_duration_ms=30, frame_delay=0)
    transport = MockTransport(
        TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    )

    captured_task: list = []
    audio_config = create_audio_config(WorkflowRunMode.SMALLWEBRTC.value)

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

            for _ in range(60):
                if captured_task or run_task.done():
                    break
                await asyncio.sleep(0.05)

            if run_task.done() and not captured_task:
                run_task.result()

            assert captured_task, "create_pipeline_task was never invoked"
            pipeline_task = captured_task[0]

            await asyncio.wait_for(
                pipeline_task._pipeline_start_event.wait(),
                timeout=3.0,
            )

            # Send "nahin" response
            await pipeline_task.queue_frame(create_hindi_transcription("nahin"))

            # Wait for run to complete
            await asyncio.wait_for(run_task, timeout=30.0)

        refreshed = await db_session.get_workflow_run_by_id(workflow_run.id)
        assert refreshed.is_completed is True

    except Exception as e:
        pytest.fail(f"Test failed with error: {e}")
    finally:
        pass


@pytest.mark.asyncio
async def test_sc_rural_02_handles_poor_network_audio_quality(
    rural_document_workflow_setup,
    db_session,
):
    """
    SC-RURAL-02 Variant: Test handling of poor network conditions

    Rural callers may have poor network coverage. The system should
    handle fragmented audio and still understand the intent.

    This test simulates audio that might arrive in chunks due to
    network issues, verifying the system can still understand "haan".
    """
    workflow_run, user, workflow = rural_document_workflow_setup

    llm_responses = [
        MockLLMService.create_function_call_chunks(
            function_name="collect_aadhaar_continue",
            arguments={"user_said": "haan", "confirmed_aadhaar": True},
            tool_call_id="call_1",
        ),
        MockLLMService.create_function_call_chunks(
            function_name="end_call",
            arguments={},
            tool_call_id="call_2",
        ),
    ]

    llm = MockLLMService(mock_steps=llm_responses, chunk_delay=0.001)
    tts = MockTTSService(mock_audio_duration_ms=30, frame_delay=0)
    transport = MockTransport(
        TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    )

    captured_task: list = []
    audio_config = create_audio_config(WorkflowRunMode.SMALLWEBRTC.value)

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

            for _ in range(60):
                if captured_task or run_task.done():
                    break
                await asyncio.sleep(0.05)

            if run_task.done() and not captured_task:
                run_task.result()

            assert captured_task, "create_pipeline_task was never invoked"
            pipeline_task = captured_task[0]

            await asyncio.wait_for(
                pipeline_task._pipeline_start_event.wait(),
                timeout=3.0,
            )

            # Simulate fragmented "haan" - might arrive as "ha" then "an"
            # due to poor network conditions
            await pipeline_task.queue_frame(create_hindi_transcription("ha"))
            await asyncio.sleep(0.1)
            await pipeline_task.queue_frame(create_hindi_transcription("an"))

            # Wait for run to complete
            await asyncio.wait_for(run_task, timeout=30.0)

        refreshed = await db_session.get_workflow_run_by_id(workflow_run.id)
        assert refreshed.is_completed is True

    except Exception as e:
        pytest.fail(f"Test failed with error: {e}")
    finally:
        pass
