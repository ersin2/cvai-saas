from django.db import models
from django.contrib.auth.models import User


class Profile(models.Model):
    PLAN_CHOICES = [
        ('free',  'Free'),
        ('pro',   'Pro'),
        ('elite', 'Elite'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='free')
    generations_count = models.IntegerField(default=3)   # remaining for free-tier
    is_premium = models.BooleanField(default=False)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)

    # ── generation limits per plan ──────────────────────────────────────────
    PLAN_LIMITS = {
        'free':  3,
        'pro':   9999,
        'elite': 9999,
    }

    # ── how many PDF templates each plan can access ─────────────────────────
    # Templates are indexed 0-based in pdf_engine.TEMPLATES list.
    # Free  →  1 template  (index 0)
    # Pro   →  5 templates (indices 0-4)
    # Elite → all templates
    PDF_TEMPLATE_LIMITS = {
        'free':  1,
        'pro':   5,
        'elite': 99,
    }

    def get_limit(self):
        """Return the generation-count limit for this plan."""
        return self.PLAN_LIMITS.get(self.plan, 3)

    def get_pdf_template_limit(self):
        """Return how many distinct PDF templates the user can choose from."""
        return self.PDF_TEMPLATE_LIMITS.get(self.plan, 1)

    def has_generations_left(self):
        """True if the user still has at least one generation available."""
        if self.plan in ('pro', 'elite'):
            return True
        return self.generations_count > 0

    def use_generation(self):
        """Decrement the free-plan counter; no-op for paid plans."""
        if self.plan == 'free':
            self.generations_count -= 1
            self.save()

    def __str__(self):
        return f'{self.user.username} Profile ({self.plan})'