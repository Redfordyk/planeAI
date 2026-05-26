from django.apps import apps
from django.db.models.signals import pre_save


def _force_verified(sender, instance, **kwargs):
    if hasattr(instance, "is_email_verified") and not instance.is_email_verified:
        instance.is_email_verified = True


def connect():
    User = apps.get_model("db", "User")
    pre_save.connect(
        _force_verified, sender=User, dispatch_uid="planeai.auto_verify_email"
    )
