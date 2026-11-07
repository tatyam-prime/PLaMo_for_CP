"""Translate model-only XML markers back to the canonical browser protocol."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

CANONICAL = re.compile(r"\[\[PLM_\d+\]\]|</?plm-[A-Za-z0-9]+>")
FORMAT = re.compile(r"<(\/?)plm-([A-Za-z0-9]+)>")
COLLISION = re.compile(r"</?(?:span\b|math-)", re.IGNORECASE)
TOKENS = re.compile(r"\[\[PLM_[^\]]*\]\]|</?plm-[^>]*>|<[^<>]*>")
MATH = re.compile(r"<math-([a-z]+)\s*/>")
SPAN = re.compile(r"<span\s+id=([\"'])plm-([A-Za-z0-9]+)\1\s*>")

PROBLEM_CONTEXT = (
    "This is a competitive programming problem statement. Preserve every condition, "
    "quantifier, number and variable exactly. Tags such as <math-a/> stand for original "
    "mathematics. Copy each mathematical tag exactly as it occurs. Do not solve the "
    "problem or add explanations. HTML span tags mark formatting around words. Keep "
    "every span id unchanged, keep tags properly nested, and translate the text inside "
    "spans normally."
)
GENERAL_CONTEXT = (
    "This is a general document. Translate the text faithfully without adding or "
    "omitting information. Preserve every condition, quantifier, number and variable "
    "exactly. Tags such as <math-a/> stand for original mathematics. Copy each "
    "mathematical tag exactly as it occurs. HTML span tags mark formatting around "
    "words. Keep every span id unchanged, keep tags properly nested, and translate "
    "the text inside spans normally."
)
LEGACY_PROBLEM_CONTEXT = (
    "This is a competitive programming problem statement. Preserve every condition, "
    "quantifier, number and variable exactly. Tokens of the form [[PLM_0]] are "
    "placeholders for original mathematics. Copy each placeholder verbatim exactly "
    "once. Do not solve the problem or add explanations. HTML tags mark formatting "
    "around words. Preserve each opening and closing tag exactly, maintaining valid "
    "nesting. Translate the text inside tags normally."
)
LEGACY_GENERAL_CONTEXT = (
    "This is a general document. Translate the text faithfully without adding or "
    "omitting information. Preserve every condition, quantifier, number and variable "
    "exactly. Tokens of the form [[PLM_0]] are placeholders for original mathematics. "
    "Copy each placeholder verbatim exactly once. HTML tags mark formatting around "
    "words. Preserve each opening and closing tag exactly, maintaining valid nesting. "
    "Translate the text inside tags normally."
)


def _alphabetic(index: int) -> str:
    result = ""
    while True:
        result = chr(97 + index % 26) + result
        index = index // 26 - 1
        if index < 0:
            return result


def _error() -> ValueError:
    return ValueError("数式・書式の識別子が変化しました。この段落は原文を保持してください。")


class MarkerCodec:
    def __init__(self, source: str, *, legacy: bool = False):
        self.source = source
        self.expected = Counter(CANONICAL.findall(source))
        self.legacy = legacy or bool(COLLISION.search(source))
        self.enabled = bool(self.expected) and not self.legacy
        self.math = {
            token: f"<math-{_alphabetic(index)}/>"
            for index, token in enumerate(
                dict.fromkeys(
                    token for token in CANONICAL.findall(source) if token.startswith("[[")
                )
            )
        }
        self.inverse = {alias: token for token, alias in self.math.items()}

    def encode(self, text: str | None = None) -> str:
        text = self.source if text is None else text
        if Counter(CANONICAL.findall(text)) != self.expected:
            raise _error()
        stack = []
        for tag in FORMAT.finditer(text):
            closing, name = tag.groups()
            if closing:
                if not stack or stack.pop() != name:
                    raise _error()
            else:
                stack.append(name)
        if stack:
            raise _error()
        if not self.enabled:
            return text

        def replace(match):
            token = match[0]
            if token in self.math:
                return self.math[token]
            closing, name = FORMAT.fullmatch(token).groups()
            return "</span>" if closing else f'<span id="plm-{name}">'

        return CANONICAL.sub(replace, text)

    def model_options(self, options: dict) -> dict:
        copied = dict(options)
        if not self.enabled:
            if COLLISION.search(self.source):
                context = copied.get("context", "")
                for current, original in (
                    (PROBLEM_CONTEXT, LEGACY_PROBLEM_CONTEXT),
                    (GENERAL_CONTEXT, LEGACY_GENERAL_CONTEXT),
                ):
                    if context.startswith(current):
                        copied["context"] = original + context[len(current) :]
                        break
            return copied
        terms, math_identities = [], []
        for pair in copied.get("glossary", []):
            source, target = pair["source"], pair["target"]
            if source == target and FORMAT.fullmatch(source):
                continue
            if source == target and source in self.math:
                source = target = self.math[source]
                math_identities.append({"source": source, "target": target})
            else:
                terms.append({"source": source, "target": target})
        copied["glossary"] = terms + math_identities
        return copied

    def decode(self, text: str, *, partial: bool = False) -> str:
        if self.legacy:
            return text
        tail = text.rfind("<")
        if tail >= 0 and ">" not in text[tail:]:
            suffix = text[tail:]
            incomplete = COLLISION.search(suffix) or any(
                candidate.startswith(suffix)
                for candidate in ("<math-", "</math-", "<span", "</span>")
            )
            if incomplete:
                if partial:
                    text = text[:tail]
                elif self.enabled:
                    raise _error()
        if not self.enabled:
            if COLLISION.search(text):
                raise _error()
            return text
        if re.search(r"\[\[PLM_|</?plm-", text, re.IGNORECASE):
            raise _error()  # Never mix canonical and model-only protocols.
        result, stack, seen, position = [], [], Counter(), 0
        for match in TOKENS.finditer(text):
            token = match[0]
            canonical = None
            if MATH.fullmatch(token):
                # XML whitespace before /> is insignificant; the alias ID is
                # exact and must belong to this one sentence.
                alias = f"<math-{MATH.fullmatch(token)[1]}/>"
                canonical = self.inverse.get(alias)
                if canonical is None:
                    raise _error()
            elif SPAN.fullmatch(token):
                name = SPAN.fullmatch(token)[2]
                canonical = f"<plm-{name}>"
                if canonical not in self.expected:
                    raise _error()
                stack.append(name)
            elif token == "</span>":
                if not stack:
                    raise _error()
                canonical = f"</plm-{stack.pop()}>"
            elif COLLISION.search(token) or CANONICAL.fullmatch(token):
                raise _error()
            if canonical is not None:
                if COLLISION.search(text[position : match.start()]):
                    raise _error()
                seen[canonical] += 1
                if seen[canonical] > self.expected[canonical]:
                    raise _error()
                result.extend((text[position : match.start()], canonical))
                position = match.end()
        if not partial and (stack or seen != self.expected):
            raise _error()
        # An incomplete reserved tag is never accepted as a final translation.
        if not partial and COLLISION.search(text[position:]):
            raise _error()
        result.append(text[position:])
        return "".join(result)


@dataclass
class ModelInput:
    codec: MarkerCodec
    text: str
    options: dict
    history: list[dict]


def prepare_model_input(text: str, options: dict, history: list[dict] | None = None) -> ModelInput:
    history = list(history or [])
    contents = [text, *(value for pair in history for value in pair.values())]
    contents.extend(value for pair in options.get("glossary", []) for value in pair.values())
    for base in (PROBLEM_CONTEXT, GENERAL_CONTEXT):
        context = options.get("context", "")
        if context.startswith(base):
            # Instructional examples belong to the grammar. User-supplied
            # surrounding text after that constant can contain literal tags.
            contents.append(context[len(base) :])
            break
    legacy = any(COLLISION.search(value) for value in contents)
    codec = MarkerCodec(text, legacy=legacy)
    encoded_history = []
    for pair in history:
        previous = MarkerCodec(pair["source"], legacy=legacy)
        encoded_history.append(
            {"source": previous.encode(), "translation": previous.encode(pair["translation"])}
        )
    model_options = codec.model_options(options)
    if legacy and not COLLISION.search(text):
        # A collision in history or a configured lexical term also keeps the
        # entire prompt on the original grammar, without interpreting literals.
        for current, original in (
            (PROBLEM_CONTEXT, LEGACY_PROBLEM_CONTEXT),
            (GENERAL_CONTEXT, LEGACY_GENERAL_CONTEXT),
        ):
            context = model_options.get("context", "")
            if context.startswith(current):
                model_options["context"] = original + context[len(current) :]
                break
    return ModelInput(codec, codec.encode(), model_options, encoded_history)
