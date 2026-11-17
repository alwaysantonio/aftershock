"""
Create (or update) the admin user and API token from environment variables.
Idempotent: safe to run on every start. Does nothing if neither is configured.

    DJANGO_ADMIN_USERNAME  (default "admin")
    DJANGO_ADMIN_PASSWORD  for the Django admin site
    DJANGO_ADMIN_TOKEN     for API calls: Authorization: Token <value>
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = "Ensure the admin user and API token configured in the environment exist."

    def handle(self, **_):
        username = os.environ.get("DJANGO_ADMIN_USERNAME", "admin")
        password = os.environ.get("DJANGO_ADMIN_PASSWORD")
        token = os.environ.get("DJANGO_ADMIN_TOKEN")
        if not (password or token):
            self.stdout.write(
                "No DJANGO_ADMIN_PASSWORD or DJANGO_ADMIN_TOKEN set; skipping."
            )
            return

        with transaction.atomic():
            user, _ = get_user_model().objects.get_or_create(
                username=username, defaults={"is_staff": True, "is_superuser": True}
            )
            if password:
                user.set_password(password)
                user.save(update_fields=["password"])
            if token:
                # The token key is the primary key, so replace rather than update it.
                Token.objects.filter(user=user).exclude(key=token).delete()
                Token.objects.get_or_create(user=user, key=token)

        self.stdout.write(self.style.SUCCESS(f"Admin user '{username}' ready."))
