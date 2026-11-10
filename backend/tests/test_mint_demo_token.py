"""Pruebas del script local `mint_demo_token` (solo dev/demo, sin BD ni red).

El script firma con `JWT_SECRET` del entorno (aqui uno de prueba) y el test
decodifica el token emitido por stdout: verifica `sub`/`roles` sin imprimir
secretos ni tokens en los asserts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import jwt

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_DIR / "scripts" / "mint_demo_token.py"

TEST_SECRET = "secreto-solo-tests-mint-analyst-1234567890"


def _mint(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(
        os.environ,
        JWT_SECRET=TEST_SECRET,
        JWT_ALGORITHM="HS256",
        ACCESS_TOKEN_MINUTES="15",
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--no-env-file", *args],
        capture_output=True,
        check=False,
        text=True,
        cwd=BACKEND_DIR,
        env=env,
        timeout=60,
    )


def _decode(stdout: str) -> dict:
    return jwt.decode(stdout.strip(), TEST_SECRET, algorithms=["HS256"])


def test_mint_default_role_analyst() -> None:
    sub = str(uuid.uuid4())
    proc = _mint("--subject", sub)
    assert proc.returncode == 0, proc.stderr
    payload = _decode(proc.stdout)
    assert payload["sub"] == sub
    assert payload["roles"] == ["analyst"]


def test_mint_credit_analyst_role() -> None:
    sub = str(uuid.uuid4())
    proc = _mint("--subject", sub, "--role", "credit_analyst")
    assert proc.returncode == 0, proc.stderr
    payload = _decode(proc.stdout)
    assert payload["sub"] == sub
    assert payload["roles"] == ["credit_analyst"]


def test_mint_random_subject_is_valid_uuid() -> None:
    proc = _mint()
    assert proc.returncode == 0, proc.stderr
    payload = _decode(proc.stdout)
    uuid.UUID(str(payload["sub"]))  # no lanza si es UUID valido
    assert payload["roles"] == ["analyst"]


def test_mint_rejects_invalid_subject() -> None:
    proc = _mint("--subject", "no-es-uuid")
    assert proc.returncode == 2


def test_mint_rejects_unknown_role() -> None:
    proc = _mint("--role", "admin")
    assert proc.returncode == 2
