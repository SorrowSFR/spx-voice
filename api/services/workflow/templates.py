from __future__ import annotations

"""Pre-built starter workflow templates.

These are simple, generic, ready-to-use voice-agent graphs that appear in the
"Create Agent" template picker so a new user can start from a working flow
instead of a blank canvas. Templates are global (not org-scoped) and seeded
idempotently on app startup via ``ensure_default_workflow_templates``.

Every template definition must validate through ``ReactFlowDTO`` and
``WorkflowGraph`` (see ``tests/test_workflow_templates.py``):

- exactly one ``startCall`` node with ``is_start = true``;
- ``agentNode`` / ``endCall`` nodes need a non-empty ``prompt``;
- ``endCall`` nodes are terminal (no outgoing edges);
- every edge needs meaningful ``label`` and ``condition`` text.
"""

import copy
from typing import Any

from loguru import logger

from api.db import db_client
from api.db.workflow_template_client import WorkflowTemplateClient


def _start_node(node_id: str, *, name: str, greeting: str, prompt: str, x: int) -> dict:
    return {
        "id": node_id,
        "type": "startCall",
        "position": {"x": x, "y": 0},
        "data": {
            "name": name,
            "greeting_type": "text",
            "greeting": greeting,
            "prompt": prompt,
            "is_start": True,
            "allow_interrupt": True,
            "add_global_prompt": False,
        },
    }


def _agent_node(node_id: str, *, name: str, prompt: str, x: int) -> dict:
    return {
        "id": node_id,
        "type": "agentNode",
        "position": {"x": x, "y": 0},
        "data": {
            "name": name,
            "prompt": prompt,
            "allow_interrupt": True,
            "add_global_prompt": False,
        },
    }


def _end_node(node_id: str, *, name: str, prompt: str, x: int) -> dict:
    return {
        "id": node_id,
        "type": "endCall",
        "position": {"x": x, "y": 0},
        "data": {
            "name": name,
            "prompt": prompt,
            "is_end": True,
            "add_global_prompt": False,
        },
    }


def _edge(source: str, target: str, *, label: str, condition: str) -> dict:
    return {
        "id": f"{source}-{target}",
        "source": source,
        "target": target,
        "data": {"label": label, "condition": condition},
    }


