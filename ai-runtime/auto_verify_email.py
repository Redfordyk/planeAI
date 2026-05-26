from django.apps import apps
from django.db.models.signals import pre_save


def _force_verified(sender, instance, **kwargs):
    if hasattr(instance, "is_email_verified") and not instance.is_email_verified:
        instance.is_email_verified = True


def connect():
    import os
    if not os.environ.get("PLANE_AI_RUNTIME_UNSAFE"):
        raise RuntimeError(
            "auto_verify_email: refusing to connect — "
            "set PLANE_AI_RUNTIME_UNSAFE=1 to explicitly opt in to this unsafe patch. "
            "Never set this in production."
        )
    
    User = apps.get_model("db", "User")
    pre_save.connect(
        _force_verified, sender=User, dispatch_uid="planeai.auto_verify_email"
    )
