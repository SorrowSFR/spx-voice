import hashlib
import json
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.services.configuration.registry import ServiceProviders
from api.services.livekit import worker
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.workflow_graph import WorkflowGraph


def _workflow_with_kb() -> WorkflowGraph:
    return WorkflowGraph(
        ReactFlowDTO.model_validate(
            {
                "nodes": [
                    {
                        "id": "start",
                        "type": "startCall",
                        "position": {"x": 0, "y": 0},
                        "data": {
                            "name": "Start",
                            "prompt": "You are helping {{customer_name}}.",
                            "is_start": True,
                            "allow_interrupt": True,
                            "add_global_prompt": False,
                            "document_uuids": ["doc-123"],
                        },
                    },
                    {
                        "id": "end",
                        "type": "endCall",
                        "position": {"x": 0, "y": 200},
                        "data": {
                            "name": "End",
                            "prompt": "Say goodbye.",
                            "is_end": True,
                            "add_global_prompt": False,
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "start-end",
                        "source": "start",
                        "target": "end",
                        "data": {"label": "End", "condition": "End the call"},
                    }
                ],
            }
        )
    )


def _workflow_without_greeting() -> WorkflowGraph:
    return WorkflowGraph(
        ReactFlowDTO.model_validate(
            {
                "nodes": [
                    {
                        "id": "start",
                        "type": "startCall",
                        "position": {"x": 0, "y": 0},
                        "data": {
                            "name": "Start",
                            "prompt": "You are a concise assistant.",
                            "is_start": True,
                            "allow_interrupt": True,
                            "add_global_prompt": False,
                        },
                    }
                ],
                "edges": [],
            }
        )
    )


def _workflow_with_start_auto_advance() -> WorkflowGraph:
    return WorkflowGraph(
        ReactFlowDTO.model_validate(
            {
                "nodes": [
                    {
                        "id": "start",
                        "type": "startCall",
                        "position": {"x": 0, "y": 0},
                        "data": {
                            "name": "Start",
                            "prompt": "Say exactly in Telugu: Hello.",
                            "is_start": True,
                            "allow_interrupt": True,
                            "add_global_prompt": False,
                        },
                    },
                    {
                        "id": "main",
                        "type": "agentNode",
                        "position": {"x": 0, "y": 200},
                        "data": {
                            "name": "Main",
                            "prompt": "Main scheme facts are available here.",
                            "allow_interrupt": True,
                            "add_global_prompt": False,
                        },
                    },
                    {
                        "id": "end",
                        "type": "endCall",
                        "position": {"x": 0, "y": 400},
                        "data": {
                            "name": "End",
                            "prompt": "Say goodbye.",
                            "is_end": True,
                            "add_global_prompt": False,
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "start-main",
                        "source": "start",
                        "target": "main",
                        "data": {
                            "label": "Move to Main",
                            "condition": "Use after the greeting.",
                        },
                    },
                    {
                        "id": "start-end",
                        "source": "start",
                        "target": "end",
                        "data": {"label": "End", "condition": "End the call"},
                    },
                    {
                        "id": "main-end",
                        "source": "main",
                        "target": "end",
                        "data": {"label": "End", "condition": "End the call"},
                    },
                ],
            }
        )
    )


def _job_context():
    return SimpleNamespace(
        room=SimpleNamespace(
            local_participant=SimpleNamespace(publish_data=Mock()),
        ),
        shutdown=Mock(),
    )


def test_livekit_prompt_includes_kb_grounding_instruction():
    graph = _workflow_with_kb()
    start_node = graph.nodes[graph.start_node_id]

    prompt = worker._compose_system_prompt(
        node=start_node,
        workflow=graph,
        call_context_vars={"customer_name": "Asha"},
    )

    assert "You are helping Asha." in prompt
    assert "KNOWLEDGE BASE GROUNDING" in prompt
    assert "Do not invent facts" in prompt


@pytest.mark.asyncio
async def test_livekit_kb_tool_uses_node_documents_and_embedding_settings(monkeypatch):
    graph = _workflow_with_kb()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={"customer_name": "Asha"},
        embeddings_api_key="embed-key",
        embeddings_provider=ServiceProviders.GOOGLE.value,
        embeddings_model="gemini-embedding-001",
        embeddings_base_url=None,
    )
    start_node = graph.nodes[graph.start_node_id]
    tools = agent._tools_for_node(start_node)
    kb_tool = next(tool for tool in tools if tool.id == "retrieve_from_knowledge_base")

    calls = {}

    async def fake_retrieve(**kwargs):
        calls.update(kwargs)
        return {
            "chunks": [{"text": "grounded"}],
            "query": kwargs["query"],
            "total_results": 1,
        }

    monkeypatch.setattr(worker, "retrieve_from_knowledge_base", fake_retrieve)

    result = await kb_tool({"query": "What does the uploaded document say?"})

    assert result["chunks"][0]["text"] == "grounded"
    assert calls == {
        "query": "What does the uploaded document say?",
        "organization_id": 9,
        "document_uuids": ["doc-123"],
        "limit": 3,
        "embeddings_api_key": "embed-key",
        "embeddings_provider": ServiceProviders.GOOGLE.value,
        "embeddings_model": "gemini-embedding-001",
        "embeddings_base_url": None,
    }


@pytest.mark.asyncio
async def test_livekit_record_lead_details_tool_persists_sheet_fields(monkeypatch):
    monkeypatch.setenv(
        worker.post_call.POST_CALL_WEBHOOK_ENV,
        "https://example.test/post-call",
    )
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )
    persisted = []

    async def fake_update_workflow_run(run_id, **kwargs):
        persisted.append((run_id, kwargs))

    monkeypatch.setattr(
        worker.db_client,
        "update_workflow_run",
        fake_update_workflow_run,
    )

    start_node = graph.nodes[graph.start_node_id]
    tools = agent._tools_for_node(start_node)
    lead_tool = next(tool for tool in tools if tool.id == "record_lead_details")

    result = await lead_tool(
        {
            "district": "Rangareddy",
            "town": "Ibrahimpatnam",
            "looking_for": "15 kW subsidy for shop",
            "customer_name": "Ravi",
            "remarks": "Caller asked about shop subsidy.",
        }
    )

    assert result["status"] == "saved"
    assert result["missing_fields"] == []
    assert result["lead_details"] == {
        "district": "Rangareddy",
        "town": "Ibrahimpatnam",
        "looking_for": "15 kW subsidy for shop",
        "customer_name": "Ravi",
        "remarks": "Caller asked about shop subsidy.",
    }
    assert persisted == [
        (
            17,
            {
                "gathered_context": {
                    "lead_details": result["lead_details"],
                    "district": "Rangareddy",
                    "town": "Ibrahimpatnam",
                    "looking_for": "15 kW subsidy for shop",
                    "customer_name": "Ravi",
                    "remarks": "Caller asked about shop subsidy.",
                    "looking for": "15 kW subsidy for shop",
                }
            },
        )
    ]