# Each entry: (name, description, definition). Definitions use a left-to-right
# layout so nodes do not overlap when first opened in the editor.
DEFAULT_WORKFLOW_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "Customer Support Assistant",
        "description": (
            "Inbound agent that greets callers, answers questions clearly, and "
            "politely wraps up. A great starting point for support and FAQ lines."
        ),
        "definition": {
            "nodes": [
                _start_node(
                    "start",
                    name="Start",
                    greeting="Hi, thanks for calling. How can I help you today?",
                    prompt=(
                        "Greet the caller warmly in one short sentence and ask how "
                        "you can help. Then wait for their request."
                    ),
                    x=0,
                ),
                _agent_node(
                    "support",
                    name="Support",
                    prompt=(
                        "You are a friendly, professional customer-support voice "
                        "assistant. Answer the caller's question clearly and "
                        "concisely using only information you are sure about. Ask "
                        "one clarifying question at a time when you need more "
                        "detail. If you do not know something, say so and offer to "
                        "take a message or connect them to a human. Never invent "
                        "prices, policies, account details, or promises."
                    ),
                    x=360,
                ),
                _end_node(
                    "end",
                    name="End",
                    prompt="Say exactly: Thanks for calling. Have a great day. Goodbye.",
                    x=720,
                ),
            ],
            "edges": [
                _edge(
                    "start",
                    "support",
                    label="Start helping",
                    condition="After greeting the caller, begin assisting them.",
                ),
                _edge(
                    "support",
                    "end",
                    label="End call",
                    condition=(
                        "When the caller's questions are answered or they ask to "
                        "end the call."
                    ),
                ),
            ],
        },
    },
    {
        "name": "Appointment Scheduling",
        "description": (
            "Inbound agent that books an appointment: collects the caller's "
            "details, reads them back to confirm, then closes the call."
        ),
        "definition": {
            "nodes": [
                _start_node(
                    "start",
                    name="Start",
                    greeting="Hi, thanks for calling. I can help you book an appointment.",
                    prompt=(
                        "Greet the caller in one short sentence and let them know "
                        "you can help schedule an appointment."
                    ),
                    x=0,
                ),
                _agent_node(
                    "collect",
                    name="Collect details",
                    prompt=(
                        "Help the caller schedule an appointment. Collect, one at a "
                        "time: their full name, their preferred date and time, and "
                        "a callback phone number. Repeat each detail back as you go "
                        "and be patient and friendly."
                    ),
                    x=360,
                ),
                _agent_node(
                    "confirm",
                    name="Confirm",
                    prompt=(
                        "Read the full appointment details back to the caller — "
                        "name, date and time, and phone number — and ask them to "
                        "confirm. If anything is wrong, correct it before finishing."
                    ),
                    x=720,
                ),
                _end_node(
                    "end",
                    name="End",
                    prompt=(
                        "Say exactly: Your appointment is booked. Thanks for "
                        "calling, and goodbye."
                    ),
                    x=1080,
                ),
            ],
            "edges": [
                _edge(
                    "start",
                    "collect",
                    label="Begin booking",
                    condition="After greeting, start collecting appointment details.",
                ),
                _edge(
                    "collect",
                    "confirm",
                    label="Confirm details",
                    condition=(
                        "Once name, date and time, and phone number are collected, "
                        "confirm them with the caller."
                    ),
                ),
                _edge(
                    "confirm",
                    "end",
                    label="Finish",
                    condition="After the caller confirms the details are correct.",
                ),
            ],
        },
    },
    {
        "name": "Lead Qualification (Outbound)",
        "description": (
            "Outbound agent that introduces itself, checks if it's a good time, "
            "asks a few qualifying questions, and offers a follow-up."
        ),
        "definition": {
            "nodes": [
                _start_node(
                    "start",
                    name="Start",
                    greeting="Hi, this is the team calling. Do you have a quick minute?",
                    prompt=(
                        "Politely introduce yourself, say you're calling to see if "
                        "your solution might be a fit, and ask if it's a good time "
                        "to talk. If it isn't, offer to call back later."
                    ),
                    x=0,
                ),
                _agent_node(
                    "qualify",
                    name="Qualify",
                    prompt=(
                        "You are a polite outbound sales-development voice "
                        "assistant. Qualify the prospect by asking, one at a time: "
                        "what challenge they are trying to solve, their team size "
                        "or scale, and their timeline for a decision. Acknowledge "
                        "each answer and never be pushy. If they are interested, "
                        "offer to schedule a follow-up with a human representative."
                    ),
                    x=360,
                ),
                _end_node(
                    "end",
                    name="End",
                    prompt=(
                        "Say exactly: Thanks for your time. We'll follow up soon. "
                        "Goodbye."
                    ),
                    x=720,
                ),
            ],
            "edges": [
                _edge(
                    "start",
                    "qualify",
                    label="Start qualifying",
                    condition="If the caller has time, begin the qualifying questions.",
                ),
                _edge(
                    "qualify",
                    "end",
                    label="End call",
                    condition=(
                        "When qualification is complete or the prospect declines."
                    ),
                ),
            ],
        },
    },
    {
        "name": "Virtual Receptionist",
        "description": (
            "Inbound agent that greets callers, finds out what they need, captures "
            "their name and reason, and wraps up — ideal for routing front desks."
        ),
        "definition": {
            "nodes": [
                _start_node(
                    "start",
                    name="Start",
                    greeting="Hello, thanks for calling. How can I direct your call?",
                    prompt=(
                        "Greet the caller in one short sentence and ask how you can "
                        "direct their call."
                    ),
                    x=0,
                ),
                _agent_node(
                    "triage",
                    name="Triage",
                    prompt=(
                        "You are a professional virtual receptionist. Find out what "
                        "the caller needs (for example sales, support, billing, or "
                        "something else) with one short question. Capture their "
                        "name and a brief reason for the call, acknowledge it, and "
                        "let them know you'll pass it along or connect them. Be warm "
                        "and efficient."
                    ),
                    x=360,
                ),
                _end_node(
                    "end",
                    name="End",
                    prompt="Say exactly: Thank you, I'll pass that along. Goodbye.",
                    x=720,
                ),
            ],
            "edges": [
                _edge(
                    "start",
                    "triage",
                    label="Identify need",
                    condition="After greeting, find out what the caller needs.",
                ),
                _edge(
                    "triage",
                    "end",
                    label="Wrap up",
                    condition="Once the caller's need and details are captured.",
                ),
            ],
        },
    },
]


def build_default_workflow_templates() -> list[dict[str, Any]]:
    """Return deep copies of the built-in template definitions."""

    return copy.deepcopy(DEFAULT_WORKFLOW_TEMPLATES)


async def ensure_default_workflow_templates() -> int:
    """Insert any built-in templates that are not already stored, by name.

    Idempotent: safe to call on every app start. Returns the number of templates
    created.
    """

    client = WorkflowTemplateClient()
    try:
        existing = await client.get_all_workflow_templates()
    except Exception as exc:  # pragma: no cover - defensive: never block startup
        logger.warning(f"Could not load workflow templates for seeding: {exc}")
        return 0

    existing_names = {template.template_name for template in existing}
    created = 0
    for template in build_default_workflow_templates():
        if template["name"] in existing_names:
            continue
        try:
            await client.create_workflow_template(
                template_name=template["name"],
                template_description=template["description"],
                template_json=template["definition"],
            )
            created += 1
        except Exception as exc:  # pragma: no cover - one bad insert shouldn't abort
            logger.warning(
                f"Failed to seed workflow template {template['name']!r}: {exc}"
            )

    if created:
        logger.info(f"Seeded {created} default workflow template(s)")
    return created


__all__ = [
    "DEFAULT_WORKFLOW_TEMPLATES",
    "build_default_workflow_templates",
    "ensure_default_workflow_templates",
]
