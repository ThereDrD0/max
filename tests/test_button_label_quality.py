from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "app" / "bot"
BUTTON_HELPERS = {"callback_button", "clipboard_button", "link_button"}
TEXT_RETURN_HELPERS = {"_builder_cancel_button_text"}
FORBIDDEN_GLOSSARY_TERMS = (
    (re.compile(r"\bивент(?:ы|ов|ам|ами|ах|е|ом|а)?\b", re.IGNORECASE), "мероприятие"),
    (re.compile(r"\bсобыти(?:е|я|ю|ем|и|й|ям|ями|ях)\b", re.IGNORECASE), "мероприятие"),
    (re.compile(r"\bменеджер(?:ы|ов|ам|ами|ах|а|у|ом|е)?\b", re.IGNORECASE), "организатор"),
)
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001FAFF"
    "\u2139"
    "\u2190-\u21FF"
    "\u2300-\u23FF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "]|\ufe0f"
)
DYNAMIC_NUMBERED_BUTTON_RE = re.compile(r"^\S+ \{\}\. \{text\}$")


@dataclass(frozen=True)
class ButtonLabel:
    text: str
    source: str


@dataclass(frozen=True)
class UiText:
    text: str
    source: str


def test_all_bot_buttons_have_emoji() -> None:
    labels = _collect_button_labels()

    missing = [
        f"{label.source}: {label.text!r}"
        for label in labels
        if not _emoji_prefix(label.text)
    ]

    assert missing == []


def test_all_bot_buttons_start_with_emoji() -> None:
    labels = _collect_button_labels()

    wrong_order = [
        f"{label.source}: {label.text!r}"
        for label in labels
        if not _starts_with_emoji(label.text)
    ]

    assert wrong_order == []


def test_same_button_text_uses_same_emoji() -> None:
    labels_by_text: dict[str, set[str]] = defaultdict(set)
    for label in _collect_button_labels():
        if _is_dynamic_numbered_button(label.text):
            continue
        normalized_text = _text_without_emoji(label.text)
        if not normalized_text:
            continue
        labels_by_text[normalized_text].add(_emoji_prefix(label.text))

    conflicts = {
        text: sorted(emojis)
        for text, emojis in labels_by_text.items()
        if len(emojis) > 1
    }

    assert conflicts == {}


def test_all_bot_buttons_have_text_besides_emoji() -> None:
    labels = _collect_button_labels()

    empty = [
        f"{label.source}: {label.text!r}"
        for label in labels
        if not _text_without_emoji(label.text)
    ]

    assert empty == []


def test_all_bot_ui_texts_use_single_regular_spaces() -> None:
    defects = [
        f"{text.source}: {defect}"
        for text in _collect_ui_texts()
        for defect in _space_quality_defects(text.text)
    ]

    assert defects == []


def test_all_bot_ui_texts_follow_glossary_terms() -> None:
    defects = [
        f"{text.source}: {pattern.pattern!r} -> используйте «{replacement}»: {text.text!r}"
        for text in _collect_ui_texts()
        for pattern, replacement in FORBIDDEN_GLOSSARY_TERMS
        if pattern.search(text.text)
    ]

    assert defects == []


def test_all_bot_ui_texts_do_not_use_low_contrast_plus_minus_emojis() -> None:
    forbidden = {"➕", "➖"}
    defects = [
        f"{text.source}: замените плохо видимый эмодзи {emoji!r}: {text.text!r}"
        for text in _collect_ui_texts()
        for emoji in forbidden
        if emoji in text.text
    ]

    assert defects == []


def test_all_bot_ui_texts_do_not_have_adjacent_emojis() -> None:
    defects = [
        f"{text.source}: рядом стоят два эмодзи: {text.text!r}"
        for text in _collect_ui_texts()
        if _has_adjacent_emojis(text.text)
    ]

    assert defects == []