def test_livekit_missing_lead_fields_treats_placeholder_as_missing():
    lead = {
        "district": "Rangareddy",
        "town": "Ibrahimpatnam",
        "looking_for": "subsidy",
        "customer_name": "not provided",
        "remarks": "Caller asked about subsidy.",
    }

    assert "customer_name" in worker.post_call.missing_lead_fields(lead)

    lead["remarks"] = "Customer name not provided after caller refused."

    assert "customer_name" not in worker.post_call.missing_lead_fields(lead)


@pytest.mark.asyncio
async def test_livekit_record_lead_details_rejects_values_not_said_by_caller(
    monkeypatch,
):
    monkeypatch.setenv(
        worker.post_call.POST_CALL_WEBHOOK_ENV,
        "https://example.test/post-call",
    )
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )
    agent._remember_final_user_transcript("My name is Ravi. I need subsidy details.")
    persisted = []

    async def fake_update_workflow_run(run_id, **kwargs):
        persisted.append((run_id, kwargs))

    monkeypatch.setattr(
        worker.db_client,
        "update_workflow_run",
        fake_update_workflow_run,
    )

    start_node = graph.nodes[graph.start_node_id]
    lead_tool = next(
        tool
        for tool in agent._tools_for_node(start_node)
        if tool.id == "record_lead_details"
    )

    result = await lead_tool(
        {
            "district": "Hyderabad",
            "town": "Khairatabad",
            "looking_for": "subsidy details",
            "customer_name": "Raju",
        }
    )

    assert result["rejected_fields"] == {
        "district": "Hyderabad",
        "town": "Khairatabad",
        "customer_name": "Raju",
    }
    assert result["lead_details"]["looking_for"] == "subsidy details"
    assert result["lead_details"]["district"] == ""
    assert result["lead_details"]["town"] == ""
    assert result["lead_details"]["customer_name"] == ""
    assert result["lead_details"]["remarks"] == "Caller asked about subsidy details."
    assert result["missing_fields"] == [
        "district",
        "town",
        "customer_name",
    ]
    assert result["next_missing_field"] == "district"
    assert result["next_followup_hint"] == worker.LEAD_FIELD_FOLLOWUP_HINTS["district"]
    assert "Continue naturally" in result["instruction"]
    assert persisted


@pytest.mark.asyncio
async def test_livekit_record_lead_details_returns_natural_next_field_hint(
    monkeypatch,
):
    monkeypatch.setenv(
        worker.post_call.POST_CALL_WEBHOOK_ENV,
        "https://example.test/post-call",
    )
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
        uses_realtime=True,
        realtime_generate_reply_supported=False,
        realtime_exact_speech_uses_tts=True,
        tts_api_key="google-api-key",
    )
    agent._remember_final_user_transcript("My name is Ravi.")
    persisted = []

    async def fake_update_workflow_run(run_id, **kwargs):
        persisted.append((run_id, kwargs))

    monkeypatch.setattr(
        worker.db_client,
        "update_workflow_run",
        fake_update_workflow_run,
    )

    start_node = graph.nodes[graph.start_node_id]
    lead_tool = next(
        tool
        for tool in agent._tools_for_node(start_node)
        if tool.id == "record_lead_details"
    )

    result = await lead_tool({"customer_name": "Ravi"})

    assert result["lead_details"]["customer_name"] == "Ravi"
    assert result["next_missing_field"] == "district"
    assert result["next_followup_hint"] == worker.LEAD_FIELD_FOLLOWUP_HINTS["district"]
    assert "Continue naturally" in result["instruction"]
    assert "latest question" in result["instruction"]
    assert persisted


@pytest.mark.asyncio
async def test_livekit_end_transition_blocks_until_required_lead_fields(monkeypatch):
    monkeypatch.setenv(
        worker.post_call.POST_CALL_WEBHOOK_ENV,
        "https://example.test/post-call",
    )
    graph = _workflow_with_start_auto_advance()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )
    main_node = graph.nodes["main"]
    end_tool = next(
        tool for tool in agent._tools_for_node(main_node) if tool.id == "end"
    )

    result = await end_tool({})

    assert result["status"] == "blocked"
    assert result["missing_fields"] == [
        "district",
        "town",
        "looking_for",
        "customer_name",
        "remarks",
    ]
    assert "Do not end the call yet" in result["instruction"]


@pytest.mark.asyncio
async def test_livekit_end_transition_blocks_without_clear_caller_close(
    monkeypatch,
):
    monkeypatch.setenv(
        worker.post_call.POST_CALL_WEBHOOK_ENV,
        "https://example.test/post-call",
    )
    graph = _workflow_with_start_auto_advance()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )
    agent._lead_details = {
        "district": "Rangareddy",
        "town": "Ibrahimpatnam",
        "looking_for": "subsidy",
        "customer_name": "Ravi",
        "remarks": "Caller asked about subsidy.",
    }
    agent._remember_final_user_transcript("\u0c2e\u0c30\u0c3f\u0c2f\u0c41")
    main_node = graph.nodes["main"]
    end_tool = next(
        tool for tool in agent._tools_for_node(main_node) if tool.id == "end"
    )

    result = await end_tool({})

    assert result["status"] == "blocked"
    assert result["missing_fields"] == []
    assert "has not clearly asked to close" in result["instruction"]
    assert agent._current_node is None


