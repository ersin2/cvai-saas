from django.db import models
from django.contrib.auth.models import User


class Profile(models.Model):
    PLAN_CHOICES = [
        ('free', 'Free'),
        ('pro', 'Pro'),
        ('enterprise', 'Enterprise'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='free')
    generations_count = models.IntegerField(default=3)  # Free generations remaining
    is_premium = models.BooleanField(default=False)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)

    PLAN_LIMITS = {
        'free': 3,
        'pro': 50,
        'enterprise': 9999,
    }

    def get_limit(self):
        """Return the daily generation limit for this plan."""
        return self.PLAN_LIMITS.get(self.plan, 3)

    def has_generations_left(self):
        """Check if user can still generate."""
        if self.plan in ('pro', 'enterprise'):
            return True
        return self.generations_count > 0

    def use_generation(self):
        """Consume one generation for free users."""
        if self.plan == 'free':
            self.generations_count -= 1
            self.save()

    def __str__(self):
        return f'{self.user.username} Profile ({self.plan})'