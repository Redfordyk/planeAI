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
        # ai-runtime into site-packages/planeai_runtime). Each entry
        # below is (module_name, attr_name) where attr_name is the
        # overlay's idempotent activation function ("install" or
        # "connect"). Errors are swallowed per-overlay so a missing
        # or broken patch can never block app boot.
        import logging

        log = logging.getLogger("plane.ai.apps")

        overlays = (
            ("permissive_password", "install"),
            ("openai_embed_fix", "install"),
            ("deepseek_chat", "install"),
            ("autoconfig_workspace", "connect"),
            ("auto_verify_email", "connect"),
        )
        for mod_name, attr in overlays:
            try:
                import importlib

                mod = importlib.import_module(f"planeai_runtime.{mod_name}")
                getattr(mod, attr)()
            except Exception:  # noqa: BLE001
                log.warning(
                    "runtime overlay %s.%s missing or failed",
                    mod_name,
                    attr,
                    exc_info=True,
                )
