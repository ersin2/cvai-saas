import os
import django
from io import BytesIO

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aigen.settings')
django.setup()

from generator.pdf_engine import build_pdf
from django.test import RequestFactory

rf = RequestFactory()
request = rf.post('/download-pdf/', {
    'template_name': 'minimal_centered',
    'full_name': 'John Doe',
    'target_role': 'Software Engineer',
    'about_me': 'I am a highly motivated software engineer with 5 years of experience.'
})

try:
    buffer = build_pdf('minimal_centered', request)
    with open('test_out.pdf', 'wb') as f:
        f.write(buffer.getbuffer())
    print(f"PDF saved to test_out.pdf")
except Exception as e:
    print(f"Error building PDF: {e}")