def test_livekit_text_google_session_uses_low_latency_model_settings(monkeypatch):
    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured["llm"] = kwargs

    class FakeSTT:
        def __init__(self, **kwargs):
            captured["stt"] = kwargs

    class FakeTTS:
        def __init__(self, **kwargs):
            captured["tts"] = kwargs

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session"] = kwargs

    monkeypatch.setattr(worker.google, "LLM", FakeLLM)
    monkeypatch.setattr(worker.google, "STT", FakeSTT)
    monkeypatch.setattr(worker.google, "TTS", FakeTTS)
    monkeypatch.setattr(worker, "AgentSession", FakeSession)

    user_config = SimpleNamespace(
        is_realtime=False,
        realtime=None,
        llm=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            model="gemini-2.5-flash",
            api_key="llm-key",
        ),
        stt=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            model="chirp",
            language="en-US",
        ),
        tts=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            model="gemini-2.5-flash-tts",
            voice="Kore",
            language="en-US",
        ),
    )

    worker._create_session(user_config, vad="vad")

    assert captured["llm"]["temperature"] == 0.1
    assert captured["llm"]["thinking_config"] == {"thinking_budget": 0}
    assert captured["session"]["turn_handling"]["endpointing"]["max_delay"] == 0.75
    assert (
        captured["session"]["turn_handling"]["preemptive_generation"]["enabled"] is True
    )


def test_livekit_google_realtime_session_uses_local_vad_turn_detection(monkeypatch):
    captured = {}

    class FakeRealtimeModel:
        def __init__(self, **kwargs):
            captured["llm"] = kwargs

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session"] = kwargs

    monkeypatch.setattr(
        worker.google.beta.realtime,
        "RealtimeModel",
        FakeRealtimeModel,
    )
    monkeypatch.setattr(worker, "AgentSession", FakeSession)

    user_config = SimpleNamespace(
        is_realtime=True,
        realtime=SimpleNamespace(
            provider=ServiceProviders.GOOGLE_REALTIME.value,
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Kore",
            language="te-IN",
            api_key="realtime-key",
        ),
        llm=None,
        stt=None,
        tts=None,
    )

    worker._create_session(user_config, vad="vad")

    realtime_input_config = captured["llm"]["realtime_input_config"]
    assert realtime_input_config.automatic_activity_detection.disabled is True
    assert captured["session"]["turn_handling"]["turn_detection"] == "vad"


def test_livekit_google_31_realtime_uses_server_turn_detection(monkeypatch):
    captured = {}

    class FakeRealtimeModel:
        def __init__(self, **kwargs):
            captured["llm"] = kwargs

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session"] = kwargs

    monkeypatch.setattr(
        worker.google.beta.realtime,
        "RealtimeModel",
        FakeRealtimeModel,
    )
    monkeypatch.setattr(worker, "AgentSession", FakeSession)

    user_config = SimpleNamespace(
        is_realtime=True,
        realtime=SimpleNamespace(
            provider=ServiceProviders.GOOGLE_REALTIME.value,
            model="gemini-3.1-flash-live-preview",
            voice="Kore",
            language="te-IN",
            api_key="realtime-key",
        ),
        llm=None,
        stt=None,
        tts=None,
    )

    worker._create_session(user_config, vad="vad")

    realtime_input_config = captured["llm"]["realtime_input_config"]
    automatic_detection = realtime_input_config.automatic_activity_detection
    assert automatic_detection.disabled is False
    assert (
        automatic_detection.start_of_speech_sensitivity
        == worker.genai_types.StartSensitivity.START_SENSITIVITY_LOW
    )
    assert (
        automatic_detection.end_of_speech_sensitivity
        == worker.genai_types.EndSensitivity.END_SENSITIVITY_LOW
    )
    assert (
        automatic_detection.silence_duration_ms
        == worker.GEMINI_SERVER_VAD_SILENCE_DURATION_MS
    )
    assert (
        realtime_input_config.activity_handling
        == worker.genai_types.ActivityHandling.NO_INTERRUPTION
    )
    assert captured["session"]["turn_handling"]["turn_detection"] == "realtime_llm"
    assert captured["session"]["turn_handling"]["interruption"]["enabled"] is True
    assert (
        captured["session"]["turn_handling"]["interruption"]["min_words"]
        == worker.FAST_INTERRUPTION_MIN_WORDS
    )
    assert (
        captured["session"]["turn_handling"]["preemptive_generation"]["enabled"]
        is False
    )


def test_livekit_fast_profile_uses_noise_resistant_google_31_turn_taking(monkeypatch):
    captured = {}

    class FakeRealtimeModel:
        def __init__(self, **kwargs):
            captured["llm"] = kwargs

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session"] = kwargs

    monkeypatch.setattr(
        worker.google.beta.realtime,
        "RealtimeModel",
        FakeRealtimeModel,
    )
    monkeypatch.setattr(worker, "AgentSession", FakeSession)

    user_config = SimpleNamespace(
        is_realtime=True,
        realtime=SimpleNamespace(
            provider=ServiceProviders.GOOGLE_REALTIME.value,
            model="gemini-3.1-flash-live-preview",
            voice="Kore",
            language="te-IN",
            api_key="realtime-key",
        ),
        llm=None,
        stt=None,
        tts=None,
    )

    worker._create_session(
        user_config,
        vad="vad",
        latency_profile="realtime_telugu_primary_fast_close_v4",
    )

    realtime_input_config = captured["llm"]["realtime_input_config"]
    automatic_detection = realtime_input_config.automatic_activity_detection
    assert (
        automatic_detection.prefix_padding_ms
        == worker.FAST_GEMINI_SERVER_VAD_PREFIX_PADDING_MS
    )
    assert (
        automatic_detection.silence_duration_ms
        == worker.FAST_GEMINI_SERVER_VAD_SILENCE_DURATION_MS
    )
    assert captured["llm"]["temperature"] == 0.0
    assert "max_output_tokens" not in captured["llm"]
    assert (
        captured["llm"]["tool_response_scheduling"]
        == worker.genai_types.FunctionResponseScheduling.INTERRUPT
    )
    turn_handling = captured["session"]["turn_handling"]
    assert turn_handling["endpointing"]["max_delay"] == (
        worker.FAST_ENDPOINTING_MAX_DELAY_SECONDS
    )
    assert turn_handling["interruption"]["enabled"] is True
    assert (
        turn_handling["interruption"]["min_words"] == worker.FAST_INTERRUPTION_MIN_WORDS
    )
    assert turn_handling["preemptive_generation"]["enabled"] is False


