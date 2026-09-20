"""Tier 1 (hermetic): the Mermaid diagrams in the documentation parse.

A sequence-diagram message label containing `;` silently ends the statement, so the next
line is read as a new one and the block fails to render — GitHub then shows a parse error
where the diagram should be. That shipped once. Rendering needs a browser, which CI does
not have and this package does not depend on, so these checks are the cheap structural
subset: the hazards that have actually broken a block here, caught with the standard
library alone. They do not replace rendering the diagrams before publishing one.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
MERMAID = re.compile(r"```mermaid\n(.*?)```", re.S)
# `A->>B: text`, `A-->>B: text`, `A-x B: text`, and the label forms that carry free text.
MESSAGE = re.compile(r"^\s*\S+\s*(?:--?>>?|--?[x)])\s*\S+\s*:(?P<label>.*)$")
LABELLED = re.compile(r"^\s*(?:participant|actor|[Nn]ote[^:]*|loop|alt|else|opt|par|rect|"
                      r"critical|break|box)\b[^:]*:?(?P<label>.*)$")
DIRECTIVES = ("sequenceDiagram", "flowchart", "graph", "autonumber", "end", "%%")


def documents() -> list[Path]:
    """Every tracked Markdown file that may carry a diagram."""
    found = [ROOT / "README.md"]
    found += sorted((ROOT / "skills").rglob("*.md"))
    return [path for path in found if path.is_file()]


def blocks(path: Path) -> list[tuple[int, str]]:
    """Each mermaid block in a document, with the line it starts on."""
    text = path.read_text(encoding="utf-8")
    found = []
    for match in MERMAID.finditer(text):
        found.append((text[: match.start()].count("\n") + 2, match.group(1)))
    return found


class MermaidTests(unittest.TestCase):
    def test_no_semicolon_ends_a_message_label_early(self) -> None:
        """`A->>B: do this; then that` parses as a message plus a stray statement."""
        offenders = []
        for path in documents():
            for start, body in blocks(path):
                for offset, line in enumerate(body.splitlines()):
                    match = MESSAGE.match(line) or LABELLED.match(line)
                    if match and ";" in match.group("label"):
                        offenders.append(f"{path.relative_to(ROOT)}:{start + offset}: {line.strip()}")
        self.assertEqual(offenders, [], "a ';' in a Mermaid label ends the statement; "
                                        "use an em dash or a comma")

    def test_every_block_declares_a_diagram_type(self) -> None:
        for path in documents():
            for start, body in blocks(path):
                first = next((line.strip() for line in body.splitlines() if line.strip()), "")
                self.assertTrue(first.startswith(DIRECTIVES),
                                f"{path.relative_to(ROOT)}:{start}: block starts with {first!r}")

    def test_sequence_blocks_open_and_close_every_group(self) -> None:
        openers = ("loop", "alt", "opt", "par", "rect", "critical", "break", "box")
        for path in documents():
            for start, body in blocks(path):
                lines = [line.strip() for line in body.splitlines()]
                if not lines or not lines[0].startswith("sequenceDiagram"):
                    continue
                depth = 0
                for line in lines:
                    # the first token, so `participant` is not read as `par`
                    head = line.split(maxsplit=1)[0] if line.split() else ""
                    if head in openers:
                        depth += 1
                    elif head == "end":
                        depth -= 1
                    self.assertGreaterEqual(depth, 0,
                                            f"{path.relative_to(ROOT)}:{start}: unmatched 'end'")
                self.assertEqual(depth, 0, f"{path.relative_to(ROOT)}:{start}: unclosed group")

    def test_every_sequence_message_names_a_declared_participant(self) -> None:
        declared_re = re.compile(r"^\s*(?:participant|actor)\s+(\w+)")
        message_re = re.compile(r"^\s*(\w+)\s*(?:--?>>?|--?[x)])\s*(\w+)\s*:")
        for path in documents():
            for start, body in blocks(path):
                lines = body.splitlines()
                if not lines or not lines[0].strip().startswith("sequenceDiagram"):
                    continue
                declared = {m.group(1) for line in lines if (m := declared_re.match(line))}
                if not declared:
                    continue
                for offset, line in enumerate(lines):
                    found = message_re.match(line)
                    if not found:
                        continue
                    for name in found.groups():
                        self.assertIn(name, declared,
                                      f"{path.relative_to(ROOT)}:{start + offset}: "
                                      f"{name!r} was never declared")

    def test_the_documents_actually_carry_the_diagrams(self) -> None:
        """A guard on the guards: if the blocks move, these checks stop checking."""
        self.assertEqual(len(blocks(ROOT / "README.md")), 2)


if __name__ == "__main__":
    unittest.main()
