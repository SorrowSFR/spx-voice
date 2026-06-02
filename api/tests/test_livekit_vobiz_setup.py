from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.routes import livekit
from api.services.livekit import vobiz as vobiz_service
from api.services.livekit.runtime_config import LiveKitRuntimeSettings


def _settings(**overrides):
    data = {
        "voice_runtime": "livekit",
        "livekit_url": "wss://old.livekit.cloud",
        "livekit_client_url": "wss://old.livekit.cloud",
        "livekit_api_key": "old-key",
        "livekit_api_secret": "old-secret",
        "livekit_agent_name": "spx-voice",
        "livekit_room_prefix": "spx-voice",
        "livekit_sip_inbound_host": "old.sip.livekit.cloud",
        "livekit_sip_max_call_duration_seconds": 1800,
        "source": "ui",
    }
    data.update(overrides)
    return LiveKitRuntimeSettings(**data)


@pytest.mark.asyncio
async def test_setup_vobiz_livekit_creates_config_and_assigns_inbound_workflow(
    monkeypatch,
):
    row = SimpleNamespace(
        id=44,
        name="Vobiz LiveKit",
        provider="vobiz",
        credentials={"auth_id": "MA_1"},
        organization_id=7,
        is_default_outbound=True,
    )
    phone_rows = [
        SimpleNamespace(id=1, is_active=True, inbound_workflow_id=None),
        SimpleNamespace(id=2, is_active=True, inbound_workflow_id=99),
        SimpleNamespace(id=3, is_active=False, inbound_workflow_id=None),
    ]
    db = SimpleNamespace(
        get_workflow=AsyncMock(return_value=SimpleNamespace(id=99)),
        list_telephony_configurations_by_provider=AsyncMock(return_value=[]),
        create_telephony_configuration=AsyncMock(return_value=row),
        set_default_telephony_configuration=AsyncMock(return_value=row),
        list_phone_numbers_for_config=AsyncMock(
            side_effect=[phone_rows, phone_rows],
        ),
        update_phone_number=AsyncMock(),
    )
    sync = AsyncMock(
        side_effect=[
            SimpleNamespace(ok=True, message=None, imported_phone_numbers=2),
            SimpleNamespace(ok=True, message=None, imported_phone_numbers=0),
        ]
    )
    save = Mock(
        return_value=_settings(
            livekit_url="wss://new.livekit.cloud",
            livekit_api_key="new-key",
            livekit_api_secret="new-secret",
            livekit_sip_inbound_host="sip.new.livekit.cloud",
        )
    )

    monkeypatch.setattr(livekit, "db_client", db)
    monkeypatch.setattr(livekit, "effective_livekit_settings", lambda: _settings())
    monkeypatch.setattr(livekit, "save_livekit_settings", save)
    monkeypatch.setattr(livekit, "apply_livekit_worker_settings", Mock())
    monkeypatch.setattr(livekit, "sync_vobiz_livekit_config", sync)
    monkeypatch.setattr(
        livekit,
        "get_worker_status",
        lambda: SimpleNamespace(
            managed_by_api=True,
            running=True,
            pid=123,
            message=None,
        ),
    )

    response = await livekit.setup_vobiz_livekit(
        livekit.VobizLiveKitSetupRequest(
            livekit_url="wss://new.livekit.cloud",
            livekit_client_url="",
            livekit_api_key="new-key",
            livekit_api_secret="new-secret",
            livekit_sip_inbound_host="sip.new.livekit.cloud",
            config_name="Vobiz LiveKit",
            vobiz_auth_id="MA_1",
            vobiz_auth_token="token",
            phone_numbers=["0000000000"],
            inbound_workflow_id=99,
        ),
        user=SimpleNamespace(selected_organization_id=7),
    )

    assert response.telephony_config_id == 44
    assert response.telephony_config_created is True
    assert response.imported_phone_numbers == 2
    assert response.active_phone_numbers == 2
    assert response.inbound_workflow_id == 99
    assert response.sync_ok is True
    assert response.sync_message == "Attached inbound workflow to 1 number."
    save.assert_called_once()
    assert save.call_args.args[0]["voice_runtime"] == "livekit"
    assert save.call_args.args[0]["livekit_client_url"] == "wss://new.livekit.cloud"
    db.create_telephony_configuration.assert_awaited_once()
    db.update_phone_number.assert_awaited_once_with(
        1,
        44,
        inbound_workflow_id=99,
    )
    assert sync.await_count == 2
    assert sync.await_args_list[0].kwargs == {
        "config_id": 44,
        "organization_id": 7,
        "import_phone_numbers": True,
        "requested_phone_numbers": ["0000000000"],
    }
    assert sync.await_args_list[1].kwargs == {
        "config_id": 44,
        "organization_id": 7,
    }