def test_livekit_google_31_realtime_say_text_allows_interruptions(monkeypatch):
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
        uses_realtime=True,
        realtime_generate_reply_supported=False,
    )
    captured = {}

    class FakeSession:
        def say(self, text, **kwargs):
            captured["text"] = text
            captured["kwargs"] = kwargs
            return "speech"

    monkeypatch.setattr(
        worker.LiveKitWorkflowAgent,
        "session",
        property(lambda self: FakeSession()),
    )

    result = agent._say_text("hello", allow_interruptions=False)

    assert result == "speech"
    assert captured == {
        "text": "hello",
        "kwargs": {
            "allow_interruptions": True,
            "add_to_chat_ctx": True,
        },
    }


@pytest.mark.asyncio
async def test_livekit_feedback_persistence_is_buffered(monkeypatch):
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )
    persisted = []

    async def fake_get_workflow_run_by_id(run_id):
        return SimpleNamespace(logs={"realtime_feedback_events": [{"type": "old"}]})

    async def fake_update_workflow_run(run_id, **kwargs):
        persisted.append((run_id, kwargs))

    monkeypatch.setattr(
        worker.db_client,
        "get_workflow_run_by_id",
        fake_get_workflow_run_by_id,
    )
    monkeypatch.setattr(
        worker.db_client,
        "update_workflow_run",
        fake_update_workflow_run,
    )

    message = {"type": "rtf-bot-text", "payload": {"text": "hi"}}
    await agent._emit_feedback_now(message)

    assert persisted == []
    await agent.flush_feedback()

    assert persisted == [
        (
            17,
            {
                "logs": {
                    "realtime_feedback_events": [
                        {"type": "old"},
                        message,
                    ]
                }
            },
        )
    ]
    if agent._feedback_flush_task:
        agent._feedback_flush_task.cancel()


def test_livekit_message_metrics_payload_keeps_latency_numbers():
    item = SimpleNamespace(
        metrics={
            "e2e_latency": 0.12345,
            "llm_node_ttft": 0.02,
            "ignored": 7,
            "playback_latency": "slow",
        }
    )

    assert worker._message_metrics_payload(item) == {
        "e2e_latency": 0.1235,
        "llm_node_ttft": 0.02,
    }


def test_livekit_usage_info_serializes_session_usage():
    session_usage = SimpleNamespace(
        model_usage=[
            SimpleNamespace(
                type="llm_usage",
                provider="google_realtime",
                model="gemini-3.1-flash-live-preview",
                input_tokens=1200,
                input_cached_tokens=100,
                input_audio_tokens=900,
                input_cached_audio_tokens=80,
                input_text_tokens=300,
                input_cached_text_tokens=20,
                output_tokens=240,
                output_audio_tokens=200,
                output_text_tokens=40,
                session_duration=31.5,
            ),
            SimpleNamespace(
                type="tts_usage",
                provider="google",
                model="gemini-2.5-flash-preview-tts",
                characters_count=42,
            ),
            SimpleNamespace(
                type="stt_usage",
                provider="google",
                model="chirp",
                audio_duration=12.5,
            ),
        ]
    )

    usage_info = worker._livekit_usage_info(session_usage, 33)

    llm_usage = next(iter(usage_info["llm"].values()))
    assert usage_info["call_duration_seconds"] == 33
    assert llm_usage["prompt_tokens"] == 1200
    assert llm_usage["completion_tokens"] == 240
    assert llm_usage["cache_read_input_tokens"] == 100
    assert llm_usage["input_audio_tokens"] == 900
    assert llm_usage["output_audio_tokens"] == 200
    assert next(iter(usage_info["tts"].values())) == 42
    assert next(iter(usage_info["stt"].values())) == 12.5


@pytest.mark.asyncio
async def test_livekit_finalize_persists_usage_and_calculates_cost(monkeypatch):
    monkeypatch.setattr(worker.time, "monotonic", lambda: 112.9)
    updates = []
    events = []
    completed_run = SimpleNamespace(
        created_at=datetime(2026, 5, 31, 7, 0, tzinfo=timezone.utc),
        initial_context={"caller_number": "+9170", "called_number": "+9180"},
        gathered_context={},
        cost_info={"call_duration_seconds": 12},
        recording_url=None,
        logs={},
    )
    refreshed_run = SimpleNamespace(
        created_at=completed_run.created_at,
        initial_context=completed_run.initial_context,
        gathered_context={},
        cost_info={"call_duration_seconds": 12, "total_cost_usd": 0.01},
        recording_url=None,
        logs={},
    )

    async def fake_update_workflow_run(run_id, **kwargs):
        updates.append((run_id, kwargs))
        return completed_run

    monkeypatch.setattr(
        worker.db_client,
        "get_workflow_run_by_id",
        AsyncMock(
            side_effect=[
                SimpleNamespace(cost_info={"call_id": "SCL_1"}),
                refreshed_run,
            ]
        ),
    )
    monkeypatch.setattr(
        worker.db_client, "update_workflow_run", fake_update_workflow_run
    )
    monkeypatch.setattr(
        worker.post_call,
        "build_post_call_payload",
        lambda *args, **kwargs: {
            "district": "Rangareddy",
            "town": "Badangpet",
            "looking_for": "subsidy",
            "customer_name": "Sai",
            "remarks": "Collected fields.",
        },
    )
    monkeypatch.setattr(
        worker.post_call,
        "lead_details_gathered_context",
        lambda fields: {"lead_details": fields},
    )

    async def fake_send_post_call_webhook(payload):
        events.append("webhook")
        return {"sent": True}

    monkeypatch.setattr(
        worker.post_call,
        "send_post_call_webhook",
        fake_send_post_call_webhook,
    )

    async def fake_calculate_cost(run_id):
        events.append("cost")

    calculate_cost = AsyncMock(side_effect=fake_calculate_cost)
    monkeypatch.setattr(worker, "_calculate_livekit_workflow_run_cost", calculate_cost)
    session = SimpleNamespace(
        history=SimpleNamespace(to_dict=lambda: {"items": []}),
        usage=SimpleNamespace(model_usage=[]),
    )
    agent = SimpleNamespace(
        _started_at=100,
        stop_recording=AsyncMock(return_value=None),
    )

    await worker._finalize_livekit_workflow_run(
        workflow_run_id=41,
        session=session,
        agent=agent,
        room_name="spx-voice-wf-1",
        reason="test complete",
    )

    first_update = updates[0][1]
    assert first_update["usage_info"]["call_duration_seconds"] == 12
    assert first_update["cost_info"] == {
        "call_id": "SCL_1",
        "call_duration_seconds": 12,
    }
    assert updates[1][1]["logs"]["post_call_webhook"] == {"sent": True}
    calculate_cost.assert_awaited_once_with(41)
    assert events == ["cost", "webhook"]


