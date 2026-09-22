from django import forms

from .models import JobApplication


class JobApplicationForm(forms.ModelForm):
    """
    The tracker's "Add Application" modal.

    The view used to pass request.POST straight to objects.create(), which
    skips model validation entirely: job_url accepted `javascript:` URLs that
    tracker.html then rendered as a live href, any string went into `status`,
    and anything longer than a column failed on PostgreSQL with a 500.
    """

    class Meta:
        model = JobApplication
        fields = [
            'company_name', 'job_title', 'job_url', 'job_description',
            'status', 'salary_range', 'notes',
        ]

    def clean_job_url(self):
        # URLField already rejects javascript: and data:, but it also accepts
        # ftp:// and friends, which are no more useful as a job link.
        url = self.cleaned_data.get('job_url', '')
        if url and not url.lower().startswith(('http://', 'https://')):
            raise forms.ValidationError('Enter a link starting with http:// or https://.')
        return url
