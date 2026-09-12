"""Utilidades de identificadores."""

import uuid


def new_uuid() -> str:
    return str(uuid.uuid4())