def test_livekit_post_call_payload_uses_metadata_and_transcript_fallback():
    run = SimpleNamespace(
        created_at=datetime(2026, 5, 31, 6, 49, 56, tzinfo=timezone.utc),
        initial_context={
            "caller_number": "+15555550123",
            "called_number": "+15555550124",
            "participant_attributes": {
                "sip.phoneNumber": "+15555550123",
                "sip.trunkPhoneNumber": "+15555550124",
            },
        },
        gathered_context={},
        cost_info={"call_duration_seconds": 129},
        recording_url=None,
        logs={},
    )
    logs = {
        "realtime_feedback_events": [
            {
                "type": "rtf-user-transcription",
                "payload": {
                    "text": (
                        "\u0c2e\u0c3e\u0c26\u0c3f "
                        "\u0c30\u0c02\u0c17\u0c3e "
                        "\u0c30\u0c46\u0c21\u0c4d\u0c21\u0c3f. "
                        "15 kW subsidy and shop registration cost"
                    ),
                    "final": True,
                },
            }
        ]
    }

    payload = worker.post_call.build_post_call_payload(
        run,
        logs=logs,
        recording_url="https://bucket.example/recordings/57.mp3",
    )

    assert payload["customer_number"] == "+15555550123"
    assert payload["rep_number"] == "+15555550124"
    assert payload["called_at"] == "2026-05-31T06:49:56+00:00"
    assert payload["duration"] == 129
    assert payload["district"] == "Rangareddy"
    assert "15 kW" in payload["looking_for"]
    assert "shop" in payload["looking_for"]
    assert payload["looking for"] == payload["looking_for"]
    assert payload["recording_url"] == "https://bucket.example/recordings/57.mp3"
    assert payload["remarks"].startswith("Caller asked about")


def test_livekit_post_call_payload_does_not_keep_placeholder_or_unsupported_leads():
    run = SimpleNamespace(
        created_at=datetime(2026, 5, 31, 6, 49, 56, tzinfo=timezone.utc),
        initial_context={"caller_number": "+9170", "called_number": "+9180"},
        gathered_context={
            "lead_details": {
                "district": "Hyderabad",
                "town": "not provided",
                "looking_for": "",
                "customer_name": "Raju",
                "remarks": "Caller asked about subsidy.",
            }
        },
        cost_info={"call_duration_seconds": 90},
        recording_url=None,
        logs={},
    )
    logs = {
        "realtime_feedback_events": [
            {
                "type": "rtf-user-transcription",
                "payload": {
                    "text": "My name is Ravi. town is Badangpet. 15 kW subsidy",
                    "final": True,
                },
            }
        ]
    }

    payload = worker.post_call.build_post_call_payload(run, logs=logs)

    assert payload["district"] == ""
    assert payload["town"].startswith("Badangpet")
    assert payload["customer_name"] == "Ravi"
    assert "15 kW" in payload["looking_for"]


def test_livekit_recording_key_uses_default_inbound_prefix():
    key = worker.post_call._recording_key(
        "spx-voice-wf-1-_+15555550123",
        58,
        worker.post_call.DEFAULT_RECORDING_PREFIX,
    )

    assert key.startswith("SPX-VOICE-INBOUND/58-spx-voice-wf-1-_")
    assert key.endswith(".mp3")


def test_livekit_recording_url_resolves_to_default_object_key(monkeypatch):
    monkeypatch.setenv(
        worker.post_call.RECORDINGS_PUBLIC_BASE_ENV,
        "https://recordings.example.test",
    )
    key = (
        "SPX-VOICE-INBOUND/"
        "60-spx-voice-wf-1-__15555550123_fJ5iwepdr987-20260531T075345Z.mp3"
    )
    url = f"https://recordings.example.test/{key}"

    assert worker.post_call.recording_object_key_from_url_or_key(url) == key
    assert worker.post_call.workflow_run_id_from_recording_key(key) == 60


def test_livekit_post_call_payload_omits_failed_recording_url():
    run = SimpleNamespace(
        created_at=datetime(2026, 5, 31, 6, 49, 56, tzinfo=timezone.utc),
        initial_context={"caller_number": "+9170", "called_number": "+9180"},
        gathered_context={
            "lead_details": {
                "district": "Rangareddy",
                "town": "Badangpet",
                "looking_for": "subsidy",
                "customer_name": "Sai Kiran",
                "remarks": "Recording failed.",
            },
            "livekit_recording": {
                "status": "failed",
                "recording_url": "https://bucket.example/SPX-VOICE-INBOUND/failed.mp3",
            },
        },
        cost_info={"call_duration_seconds": 10},
        recording_url=None,
        logs={},
    )

    payload = worker.post_call.build_post_call_payload(run)

    assert payload["recording_url"] == ""


def test_livekit_professional_persona_does_not_prompt_casual_phrases():
    casual_examples = [
        "Namaskaram ji",
        "ji",
        "all the best",
        "good luck",
        "babu",
        "amma",
        "dear",
        "friendly woman",
        "warm, sweet",
        "sweet",
        "Ey ante",
        "Bagunte",
        "Aage ela",
        "Lede...",
    ]
    prompt = worker.FAST_RESPONSE_INSTRUCTIONS

    assert "professional SPX Voice assistant" in prompt
    assert "honorific-heavy phrasing" in prompt
    assert "casual blessings or sign-offs" in prompt
    assert "Do not ask for OTPs" in prompt
    assert "Do not promise messages" in prompt
    assert "Required tracking fields" in prompt
    assert "record_lead_details" in prompt
    for term in casual_examples:
        assert term.lower() not in prompt.lower()


