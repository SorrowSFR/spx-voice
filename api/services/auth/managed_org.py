from api.constants import MANAGED_ORGANIZATION_PROVIDER_ID
from api.db import db_client
from api.db.models import OrganizationModel, UserModel


async def assign_user_to_managed_organization(
    user: UserModel,
) -> tuple[OrganizationModel, bool]:
    """Attach a user to the single managed organization used in OSS mode."""
    (
        organization,
        was_created,
    ) = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id=MANAGED_ORGANIZATION_PROVIDER_ID,
        user_id=user.id,
    )

    await db_client.add_user_to_organization(user.id, organization.id)

    if user.selected_organization_id != organization.id:
        await db_client.update_user_selected_organization(user.id, organization.id)
        user.selected_organization_id = organization.id

    return organization, was_created
