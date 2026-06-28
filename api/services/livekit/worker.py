from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types as genai_types
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, cli, llm
from livekit.plugins import google, openai, silero
from loguru import logger

from api.db import db_client
from api.enums import CallType, WorkflowRunMode, WorkflowRunState
from api.services.livekit.runtime_config import (
    effective_livekit_settings,
    livekit_environment,
)
from api.services.livekit import post_call
from api.services.configuration.registry import ServiceProviders
from api.services.configuration.resolve import resolve_effective_config
from api.services.gen_ai import resolve_embedding_settings
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.pipecat_engine_context_composer import (
    compose_system_prompt_for_node,
)
from api.services.workflow.tools.knowledge_base import (
    get_knowledge_base_tool,
    retrieve_from_knowledge_base,
)
from api.services.workflow.workflow_graph import Edge, Node, WorkflowGraph
from api.utils.template_renderer import render_template

FEEDBACK_TOPIC = "spx-voice.feedback"
DEFAULT_OPENING = (
    "Hello, this is your SPX Voice assistant. How can I help you today?"
)
OPENING_AUDIO_CACHE_DIR = (
    Path(__file__).resolve().parents[2] / ".runtime" / "livekit_openings"
)
OPENING_AUDIO_LEADING_SILENCE_MS = 1000
OPENING_AUDIO_TRAILING_SILENCE_MS = 1100
OPENING_AUDIO_CACHE_FORMAT = (
    "pcm24k-wav-v4"
    f"-lead{OPENING_AUDIO_LEADING_SILENCE_MS}"
    f"-tail{OPENING_AUDIO_TRAILING_SILENCE_MS}"
)
OPENING_AUDIO_GENERATION_TIMEOUT_SECONDS = 20.0
OPENING_AUDIO_MAX_DURATION_SECONDS = 12.0
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_SAMPLE_RATE = 24000
GEMINI_TTS_CHANNELS = 1
GEMINI_TTS_FRAME_MS = 20
WAV_FRAME_MS = 20
FEEDBACK_FLUSH_INTERVAL_SECONDS = 1.0
GEMINI_SERVER_VAD_PREFIX_PADDING_MS = 220
GEMINI_SERVER_VAD_SILENCE_DURATION_MS = 450
FAST_GEMINI_SERVER_VAD_PREFIX_PADDING_MS = 320
FAST_GEMINI_SERVER_VAD_SILENCE_DURATION_MS = 480
FAST_ENDPOINTING_MIN_DELAY_SECONDS = 0.14
FAST_ENDPOINTING_MAX_DELAY_SECONDS = 0.32
FAST_INTERRUPTION_MIN_DURATION_SECONDS = 0.55
FAST_INTERRUPTION_MIN_WORDS = 2
FALSE_INTERRUPTION_TIMEOUT_SECONDS = 1.5
FAST_PREEMPTIVE_MAX_SPEECH_DURATION_SECONDS = 3.2
FAST_LATENCY_PROFILE_MARKERS = ("fast", "insane", "low_latency", "ultra")
FAST_RESPONSE_INSTRUCTIONS = """\nPROFESSIONAL VOICE ASSISTANT:
You are a professional SPX Voice assistant for the configured workflow.

Tone: courteous, neutral, concise, and official. Avoid honorific-heavy phrasing, casual blessings or sign-offs, slang, jokes, flirting, and filler.

Call flow:
- Answer the caller's latest question first in one short sentence.
- Then collect one missing tracking field only.
- Required tracking fields: customer_name, district, town, looking_for, remarks.
- Use record_lead_details when a field is learned or corrected.
- Only save customer_name, district, and town when the caller explicitly says them in this call. Never infer from examples, phone number, office location, prior calls, or memory.
- Use tool results as guidance only. Do not sound like an IVR; keep replies conversational and responsive.

Speech:
- Mirror the caller's language when possible.
- Use "saar", "madam", or "andi" sparingly and professionally.
- Never repeat the opening identity unless asked who is speaking.
- Do not ask for OTPs, bank details, payment details, government IDs, or other sensitive personal data unless the workflow explicitly requires it and the caller has consented.
- Do not promise messages, links, callbacks, or third-party actions unless the workflow provides that capability.
- Close only after a clear caller close intent and complete tracking fields."""
LEAD_CAPTURE_INSTRUCTIONS = """\
LEAD CAPTURE:
- Required sheet fields from the conversation: customer_name, district, town, looking_for, remarks.
- customer_number, rep_number, called_at, and duration are captured automatically by the system.
- Ask for the next missing required field returned by record_lead_details; do not choose your own field order.
- Call record_lead_details whenever any required field is known or corrected.
- Only pass values the caller explicitly stated in this call. Do not guess district, town, or customer_name.
- Before ending, record all required fields. If the caller refuses or does not know a field after being asked, record "not provided" for that field and note the exact refused field in remarks.
- Do not ask for OTPs, bank details, payment details, government IDs, or other sensitive personal data unless explicitly required."""
POST_OPENING_STATE_INSTRUCTIONS = """\
OPENING STATE:
- The assistant has already spoken this opening greeting exactly once: {opening}
- Do not repeat or paraphrase that greeting or the agent identity unless the caller
  asks who this is.
- If the caller only says hello, hi, namaste, namaskaram, or another short
  greeting, continue directly with a brief helpful prompt instead of greeting again."""
EXACT_SAY_RE = re.compile(r"say exactly(?:\s+in\s+[^:]+)?:\s*(.+)", re.IGNORECASE)
ASSISTANT_CLOSE_RE = re.compile(
    r"(కాల్\s*(ఎండ్|ముగిస్తున్నాను|ముగిస్తాను)|"
    r"ధన్యవాదాలు|ఉంటాను|"
    r"dhanyavadalu|call\s+(?:end\s+ches(?:t|th)?unnanu|mugistunnanu|mugistanu)|"
    r"goodbye|bye|ending the call|end the call)",
    re.IGNORECASE,
)
USER_CLOSE_RE = re.compile(
    r"(థ్యాంక్యూ|థాంక్యూ|ధన్యవాదాలు|ఉంటాను|బై|చాలు|"
    r"\u0c0f\u0c02\s+\u0c32\u0c47\u0c35\u0c41|"
    r"\u0c0f\u0c2e\u0c40\s+\u0c32\u0c47\u0c26\u0c41|"
    r"\u0c07\u0c02\u0c15\u0c47\u0c2e\u0c40\s+\u0c32\u0c47\u0c26\u0c41|"
    r"thank you|thanks|bye|done|no more|no questions|nothing|that's all)",
    re.IGNORECASE,
)
USER_CLOSE_INTENT_TTL_SECONDS = 45.0
DEFAULT_END_CALL_TEXT = "Dhanyavadalu, call mugistunnanu."
LEAD_COLLECTION_FIELDS = ("customer_name", "district", "town", "looking_for")
LEAD_FIELD_FOLLOWUP_HINTS = {
    "customer_name": "ask for the caller's name",
    "district": "ask for the caller's district",
    "town": "ask for the caller's town, village, or locality",
    "looking_for": "ask what help they need",
}
_GEMINI_TTS_CACHE: dict[tuple[str, str, str, str], bytes] = {}
_OPENING_AUDIO_INFLIGHT: dict[Path, asyncio.Task[Path]] = {}
KNOWLEDGE_BASE_GROUNDING_INSTRUCTIONS = """\
KNOWLEDGE BASE GROUNDING:
- This node has knowledge base documents attached. Use retrieve_from_knowledge_base before answering questions about those documents, policies, procedures, or agent/business details.
- Answer only from retrieved chunks and the node instructions. If the retrieved chunks do not contain the answer, say you do not have that information.
- Do not invent facts, prices, policies, availability, names, or contact details that are not in the retrieved knowledge base content."""


def _is_fast_latency_profile(latency_profile: str | None) -> bool:
    value = str(latency_profile or "").lower()
    return any(marker in value for marker in FAST_LATENCY_PROFILE_MARKERS)


def _latency_response_instructions(latency_profile: str | None) -> str | None:
    if _is_fast_latency_profile(latency_profile):
        return FAST_RESPONSE_INSTRUCTIONS
    return None


def _fast_turn_handling(
    turn_detection: str | None,
    *,
    latency_profile: str | None = None,
) -> dict[str, Any]:
    fast_profile = _is_fast_latency_profile(latency_profile)
    if turn_detection == "realtime_llm":
        return {
            "endpointing": {
                "mode": "fixed",
                "min_delay": (
                    FAST_ENDPOINTING_MIN_DELAY_SECONDS if fast_profile else 0.25
                ),
                "max_delay": (
                    FAST_ENDPOINTING_MAX_DELAY_SECONDS if fast_profile else 0.75
                ),
            },
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "discard_audio_if_uninterruptible": False,
                "min_duration": (
                    FAST_INTERRUPTION_MIN_DURATION_SECONDS if fast_profile else 0.7
                ),
                "min_words": FAST_INTERRUPTION_MIN_WORDS,
                "resume_false_interruption": True,
                "false_interruption_timeout": FALSE_INTERRUPTION_TIMEOUT_SECONDS,
            },
            "preemptive_generation": {"enabled": False},
            "turn_detection": turn_detection,
        }

    options: dict[str, Any] = {
        "endpointing": {
            "mode": "fixed",
            "min_delay": FAST_ENDPOINTING_MIN_DELAY_SECONDS if fast_profile else 0.25,
            "max_delay": FAST_ENDPOINTING_MAX_DELAY_SECONDS if fast_profile else 0.75,
        },
        "interruption": {
            "enabled": True,
            "mode": "vad",
            "discard_audio_if_uninterruptible": False,
            "min_duration": (
                FAST_INTERRUPTION_MIN_DURATION_SECONDS if fast_profile else 0.7
            ),
            "min_words": FAST_INTERRUPTION_MIN_WORDS,
            "resume_false_interruption": True,
            "false_interruption_timeout": FALSE_INTERRUPTION_TIMEOUT_SECONDS,
        },
        "preemptive_generation": {
            "enabled": True,
            "preemptive_tts": False,
            "max_speech_duration": (
                FAST_PREEMPTIVE_MAX_SPEECH_DURATION_SECONDS if fast_profile else 6.0
            ),
            "max_retries": 2,
        },
    }
    if turn_detection is not None:
        options["turn_detection"] = turn_detection
    return options