@pytest.mark.asyncio
async def test_livekit_google_31_realtime_opening_uses_tts_fallback(monkeypatch):
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
        uses_realtime=True,
        realtime_generate_reply_supported=False,
        realtime_tool_choice_supported=False,
        realtime_exact_speech_uses_tts=True,
        tts_api_key="google-api-key",
    )
    spoken = {}

    async def fake_speak_text(text, *, allow_interruptions=True):
        spoken["text"] = text
        spoken["allow_interruptions"] = allow_interruptions
        return None

    async def fake_record_node_transition(node, previous_node):
        spoken["node"] = node.id
        spoken["previous_node"] = previous_node

    monkeypatch.setattr(agent, "_speak_text", fake_speak_text)
    monkeypatch.setattr(agent, "_record_node_transition", fake_record_node_transition)

    await agent.start_opening()

    assert spoken["text"] == worker.DEFAULT_OPENING
    assert "ji" not in spoken["text"].lower()
    assert spoken["text"].startswith("Hello, this is your SPX Voice assistant.")
    assert spoken["allow_interruptions"] is False
    assert spoken["node"] == "start"


@pytest.mark.asyncio
async def test_livekit_google_31_realtime_end_node_uses_exact_tts_fallback(
    monkeypatch,
):
    graph = _workflow_with_start_auto_advance()
    ctx = _job_context()
    agent = worker.LiveKitWorkflowAgent(
        ctx=ctx,
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
        uses_realtime=True,
        realtime_generate_reply_supported=False,
        realtime_tool_choice_supported=False,
        realtime_exact_speech_uses_tts=True,
        tts_api_key="google-api-key",
    )
    spoken = {}
    emitted = []

    class FakeSession:
        def generate_reply(self, **kwargs):
            raise AssertionError("Gemini 3.1 realtime must not call generate_reply")

    class FakeSpeech:
        async def wait_for_playout(self):
            spoken["waited"] = True

    async def fake_speak_text(text, *, allow_interruptions=True):
        spoken["text"] = text
        spoken["allow_interruptions"] = allow_interruptions
        return FakeSpeech()

    async def fake_emit_feedback_now(message):
        emitted.append(message)

    async def fake_shutdown_call(reason):
        spoken["shutdown_reason"] = reason

    monkeypatch.setattr(
        worker.LiveKitWorkflowAgent,
        "session",
        property(lambda self: FakeSession()),
    )
    monkeypatch.setattr(agent, "_speak_text", fake_speak_text)
    monkeypatch.setattr(agent, "_emit_feedback_now", fake_emit_feedback_now)
    monkeypatch.setattr(agent, "_shutdown_call", fake_shutdown_call)

    await agent._complete_end_node(graph.nodes["end"])

    assert spoken["text"] == worker.DEFAULT_END_CALL_TEXT
    assert spoken["allow_interruptions"] is graph.nodes["end"].allow_interrupt
    assert spoken["waited"] is True
    assert emitted[-1]["type"] == "rtf-run-completed"
    assert spoken["shutdown_reason"] == "workflow reached end node"


@pytest.mark.asyncio
async def test_livekit_realtime_opening_prefers_cached_live_audio(monkeypatch):
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
        uses_realtime=True,
        realtime_generate_reply_supported=False,
        realtime_exact_speech_uses_tts=True,
        tts_api_key="google-api-key",
        opening_model="gemini-3.1-flash-live-preview",
        tts_voice="Kore",
        tts_language="te-IN",
    )
    captured = {}
    live_opening_call = {}

    async def fake_live_opening_audio_path(**kwargs):
        live_opening_call.update(kwargs)
        return Path("opening.wav")

    monkeypatch.setattr(
        worker,
        "_live_opening_audio_path",
        fake_live_opening_audio_path,
    )
    monkeypatch.setattr(worker, "_wav_audio_frames", lambda path: "audio-frames")

    def fake_say_text(text, *, allow_interruptions=True, audio=None):
        captured["text"] = text
        captured["allow_interruptions"] = allow_interruptions
        captured["audio"] = audio
        return "speech"

    monkeypatch.setattr(agent, "_say_text", fake_say_text)

    greeting = "\u0c28\u0c2e\u0c38\u0c4d\u0c15\u0c3e\u0c30\u0c02"
    result = await agent._speak_opening(greeting, allow_interruptions=False)

    assert result == "speech"
    assert live_opening_call == {
        "api_key": "google-api-key",
        "model": "gemini-3.1-flash-live-preview",
        "voice": "Kore",
        "language": "te-IN",
        "text": greeting,
    }
    assert captured == {
        "text": greeting,
        "allow_interruptions": False,
        "audio": "audio-frames",
    }


@pytest.mark.asyncio
async def test_livekit_realtime_opening_cache_failure_records_fallback(monkeypatch):
    graph = _workflow_without_greeting()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
        uses_realtime=True,
        realtime_generate_reply_supported=False,
        realtime_exact_speech_uses_tts=True,
        tts_api_key="google-api-key",
        opening_model="gemini-3.1-flash-live-preview",
        tts_voice="Kore",
        tts_language="en",
    )
    captured = {}
    persisted = AsyncMock()

    async def fail_live_opening_audio_path(**kwargs):
        raise RuntimeError("Gemini cache failed")

    def fake_say_text(text, *, allow_interruptions=True, audio=None):
        captured["text"] = text
        captured["allow_interruptions"] = allow_interruptions
        captured["audio"] = audio
        return "speech"

    monkeypatch.setattr(
        worker,
        "_live_opening_audio_path",
        fail_live_opening_audio_path,
    )
    monkeypatch.setattr(worker, "_append_livekit_run_event", persisted)
    monkeypatch.setattr(agent, "_say_text", fake_say_text)

    result = await agent._speak_opening("Hello.", allow_interruptions=False)

    assert result == "speech"
    assert captured == {
        "text": "Hello.",
        "allow_interruptions": False,
        "audio": None,
    }
    persisted.assert_awaited_once()
    assert persisted.await_args.args[0] == 17
    assert persisted.await_args.args[1]["type"] == "opening_audio_cache_failed"
    assert persisted.await_args.args[1]["fallback"] == "gemini_tts"