@pytest.mark.asyncio
async def test_setup_vobiz_livekit_defaults_to_starter_workflow(monkeypatch):
    row = SimpleNamespace(
        id=45,
        name="Vobiz LiveKit",
        provider="vobiz",
        credentials={"auth_id": "MA_1"},
        organization_id=7,
        is_default_outbound=True,
    )
    default_workflow = SimpleNamespace(id=99, name="Default Voice Assistant")
    phone_rows = [
        SimpleNamespace(id=1, is_active=True, inbound_workflow_id=None),
        SimpleNamespace(id=2, is_active=True, inbound_workflow_id=None),
    ]
    db = SimpleNamespace(
        get_all_workflows_for_listing=AsyncMock(return_value=[default_workflow]),
        list_telephony_configurations_by_provider=AsyncMock(return_value=[]),
        create_telephony_configuration=AsyncMock(return_value=row),
        set_default_telephony_configuration=AsyncMock(return_value=row),
        list_phone_numbers_for_config=AsyncMock(
            side_effect=[phone_rows, phone_rows],
        ),
        update_phone_number=AsyncMock(),
    )
    sync = AsyncMock(
        side_effect=[
            SimpleNamespace(ok=True, message=None, imported_phone_numbers=2),
            SimpleNamespace(ok=True, message=None, imported_phone_numbers=0),
        ]
    )

    monkeypatch.setattr(livekit, "db_client", db)
    monkeypatch.setattr(livekit, "effective_livekit_settings", lambda: _settings())
    monkeypatch.setattr(livekit, "save_livekit_settings", Mock(return_value=_settings()))
    monkeypatch.setattr(livekit, "apply_livekit_worker_settings", Mock())
    monkeypatch.setattr(livekit, "sync_vobiz_livekit_config", sync)
    ensure_default = AsyncMock()
    monkeypatch.setattr(
        livekit,
        "ensure_default_workflow_for_organization",
        ensure_default,
    )
    monkeypatch.setattr(
        livekit,
        "get_worker_status",
        lambda: SimpleNamespace(
            managed_by_api=True,
            running=True,
            pid=123,
            message=None,
        ),
    )

    response = await livekit.setup_vobiz_livekit(
        livekit.VobizLiveKitSetupRequest(
            livekit_url="wss://old.livekit.cloud",
            livekit_api_key="old-key",
            livekit_api_secret="old-secret",
            livekit_sip_inbound_host="old.sip.livekit.cloud",
            config_name="Vobiz LiveKit",
            vobiz_auth_id="MA_1",
            vobiz_auth_token="token",
        ),
        user=SimpleNamespace(id=5, selected_organization_id=7),
    )

    assert response.inbound_workflow_id == 99
    assert response.sync_message == "Attached inbound workflow to 2 numbers."
    ensure_default.assert_awaited_once_with(user_id=5, organization_id=7)
    db.get_all_workflows_for_listing.assert_awaited_once_with(
        organization_id=7,
        status="active",
    )
    assert db.update_phone_number.await_args_list[0].args == (1, 45)
    assert db.update_phone_number.await_args_list[0].kwargs == {
        "inbound_workflow_id": 99
    }
    assert db.update_phone_number.await_args_list[1].args == (2, 45)
    assert db.update_phone_number.await_args_list[1].kwargs == {
        "inbound_workflow_id": 99
    }
    assert sync.await_count == 2


@pytest.mark.asyncio
async def test_setup_vobiz_livekit_updates_existing_same_account(monkeypatch):
    existing = SimpleNamespace(
        id=12,
        name="Old",
        provider="vobiz",
        organization_id=7,
        is_default_outbound=False,
        credentials={
            "auth_id": "MA_EXISTING",
            "auth_token": "old",
            "livekit_sip_outbound_trunk_id": "ST_old",
            "vobiz_sip_password": "saved-password",
        },
    )
    updated = SimpleNamespace(
        **{
            **existing.__dict__,
            "name": "Renamed",
            "credentials": {
                **existing.credentials,
                "auth_token": "new-token",
            },
        }
    )
    db = SimpleNamespace(
        list_telephony_configurations_by_provider=AsyncMock(return_value=[existing]),
        update_telephony_configuration=AsyncMock(return_value=updated),
        set_default_telephony_configuration=AsyncMock(return_value=updated),
        list_phone_numbers_for_config=AsyncMock(return_value=[]),
    )

    monkeypatch.setattr(livekit, "db_client", db)
    monkeypatch.setattr(livekit, "effective_livekit_settings", lambda: _settings())
    monkeypatch.setattr(livekit, "save_livekit_settings", Mock(return_value=_settings()))
    monkeypatch.setattr(livekit, "apply_livekit_worker_settings", Mock())
    monkeypatch.setattr(
        livekit,
        "sync_vobiz_livekit_config",
        AsyncMock(return_value=SimpleNamespace(ok=True, message=None, imported_phone_numbers=0)),
    )
    monkeypatch.setattr(
        livekit,
        "get_worker_status",
        lambda: SimpleNamespace(
            managed_by_api=True,
            running=True,
            pid=123,
            message=None,
        ),
    )

    response = await livekit.setup_vobiz_livekit(
        livekit.VobizLiveKitSetupRequest(
            livekit_url="wss://old.livekit.cloud",
            livekit_api_key="old-key",
            livekit_sip_inbound_host="old.sip.livekit.cloud",
            config_name="Renamed",
            vobiz_auth_id="MA_EXISTING",
            vobiz_auth_token="new-token",
        ),
        user=SimpleNamespace(selected_organization_id=7),
    )

    assert response.telephony_config_id == 12
    assert response.telephony_config_created is False
    update_kwargs = db.update_telephony_configuration.await_args.kwargs
    assert update_kwargs["credentials"]["auth_token"] == "new-token"
    assert update_kwargs["credentials"]["livekit_sip_outbound_trunk_id"] == "ST_old"
    assert update_kwargs["credentials"]["vobiz_sip_password"] == "saved-password"


