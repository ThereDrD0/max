from __future__ import annotations

import argparse
from collections.abc import Sequence

from app.config import get_settings
from app.services.registration_codes import default_code_generator
from app.storage.base import CodeGenerator, Storage
from app.storage.factory import create_storage


def rewrite_registration_codes_v2(
    storage: Storage,
    code_generator: CodeGenerator = default_code_generator,
) -> int:
    return storage.rewrite_registration_codes(code_generator)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Переписать все коды записей в формат 123-456.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Выполнить перепись кодов. Без флага команда ничего не меняет.",
    )
    args = parser.parse_args(argv)
    if not args.apply:
        print("Dry run: добавьте --apply, чтобы переписать коды записей.")
        return
    storage = create_storage(get_settings())
    changed = rewrite_registration_codes_v2(storage)
    print(f"Registration codes rewritten: {changed}")


if __name__ == "__main__":
    main()