@pytest.mark.asyncio
async def test_livekit_immutable_realtime_auto_advance_skips_session_updates(
    monkeypatch,
):
    monkeypatch.delenv(worker.post_call.POST_CALL_WEBHOOK_ENV, raising=False)
    graph = _workflow_with_start_auto_advance()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
        uses_realtime=True,
        realtime_generate_reply_supported=False,
    )
    observed = {"transitions": []}

    class FakeSpeech:
        async def wait_for_playout(self):
            observed["waited"] = True

    async def fake_speak_opening(text, *, allow_interruptions=True):
        observed["spoken"] = text
        observed["allow_interruptions"] = allow_interruptions
        return FakeSpeech()

    async def fake_record_node_transition(node, previous_node):
        observed["transitions"].append(
            (node.id, previous_node.id if previous_node else None)
        )

    async def fail_update_instructions(instructions):
        raise AssertionError("immutable realtime sessions must not update instructions")

    async def fail_update_tools(tools):
        raise AssertionError("immutable realtime sessions must not update tools")

    monkeypatch.setattr(agent, "_speak_opening", fake_speak_opening)
    monkeypatch.setattr(agent, "_record_node_transition", fake_record_node_transition)
    monkeypatch.setattr(agent, "update_instructions", fail_update_instructions)
    monkeypatch.setattr(agent, "update_tools", fail_update_tools)

    await agent.start_opening()

    assert [tool.id for tool in agent.tools] == ["end"]
    assert "OPENING STATE:" in agent.instructions
    assert '"Hello."' in agent.instructions
    assert observed["spoken"] == "Hello."
    assert observed["allow_interruptions"] is False
    assert observed["waited"] is True
    assert observed["transitions"] == [("start", None), ("main", "start")]
    assert agent._current_node.id == "main"