@pytest.mark.asyncio
async def test_setup_vobiz_livekit_can_skip_livekit_sip_provisioning(monkeypatch):
    row = SimpleNamespace(
        id=55,
        name="Vobiz Local",
        provider="vobiz",
        credentials={"auth_id": "MA_1", "auth_token": "token"},
        organization_id=7,
        is_default_outbound=True,
    )
    phone_rows = [SimpleNamespace(id=1, is_active=True, inbound_workflow_id=None)]
    db = SimpleNamespace(
        get_workflow=AsyncMock(return_value=SimpleNamespace(id=99)),
        list_telephony_configurations_by_provider=AsyncMock(return_value=[]),
        create_telephony_configuration=AsyncMock(return_value=row),
        set_default_telephony_configuration=AsyncMock(return_value=row),
        list_phone_numbers_for_config=AsyncMock(
            side_effect=[phone_rows, phone_rows],
        ),
        update_phone_number=AsyncMock(),
    )
    save = Mock()
    apply_worker = Mock()
    sync = AsyncMock()
    import_numbers = AsyncMock(return_value=1)

    monkeypatch.setattr(livekit, "db_client", db)
    monkeypatch.setattr(livekit, "effective_livekit_settings", lambda: _settings())
    monkeypatch.setattr(livekit, "save_livekit_settings", save)
    monkeypatch.setattr(livekit, "apply_livekit_worker_settings", apply_worker)
    monkeypatch.setattr(livekit, "sync_vobiz_livekit_config", sync)
    monkeypatch.setattr(livekit, "import_vobiz_phone_numbers", import_numbers)
    monkeypatch.setattr(
        livekit,
        "get_worker_status",
        lambda: SimpleNamespace(
            managed_by_api=True,
            running=True,
            pid=123,
            message=None,
        ),
    )

    response = await livekit.setup_vobiz_livekit(
        livekit.VobizLiveKitSetupRequest(
            provision_livekit_sip=False,
            config_name="Vobiz Local",
            vobiz_auth_id="MA_1",
            vobiz_auth_token="token",
            phone_numbers=["+9100000000"],
            inbound_workflow_id=99,
        ),
        user=SimpleNamespace(selected_organization_id=7),
    )

    assert response.telephony_config_id == 55
    assert response.telephony_config_created is True
    assert response.imported_phone_numbers == 1
    assert response.sync_ok is True
    assert response.sync_message == (
        "Vobiz config saved locally; LiveKit SIP provisioning skipped. "
        "Attached inbound workflow to 1 number."
    )
    save.assert_not_called()
    apply_worker.assert_not_called()
    sync.assert_not_awaited()
    import_numbers.assert_awaited_once_with(
        config_id=55,
        organization_id=7,
        credentials=row.credentials,
        requested_phone_numbers=["+9100000000"],
    )
    db.update_phone_number.assert_awaited_once_with(
        1,
        55,
        inbound_workflow_id=99,
    )