def _session_latency_options(
    turn_detection: str | None,
    *,
    latency_profile: str | None = None,
) -> dict[str, Any]:
    return {
        "turn_handling": _fast_turn_handling(
            turn_detection,
            latency_profile=latency_profile,
        ),
        "min_consecutive_speech_delay": 0.0,
        "aec_warmup_duration": 0.0,
        "user_away_timeout": None,
        "session_close_transcript_timeout": 0.2,
    }


def _local_vad_realtime_input_config() -> genai_types.RealtimeInputConfig:
    return genai_types.RealtimeInputConfig(
        automatic_activity_detection=genai_types.AutomaticActivityDetection(
            disabled=True
        )
    )


def _fast_server_vad_realtime_input_config(
    latency_profile: str | None = None,
) -> genai_types.RealtimeInputConfig:
    fast_profile = _is_fast_latency_profile(latency_profile)
    return genai_types.RealtimeInputConfig(
        automatic_activity_detection=genai_types.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_LOW,
            end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
            prefix_padding_ms=(
                FAST_GEMINI_SERVER_VAD_PREFIX_PADDING_MS
                if fast_profile
                else GEMINI_SERVER_VAD_PREFIX_PADDING_MS
            ),
            silence_duration_ms=(
                FAST_GEMINI_SERVER_VAD_SILENCE_DURATION_MS
                if fast_profile
                else GEMINI_SERVER_VAD_SILENCE_DURATION_MS
            ),
        ),
        activity_handling=genai_types.ActivityHandling.NO_INTERRUPTION,
        turn_coverage=genai_types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
    )


def _extract_exact_say_text(prompt: str | None) -> str | None:
    for line in (prompt or "").splitlines():
        match = EXACT_SAY_RE.search(line.strip())
        if not match:
            continue
        text = match.group(1).strip().strip("\"'")
        if text:
            return text
    return None


def _gemini_tts_cache_key(
    *, api_key: str, voice: str, language: str, text: str
) -> tuple[str, str, str, str]:
    return (api_key[-8:], voice, language, text)


def _extract_gemini_audio(response: Any) -> bytes | None:
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline_data = getattr(part, "inline_data", None)
            data = getattr(inline_data, "data", None)
            if data:
                return bytes(data)
    return None


def _synthesize_gemini_tts_pcm(
    *,
    api_key: str,
    voice: str,
    language: str,
    text: str,
) -> bytes:
    attempts = [text]
    if text != DEFAULT_OPENING:
        attempts.append(DEFAULT_OPENING)

    client = genai.Client(api_key=api_key)
    last_error: Exception | None = None
    for attempt in attempts:
        cache_key = _gemini_tts_cache_key(
            api_key=api_key, voice=voice, language=language, text=attempt
        )
        cached = _GEMINI_TTS_CACHE.get(cache_key)
        if cached:
            return cached

        try:
            response = client.models.generate_content(
                model=GEMINI_TTS_MODEL,
                contents=attempt,
                config=genai_types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=genai_types.SpeechConfig(
                        language_code=language,
                        voice_config=genai_types.VoiceConfig(
                            prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                                voice_name=voice
                            )
                        ),
                    ),
                ),
            )
            audio = _extract_gemini_audio(response)
            if audio:
                _GEMINI_TTS_CACHE[cache_key] = audio
                return audio
            logger.warning(
                f"[LiveKit] Gemini TTS returned no audio for {attempt!r}; "
                "trying fallback"
            )
        except Exception as exc:
            last_error = exc
            logger.warning(f"[LiveKit] Gemini TTS failed: {exc}")

    if last_error:
        raise last_error
    raise RuntimeError("Gemini TTS returned no audio")


async def _gemini_tts_audio_frames(
    *,
    api_key: str,
    voice: str,
    language: str,
    text: str,
):
    audio = await asyncio.to_thread(
        _synthesize_gemini_tts_pcm,
        api_key=api_key,
        voice=voice,
        language=language,
        text=text,
    )
    bytes_per_sample = 2 * GEMINI_TTS_CHANNELS
    frame_size = int(
        GEMINI_TTS_SAMPLE_RATE * (GEMINI_TTS_FRAME_MS / 1000) * bytes_per_sample
    )
    for offset in range(0, len(audio), frame_size):
        chunk = audio[offset : offset + frame_size]
        if len(chunk) < bytes_per_sample:
            continue
        if len(chunk) % bytes_per_sample:
            chunk = chunk[: -(len(chunk) % bytes_per_sample)]
        yield rtc.AudioFrame(
            chunk,
            sample_rate=GEMINI_TTS_SAMPLE_RATE,
            num_channels=GEMINI_TTS_CHANNELS,
            samples_per_channel=len(chunk) // bytes_per_sample,
        )


def _opening_audio_cache_path(
    *,
    text: str,
    model: str | None,
    voice: str | None,
    language: str | None,
) -> Path:
    cache_input = json.dumps(
        {
            "text": text,
            "model": model or "",
            "voice": voice or "",
            "language": language or "",
            "format": OPENING_AUDIO_CACHE_FORMAT,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(cache_input.encode("utf-8")).hexdigest()[:24]
    return OPENING_AUDIO_CACHE_DIR / f"{digest}.wav"


def _silence_pcm(duration_ms: int, *, sample_rate: int) -> bytes:
    if duration_ms <= 0:
        return b""
    samples = int(sample_rate * (duration_ms / 1000))
    return b"\x00\x00" * samples


def _write_pcm_wav(
    path: Path,
    pcm: bytes,
    *,
    sample_rate: int = 24000,
    leading_silence_ms: int = 0,
    trailing_silence_ms: int = 0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp.wav")
    padded_pcm = (
        _silence_pcm(leading_silence_ms, sample_rate=sample_rate)
        + pcm
        + _silence_pcm(trailing_silence_ms, sample_rate=sample_rate)
    )
    with wave.open(str(tmp_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(padded_pcm)
    tmp_path.replace(path)


async def _generate_live_opening_audio(
    *,
    api_key: str,
    model: str,
    voice: str,
    language: str,
    text: str,
    output_path: Path,
    leading_silence_ms: int = OPENING_AUDIO_LEADING_SILENCE_MS,
    trailing_silence_ms: int = OPENING_AUDIO_TRAILING_SILENCE_MS,
    max_duration_seconds: float = OPENING_AUDIO_MAX_DURATION_SECONDS,
) -> Path:
    client = genai.Client(api_key=api_key)
    audio = bytearray()
    config = genai_types.LiveConnectConfig(
        response_modalities=[genai_types.Modality.AUDIO],
        speech_config=genai_types.SpeechConfig(
            language_code=language,
            voice_config=genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice)
            ),
        ),
        temperature=0.0,
        system_instruction=(
            "You are a TTS engine. Speak only the exact quoted text. "
            "No explanation. Stop after the text."
        ),
    )

    async def _run() -> None:
        async with client.aio.live.connect(model=model, config=config) as session:
            await session.send_client_content(
                turns=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=f'"{text}"')],
                ),
                turn_complete=True,
            )
            async for message in session.receive():
                server_content = getattr(message, "server_content", None)
                if server_content is None:
                    continue
                model_turn = getattr(server_content, "model_turn", None)
                for part in getattr(model_turn, "parts", None) or []:
                    inline_data = getattr(part, "inline_data", None)
                    data = getattr(inline_data, "data", None)
                    if data:
                        audio.extend(bytes(data))
                if getattr(server_content, "turn_complete", False):
                    break

    await asyncio.wait_for(_run(), timeout=OPENING_AUDIO_GENERATION_TIMEOUT_SECONDS)
    duration_seconds = len(audio) / (24000 * 2)
    if not audio or duration_seconds > max_duration_seconds:
        raise RuntimeError(
            "Generated opening audio was empty or too long "
            f"({duration_seconds:.2f}s)"
        )
    _write_pcm_wav(
        output_path,
        bytes(audio),
        sample_rate=24000,
        leading_silence_ms=leading_silence_ms,
        trailing_silence_ms=trailing_silence_ms,
    )
    return output_path


async def _wav_audio_frames(path: Path):
    with wave.open(str(path), "rb") as wav:
        sample_width = wav.getsampwidth()
        if sample_width != 2:
            raise ValueError(f"Unsupported WAV sample width: {sample_width}")

        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        frames_per_chunk = max(1, int(sample_rate * (WAV_FRAME_MS / 1000)))
        bytes_per_sample = sample_width * channels

        while True:
            chunk = wav.readframes(frames_per_chunk)
            if not chunk:
                break
            yield rtc.AudioFrame(
                chunk,
                sample_rate=sample_rate,
                num_channels=channels,
                samples_per_channel=len(chunk) // bytes_per_sample,
            )


async def _live_opening_audio_path(
    *,
    api_key: str | None,
    model: str | None,
    voice: str | None,
    language: str | None,
    text: str,
) -> Path | None:
    if not (api_key and model and voice and language and text):
        return None

    path = _opening_audio_cache_path(
        text=text,
        model=model,
        voice=voice,
        language=language,
    )
    if path.exists():
        return path

    task = _OPENING_AUDIO_INFLIGHT.get(path)
    if task is None or task.done():
        logger.info(
            "[LiveKit] generating cached opening audio "
            f"model={model!r} voice={voice!r} language={language!r}"
        )
        task = asyncio.create_task(
            _generate_live_opening_audio(
                api_key=api_key,
                model=model,
                voice=voice,
                language=language,
                text=text,
                output_path=path,
            )
        )
        _OPENING_AUDIO_INFLIGHT[path] = task

    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _OPENING_AUDIO_INFLIGHT.get(path) is task:
            _OPENING_AUDIO_INFLIGHT.pop(path, None)


def _is_assistant_close_text(text: str) -> bool:
    return bool(ASSISTANT_CLOSE_RE.search(text or ""))