def test_livekit_opening_cache_key_changes_only_for_opening_inputs():
    base = worker._opening_audio_cache_path(
        text="hello",
        model="gemini-3.1-flash-live-preview",
        voice="Kore",
        language="te",
    )

    assert base == worker._opening_audio_cache_path(
        text="hello",
        model="gemini-3.1-flash-live-preview",
        voice="Kore",
        language="te",
    )
    assert base != worker._opening_audio_cache_path(
        text="hello",
        model="gemini-3.1-flash-live-preview",
        voice="Puck",
        language="te",
    )
    assert base != worker._opening_audio_cache_path(
        text="hello",
        model="gemini-3.1-flash-live-preview",
        voice="Kore",
        language="en",
    )
    assert base != worker._opening_audio_cache_path(
        text="hi",
        model="gemini-3.1-flash-live-preview",
        voice="Kore",
        language="te",
    )

    old_cache_input = json.dumps(
        {
            "text": "hello",
            "model": "gemini-3.1-flash-live-preview",
            "voice": "Kore",
            "language": "te",
            "format": "pcm24k-wav-v2",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    old_digest = hashlib.sha256(old_cache_input.encode("utf-8")).hexdigest()[:24]
    assert base != worker.OPENING_AUDIO_CACHE_DIR / f"{old_digest}.wav"
    assert (
        str(worker.OPENING_AUDIO_LEADING_SILENCE_MS)
        in worker.OPENING_AUDIO_CACHE_FORMAT
    )
    assert (
        str(worker.OPENING_AUDIO_TRAILING_SILENCE_MS)
        in worker.OPENING_AUDIO_CACHE_FORMAT
    )


def test_livekit_write_pcm_wav_can_pad_opening_audio(tmp_path):
    path = tmp_path / "opening.wav"
    source_pcm = b"\x01\x02\x03\x04"

    worker._write_pcm_wav(
        path,
        source_pcm,
        sample_rate=1000,
        leading_silence_ms=2,
        trailing_silence_ms=1,
    )

    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 1000
        assert wav.readframes(wav.getnframes()) == (
            b"\x00\x00" * 2 + source_pcm + b"\x00\x00"
        )


def test_livekit_close_text_detection():
    telugu_call_end = (
        "\u0c28\u0c47\u0c28\u0c41 "
        "\u0c15\u0c3e\u0c32\u0c4d "
        "\u0c0e\u0c02\u0c21\u0c4d "
        "\u0c1a\u0c47\u0c38\u0c4d\u0c24\u0c41"
        "\u0c28\u0c4d\u0c28\u0c3e\u0c28\u0c41"
    )
    telugu_thanks = "\u0c27\u0c28\u0c4d\u0c2f\u0c35\u0c3e" "\u0c26\u0c3e\u0c32\u0c41"
    telugu_no_questions = "\u0c0f\u0c02 \u0c32\u0c47\u0c35\u0c41"

    assert worker._is_assistant_close_text(telugu_call_end)
    assert worker._is_assistant_close_text(
        "\u0c38\u0c30\u0c47, "
        "\u0c27\u0c28\u0c4d\u0c2f\u0c35\u0c3e\u0c26\u0c3e\u0c32\u0c41. "
        "\u0c2e\u0c40\u0c15\u0c41 "
        "\u0c07\u0c02\u0c15\u0c47\u0c2e\u0c48\u0c28\u0c3e "
        "\u0c2a\u0c4d\u0c30\u0c36\u0c4d\u0c28\u0c32\u0c41 "
        "\u0c09\u0c02\u0c1f\u0c47 "
        "\u0c0e\u0c2a\u0c4d\u0c2a\u0c41\u0c21\u0c48\u0c28\u0c3e "
        "\u0c05\u0c21\u0c17\u0c02\u0c21\u0c3f."
    )
    assert worker._is_assistant_close_text("Thanks, I am ending the call now.")
    assert worker._is_assistant_close_text("Dhanyavadalu, call mugistunnanu.")
    assert worker._is_assistant_close_text("Call end chestunnanu.")
    assert worker._is_user_close_text(telugu_thanks)
    assert worker._is_user_close_text(telugu_no_questions)
    assert worker._is_user_close_text("thank you, bye")
    assert not worker._is_user_close_text(
        "\u0c28\u0c3e\u0c15\u0c41 "
        "\u0c32\u0c46\u0c1f\u0c30\u0c4d "
        "\u0c2c\u0c48 "
        "\u0c39\u0c3e\u0c30\u0c4d\u0c1f\u0c4d "
        "\u0c38\u0c4d\u0c2a\u0c46\u0c32\u0c4d\u0c32\u0c3f\u0c02\u0c17\u0c4d "
        "\u0c1a\u0c46\u0c2a\u0c4d\u0c2a\u0c2e\u0c4d\u0c2e\u0c3e"
    )
    assert not worker._is_assistant_close_text("Please explain the subsidy.")
    assert not worker._is_user_close_text("What documents are required?")
    assert not worker._is_user_close_text(
        "\u0c38\u0c30\u0c47, \u0c28\u0c3e \u0c07\u0c02"
        "\u0c1f\u0c3f\u0c15\u0c3f \u0c15\u0c3e\u0c35\u0c3e\u0c32\u0c3f"
    )


@pytest.mark.asyncio
async def test_livekit_schedule_shutdown_emits_completion_and_shuts_down(monkeypatch):
    graph = _workflow_without_greeting()
    ctx = _job_context()
    agent = worker.LiveKitWorkflowAgent(
        ctx=ctx,
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )
    emitted = []

    async def fake_emit_feedback_now(message):
        emitted.append(message)

    monkeypatch.setattr(agent, "_emit_feedback_now", fake_emit_feedback_now)

    agent._schedule_shutdown("assistant closed conversation")
    await agent._shutdown_task

    assert emitted[-1]["type"] == "rtf-run-completed"
    assert emitted[-1]["payload"] == {"reason": "assistant closed conversation"}
    ctx.shutdown.assert_called_once_with("assistant closed conversation")


@pytest.mark.asyncio
async def test_livekit_schedule_shutdown_deletes_room_before_worker_shutdown(
    monkeypatch,
):
    graph = _workflow_without_greeting()
    ctx = _job_context()
    ctx.delete_room = Mock(return_value=None)
    agent = worker.LiveKitWorkflowAgent(
        ctx=ctx,
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )

    async def fake_emit_feedback_now(message):
        return None

    monkeypatch.setattr(agent, "_emit_feedback_now", fake_emit_feedback_now)

    agent._schedule_shutdown("assistant closed conversation")
    await agent._shutdown_task

    ctx.delete_room.assert_called_once_with()
    ctx.shutdown.assert_called_once_with("assistant closed conversation")


@pytest.mark.asyncio
async def test_livekit_opening_auto_advances_single_non_end_start_transition(
    monkeypatch,
):
    monkeypatch.delenv(worker.post_call.POST_CALL_WEBHOOK_ENV, raising=False)
    graph = _workflow_with_start_auto_advance()
    agent = worker.LiveKitWorkflowAgent(
        ctx=_job_context(),
        workflow=graph,
        workflow_run_id=17,
        organization_id=9,
        call_context_vars={},
    )
    observed = {"transitions": [], "events": []}

    class FakeSpeech:
        async def wait_for_playout(self):
            observed["events"].append("wait_for_playout")
            observed["waited"] = True

    async def fake_speak_text(text, *, allow_interruptions=True):
        observed["events"].append("speak")
        observed["spoken"] = text
        observed["allow_interruptions"] = allow_interruptions
        return FakeSpeech()

    async def fake_record_node_transition(node, previous_node):
        observed["events"].append(f"record:{node.id}")
        observed["transitions"].append(
            (node.id, previous_node.id if previous_node else None)
        )

    async def fake_update_instructions(instructions):
        observed["events"].append("update_instructions")
        observed["instructions"] = instructions

    async def fake_update_tools(tools):
        observed["events"].append("update_tools")
        observed["tools"] = [tool.id for tool in tools]

    monkeypatch.setattr(agent, "_speak_text", fake_speak_text)
    monkeypatch.setattr(agent, "_record_node_transition", fake_record_node_transition)
    monkeypatch.setattr(agent, "update_instructions", fake_update_instructions)
    monkeypatch.setattr(agent, "update_tools", fake_update_tools)

    await agent.start_opening()

    assert observed["spoken"] == "Hello."
    assert observed["allow_interruptions"] is False
    assert observed["waited"] is True
    assert observed["events"] == [
        "speak",
        "record:start",
        "wait_for_playout",
        "update_instructions",
        "update_tools",
        "record:main",
    ]
    assert observed["transitions"] == [("start", None), ("main", "start")]
    assert observed["instructions"].startswith("Main scheme facts are available here.")
    assert "OPENING STATE:" in observed["instructions"]
    assert '"Hello."' in observed["instructions"]
    assert observed["tools"] == ["end"]
    assert agent._current_node.id == "main"


def _realtime_config(model="gemini-3.1-flash-live-preview"):
    return SimpleNamespace(
        is_realtime=True,
        realtime=SimpleNamespace(
            provider=ServiceProviders.GOOGLE_REALTIME,
            model=model,
            voice="Kore",
            language="en",
        ),
        llm=None,
        stt=None,
        tts=None,
    )


def test_uses_realtime_requires_realtime_section():
    cfg = _realtime_config()
    assert worker._uses_realtime(cfg) is True

    # is_realtime flag set but no realtime section -> NOT realtime (no split brain)
    cfg_missing = SimpleNamespace(is_realtime=True, realtime=None)
    assert worker._uses_realtime(cfg_missing) is False

    cfg_pipeline = SimpleNamespace(is_realtime=False, realtime=None)
    assert worker._uses_realtime(cfg_pipeline) is False


def test_runtime_configuration_reports_mode():
    realtime_mode = worker._runtime_configuration_from_user_config(_realtime_config())
    assert realtime_mode["mode"] == "realtime"

    pipeline_cfg = SimpleNamespace(
        is_realtime=True,
        realtime=None,
        llm=SimpleNamespace(provider=ServiceProviders.OPENAI, model="gpt-4.1"),
        stt=SimpleNamespace(provider=ServiceProviders.OPENAI, model="gpt-4o-transcribe"),
        tts=SimpleNamespace(provider=ServiceProviders.OPENAI, model="gpt-4o-mini-tts"),
    )
    pipeline_mode = worker._runtime_configuration_from_user_config(pipeline_cfg)
    assert pipeline_mode["mode"] == "pipeline"


def test_supports_realtime_generate_reply_prefix_match():
    google = ServiceProviders.GOOGLE_REALTIME.value
    # The known unsupported preview model.
    assert worker._supports_realtime_generate_reply(google, "gemini-3.1-flash-live-preview") is False
    # A "3.1"-containing but unrelated name must NOT be misclassified.
    assert worker._supports_realtime_generate_reply(google, "gemini-3.10-pro-live") is True
    assert worker._supports_realtime_generate_reply(google, "gemini-2.5-flash-live") is True
    # Non-google realtime providers always support it here.
    assert worker._supports_realtime_generate_reply(
        ServiceProviders.OPENAI_REALTIME.value, "gpt-realtime"
    ) is True
