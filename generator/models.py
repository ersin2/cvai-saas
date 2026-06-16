from django.db import models
from django.contrib.auth.models import User


class Generation(models.Model):
    """Stores each AI cover letter generation for history."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='generations')
    resume_text = models.TextField()
    job_description = models.TextField()
    company_name = models.CharField(max_length=255, blank=True, default='')
    job_title = models.CharField(max_length=255, blank=True, default='')
    tone = models.CharField(max_length=50, default='Professional')
    language = models.CharField(max_length=50, default='English')
    result = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} — {self.company_name} ({self.created_at:%Y-%m-%d})'


class JobApplication(models.Model):
    """Track job applications through the hiring pipeline."""
    STATUS_CHOICES = [
        ('saved', 'Saved'),
        ('applied', 'Applied'),
        ('interview', 'Interview'),
        ('offer', 'Offer'),
        ('rejected', 'Rejected'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='applications')
    company_name = models.CharField(max_length=255)
    job_title = models.CharField(max_length=255)
    job_url = models.URLField(blank=True, default='')
    job_description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='saved')
    salary_range = models.CharField(max_length=100, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    applied_date = models.DateField(null=True, blank=True)
    interview_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.company_name} — {self.job_title} ({self.status})'


class AIResult(models.Model):
    """Store AI-generated content: interview prep, follow-ups, ATS scores."""
    TYPE_CHOICES = [
        ('interview', 'Interview Prep'),
        ('followup', 'Follow-Up Email'),
        ('ats', 'ATS Score'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_results')
    result_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, null=True, blank=True, related_name='ai_results')
    input_summary = models.CharField(max_length=500, blank=True, default='')
    result = models.TextField()
    score = models.IntegerField(null=True, blank=True)  # For ATS scores
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.result_type} — {self.user.username} ({self.created_at:%Y-%m-%d})'
