from django.apps import AppConfig


class ContactsConfig(AppConfig):
    name = "apps.contacts"
    label = "contacts"

    def ready(self) -> None:
        from apps.access.scopes import Everyone, FieldScopes, register
        from apps.contacts.models import Contact, Tag

        # ADR-003 §5. `OWN`: el responsable o quien lo creó; `BRANCH`: su sucursal. `TEAM` no
        # amplía nada más allá de `OWN` hasta que el mantenedor decida D-F4-01 (phase-4.md):
        # el contacto no tiene `team_id`.
        register(
            Contact, FieldScopes(own=("owner_user_id", "created_by_user_id"), branch="branch_id")
        )
        # Las etiquetas son el catálogo de la organización (F4-11): con `contacts.view`, cualquier
        # alcance las ve todas. El alcance decide qué contactos se etiquetan, no qué etiquetas.
        register(Tag, Everyone())