def _is_false_user_close_match(text: str, match: re.Match[str]) -> bool:
    if match.group(0) != "\u0c2c\u0c48":
        return False
    after = text[match.end() :]
    return bool(
        re.match(
            r"\s*(\u0c39\u0c3e\u0c30\u0c4d\u0c1f\u0c4d|heart)(?=$|\s|[.,!?])",
            after,
            re.IGNORECASE,
        )
    )


def _is_user_close_text(text: str) -> bool:
    value = text or ""
    match = USER_CLOSE_RE.search(value)
    if not match:
        return False
    return not _is_false_user_close_match(value, match)


def _shutdown_delay_for_text(text: str) -> float:
    word_count = max(1, len((text or "").split()))
    return min(6.0, max(1.5, word_count * 0.32))


def _metadata_from_job(ctx: JobContext) -> dict[str, Any]:
    return _metadata_from_json(ctx.job.metadata, "job")


def _metadata_from_room(ctx: JobContext) -> dict[str, Any]:
    return _metadata_from_json(getattr(ctx.room, "metadata", ""), "room")


def _metadata_from_json(value: str | None, source: str) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        logger.warning(f"Invalid LiveKit {source} metadata: {value!r}")
        return {}


def _participant_context(participant) -> dict[str, Any]:
    attrs = dict(getattr(participant, "attributes", {}) or {})
    metadata = getattr(participant, "metadata", "") or ""
    return {
        "participant_identity": getattr(participant, "identity", ""),
        "participant_name": getattr(participant, "name", ""),
        "participant_kind": str(getattr(participant, "kind", "")),
        "participant_metadata": metadata,
        "participant_attributes": attrs,
        "caller_number": attrs.get("sip.phoneNumber")
        or attrs.get("sip.from")
        or attrs.get("livekit.sip.phoneNumber"),
        "called_number": attrs.get("sip.trunkPhoneNumber")
        or attrs.get("sip.to")
        or attrs.get("livekit.sip.trunkPhoneNumber"),
        "call_id": attrs.get("sip.callID") or attrs.get("livekit.sip.callID"),
    }


def _safe_error_text(exc: Exception, *, limit: int = 1000) -> str:
    return f"{type(exc).__name__}: {exc}"[:limit]


async def _append_livekit_run_event(
    workflow_run_id: int,
    event: dict[str, Any],
) -> None:
    """Persist a compact LiveKit runtime event on the workflow run logs."""
    try:
        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        current_logs = workflow_run.logs if workflow_run else {}
        current_events = (current_logs or {}).get("livekit_events")
        if not isinstance(current_events, list):
            current_events = []
        await db_client.update_workflow_run(
            workflow_run_id,
            logs={
                "livekit_events": [
                    *current_events,
                    {
                        "timestamp": time.time(),
                        **event,
                    },
                ]
            },
        )
    except Exception as log_exc:
        logger.debug(f"[LiveKit] failed to persist runtime event: {log_exc}")


def _render_node_prompt(prompt: str | None, call_context_vars: dict[str, Any]) -> str:
    return render_template(prompt or "", call_context_vars)


def _compose_system_prompt(
    *,
    node: Node,
    workflow: WorkflowGraph,
    call_context_vars: dict[str, Any],
    has_recordings: bool = False,
    latency_profile: str | None = None,
) -> str:
    system_prompt = compose_system_prompt_for_node(
        node=node,
        workflow=workflow,
        format_prompt=lambda prompt: _render_node_prompt(prompt, call_context_vars),
        has_recordings=has_recordings,
    )
    latency_instructions = _latency_response_instructions(latency_profile)
    if latency_instructions:
        system_prompt = "\n\n".join(
            part for part in (latency_instructions, system_prompt) if part
        )
    if post_call.post_call_enabled():
        system_prompt = "\n\n".join(
            part for part in (system_prompt, LEAD_CAPTURE_INSTRUCTIONS) if part
        )
    if node.document_uuids:
        system_prompt = "\n\n".join(
            part
            for part in (system_prompt, KNOWLEDGE_BASE_GROUNDING_INSTRUCTIONS)
            if part
        )
    return system_prompt


