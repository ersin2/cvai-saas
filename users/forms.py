from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Profile


class ProfilePreferencesForm(forms.ModelForm):
    """
    The "global defaults" form on the profile page.

    Font and language are limited to what the page's <select>s offer. They
    used to be saved as posted, so a value over the column's 50 characters
    failed on PostgreSQL with a 500, and anything else was stored and fed to
    the Studio as a font name.
    """
    FONT_CHOICES = [(f, f) for f in (
        'Inter', 'Outfit', 'Roboto', 'Montserrat', 'Merriweather', 'JetBrains Mono',
    )]
    LANGUAGE_CHOICES = [(l, l) for l in ('English', 'Spanish', 'French', 'German', 'Russian')]

    default_font = forms.ChoiceField(choices=FONT_CHOICES)
    default_language = forms.ChoiceField(choices=LANGUAGE_CHOICES)

    class Meta:
        model = Profile
        fields = ['base_resume', 'default_font', 'default_language']


class UserRegisterForm(UserCreationForm):
    """
    Extends Django's default UserCreationForm with a mandatory, unique-
    validated email field.  This is required for:
      - Password-reset emails
      - Stripe customer creation (customer_email)
      - Future transactional notifications
    """

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={
            'placeholder': 'you@example.com',
            'autocomplete': 'email',
        }),
        help_text='Required. A valid email address for account recovery and billing.',
    )

    class Meta(UserCreationForm.Meta):
        model = User
        # Keep username + email + the two password fields from the parent
        fields = ('username', 'email', 'password1', 'password2')

    def clean_email(self):
        """
        Reject duplicate emails at the form-validation stage so we never
        reach the DB with a conflicting value.
        """
        email = self.cleaned_data.get('email', '').strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                'An account with this email address already exists. '
                'Please use a different email or log in.'
            )
        return email

    def save(self, commit=True):
        """Persist the normalised (lowercase) email onto the User instance."""
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
        return user
