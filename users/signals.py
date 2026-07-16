from django.db.models.signals import post_save
from django.contrib.auth.models import User
from django.dispatch import receiver
from .models import Profile


@receiver(post_save, sender=User)
def ensure_profile(sender, instance, created, **kwargs):
    """
    Create a Profile the first time a User is created.

    Consolidated from the old two-signal pattern: the previous `save_profile`
    ran an extra `profile.save()` on EVERY `User.save()` — including each
    login's `last_login` update — which was a wasted write per request and could
    clobber concurrent profile changes. `get_or_create` keeps this idempotent in
    case a profile already exists (e.g. created by a defensive get_or_create in a
    view or by a data migration).
    """
    if created:
        Profile.objects.get_or_create(user=instance)