def _tool_schema(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description or name,
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


def _knowledge_base_tool_schema(document_uuids: list[str]) -> dict[str, Any]:
    tool_def = get_knowledge_base_tool(document_uuids)["function"]
    return {
        "name": tool_def["name"],
        "description": tool_def["description"],
        "parameters": tool_def["parameters"],
    }


def _record_lead_details_tool_schema() -> dict[str, Any]:
    return {
        "name": "record_lead_details",
        "description": (
            "Persist post-call tracking fields. Call this whenever "
            "customer name, district, town, looking_for, or remarks are known "
            "or corrected. Only submit values that the caller explicitly stated "
            "in this call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "district": {
                    "type": "string",
                    "description": (
                        "Customer district explicitly stated by the caller, "
                        "for example Rangareddy. Do not infer."
                    ),
                },
                "town": {
                    "type": "string",
                    "description": (
                        "Customer town, village, mandal, or locality explicitly "
                        "stated by the caller. Do not infer."
                    ),
                },
                "looking_for": {
                    "type": "string",
                    "description": (
                        "What the caller wants, such as subsidy, cost, "
                        "registration, vendor, business use, or kW size."
                    ),
                },
                "customer_name": {
                    "type": "string",
                    "description": (
                        "Caller's name explicitly stated by the caller. Use "
                        "'not provided' only if the caller refused after being asked."
                    ),
                },
                "remarks": {
                    "type": "string",
                    "description": (
                        "Short professional note summarizing the enquiry and any "
                        "missing/refused detail."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    }


def _provider_value(provider: Any) -> str | None:
    if provider is None:
        return None
    return provider.value if hasattr(provider, "value") else str(provider)


def _google_thinking_config(model: str) -> dict[str, Any] | None:
    model_name = (model or "").lower()
    if "gemini-2.5" in model_name or model_name.startswith("gemini-2.5"):
        return {"thinking_budget": 0}
    if "gemini-3" in model_name or model_name.startswith("gemini-3"):
        return {"thinking_level": "minimal"}
    return None


# Realtime (speech-to-speech) Google models that do NOT support the
# generate_reply + local-VAD turn handling and must fall back to server-side
# VAD. Matched as a prefix so preview/version suffixes are tolerated. This
# replaces a brittle ``"3.1" in model_name`` substring check that would also
# misclassify unrelated names (e.g. a future ``gemini-3.10``). Add a model here
# only when it genuinely lacks generate_reply support.
_REALTIME_GENERATE_REPLY_UNSUPPORTED_PREFIXES = (
    "gemini-3.1-flash-live",
)


def _supports_realtime_generate_reply(provider: str | None, model: str | None) -> bool:
    if provider not in (
        ServiceProviders.GOOGLE_REALTIME.value,
        ServiceProviders.GOOGLE_VERTEX_REALTIME.value,
    ):
        return True
    model_name = (model or "").lower()
    return not any(
        model_name.startswith(prefix)
        for prefix in _REALTIME_GENERATE_REPLY_UNSUPPORTED_PREFIXES
    )


def _supports_realtime_tool_choice(provider: str | None) -> bool:
    return provider == ServiceProviders.OPENAI_REALTIME.value


class LiveKitWorkflowAgent(Agent):
    def __init__(
        self,
        *,
        ctx: JobContext,
        workflow: WorkflowGraph,
        workflow_run_id: int,
        organization_id: int,
        call_context_vars: dict[str, Any],
        embeddings_api_key: str | None = None,
        embeddings_provider: str | None = None,
        embeddings_model: str | None = None,
        embeddings_base_url: str | None = None,
        has_recordings: bool = False,
        uses_realtime: bool = False,
        realtime_generate_reply_supported: bool = True,
        realtime_tool_choice_supported: bool = True,
        realtime_exact_speech_uses_tts: bool = False,
        tts_api_key: str | None = None,
        opening_model: str | None = None,
        tts_voice: str = "Leda",  # Gemini voice id, not the assistant name.
        tts_language: str = "te-IN",  # Telugu - India
        latency_profile: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._workflow = workflow
        self._workflow_run_id = workflow_run_id
        self._organization_id = organization_id
        self._call_context_vars = call_context_vars
        self._embeddings_api_key = embeddings_api_key
        self._embeddings_provider = embeddings_provider
        self._embeddings_model = embeddings_model
        self._embeddings_base_url = embeddings_base_url
        self._has_recordings = has_recordings
        self._uses_realtime = uses_realtime
        self._realtime_generate_reply_supported = realtime_generate_reply_supported
        self._realtime_tool_choice_supported = realtime_tool_choice_supported
        self._realtime_exact_speech_uses_tts = realtime_exact_speech_uses_tts
        self._tts_api_key = tts_api_key
        self._opening_model = opening_model
        self._tts_voice = tts_voice
        self._tts_language = tts_language
        self._latency_profile = latency_profile
        self._current_node: Node | None = None
        self._visited_nodes: list[str] = []
        self._started_at = time.monotonic()
        self._feedback_persist_lock = asyncio.Lock()
        self._feedback_buffer: list[dict[str, Any]] = []
        self._feedback_flush_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._shutdown_deadline: float | None = None
        self._opening_prewarm_task: asyncio.Task | None = None
        self._last_user_speech_end_at: float | None = None
        self._last_final_transcript_at: float | None = None
        self._last_final_transcript: str | None = None
        self._last_final_transcript_seen_at: float | None = None
        self._final_user_transcripts: list[str] = []
        self._lead_details = post_call.extract_lead_details({})
        self._recording_state: post_call.LiveKitRecordingState | None = None
        self._recording_stop_lock = asyncio.Lock()
        start_node = workflow.nodes[workflow.start_node_id]
        self._start_opening_text = self._opening_text_for_start_node(start_node)
        initial_session_node = self._initial_session_node(start_node)
        post_opening_text = (
            self._start_opening_text
            if initial_session_node.id != start_node.id
            else None
        )
        super().__init__(
            instructions=self._compose_node_instructions(
                initial_session_node,
                post_opening_text=post_opening_text,
            ),
            tools=self._tools_for_node(initial_session_node),
        )

    def _supports_mid_call_session_updates(self) -> bool:
        return not (self._uses_realtime and not self._realtime_generate_reply_supported)

    def _initial_session_node(self, start_node: Node) -> Node:
        if self._supports_mid_call_session_updates():
            return start_node
        edge = self._auto_advance_edge_after_opening(start_node)
        if edge is None:
            return start_node
        return self._workflow.nodes[edge.target]

    def _compose_node_instructions(
        self,
        node: Node,
        *,
        post_opening_text: str | None = None,
    ) -> str:
        instructions = _compose_system_prompt(
            node=node,
            workflow=self._workflow,
            call_context_vars=self._call_context_vars,
            has_recordings=self._has_recordings,
            latency_profile=self._latency_profile,
        )
        if post_opening_text and not node.is_start and not node.is_end:
            instructions = "\n\n".join(
                [
                    instructions,
                    POST_OPENING_STATE_INSTRUCTIONS.format(
                        opening=json.dumps(post_opening_text, ensure_ascii=False)
                    ),
                ]
            )
        return instructions

    async def _publish_feedback(self, message: dict[str, Any]) -> None:
        try:
            payload = json.dumps(message, separators=(",", ":"), default=str)
            await self._ctx.room.local_participant.publish_data(
                payload,
                reliable=True,
                topic=FEEDBACK_TOPIC,
            )
        except Exception as exc:
            logger.debug(f"Failed to publish LiveKit feedback event: {exc}")

    async def flush_feedback(self) -> None:
        messages: list[dict[str, Any]] = []
        try:
            async with self._feedback_persist_lock:
                if not self._feedback_buffer:
                    return
                messages = self._feedback_buffer
                self._feedback_buffer = []
                workflow_run = await db_client.get_workflow_run_by_id(
                    self._workflow_run_id
                )
                current_logs = workflow_run.logs if workflow_run else {}
                current_events = (current_logs or {}).get("realtime_feedback_events")
                if not isinstance(current_events, list):
                    current_events = []
                await db_client.update_workflow_run(
                    self._workflow_run_id,
                    logs={"realtime_feedback_events": [*current_events, *messages]},
                )
        except Exception as exc:
            self._feedback_buffer = [*messages, *self._feedback_buffer]
            logger.debug(f"Failed to persist LiveKit feedback event: {exc}")

    def _queue_feedback_persist(self, message: dict[str, Any]) -> None:
        self._feedback_buffer.append(message)
        if self._feedback_flush_task and not self._feedback_flush_task.done():
            return

        async def _flush_later() -> None:
            try:
                await asyncio.sleep(FEEDBACK_FLUSH_INTERVAL_SECONDS)
                await self.flush_feedback()
            except asyncio.CancelledError:
                raise

        try:
            self._feedback_flush_task = asyncio.create_task(_flush_later())
        except RuntimeError:
            pass

    async def _emit_feedback_now(self, message: dict[str, Any]) -> None:
        await self._publish_feedback(message)
        self._queue_feedback_persist(message)

    def _emit_feedback(self, message: dict[str, Any]) -> None:
        try:
            asyncio.create_task(self._emit_feedback_now(message))
        except RuntimeError:
            pass

    def _tools_for_node(self, node: Node) -> list[llm.Tool]:
        if node.is_end:
            return []

        tools: list[llm.Tool] = []
        if node.document_uuids:
            tools.append(self._make_knowledge_base_tool(node.document_uuids))
        if post_call.post_call_enabled():
            tools.append(self._make_record_lead_details_tool())
        for edge in node.out_edges:
            tools.append(self._make_transition_tool(edge))
        return tools

    def _missing_lead_fields(self) -> list[str]:
        if not post_call.post_call_enabled():
            return []
        return post_call.missing_lead_fields(self._lead_details)

    def _remember_final_user_transcript(self, text: str) -> None:
        cleaned = post_call.normalize_lead_value(text)
        if not cleaned:
            return
        self._last_final_transcript = cleaned
        self._last_final_transcript_seen_at = time.monotonic()
        self._final_user_transcripts.append(cleaned)
        del self._final_user_transcripts[:-24]

    def _recent_user_requested_close(self) -> bool:
        if not self._last_final_transcript:
            return False
        if not _is_user_close_text(self._last_final_transcript):
            return False
        if self._last_final_transcript_seen_at is None:
            return True
        return (
            time.monotonic() - self._last_final_transcript_seen_at
            <= USER_CLOSE_INTENT_TTL_SECONDS
        )

    def _lead_evidence_text(self) -> str:
        return " ".join(self._final_user_transcripts)

    def _filter_unsupported_lead_updates(
        self,
        updates: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        evidence_text = self._lead_evidence_text()
        if not evidence_text:
            return updates, {}

        allowed: dict[str, Any] = {}
        rejected: dict[str, str] = {}
        for field, value in updates.items():
            if post_call.user_evidence_supports_lead_value(
                field,
                value,
                evidence_text,
            ):
                allowed[field] = value
            else:
                rejected[field] = post_call.normalize_lead_value(value)
        return allowed, rejected

    def _default_remarks_for_lead(self, lead: dict[str, Any]) -> str:
        looking_for = post_call.normalize_lead_value(lead.get("looking_for"))
        if looking_for and not post_call.is_placeholder_lead_value(looking_for):
            return f"Caller asked about {looking_for}."
        return ""

    def _with_runtime_lead_defaults(
        self,
        lead: dict[str, Any],
    ) -> dict[str, str]:
        normalized = {
            field: post_call.normalize_lead_value(lead.get(field))
            for field in post_call.LEAD_FIELDS
        }
        if post_call.is_missing_lead_value(
            normalized.get("remarks"),
            lead=normalized,
            field="remarks",
        ):
            remarks = self._default_remarks_for_lead(normalized)
            if remarks:
                normalized["remarks"] = remarks
        return normalized

    def _next_lead_field(self) -> str | None:
        missing = self._missing_lead_fields()
        for field in LEAD_COLLECTION_FIELDS:
            if field in missing:
                return field
        return None

    def _lead_prompt_for_field(self, field: str | None) -> str:
        if not field:
            return ""
        return LEAD_FIELD_FOLLOWUP_HINTS.get(field, "")

    async def _persist_lead_details(self, updates: dict[str, Any]) -> dict[str, str]:
        self._lead_details = self._with_runtime_lead_defaults(
            post_call.merge_lead_details(self._lead_details, updates)
        )
        await db_client.update_workflow_run(
            self._workflow_run_id,
            gathered_context=post_call.lead_details_gathered_context(
                self._lead_details
            ),
        )
        return self._lead_details

    def _make_record_lead_details_tool(self) -> llm.Tool:
        async def record_lead_details_tool(
            raw_arguments: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            raw_arguments = raw_arguments or {}
            updates = {
                field: raw_arguments.get(field)
                for field in post_call.LEAD_FIELDS
                if field in raw_arguments
            }
            updates, rejected = self._filter_unsupported_lead_updates(updates)
            lead = await self._persist_lead_details(updates)
            missing = post_call.missing_lead_fields(lead)
            next_field = next(iter(rejected), None) or self._next_lead_field()
            next_followup_hint = self._lead_prompt_for_field(next_field)
            logger.info(
                "[LiveKit] lead details recorded "
                f"run_id={self._workflow_run_id} missing={missing} "
                f"rejected={list(rejected)} next_field={next_field!r}"
            )
            if rejected:
                instruction = (
                    "Do not infer lead fields. Continue naturally, answer any "
                    "pending scheme question first, then ask one concise follow-up "
                    f"to collect: {', '.join(rejected)}."
                )
            elif next_field and next_followup_hint:
                instruction = (
                    "Continue naturally. If the caller's latest question is already "
                    f"answered, {next_followup_hint}."
                )
            elif missing:
                instruction = (
                    "Continue naturally and collect one missing tracking field "
                    "before ending the call."
                )
            else:
                instruction = "All required sheet fields are saved."
            return {
                "status": "saved",
                "lead_details": lead,
                "missing_fields": missing,
                "rejected_fields": rejected,
                "next_missing_field": next_field,
                "next_followup_hint": next_followup_hint,
                "instruction": instruction,
            }

        return llm.function_tool(
            record_lead_details_tool,
            raw_schema=_record_lead_details_tool_schema(),
        )

    def _make_knowledge_base_tool(self, document_uuids: list[str]) -> llm.Tool:
        async def retrieve_kb_tool(
            raw_arguments: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            query = str((raw_arguments or {}).get("query") or "").strip()
            if not query:
                return {
                    "error": "query is required",
                    "chunks": [],
                    "query": query,
                    "total_results": 0,
                    "instruction": "Ask the user a brief clarifying question instead of guessing.",
                }

            result = await retrieve_from_knowledge_base(
                query=query,
                organization_id=self._organization_id,
                document_uuids=document_uuids,
                limit=3,
                embeddings_api_key=self._embeddings_api_key,
                embeddings_provider=self._embeddings_provider,
                embeddings_model=self._embeddings_model,
                embeddings_base_url=self._embeddings_base_url,
            )
            if result.get("total_results", 0) == 0:
                result = {
                    **result,
                    "instruction": (
                        "No matching knowledge base content was found. "
                        "Do not invent an answer; say you do not have that information."
                    ),
                }
            return result

        return llm.function_tool(
            retrieve_kb_tool,
            raw_schema=_knowledge_base_tool_schema(document_uuids),
        )

    def _make_transition_tool(self, edge: Edge) -> llm.Tool:
        async def transition_tool(
            raw_arguments: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            target_node = self._workflow.nodes[edge.target]
            missing_lead_fields = (
                self._missing_lead_fields() if target_node.is_end else []
            )
            if missing_lead_fields:
                logger.info(
                    "[LiveKit] blocked end transition until lead fields are saved "
                    f"run_id={self._workflow_run_id} missing={missing_lead_fields}"
                )
                return {
                    "status": "blocked",
                    "missing_fields": missing_lead_fields,
                    "instruction": (
                        "Do not end the call yet. Ask one concise follow-up for "
                        "the next missing field, then call record_lead_details. "
                        "If the caller refuses, save that field as 'not provided' "
                        "and include it in remarks."
                    ),
                }
            if (
                target_node.is_end
                and post_call.post_call_enabled()
                and not self._recent_user_requested_close()
            ):
                logger.info(
                    "[LiveKit] blocked end transition without caller close intent "
                    f"run_id={self._workflow_run_id} "
                    f"last_user={self._last_final_transcript!r}"
                )
                return {
                    "status": "blocked",
                    "missing_fields": [],
                    "instruction": (
                        "Do not end the call yet. The caller has not clearly asked "
                        "to close. Answer the current question or ask one concise "
                        "professional follow-up."
                    ),
                }
            logger.info(
                "[LiveKit] transition tool called "
                f"run_id={self._workflow_run_id} "
                f"edge={edge.label!r} target={edge.target!r} "
                f"raw_arguments={raw_arguments or {}}"
            )
            if edge.transition_speech:
                transition_speech = render_template(
                    edge.transition_speech, self._call_context_vars
                )
                if transition_speech:
                    speech = await self._speak_text(str(transition_speech))
                    if speech:
                        await speech.wait_for_playout()

            await self.set_node(edge.target)
            active_node = self._current_node or target_node
            result: dict[str, Any] = {
                "status": "done",
                "transition": edge.label,
                "node": active_node.name,
            }
            if not active_node.is_end:
                result["node_instructions"] = _compose_system_prompt(
                    node=active_node,
                    workflow=self._workflow,
                    call_context_vars=self._call_context_vars,
                    has_recordings=self._has_recordings,
                    latency_profile=self._latency_profile,
                )
                result["available_transitions"] = [
                    {
                        "name": next_edge.get_function_name(),
                        "condition": next_edge.condition,
                    }
                    for next_edge in active_node.out_edges
                ]
            return result

        return llm.function_tool(
            transition_tool,
            raw_schema=_tool_schema(edge.get_function_name(), edge.condition),
        )

    def _auto_advance_edge_after_opening(self, node: Node) -> Edge | None:
        if not node.is_start:
            return None

        non_end_edges = [
            edge
            for edge in node.out_edges
            if not self._workflow.nodes[edge.target].is_end
        ]
        if len(non_end_edges) != 1:
            return None

        edge = non_end_edges[0]
        if edge.transition_speech:
            return None
        return edge

    async def _auto_advance_after_opening(self, node: Node) -> None:
        edge = self._auto_advance_edge_after_opening(node)
        if edge is None:
            return

        logger.info(
            "[LiveKit] auto advancing after opening "
            f"run_id={self._workflow_run_id} "
            f"edge={edge.label!r} target={edge.target!r}"
        )
        await self.set_node(edge.target)

    def _opening_text_for_start_node(self, start_node: Node) -> str | None:
        opening = self._render_greeting(start_node) or _extract_exact_say_text(
            start_node.prompt
        )
        if (
            not opening
            and self._uses_realtime
            and not self._realtime_generate_reply_supported
        ):
            opening = DEFAULT_OPENING
        return opening

    def prewarm_opening_audio(self) -> None:
        if not (self._uses_realtime and self._realtime_exact_speech_uses_tts):
            return
        if self._opening_prewarm_task and not self._opening_prewarm_task.done():
            return

        opening = self._opening_text_for_start_node(
            self._workflow.nodes[self._workflow.start_node_id]
        )
        if not opening:
            return

        self._opening_prewarm_task = asyncio.create_task(
            _live_opening_audio_path(
                api_key=self._tts_api_key,
                model=self._opening_model,
                voice=self._tts_voice,
                language=self._tts_language,
                text=opening,
            )
        )

        def _log_prewarm_result(task: asyncio.Task) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[LiveKit] opening audio prewarm failed: {exc}")

        self._opening_prewarm_task.add_done_callback(_log_prewarm_result)

    async def _speak_text(self, text: str, *, allow_interruptions: bool = True):
        if self._uses_realtime and self._realtime_exact_speech_uses_tts:
            return self._say_text(text, allow_interruptions=allow_interruptions)

        if self._uses_realtime:
            if not self._realtime_generate_reply_supported:
                logger.warning(
                    "[LiveKit] realtime model cannot be manually prompted to speak "
                    "and no realtime TTS fallback is configured"
                )
                return None
            try:
                kwargs: dict[str, Any] = {
                    "instructions": f"Say exactly and only this text: {text}",
                    "allow_interruptions": allow_interruptions,
                }
                if self._realtime_tool_choice_supported:
                    kwargs["tool_choice"] = "none"
                return self.session.generate_reply(**kwargs)
            except RuntimeError as exc:
                logger.warning(
                    f"[LiveKit] realtime exact speech failed, falling back to TTS: {exc}"
                )
        return self._say_text(text, allow_interruptions=allow_interruptions)

    async def _speak_opening(self, text: str, *, allow_interruptions: bool = True):
        if self._uses_realtime and self._realtime_exact_speech_uses_tts:
            try:
                opening_audio_path = await _live_opening_audio_path(
                    api_key=self._tts_api_key,
                    model=self._opening_model,
                    voice=self._tts_voice,
                    language=self._tts_language,
                    text=text,
                )
            except Exception as exc:
                opening_audio_path = None
                error_text = _safe_error_text(exc)
                logger.warning(
                    "[LiveKit] live opening audio cache failed; "
                    f"falling back to Gemini TTS: {error_text}"
                )
                await _append_livekit_run_event(
                    self._workflow_run_id,
                    {
                        "type": "opening_audio_cache_failed",
                        "level": "warning",
                        "message": error_text,
                        "fallback": "gemini_tts",
                        "model": self._opening_model,
                        "voice": self._tts_voice,
                        "language": self._tts_language,
                    },
                )

            if opening_audio_path:
                logger.info(
                    "[LiveKit] using cached live opening audio "
                    f"run_id={self._workflow_run_id} path={opening_audio_path.name!r}"
                )
                return self._say_text(
                    text,
                    allow_interruptions=allow_interruptions,
                    audio=_wav_audio_frames(opening_audio_path),
                )

        return await self._speak_text(text, allow_interruptions=allow_interruptions)

    def _schedule_shutdown(self, reason: str, *, delay: float = 0.0) -> None:
        deadline = time.monotonic() + delay
        if (
            self._shutdown_task
            and not self._shutdown_task.done()
            and self._shutdown_deadline is not None
            and self._shutdown_deadline <= deadline
        ):
            return

        if self._shutdown_task and not self._shutdown_task.done():
            self._shutdown_task.cancel()

        async def _shutdown_later() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._emit_feedback_now(
                    {
                        "type": "rtf-run-completed",
                        "payload": {"reason": reason},
                        "timestamp": time.time(),
                    }
                )
                await self._shutdown_call(reason)
            except asyncio.CancelledError:
                raise

        try:
            self._shutdown_deadline = deadline
            self._shutdown_task = asyncio.create_task(_shutdown_later())
        except RuntimeError:
            self._ctx.shutdown(reason)

    def set_recording_state(
        self, recording_state: post_call.LiveKitRecordingState | None
    ) -> None:
        self._recording_state = recording_state

    async def stop_recording(self) -> post_call.LiveKitRecordingState | None:
        async with self._recording_stop_lock:
            if self._recording_state is None:
                return None
            self._recording_state = await post_call.stop_livekit_room_recording(
                self._recording_state
            )
            return self._recording_state

    async def _shutdown_call(self, reason: str) -> None:
        await self.flush_feedback()
        recording_state = await self.stop_recording()
        if recording_state is not None:
            try:
                await db_client.update_workflow_run(
                    self._workflow_run_id,
                    recording_url=post_call.recording_available_url(recording_state),
                    gathered_context={"livekit_recording": recording_state.to_log()},
                )
            except Exception as exc:
                logger.warning(f"[LiveKit] failed to persist recording state: {exc}")
        delete_room = getattr(self._ctx, "delete_room", None)
        if callable(delete_room):
            try:
                result = delete_room()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning(f"[LiveKit] failed to delete room on shutdown: {exc}")
        self._ctx.shutdown(reason)

    def _say_text(
        self,
        text: str,
        *,
        allow_interruptions: bool = True,
        audio=None,
    ):
        if self._uses_realtime and not self._realtime_generate_reply_supported:
            allow_interruptions = True

        kwargs: dict[str, Any] = {
            "allow_interruptions": allow_interruptions,
            "add_to_chat_ctx": True,
        }
        if audio is not None:
            kwargs["audio"] = audio
        elif self._tts_api_key:
            kwargs["audio"] = _gemini_tts_audio_frames(
                api_key=self._tts_api_key,
                voice=self._tts_voice,
                language=self._tts_language,
                text=text,
            )
        try:
            return self.session.say(text, **kwargs)
        except RuntimeError as exc:
            logger.warning(f"[LiveKit] cannot speak text without TTS: {exc}")
            return None

    async def _record_node_transition(
        self, node: Node, previous_node: Node | None
    ) -> None:
        feedback = {
            "type": "rtf-node-transition",
            "payload": {
                "node_id": node.id,
                "node_name": node.name,
                "previous_node_id": previous_node.id if previous_node else None,
                "previous_node_name": previous_node.name if previous_node else None,
                "allow_interrupt": node.allow_interrupt,
            },
            "timestamp": time.time(),
        }
        await self._emit_feedback_now(feedback)
        await db_client.update_workflow_run(
            self._workflow_run_id,
            gathered_context={"nodes_visited": self._visited_nodes},
        )

    async def set_node(self, node_id: str) -> None:
        node = self._workflow.nodes[node_id]
        previous_node = self._current_node
        self._current_node = node
        if node.name not in self._visited_nodes:
            self._visited_nodes.append(node.name)

        if self._supports_mid_call_session_updates():
            post_opening_text = (
                self._start_opening_text
                if previous_node is not None
                and previous_node.is_start
                and not node.is_start
                else None
            )
            await self.update_instructions(
                self._compose_node_instructions(
                    node,
                    post_opening_text=post_opening_text,
                )
            )
            await self.update_tools(self._tools_for_node(node))
        else:
            logger.info(
                "[LiveKit] skipping realtime mid-call session update "
                f"run_id={self._workflow_run_id} node={node.id!r} "
                "because this realtime model would restart the audio session"
            )

        await self._record_node_transition(node, previous_node)

        if node.is_end:
            await self._complete_end_node(node)

    async def start_opening(self) -> None:
        start_node = self._workflow.nodes[self._workflow.start_node_id]
        previous_node = self._current_node
        self._current_node = start_node
        if start_node.name not in self._visited_nodes:
            self._visited_nodes.append(start_node.name)

        opening = self._start_opening_text
        if opening:
            speech = await self._speak_opening(
                opening,
                allow_interruptions=False,
            )
        else:
            speech = self.session.generate_reply(
                instructions=(
                    "Start the conversation now. Follow the current node instructions, "
                    "use the available tools when needed, and do not invent facts."
                ),
                allow_interruptions=start_node.allow_interrupt,
            )

        record_task = asyncio.create_task(
            self._record_node_transition(start_node, previous_node)
        )
        if opening:
            await record_task
            if speech:
                await speech.wait_for_playout()
            await self._auto_advance_after_opening(start_node)
        else:
            if speech:
                await speech.wait_for_playout()
            await record_task

    def _render_greeting(self, node: Node) -> str | None:
        if node.greeting_type == "audio" and node.greeting_recording_id:
            logger.warning(
                "LiveKit runtime does not yet play pre-recorded greetings; "
                "falling back to generated opening."
            )
            return None
        if node.greeting:
            return str(render_template(node.greeting, self._call_context_vars))
        return None

    async def _complete_end_node(self, node: Node) -> None:
        exact_text = _extract_exact_say_text(node.prompt)
        if (
            not exact_text
            and self._uses_realtime
            and not self._realtime_generate_reply_supported
        ):
            exact_text = DEFAULT_END_CALL_TEXT
        if exact_text:
            speech = await self._speak_text(
                exact_text,
                allow_interruptions=node.allow_interrupt,
            )
        else:
            kwargs: dict[str, Any] = {
                "instructions": (
                    "Close the conversation according to the current end node "
                    "instructions, then stop speaking."
                ),
                "allow_interruptions": node.allow_interrupt,
            }
            if self._realtime_tool_choice_supported:
                kwargs["tool_choice"] = "none"
            speech = self.session.generate_reply(**kwargs)
        if speech:
            await speech.wait_for_playout()
        await self._emit_feedback_now(
            {
                "type": "rtf-run-completed",
                "payload": {"reason": "workflow_end", "node_id": node.id},
                "timestamp": time.time(),
            }
        )
        await self._shutdown_call("workflow reached end node")


def _first_content_text(item) -> str:
    content = getattr(item, "content", None) or []
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif hasattr(part, "text"):
            parts.append(str(part.text))
    return " ".join(parts).strip()


def _message_metrics_payload(item) -> dict[str, float]:
    metrics = getattr(item, "metrics", None) or {}
    if not isinstance(metrics, dict):
        metrics = dict(metrics)
    metric_keys = {
        "transcription_delay",
        "end_of_turn_delay",
        "on_user_turn_completed_delay",
        "llm_node_ttft",
        "tts_node_ttfb",
        "playback_latency",
        "e2e_latency",
    }
    return {
        key: round(float(value), 4)
        for key, value in metrics.items()
        if isinstance(value, (int, float))
        and (key.endswith("_latency") or key in metric_keys)
    }


def _uses_realtime(user_config) -> bool:
    """Single source of truth for whether the worker runs in realtime mode.

    Mirrors ``_create_session``: realtime requires both the ``is_realtime`` flag
    and a populated ``realtime`` section. Using only ``is_realtime`` elsewhere
    produced a split-brain agent (realtime code paths over a pipeline session).
    """

    return bool(user_config.is_realtime and user_config.realtime is not None)


def _runtime_configuration_from_user_config(user_config) -> dict[str, str]:
    runtime_configuration: dict[str, str] = {
        "mode": "realtime" if _uses_realtime(user_config) else "pipeline",
    }
    if user_config.is_realtime and user_config.realtime:
        runtime_configuration["realtime_provider"] = _provider_value(
            user_config.realtime.provider
        )
        realtime_model = getattr(user_config.realtime, "model", None)
        if realtime_model:
            runtime_configuration["realtime_model"] = realtime_model
    if getattr(user_config, "llm", None):
        runtime_configuration["llm_provider"] = _provider_value(
            user_config.llm.provider
        )
        llm_model = getattr(user_config.llm, "model", None)
        if llm_model:
            runtime_configuration["llm_model"] = llm_model
    if getattr(user_config, "stt", None):
        runtime_configuration["stt_provider"] = _provider_value(
            user_config.stt.provider
        )
        stt_model = getattr(user_config.stt, "model", None)
        if stt_model:
            runtime_configuration["stt_model"] = stt_model
    if getattr(user_config, "tts", None):
        runtime_configuration["tts_provider"] = _provider_value(
            user_config.tts.provider
        )
        tts_model = getattr(user_config.tts, "model", None)
        if tts_model:
            runtime_configuration["tts_model"] = tts_model
    return runtime_configuration


def _model_usage_item_dict(item) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if isinstance(item, dict):
        return item
    return {
        key: getattr(item, key)
        for key in dir(item)
        if not key.startswith("_") and not callable(getattr(item, key, None))
    }


def _livekit_usage_info(session_usage, duration_seconds: int) -> dict[str, Any]:
    usage_info: dict[str, Any] = {
        "llm": {},
        "tts": {},
        "stt": {},
        "call_duration_seconds": duration_seconds,
    }
    model_usage = getattr(session_usage, "model_usage", None) or []
    for index, item in enumerate(model_usage):
        item_data = _model_usage_item_dict(item)
        usage_type = item_data.get("type")
        provider = str(item_data.get("provider") or "livekit").replace("-", "_")
        model = str(item_data.get("model") or "unknown")
        if usage_type == "llm_usage":
            input_tokens = int(item_data.get("input_tokens") or 0)
            output_tokens = int(item_data.get("output_tokens") or 0)
            total_tokens = int(item_data.get("total_tokens") or 0) or (
                input_tokens + output_tokens
            )
            if total_tokens <= 0:
                continue
            key = f"LiveKit{provider}LLMService#{index}|||{model}"
            usage_info["llm"][key] = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_input_tokens": int(
                    item_data.get("input_cached_tokens") or 0
                ),
                "cache_creation_input_tokens": 0,
                "input_audio_tokens": int(item_data.get("input_audio_tokens") or 0),
                "input_cached_audio_tokens": int(
                    item_data.get("input_cached_audio_tokens") or 0
                ),
                "input_text_tokens": int(item_data.get("input_text_tokens") or 0),
                "input_cached_text_tokens": int(
                    item_data.get("input_cached_text_tokens") or 0
                ),
                "output_audio_tokens": int(item_data.get("output_audio_tokens") or 0),
                "output_text_tokens": int(item_data.get("output_text_tokens") or 0),
                "session_duration": float(item_data.get("session_duration") or 0),
            }
        elif usage_type == "tts_usage":
            characters_count = int(item_data.get("characters_count") or 0)
            if characters_count <= 0:
                continue
            key = f"LiveKit{provider}TTSService#{index}|||{model}"
            usage_info["tts"][key] = characters_count
        elif usage_type == "stt_usage":
            audio_duration = float(item_data.get("audio_duration") or 0)
            if audio_duration <= 0:
                continue
            key = f"LiveKit{provider}STTService#{index}|||{model}"
            usage_info["stt"][key] = audio_duration
    return usage_info


async def _calculate_livekit_workflow_run_cost(workflow_run_id: int) -> None:
    from api.services.pricing.workflow_run_cost import calculate_workflow_run_cost

    await calculate_workflow_run_cost(workflow_run_id)


def _register_feedback_handlers(
    session: AgentSession, agent: LiveKitWorkflowAgent
) -> None:
    @session.on("user_state_changed")
    def _on_user_state_changed(ev: agents.UserStateChangedEvent) -> None:
        if ev.old_state == "speaking" and ev.new_state == "listening":
            agent._last_user_speech_end_at = ev.created_at

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev: agents.AgentStateChangedEvent) -> None:
        if ev.new_state != "speaking" or agent._last_user_speech_end_at is None:
            return
        latency_ms = max(0.0, (ev.created_at - agent._last_user_speech_end_at) * 1000)
        logger.info(
            "[LiveKit] agent speech latency "
            f"run_id={agent._workflow_run_id} "
            f"speech_start_after_user_speech_ms={latency_ms:.0f}"
        )
        agent._emit_feedback(
            {
                "type": "rtf-latency-measured",
                "payload": {"speech_start_after_user_speech_ms": round(latency_ms, 1)},
                "timestamp": ev.created_at,
            }
        )

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev: agents.UserInputTranscribedEvent) -> None:
        if ev.is_final:
            agent._last_final_transcript_at = ev.created_at
            agent._remember_final_user_transcript(ev.transcript)
            logger.info(
                "[LiveKit] user transcript "
                f"run_id={agent._workflow_run_id} text={ev.transcript!r}"
            )
            if _is_user_close_text(ev.transcript):
                missing = agent._missing_lead_fields()
                if missing:
                    logger.info(
                        "[LiveKit] caller close detected but lead fields are missing "
                        f"run_id={agent._workflow_run_id} missing={missing}"
                    )
                else:
                    agent._schedule_shutdown("caller requested end", delay=8.0)
        agent._emit_feedback(
            {
                "type": "rtf-user-transcription",
                "payload": {"text": ev.transcript, "final": ev.is_final},
                "timestamp": ev.created_at,
            }
        )

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev: agents.ConversationItemAddedEvent) -> None:
        item = ev.item
        role = getattr(item, "role", "")
        if role != "assistant":
            return
        text = _first_content_text(item)
        if not text:
            return
        logger.info(
            "[LiveKit] assistant text " f"run_id={agent._workflow_run_id} text={text!r}"
        )
        metrics = _message_metrics_payload(item)
        if agent._last_final_transcript_at is not None:
            metrics["assistant_after_final_transcript_ms"] = round(
                max(0.0, (ev.created_at - agent._last_final_transcript_at) * 1000),
                1,
            )
        if metrics:
            logger.info(
                "[LiveKit] assistant latency metrics "
                f"run_id={agent._workflow_run_id} metrics={metrics}"
            )
        if _is_assistant_close_text(text):
            missing = agent._missing_lead_fields()
            if missing:
                logger.info(
                    "[LiveKit] assistant close detected but lead fields are missing "
                    f"run_id={agent._workflow_run_id} missing={missing}"
                )
            elif (
                post_call.post_call_enabled()
                and not agent._recent_user_requested_close()
            ):
                logger.info(
                    "[LiveKit] assistant close detected without caller close intent "
                    f"run_id={agent._workflow_run_id} "
                    f"last_user={agent._last_final_transcript!r}"
                )
            else:
                agent._schedule_shutdown(
                    "assistant closed conversation",
                    delay=_shutdown_delay_for_text(text),
                )
        agent._emit_feedback(
            {
                "type": "rtf-bot-text",
                "payload": {"text": text, "metrics": metrics},
                "timestamp": ev.created_at,
            }
        )

    @session.on("function_tools_executed")
    def _on_function_tools_executed(ev: agents.FunctionToolsExecutedEvent) -> None:
        for call, output in zip(ev.function_calls, ev.function_call_outputs):
            logger.info(
                "[LiveKit] function tool completed "
                f"run_id={agent._workflow_run_id} name={call.name!r} "
                f"output={getattr(output, 'output', None)!r}"
            )
            agent._emit_feedback(
                {
                    "type": "rtf-function-call-end",
                    "payload": {
                        "tool_call_id": call.call_id,
                        "function_name": call.name,
                        "result": getattr(output, "output", None) if output else None,
                    },
                    "timestamp": ev.created_at,
                }
            )


async def _finalize_livekit_workflow_run(
    *,
    workflow_run_id: int,
    session: AgentSession,
    agent: LiveKitWorkflowAgent,
    room_name: str,
    reason: str,
) -> None:
    elapsed = int(time.monotonic() - agent._started_at)
    try:
        final_recording_state = await agent.stop_recording()
        livekit_history = session.history.to_dict()
        gathered_context = {
            "livekit_room": room_name,
            "livekit_shutdown_reason": reason,
        }
        recording_url = None
        if final_recording_state is not None:
            gathered_context["livekit_recording"] = final_recording_state.to_log()
            recording_url = post_call.recording_available_url(final_recording_state)

        existing_cost_info: dict[str, Any] = {}
        try:
            existing_run = await db_client.get_workflow_run_by_id(workflow_run_id)
            existing_cost_info = dict(getattr(existing_run, "cost_info", None) or {})
        except Exception as exc:
            logger.warning(
                f"[LiveKit] failed to load existing cost info before finalize: {exc}"
            )

        completed_run = await db_client.update_workflow_run(
            workflow_run_id,
            is_completed=True,
            state=WorkflowRunState.COMPLETED.value,
            recording_url=recording_url,
            usage_info=_livekit_usage_info(getattr(session, "usage", None), elapsed),
            cost_info={**existing_cost_info, "call_duration_seconds": elapsed},
            logs={"livekit_history": livekit_history},
            gathered_context=gathered_context,
        )
        try:
            await _calculate_livekit_workflow_run_cost(workflow_run_id)
            refreshed_run = await db_client.get_workflow_run_by_id(workflow_run_id)
            if refreshed_run is not None:
                completed_run = refreshed_run
        except Exception as exc:
            logger.error(
                f"Error calculating LiveKit workflow run cost for {workflow_run_id}: {exc}"
            )
        try:
            post_call_payload = post_call.build_post_call_payload(
                completed_run,
                duration_seconds=elapsed,
                logs=getattr(completed_run, "logs", None),
                recording_url=recording_url,
            )
            lead_context = post_call.lead_details_gathered_context(
                {
                    "district": post_call_payload.get("district"),
                    "town": post_call_payload.get("town"),
                    "looking_for": post_call_payload.get("looking_for"),
                    "customer_name": post_call_payload.get("customer_name"),
                    "remarks": post_call_payload.get("remarks"),
                }
            )
            webhook_result = await post_call.send_post_call_webhook(post_call_payload)
            await db_client.update_workflow_run(
                workflow_run_id,
                gathered_context=lead_context,
                logs={
                    "post_call_webhook": webhook_result,
                    "post_call_payload": post_call_payload,
                },
            )
        except Exception as exc:
            logger.error(f"Failed to send LiveKit post-call webhook: {exc}")
    except Exception as exc:
        logger.error(f"Failed to finalize LiveKit workflow run: {exc}")


def _google_realtime_generation_options(
    latency_profile: str | None,
) -> dict[str, Any]:
    if not _is_fast_latency_profile(latency_profile):
        return {"temperature": 0.1}
    return {
        "temperature": 0.0,
        "top_p": 0.8,
        "top_k": 20,
        "tool_response_scheduling": genai_types.FunctionResponseScheduling.INTERRUPT,
    }


def _silero_vad_options() -> dict[str, Any]:
    return {
        "sample_rate": 16000,
        "min_speech_duration": 0.04,
        "min_silence_duration": 0.2,
        "prefix_padding_duration": 0.18,
        "activation_threshold": 0.35,
    }


def _create_session(
    user_config,
    *,
    vad,
    latency_profile: str | None = None,
) -> AgentSession:
    if user_config.is_realtime and user_config.realtime is not None:
        realtime = user_config.realtime
        provider = _provider_value(realtime.provider)
        model = getattr(realtime, "model", "")
        thinking_config = _google_thinking_config(model)
        turn_detection_mode = "realtime_llm"
        if provider == ServiceProviders.OPENAI_REALTIME.value:
            realtime_llm = openai.realtime.RealtimeModel(
                model=model,
                voice=getattr(realtime, "voice", "alloy"),
                api_key=realtime.api_key,
            )
        elif provider == ServiceProviders.GOOGLE_REALTIME.value:
            kwargs: dict[str, Any] = {
                "model": model,
                "voice": getattr(realtime, "voice", "Puck"),
                "language": getattr(realtime, "language", "en"),
                "api_key": realtime.api_key,
                **_google_realtime_generation_options(latency_profile),
            }
            if _supports_realtime_generate_reply(provider, model):
                kwargs["realtime_input_config"] = _local_vad_realtime_input_config()
                turn_detection_mode = "vad"
            else:
                kwargs["realtime_input_config"] = (
                    _fast_server_vad_realtime_input_config(latency_profile)
                )
            if thinking_config:
                kwargs["thinking_config"] = genai_types.ThinkingConfig(
                    **thinking_config
                )
            realtime_llm = google.beta.realtime.RealtimeModel(
                **kwargs,
            )
        elif provider == ServiceProviders.GOOGLE_VERTEX_REALTIME.value:
            kwargs = {
                "model": model,
                "voice": getattr(realtime, "voice", "Charon"),
                "language": getattr(realtime, "language", "en-US"),
                "vertexai": True,
                "project": getattr(realtime, "project_id", None),
                "location": getattr(realtime, "location", "us-east4"),
                **_google_realtime_generation_options(latency_profile),
            }
            if _supports_realtime_generate_reply(provider, model):
                kwargs["realtime_input_config"] = _local_vad_realtime_input_config()
                turn_detection_mode = "vad"
            else:
                kwargs["realtime_input_config"] = (
                    _fast_server_vad_realtime_input_config(latency_profile)
                )
            if thinking_config:
                kwargs["thinking_config"] = genai_types.ThinkingConfig(
                    **thinking_config
                )
            realtime_llm = google.beta.realtime.RealtimeModel(
                **kwargs,
            )
        else:
            raise ValueError(f"LiveKit realtime provider is unsupported: {provider}")
        return AgentSession(
            llm=realtime_llm,
            vad=vad,
            **_session_latency_options(
                turn_detection_mode,
                latency_profile=latency_profile,
            ),
        )

    if not (user_config.llm and user_config.stt and user_config.tts):
        raise ValueError(
            "LiveKit non-realtime mode requires llm, stt, and tts configuration"
        )

    llm_provider = _provider_value(user_config.llm.provider)
    stt_provider = _provider_value(user_config.stt.provider)
    tts_provider = _provider_value(user_config.tts.provider)
    llm_model = user_config.llm.model

    if llm_provider == ServiceProviders.OPENAI.value:
        kwargs = {
            "model": llm_model,
            "api_key": user_config.llm.api_key,
            "base_url": getattr(user_config.llm, "base_url", None),
            "parallel_tool_calls": False,
        }
        if "gpt-5" in (llm_model or "").lower():
            kwargs["reasoning_effort"] = "minimal"
        else:
            kwargs["temperature"] = 0.1
        livekit_llm = openai.LLM(
            **kwargs,
        )
    elif llm_provider == ServiceProviders.GOOGLE.value:
        kwargs = {
            "model": llm_model,
            "api_key": user_config.llm.api_key,
            "temperature": 0.1,
        }
        thinking_config = _google_thinking_config(llm_model)
        if thinking_config:
            kwargs["thinking_config"] = thinking_config
        livekit_llm = google.LLM(
            **kwargs,
        )
    else:
        raise ValueError(f"LiveKit LLM provider is unsupported: {llm_provider}")

    if stt_provider == ServiceProviders.OPENAI.value:
        livekit_stt = openai.STT(
            model=user_config.stt.model,
            api_key=user_config.stt.api_key,
        )
    elif stt_provider == ServiceProviders.GOOGLE.value:
        livekit_stt = google.STT(
            model=user_config.stt.model,
            languages=getattr(user_config.stt, "language", "en-US"),
        )
    else:
        raise ValueError(f"LiveKit STT provider is unsupported: {stt_provider}")

    if tts_provider == ServiceProviders.OPENAI.value:
        livekit_tts = openai.TTS(
            model=user_config.tts.model,
            voice=getattr(user_config.tts, "voice", "alloy"),
            api_key=user_config.tts.api_key,
        )
    elif tts_provider == ServiceProviders.GOOGLE.value:
        livekit_tts = google.TTS(
            voice_name=getattr(user_config.tts, "voice", None),
            language=getattr(user_config.tts, "language", "en-US"),
        )
    else:
        raise ValueError(f"LiveKit TTS provider is unsupported: {tts_provider}")

    return AgentSession(
        stt=livekit_stt,
        llm=livekit_llm,
        tts=livekit_tts,
        vad=vad,
        **_session_latency_options("vad", latency_profile=latency_profile),
    )


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(**_silero_vad_options())


async def _resolve_or_create_workflow_run(
    metadata: dict[str, Any],
    participant_context: dict[str, Any],
) -> tuple[int, int, int, int, dict[str, Any]]:
    workflow_id = int(metadata["workflow_id"])
    user_id = int(metadata["user_id"])
    organization_id = int(metadata["organization_id"])
    call_type = metadata.get("call_type") or "web"
    workflow_run_id = metadata.get("workflow_run_id")
    initial_context = {
        **(metadata.get("initial_context") or {}),
        **{k: v for k, v in participant_context.items() if v not in (None, "", {})},
        "provider": WorkflowRunMode.LIVEKIT.value,
    }

    if workflow_run_id:
        return (
            workflow_id,
            int(workflow_run_id),
            user_id,
            organization_id,
            initial_context,
        )

    numeric_suffix = int(str(uuid.uuid4()).replace("-", "")[:8], 16) % 100000000
    workflow_run = await db_client.create_workflow_run(
        f"WR-LK-IN-{numeric_suffix:08d}",
        workflow_id,
        WorkflowRunMode.LIVEKIT.value,
        user_id=user_id,
        call_type=CallType.INBOUND if call_type == "inbound" else CallType.OUTBOUND,
        initial_context=initial_context,
        gathered_context={"call_id": participant_context.get("call_id")},
        use_draft=False,
        organization_id=organization_id,
    )
    return workflow_id, workflow_run.id, user_id, organization_id, initial_context


async def _record_livekit_startup_failure(
    workflow_run_id: int,
    exc: Exception,
    *,
    provider: str | None = None,
    model: str | None = None,
    user_config=None,
) -> None:
    """Make a startup failure visible from the workflow run, not only logs.

    Emits a ``startup_failed`` event into ``logs.livekit_events`` and records
    ``annotations.livekit_startup_error`` so the run UI can surface why a call
    never started. Safe to call from any startup failure path.
    """

    error_text = _safe_error_text(exc)
    logger.exception(
        "[LiveKit] workflow run startup failed "
        f"run_id={workflow_run_id} provider={provider} model={model}"
    )
    await _append_livekit_run_event(
        workflow_run_id,
        {
            "type": "startup_failed",
            "level": "error",
            "message": error_text,
            "provider": provider,
            "model": model,
            "is_realtime": bool(getattr(user_config, "is_realtime", False)),
        },
    )
    try:
        await db_client.update_workflow_run(
            workflow_run_id,
            is_completed=True,
            state=WorkflowRunState.COMPLETED.value,
            annotations={"livekit_startup_error": error_text},
        )
    except Exception as persist_exc:
        logger.warning(f"[LiveKit] failed to persist startup failure: {persist_exc}")


async def entrypoint(ctx: JobContext) -> None:
    metadata = _metadata_from_job(ctx)
    await ctx.connect()
    room_metadata = _metadata_from_room(ctx)
    if room_metadata:
        metadata = {**room_metadata, **metadata}
    logger.info(
        "[LiveKit] dispatch received "
        f"job_id={ctx.job.id} room={ctx.room.name} "
        f"workflow_id={metadata.get('workflow_id')} "
        f"call_type={metadata.get('call_type')}"
    )
    participant = await ctx.wait_for_participant()
    participant_context = _participant_context(participant)
    logger.info(
        "[LiveKit] SIP participant connected "
        f"room={ctx.room.name} "
        f"caller={participant_context.get('caller_number')} "
        f"called={participant_context.get('called_number')} "
        f"call_id={participant_context.get('call_id')}"
    )
    (
        workflow_id,
        workflow_run_id,
        user_id,
        organization_id,
        initial_context,
    ) = await _resolve_or_create_workflow_run(metadata, participant_context)

    await db_client.update_workflow_run(
        workflow_run_id,
        state=WorkflowRunState.RUNNING.value,
        initial_context=initial_context,
        gathered_context={"livekit_room": ctx.room.name},
    )

    # Resolve workflow/config before the call starts. A failure here (missing
    # workflow, invalid saved config, bad workflow JSON) must still be recorded
    # on the run instead of leaving it stuck in RUNNING with no signal.
    user_config = None
    realtime_provider = None
    realtime_model = None
    try:
        workflow_run_task = asyncio.create_task(
            db_client.get_workflow_run(
                workflow_run_id, organization_id=organization_id
            )
        )
        workflow_task = asyncio.create_task(
            db_client.get_workflow(workflow_id, organization_id=organization_id)
        )
        user_config_task = asyncio.create_task(
            db_client.get_user_configurations(user_id)
        )
        has_recordings_task = asyncio.create_task(
            db_client.has_active_recordings(organization_id)
        )

        workflow_run = await workflow_run_task
        if not workflow_run:
            raise ValueError(f"Workflow run {workflow_run_id} not found")

        workflow = await workflow_task
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")

        run_definition = workflow_run.definition
        run_configs = run_definition.workflow_configurations or {}
        latency_profile = run_configs.get("latency_profile")
        user_config = await user_config_task
        user_config = resolve_effective_config(
            user_config, run_configs.get("model_overrides")
        )
        uses_realtime = _uses_realtime(user_config)
        runtime_configuration = _runtime_configuration_from_user_config(user_config)
        if runtime_configuration:
            initial_context = {
                **initial_context,
                "runtime_configuration": runtime_configuration,
            }
            await db_client.update_workflow_run(
                workflow_run_id,
                initial_context=initial_context,
            )
        workflow_graph = WorkflowGraph(
            ReactFlowDTO.model_validate(run_definition.workflow_json)
        )
        embedding_settings = resolve_embedding_settings(user_config)
        has_recordings = await has_recordings_task
        realtime_provider = (
            _provider_value(user_config.realtime.provider)
            if user_config.is_realtime and user_config.realtime
            else None
        )
        realtime_model = (
            getattr(user_config.realtime, "model", None)
            if user_config.is_realtime and user_config.realtime
            else None
        )
        realtime_generate_reply_supported = _supports_realtime_generate_reply(
            realtime_provider, realtime_model
        )
        realtime_tts_api_key = (
            getattr(user_config.realtime, "api_key", None)
            if user_config.is_realtime and user_config.realtime
            else None
        )
    except Exception as exc:
        await _record_livekit_startup_failure(
            workflow_run_id,
            exc,
            provider=realtime_provider,
            model=realtime_model,
            user_config=user_config,
        )
        raise

    try:
        if user_config.is_realtime and user_config.realtime is None:
            raise ValueError(
                "Realtime mode is enabled but no realtime model is configured "
                "for this run. Configure a realtime model (e.g. Google Gemini "
                "realtime) or turn realtime mode off."
            )
        vad = getattr(ctx.proc, "userdata", {}).get("vad") or silero.VAD.load(
            **_silero_vad_options()
        )
        session = _create_session(
            user_config,
            vad=vad,
            latency_profile=latency_profile,
        )
        agent = LiveKitWorkflowAgent(
            ctx=ctx,
            workflow=workflow_graph,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            call_context_vars=initial_context,
            embeddings_api_key=embedding_settings.get("api_key"),
            embeddings_provider=embedding_settings.get("provider"),
            embeddings_model=embedding_settings.get("model"),
            embeddings_base_url=embedding_settings.get("base_url"),
            has_recordings=has_recordings,
            uses_realtime=uses_realtime,
            realtime_generate_reply_supported=realtime_generate_reply_supported,
            realtime_tool_choice_supported=_supports_realtime_tool_choice(
                realtime_provider
            ),
            realtime_exact_speech_uses_tts=(
                uses_realtime
                and not realtime_generate_reply_supported
                and realtime_provider == ServiceProviders.GOOGLE_REALTIME.value
                and bool(realtime_tts_api_key)
            ),
            tts_api_key=(
                realtime_tts_api_key
                if realtime_provider == ServiceProviders.GOOGLE_REALTIME.value
                else None
            ),
            opening_model=(
                getattr(user_config.realtime, "model", None)
                if user_config.is_realtime and user_config.realtime
                else None
            ),
            tts_voice=(
                getattr(user_config.realtime, "voice", None)
                if user_config.is_realtime and user_config.realtime
                else None
            )
            or "Leda",  # Gemini voice id, not the assistant name.
            tts_language=(
                getattr(user_config.realtime, "language", None)
                if user_config.is_realtime and user_config.realtime
                else None
            )
            or "te-IN",
            latency_profile=latency_profile,
        )
        _register_feedback_handlers(session, agent)
        agent.prewarm_opening_audio()
        try:
            recording_state = await asyncio.wait_for(
                post_call.start_livekit_room_recording(
                    room_name=ctx.room.name,
                    workflow_run_id=workflow_run_id,
                ),
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning(f"[LiveKit] recording start skipped: {exc}")
            recording_state = None
        agent.set_recording_state(recording_state)
        if recording_state is not None:
            try:
                await db_client.update_workflow_run(
                    workflow_run_id,
                    recording_url=post_call.recording_available_url(recording_state),
                    gathered_context={"livekit_recording": recording_state.to_log()},
                )
            except Exception as exc:
                logger.warning(f"[LiveKit] failed to persist recording start: {exc}")

        async def _shutdown(reason: str) -> None:
            await _finalize_livekit_workflow_run(
                workflow_run_id=workflow_run_id,
                session=session,
                agent=agent,
                room_name=ctx.room.name,
                reason=reason,
            )

        ctx.add_shutdown_callback(_shutdown)

        await session.start(agent=agent, room=ctx.room)
        await agent.start_opening()
    except Exception as exc:
        await _record_livekit_startup_failure(
            workflow_run_id,
            exc,
            provider=realtime_provider,
            model=realtime_model,
            user_config=user_config,
        )
        raise


if __name__ == "__main__":
    settings = effective_livekit_settings()
    import os

    os.environ.update(livekit_environment(settings))
    cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=settings.livekit_agent_name,
            num_idle_processes=2,
        )
    )
