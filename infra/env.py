"""프로젝트 루트의 .env 파일을 읽어 os.environ에 주입하는 zero-dep 로더.

python-dotenv 같은 외부 패키지를 쓰지 않고 표준 라이브러리만으로 동작한다.
형식: 한 줄에 KEY=VALUE. 빈 줄과 '#'으로 시작하는 주석은 무시한다.
값을 감싼 큰/작은 따옴표는 벗긴다. 이미 환경변수에 존재하는 키는 덮어쓰지 않는다.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | os.PathLike[str] | None = None) -> None:
    """.env 파일을 읽어 os.environ에 채워 넣는다.

    Args:
        path: .env 파일 경로. None이면 프로젝트 루트(infra/의 상위)의 .env를 본다.
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".env"
    env_path = Path(path)
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
