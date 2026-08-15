import datetime

from django.db import models
from django.db.models import F
from django.contrib.auth.models import User
from django.utils import timezone


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

    # ── Paid-plan metering ──────────────────────────────────────────────────
    # Free is a lifetime trial and counts DOWN in `generations_count`. Paid
    # plans are a monthly allowance, which needs a period anchor the old schema
    # had no room for — so paid usage counts UP here and resets each month.
    #
    # This existed as advertising only: the pricing page has always sold Pro as
    # "50 generations / month" while use_generation() was a no-op for paid
    # plans, so nothing counted and nothing stopped anyone. That cost nothing
    # while generation ran on a free provider; with a metered API behind it, one
    # Pro subscriber could run up far more in tokens than the plan collects.
    monthly_usage = models.PositiveIntegerField(default=0)
    usage_period_start = models.DateField(null=True, blank=True)

    # ── Global Defaults ─────────────────────────────────────────────────────
    base_resume = models.TextField(blank=True, help_text="User's core career history")
    default_font = models.CharField(max_length=50, default='Inter', help_text="Preferred Studio Font")
    default_language = models.CharField(max_length=50, default='English', help_text="Preferred Output Language")
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True, help_text="Profile Image")

    # ── generation limits per plan ──────────────────────────────────────────
    # Free  → 3 total, lifetime (the trial; counts down in generations_count)
    # Pro   → 50 per calendar month, which is exactly what the pricing page
    #         has always advertised
    # Elite → fair-use ceiling. The pricing page says "Unlimited"; at roughly
    #         5c of tokens per generation a $10/mo plan stops paying for itself
    #         around 200, so this is set well above any genuine job search
    #         (~10/day) while capping abuse. If "Unlimited" is to stay in the
    #         copy verbatim, this number is the business decision to revisit —
    #         not the mechanism.
    PLAN_LIMITS = {
        'free':  3,
        'pro':   50,
        'elite': 300,
    }

    # Plans metered monthly rather than as a lifetime trial.
    _METERED_PLANS = ('pro', 'elite')

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

    @staticmethod
    def _current_period():
        """
        First day of the current calendar month.

        Calendar month rather than the Stripe billing anchor: it needs no API
        call on the request path, it is the same for everyone, and it is what
        "50 per month" means to a reader. A subscriber who joins mid-month gets
        a short first period, which errs in the customer's favour.
        """
        today = timezone.localdate()
        return datetime.date(today.year, today.month, 1)

    def _roll_period_if_needed(self):
        """
        Reset the monthly counter when the calendar month has turned over.

        Written as a conditional UPDATE rather than read-then-write so that two
        concurrent requests at a month boundary cannot both reset (and thereby
        grant an extra generation): the second one matches zero rows because
        the period no longer differs.
        """
        period = self._current_period()
        if self.usage_period_start == period:
            return
        Profile.objects.filter(pk=self.pk).exclude(
            usage_period_start=period
        ).update(monthly_usage=0, usage_period_start=period)
        self.refresh_from_db(fields=['monthly_usage', 'usage_period_start'])

    def quota_message(self):
        """
        Why the user is blocked, in their own plan's terms.

        Every call site said "You've used all free generations! Upgrade to Pro"
        — which is wrong for a Pro subscriber who has hit their monthly
        allowance, and reads as a upsell for something they already bought.
        """
        if self.plan in self._METERED_PLANS:
            return (
                f"You've used all {self.get_limit()} generations included with "
                f"{self.get_plan_display()} this month. Your allowance resets on "
                "the 1st."
            )
        return "You've used all 3 free generations. Upgrade to Pro for 50 a month."

    def generations_remaining(self):
        """How many generations are left in the current period."""
        if self.plan in self._METERED_PLANS:
            self._roll_period_if_needed()
            return max(0, self.get_limit() - self.monthly_usage)
        return max(0, self.generations_count)

    def has_generations_left(self):
        """True if the user still has at least one generation available."""
        return self.generations_remaining() > 0

    def use_generation(self):
        """
        Atomically consume one generation. Returns True if one was consumed,
        False if the quota is exhausted.

        Both paths use a guarded DB-level update so concurrent requests cannot
        lose an update or overshoot the limit — a read-modify-write let parallel
        requests act on the same in-memory value and hand out extras.

        Paid plans were previously a no-op here, so the advertised monthly
        allowance was never enforced.
        """
        if self.plan in self._METERED_PLANS:
            self._roll_period_if_needed()
            limit = self.get_limit()
            updated = Profile.objects.filter(
                pk=self.pk,
                usage_period_start=self._current_period(),
                monthly_usage__lt=limit,
            ).update(monthly_usage=F('monthly_usage') + 1)
            if updated:
                self.refresh_from_db(fields=['monthly_usage'])
                return True
            return False

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