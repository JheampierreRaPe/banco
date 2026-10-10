"""Emite un JWT de analista para probar el panel `admin-web` (solo desarrollo/demo).

Uso (desde `backend/`, PowerShell):

    .venv\\Scripts\\python.exe scripts\\mint_demo_token.py > $env:TEMP\\analyst.jwt
    .venv\\Scripts\\python.exe scripts\\mint_demo_token.py --role credit_analyst
    .venv\\Scripts\\python.exe scripts\\mint_demo_token.py --subject <uuid> --minutes 60

- Imprime SOLO el `access_token` por stdout (redirigible a archivo o variable).
- El diagnostico (`sub`/`roles`/expiracion/fuente del secreto) va a stderr;
  el secreto jamas se imprime.
- Solo emite los roles `analyst`/`credit_analyst` (los que exige
  `get_analyst_context` en `app/modules/credits/api/applications.py`).
  No es un backdoor: no crea ni toca endpoints; firma localmente con el
  `JWT_SECRET` vigente, igual que `create_access_token`.
- Fuente del secreto (orden): `--jwt-secret` > `JWT_SECRET` ya exportado en
  el entorno > `.env` de la raiz del repo (el mismo que `docker compose`
  inyecta al backend como `JWT_SECRET`) > `backend/.env`/defaults de `local`.
  Para apuntar contra Docker (`http://localhost:8000`), no exportes
  `JWT_SECRET` y deja que lea el `.env` de la raiz.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

#: Roles que el script puede emitir (los que acepta la bandeja del analista).
ALLOWED_ROLES = ("analyst", "credit_analyst")

#: Variables que se heredan del `.env` de la raiz si no estan en el entorno.
INHERITED_KEYS = ("JWT_SECRET", "JWT_ALGORITHM", "ACCESS_TOKEN_MINUTES")

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


def load_root_env(path: Path) -> dict[str, str]:
    """Lee `KEY=VALUE` del `.env` indicado sin dependencias externas.

    Solo hereda claves presentes en `INHERITED_KEYS` y nunca pisa variables
    ya exportadas en el entorno (manda el entorno). Retorna lo heredado.
    """
    inherited: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return inherited
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in INHERITED_KEYS or key in os.environ:
            continue
        value = value.strip().strip("'").strip('"').strip()
        if value:
            os.environ[key] = value
            inherited[key] = value
    return inherited


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emite un JWT local con rol de analista (solo dev/demo).",
    )
    parser.add_argument(
        "--role",
        default="analyst",
        choices=ALLOWED_ROLES,
        help="Rol del claim `roles` (default: analyst).",
    )
    parser.add_argument(
        "--subject",
        default=None,
        help="UUID del `sub` (default: aleatorio; debe ser UUID valido).",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="Vigencia en minutos (default: ACCESS_TOKEN_MINUTES vigente).",
    )
    parser.add_argument(
        "--jwt-secret",
        default=None,
        help="Secreto de firma (default: entorno o .env de la raiz; nunca se imprime).",
    )
    parser.add_argument(
        "--algorithm",
        default=None,
        help="Algoritmo JWT (default: JWT_ALGORITHM vigente).",
    )
    parser.add_argument(
        "--env-file",
        default=str(REPO_ROOT / ".env"),
        help="Archivo .env del que heredar el secreto (default: .env de la raiz).",
    )
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="No leer ningun .env (usa solo el entorno ya exportado).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        subject = uuid.UUID(str(args.subject)) if args.subject else uuid.uuid4()
    except (ValueError, AttributeError, TypeError):
        print(
            f"ERROR: --subject debe ser un UUID valido, recibido: {args.subject!r}", file=sys.stderr
        )
        return 2

    if args.minutes is not None and args.minutes <= 0:
        print("ERROR: --minutes debe ser > 0", file=sys.stderr)
        return 2

    had_env_secret = "JWT_SECRET" in os.environ
    inherited = {} if args.no_env_file else load_root_env(Path(args.env_file))
    if args.jwt_secret:
        os.environ["JWT_SECRET"] = args.jwt_secret
        secret_source = "--jwt-secret"
    elif had_env_secret:
        secret_source = "entorno"
    elif "JWT_SECRET" in inherited:
        secret_source = str(args.env_file)
    else:
        secret_source = "backend/.env o default local"
    if args.algorithm:
        os.environ["JWT_ALGORITHM"] = args.algorithm
    if args.minutes is not None:
        os.environ["ACCESS_TOKEN_MINUTES"] = str(args.minutes)

    sys.path.insert(0, str(BACKEND_DIR))
    from app.core.security import create_access_token, decode_token

    token = create_access_token(subject=str(subject), roles=[args.role])
    try:
        claims = decode_token(token)
    except Exception as exc:  # noqa: BLE001 - diagnostico local, sin PII
        print(f"ERROR: el token emitido no verifica: {type(exc).__name__}", file=sys.stderr)
        return 1

    exp = claims.get("exp")
    print(token)
    print(
        f"sub={claims.get('sub')} roles={claims.get('roles')} "
        f"alg={args.algorithm or os.environ.get('JWT_ALGORITHM', 'HS256')} "
        f"minutes={args.minutes or os.environ.get('ACCESS_TOKEN_MINUTES', '?')} "
        f"exp={exp} secret_source={secret_source}",
        file=sys.stderr,
    )
    secret = os.environ.get("JWT_SECRET", "")
    if not secret or secret == "change-me":
        print(
            "AVISO: firmas con el secreto local de desarrollo ('change-me' o vacio); "
            "contra Docker usa el .env de la raiz (no exportes JWT_SECRET).",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