def _collect_button_labels() -> list[ButtonLabel]:
    labels: list[ButtonLabel] = []
    for path in sorted(BOT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        helper_returns = _helper_return_strings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            helper_name = _call_name(node.func)
            if helper_name not in BUTTON_HELPERS:
                continue
            labels.extend(
                ButtonLabel(text=text, source=f"{path.name}:{node.lineno}")
                for text in _resolve_texts(node.args[0], constants, helper_returns)
            )
    assert labels, "Не найдено ни одной кнопки для проверки"
    return labels


def _collect_ui_texts() -> list[UiText]:
    texts = [
        UiText(text=label.text, source=f"{label.source} button")
        for label in _collect_button_labels()
    ]
    for path in sorted(BOT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        helper_returns = _helper_return_strings(tree)
        texts.extend(_module_ui_constants(tree, path))
        texts.extend(_ui_text_expressions(tree, path, constants, helper_returns))
    assert texts, "Не найдено ни одного UI-текста для проверки"
    return texts


def _module_string_constants(tree: ast.AST) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _module_ui_constants(tree: ast.AST, path: Path) -> list[UiText]:
    texts: list[UiText] = []
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if not names or not any(name.endswith(("_TEXT", "_LINES")) for name in names):
            continue
        for text in _literal_text_values(node.value):
            if _looks_like_ui_text(text):
                texts.append(UiText(text=text, source=f"{path.name}:{node.lineno}"))
    return texts


def _helper_return_strings(tree: ast.AST) -> dict[str, list[str]]:
    returns: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in TEXT_RETURN_HELPERS:
            continue
        values = [
            item.value.value
            for item in ast.walk(node)
            if isinstance(item, ast.Return)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        ]
        returns[node.name] = values
    return returns


def _ui_text_expressions(
    tree: ast.AST,
    path: Path,
    constants: dict[str, str],
    helper_returns: dict[str, list[str]],
) -> list[UiText]:
    texts: list[UiText] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for expression in _call_ui_text_arguments(node):
                texts.extend(
                    UiText(text=text, source=f"{path.name}:{node.lineno}")
                    for text in _resolve_texts_or_empty(
                        expression,
                        constants,
                        helper_returns,
                    )
                    if _looks_like_ui_text(text)
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            texts.extend(_function_ui_texts(node, path, constants, helper_returns))
    return texts


def _call_ui_text_arguments(node: ast.Call) -> list[ast.AST]:
    name = _call_name(node.func)
    expressions: list[ast.AST] = []
    if name in {"_send", "send_message", "edit_message"}:
        expressions.extend(
            keyword.value for keyword in node.keywords if keyword.arg == "text"
        )
    if name == "_send_builder_prompt":
        expressions.extend(
            keyword.value for keyword in node.keywords if keyword.arg == "prefix"
        )
    if name == "_start_simple_organizer_state" and len(node.args) >= 5:
        expressions.append(node.args[4])
    if name in {"append", "extend"} and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id in {"lines", "status_lines"}:
            expressions.extend(node.args[:1])
    return expressions


def _function_ui_texts(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    path: Path,
    constants: dict[str, str],
    helper_returns: dict[str, list[str]],
) -> list[UiText]:
    texts: list[UiText] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Return) and item.value is not None:
            texts.extend(
                UiText(text=text, source=f"{path.name}:{item.lineno}")
                for text in _resolve_texts_or_empty(
                    item.value,
                    constants,
                    helper_returns,
                )
                if _looks_like_ui_text(text)
            )
        elif isinstance(item, ast.Assign):
            target_names = [
                target.id for target in item.targets if isinstance(target, ast.Name)
            ]
            if any(name in {"lines", "status_lines"} for name in target_names):
                texts.extend(
                    UiText(text=text, source=f"{path.name}:{item.lineno}")
                    for text in _literal_text_values(item.value)
                    if _looks_like_ui_text(text)
                )
        elif isinstance(item, ast.Dict):
            for value in item.values:
                for text in _literal_text_values(value):
                    if _looks_like_ui_text(text):
                        texts.append(UiText(text=text, source=f"{path.name}:{item.lineno}"))
    return texts


def _resolve_texts(
    node: ast.AST,
    constants: dict[str, str],
    helper_returns: dict[str, list[str]],
) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in constants:
        return [constants[node.id]]
    if isinstance(node, ast.JoinedStr):
        return [_joined_string_template(node)]
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in helper_returns:
            return helper_returns[name]
    raise AssertionError(f"Не удалось определить текст кнопки из AST: {ast.dump(node)}")


def _resolve_texts_or_empty(
    node: ast.AST,
    constants: dict[str, str],
    helper_returns: dict[str, list[str]],
) -> list[str]:
    try:
        return _resolve_texts(node, constants, helper_returns)
    except AssertionError:
        return []


def _literal_text_values(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [
            text
            for item in node.elts
            for text in _literal_text_values(item)
        ]
    return []


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _joined_string_template(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append(_formatted_value_template(value.value))
    return "".join(parts)


def _formatted_value_template(node: ast.AST) -> str:
    if isinstance(node, ast.Name) and node.id in {"index", "offset", "page"}:
        return "{}"
    if isinstance(node, ast.Name) and node.id in {"icon", "prefix"}:
        return "✅"
    return "{text}"


def _emoji_prefix(text: str) -> str:
    matches = EMOJI_RE.findall(text)
    if not matches:
        return ""
    return "".join(matches)


def _starts_with_emoji(text: str) -> bool:
    return bool(text) and bool(EMOJI_RE.match(text[0]))


def _text_without_emoji(text: str) -> str:
    value = EMOJI_RE.sub("", text)
    value = value.replace("\u200d", "")
    value = re.sub(r"\{\}", "", value)
    value = re.sub(r"^\d+[\s.)-]*", "", value)
    return " ".join(value.split())


def _is_dynamic_numbered_button(text: str) -> bool:
    return bool(DYNAMIC_NUMBERED_BUTTON_RE.match(text))


def _looks_like_ui_text(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", text) or EMOJI_RE.search(text))


def _space_quality_defects(text: str) -> list[str]:
    defects: list[str] = []
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line:
            continue
        if line[0] == " ":
            defects.append(f"строка {line_number} начинается с пробела: {line!r}")
        if line[-1] == " ":
            defects.append(f"строка {line_number} заканчивается пробелом: {line!r}")
        if re.search(r" {2,}", line):
            defects.append(f"строка {line_number} содержит несколько пробелов подряд: {line!r}")
        if re.search(r"[\t\u00a0\u2007\u202f]", line):
            defects.append(f"строка {line_number} содержит не обычный пробел: {line!r}")
    return defects


def _has_adjacent_emojis(text: str) -> bool:
    clusters = _emoji_clusters(text)
    return any(left[1] == right[0] for left, right in zip(clusters, clusters[1:]))


def _emoji_clusters(text: str) -> list[tuple[int, int]]:
    clusters: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if not _is_emoji_base(text[index]):
            index += 1
            continue
        start = index
        index = _consume_emoji_base(text, index)
        while (
            index + 1 < len(text)
            and text[index] == "\u200d"
            and _is_emoji_base(text[index + 1])
        ):
            index = _consume_emoji_base(text, index + 1)
        clusters.append((start, index))
    return clusters


def _consume_emoji_base(text: str, index: int) -> int:
    index += 1
    while index < len(text) and text[index] == "\ufe0f":
        index += 1
    return index


def _is_emoji_base(char: str) -> bool:
    return char != "\ufe0f" and bool(EMOJI_RE.fullmatch(char))
