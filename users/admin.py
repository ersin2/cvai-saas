from django.contrib import admin
from .models import Profile

# Регистрируем модель, чтобы она появилась в админке
admin.site.register(Profile)

# Register your models here.
