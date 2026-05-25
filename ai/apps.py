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

        # planeAI runtime overlays (bind-mounted from /opt/planeAI/
        # ai-runtime into site-packages/planeai_runtime). Each
        # overlay's install() must be idempotent. We swallow errors so
        # a missing overlay can never block app boot — the platform
        # has to keep starting even if a relaxation patch is gone.
        try:
            from planeai_runtime import permissive_password

            permissive_password.install()
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("plane.ai.apps").warning(
                "permissive_password overlay missing or failed; signup will keep "
                "the upstream zxcvbn strength check",
                exc_info=True,
            )