@pytest.mark.asyncio
async def test_setup_vobiz_livekit_reports_sync_failure_without_500(monkeypatch):
    row = SimpleNamespace(
        id=56,
        name="Vobiz LiveKit",
        provider="vobiz",
        credentials={"auth_id": "MA_1", "auth_token": "token"},
        organization_id=7,
        is_default_outbound=True,
    )
    db = SimpleNamespace(
        list_telephony_configurations_by_provider=AsyncMock(return_value=[]),
        create_telephony_configuration=AsyncMock(return_value=row),
        set_default_telephony_configuration=AsyncMock(return_value=row),
        list_phone_numbers_for_config=AsyncMock(return_value=[]),
    )

    async def sync_failure(**kwargs):
        raise RuntimeError("401 Unauthorized from LiveKit")

    monkeypatch.setattr(livekit, "db_client", db)
    monkeypatch.setattr(livekit, "effective_livekit_settings", lambda: _settings())
    monkeypatch.setattr(livekit, "save_livekit_settings", Mock(return_value=_settings()))
    monkeypatch.setattr(livekit, "apply_livekit_worker_settings", Mock())
    monkeypatch.setattr(livekit, "sync_vobiz_livekit_config", sync_failure)
    monkeypatch.setattr(
        livekit,
        "get_worker_status",
        lambda: SimpleNamespace(
            managed_by_api=True,
            running=True,
            pid=123,
            message=None,
        ),
    )

    response = await livekit.setup_vobiz_livekit(
        livekit.VobizLiveKitSetupRequest(
            livekit_url="wss://old.livekit.cloud",
            livekit_api_key="old-key",
            livekit_sip_inbound_host="old.sip.livekit.cloud",
            config_name="Vobiz LiveKit",
            vobiz_auth_id="MA_1",
            vobiz_auth_token="token",
        ),
        user=SimpleNamespace(selected_organization_id=7),
    )

    assert response.sync_ok is False
    assert "LiveKit rejected the API key/secret" in response.sync_message


@pytest.mark.asyncio
async def test_import_vobiz_phone_numbers_adds_missing_numbers_on_rerun(monkeypatch):
    db = SimpleNamespace(
        list_phone_numbers_for_config=AsyncMock(
            return_value=[
                SimpleNamespace(address="+910000000000", address_normalized="+910000000000")
            ],
        ),
        create_phone_number=AsyncMock(),
    )
    monkeypatch.setattr(vobiz_service, "db_client", db)
    monkeypatch.setattr(
        vobiz_service,
        "list_vobiz_account_phone_numbers",
        AsyncMock(return_value=["+910000000000", "+910000000001"]),
    )

    imported = await vobiz_service._import_vobiz_phone_numbers(
        config_id=44,
        organization_id=7,
        credentials={"auth_id": "MA_1", "auth_token": "token"},
        requested_phone_numbers=[],
    )

    assert imported == 1
    db.create_phone_number.assert_awaited_once_with(
        organization_id=7,
        telephony_configuration_id=44,
        address="+910000000001",
        country_code=None,
        is_default_caller_id=False,
    )


@pytest.mark.asyncio
async def test_sync_livekit_dispatch_rules_deletes_stale_rule_before_recreate(
    monkeypatch,
):
    calls = []

    async def delete_rule(request):
        calls.append(("delete", request.sip_dispatch_rule_id))

    class FakeLiveKitAPI:
        def __init__(self, *_args, **_kwargs):
            self.sip = SimpleNamespace(delete_sip_dispatch_rule=delete_rule)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def ensure_rule(_lkapi, **kwargs):
        calls.append(("ensure", kwargs["workflow_id"]))
        return {
            "sip_dispatch_rule_id": "new-rule",
            "workflow_id": kwargs["workflow_id"],
            "inbound_numbers": kwargs["inbound_numbers"],
            "target_numbers": kwargs["target_numbers"],
            "room_prefix": "spx-voice-wf-2-",
        }

    monkeypatch.setattr(vobiz_service.livekit_api, "LiveKitAPI", FakeLiveKitAPI)
    monkeypatch.setattr(vobiz_service, "_ensure_livekit_dispatch_rule", ensure_rule)
    monkeypatch.setattr(vobiz_service, "effective_livekit_settings", lambda: _settings())
    monkeypatch.setattr(
        vobiz_service,
        "db_client",
        SimpleNamespace(
            get_workflow=AsyncMock(return_value=SimpleNamespace(user_id=5)),
        ),
    )

    result = await vobiz_service._sync_livekit_dispatch_rules(
        credentials={
            "livekit_sip_inbound_trunk_id": "inbound-trunk",
            "livekit_sip_dispatch_rules": {
                "1": {"sip_dispatch_rule_id": "old-rule"}
            },
        },
        phone_rows=[
            SimpleNamespace(
                is_active=True,
                inbound_workflow_id=2,
                address_normalized="+918037565232",
            )
        ],
        organization_id=7,
        telephony_configuration_id=44,
        config_name="Vobiz LiveKit",
    )

    assert calls == [("delete", "old-rule"), ("ensure", 2)]
    assert result == {
        "2": {
            "sip_dispatch_rule_id": "new-rule",
            "workflow_id": 2,
            "inbound_numbers": ["08037565232"],
            "target_numbers": ["08037565232"],
            "room_prefix": "spx-voice-wf-2-",
        }
    }
