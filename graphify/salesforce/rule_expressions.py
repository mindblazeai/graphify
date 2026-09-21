"""Bounded dependency parsing for FieldRestrictionRule expressions, never eval."""
from __future__ import annotations

import re

TOKEN = re.compile(r'''\s+|'(?:\\.|''|[^'\\])*'|"(?:\\.|""|[^"\\])*"|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\$?[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*|<>|!=|<=|>=|&&|\|\||[(),=<>+*/^&!%-]''')
IDENTIFIER = re.compile(r"\$?[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*\Z")
PRECEDENCE = {"OR": 1, "||": 1, "AND": 2, "&&": 2,
              "=": 3, "!=": 3, "<>": 3, "<": 3, ">": 3, "<=": 3, ">=": 3,
              "&": 4, "+": 5, "-": 5, "*": 6, "/": 6, "%": 6, "^": 7}
ARITY = {"IF": 3, "NOT": 1, "ISBLANK": 1, "ISNULL": 1, "BLANKVALUE": 2,
         "NULLVALUE": 2, "LEN": 1, "LOWER": 1, "UPPER": 1, "ISPICKVAL": 2,
         "CONTAINS": 2, "BEGINS": 2, "INCLUDES": 2, "TEXT": 1, "VALUE": 1,
         "CASESAFEID": 1, "ISNUMBER": 1, "TRIM": 1}


def restriction_expression(facts, node, obj, *, issue):
    if node is None:
        return
    text = node.text
    if len(text) > 16384:
        issue("restriction_expression_limit", node)
        return
    tokens = []
    end = 0
    for match in TOKEN.finditer(text):
        if match.start() != end:
            issue("restriction_expression_syntax_unsupported", node)
            return
        end = match.end()
        if not match.group().isspace():
            tokens.append((match.group(), match.start()))
    if end != len(text) or not tokens:
        issue("restriction_expression_syntax_unsupported", node)
        return
    position = 0
    references, unknown_calls = [], []

    def peek():
        return tokens[position][0].upper() if position < len(tokens) else ""

    def take(expected=None):
        nonlocal position
        if position == len(tokens) or expected is not None and peek() != expected:
            raise ValueError
        value = tokens[position]
        position += 1
        return value

    def expression(minimum=0, depth=0):
        if depth > 64:
            raise ValueError
        value, offset = take()
        upper = value.upper()
        if upper in {"+", "-", "!"} or upper == "NOT" and peek() != "(":
            expression(8, depth + 1)
        elif value == "(":
            expression(0, depth + 1)
            take(")")
        elif IDENTIFIER.fullmatch(value):
            if peek() == "(":
                take("(")
                count = 0
                if peek() != ")":
                    while True:
                        expression(0, depth + 1)
                        count += 1
                        if peek() != ",":
                            break
                        take(",")
                take(")")
                if upper in ARITY:
                    if count != ARITY[upper]:
                        raise ValueError
                elif upper == "CASE":
                    if count < 4 or count % 2:
                        raise ValueError
                elif upper in {"AND", "OR"}:
                    if count == 0:
                        raise ValueError
                else:
                    unknown_calls.append(value)
            elif upper not in {"TRUE", "FALSE", "NULL"}:
                references.append((value, offset))
        elif value[0] not in "'\"" and not value[0].isdigit():
            raise ValueError
        while peek() in PRECEDENCE and PRECEDENCE[peek()] >= minimum:
            op = take()[0].upper()
            expression(PRECEDENCE[op] + 1, depth + 1)

    try:
        expression()
        if position != len(tokens):
            raise ValueError
    except (ValueError, RecursionError):
        issue("restriction_expression_syntax_unsupported", node)
        return
    for name in sorted(set(unknown_calls)):
        issue("restriction_formula_function_unsupported", node, function=name)
    for value, offset in references:
        if value.casefold().startswith("$user."):
            name = "User." + value.split(".", 1)[1]
        elif value.startswith("$") or not obj:
            issue("restriction_expression_context_unsupported", node, reference=value)
            continue
        else:
            name = value if value.casefold().startswith(obj.casefold() + ".") else obj + "." + value
        facts.ref(facts.source.component_id, "FieldPath", name, "reads",
                  node.line + text[:offset].count("\n"), identity_contract="restriction_field")
