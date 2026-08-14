from django.db import models
from django.db.models import F
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

    # ── Global Defaults ─────────────────────────────────────────────────────
    base_resume = models.TextField(blank=True, help_text="User's core career history")
    default_font = models.CharField(max_length=50, default='Inter', help_text="Preferred Studio Font")
    default_language = models.CharField(max_length=50, default='English', help_text="Preferred Output Language")
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True, help_text="Profile Image")

    # ── generation limits per plan ──────────────────────────────────────────
    PLAN_LIMITS = {
        'free':  3,
        'pro':   9999,
        'elite': 9999,
    }

    # ── how many PDF templates each plan can access ─────────────────────────
    # Free → 2 templates | Pro → 10 templates | Elite → 20 (all) templates
    PDF_TEMPLATE_LIMITS = {
        'free':  2,
        'pro':   10,
        'elite': 20,
    }

    # ── how many tracked jobs each plan can store ────────────────────────────
    # Free → 10  |  Pro → 50  |  Elite → unlimited
    JOB_TRACKER_LIMITS = {
        'free':  10,
        'pro':   50,
        'elite': 99999,
    }

    def get_limit(self):
        """Return the generation-count limit for this plan."""
        return self.PLAN_LIMITS.get(self.plan, 3)

    def get_pdf_template_limit(self):
        """Free: 2 templates, Pro: 10 templates, Elite: 20 (all) templates."""
        return self.PDF_TEMPLATE_LIMITS.get(self.plan, self.PDF_TEMPLATE_LIMITS.get('free', 2))

    def get_max_tracked_jobs(self):
        """Free: 10 jobs, Pro: 50 jobs, Elite: Unlimited (99999)."""
        return self.JOB_TRACKER_LIMITS.get(self.plan, 10)

    def has_generations_left(self):
        """True if the user still has at least one generation available."""
        if self.plan in ('pro', 'elite'):
            return True
        return self.generations_count > 0

    def use_generation(self):
        """
        Atomically consume one free-plan generation; no-op for paid plans.

        Uses a DB-level guarded decrement (F() with generations_count__gt=0) so
        concurrent requests can't lose updates or drive the counter negative —
        the previous read-modify-write let parallel requests each decrement the
        same in-memory value, effectively granting free users extra generations.

        Returns True if a generation was consumed (or the plan is unlimited),
        False if the free quota was already exhausted.
        """
        if self.plan in ('pro', 'elite'):
            return True

        updated = Profile.objects.filter(
            pk=self.pk, generations_count__gt=0
        ).update(generations_count=F('generations_count') - 1)

        if updated:
            # Keep the in-memory instance consistent for the rest of the request.
            self.refresh_from_db(fields=['generations_count'])
            return True
        return False

    def __str__(self):
        return f'{self.user.username} Profile ({self.plan})'