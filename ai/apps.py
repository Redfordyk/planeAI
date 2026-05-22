from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ai"
    label = "ai"
    verbose_name = "Plane AI add-on"

    def ready(self) -> None:
        # Import-time side effects on Django's `apps.ready` signal:
        # connect ingest signals (TZ 1.4). Lazy import keeps app load
        # cheap when ai is only being imported for its models (e.g. in
        # migrations).
        from ai import signals

        signals.connect()
