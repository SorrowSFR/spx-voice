from types import SimpleNamespace

import pytest

from api.services.workflow import templates as templates_module
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.templates import (
    DEFAULT_WORKFLOW_TEMPLATES,
    build_default_workflow_templates,
    ensure_default_workflow_templates,
)
from api.services.workflow.workflow_graph import WorkflowGraph


def test_templates_have_unique_names_and_descriptions():
    names = [t["name"] for t in DEFAULT_WORKFLOW_TEMPLATES]
    assert len(names) == len(set(names)), "template names must be unique"
    assert len(DEFAULT_WORKFLOW_TEMPLATES) >= 3
    for template in DEFAULT_WORKFLOW_TEMPLATES:
        assert template["name"].strip()
        assert template["description"].strip()


@pytest.mark.parametrize(
    "template", DEFAULT_WORKFLOW_TEMPLATES, ids=lambda t: t["name"]
)
def test_template_definition_is_a_valid_graph(template):
    dto = ReactFlowDTO.model_validate(template["definition"])
    graph = WorkflowGraph(dto)

    # Exactly one start node, and a terminal end node reachable.
    assert graph.start_node_id is not None
    node_types = {node.node_type for node in graph.nodes.values()}
    assert "startCall" in node_types
    assert "endCall" in node_types


@pytest.mark.asyncio
async def test_ensure_default_templates_seeds_only_missing(monkeypatch):
    created: list[str] = []

    existing = [SimpleNamespace(template_name="Customer Support Assistant")]

    class FakeClient:
        async def get_all_workflow_templates(self):
            return existing

        async def create_workflow_template(
            self, template_name, template_description, template_json
        ):
            created.append(template_name)
            return SimpleNamespace(template_name=template_name)

    monkeypatch.setattr(templates_module, "WorkflowTemplateClient", FakeClient)

    count = await ensure_default_workflow_templates()

    all_names = {t["name"] for t in build_default_workflow_templates()}
    # Every template except the already-present one is created exactly once.
    assert set(created) == all_names - {"Customer Support Assistant"}
    assert count == len(all_names) - 1
    assert "Customer Support Assistant" not in created


@pytest.mark.asyncio
async def test_ensure_default_templates_is_idempotent(monkeypatch):
    created: list[str] = []
    all_names = {t["name"] for t in build_default_workflow_templates()}

    class FakeClient:
        async def get_all_workflow_templates(self):
            return [SimpleNamespace(template_name=name) for name in all_names]

        async def create_workflow_template(self, **kwargs):  # pragma: no cover
            created.append(kwargs.get("template_name"))

    monkeypatch.setattr(templates_module, "WorkflowTemplateClient", FakeClient)

    count = await ensure_default_workflow_templates()

    assert count == 0
    assert created == []
