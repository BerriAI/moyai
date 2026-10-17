"""Bounded GitHub classic-protection pathname matching; unsupported syntax fails closed."""

from __future__ import annotations

import re
from typing import Literal

from .github_workflow_types import branch_name

PatternToken = tuple[Literal["literal", "star", "any", "set", "not_set"], frozenset[str]]


def classic_pattern_tokens(component: str) -> list[PatternToken]:
    """Accept a bounded Ruby fnmatch subset; unknown syntax is not a nonmatch."""
    tokens: list[PatternToken] = []
    position = 0
    while position < len(component):
        character = component[position]
        position += 1
        if character == '*':
            if not tokens or tokens[-1][0] != 'star':
                tokens.append(('star', frozenset()))
        elif character == '?':
            tokens.append(('any', frozenset()))
        elif character == '[':
            closing = component.find(']', position)
            if closing < 0:
                raise ValueError('Unsupported classic protection pattern.')
            members = component[position:closing]
            negated = members.startswith(('!', '^'))
            if negated:
                members = members[1:]
            if not members or not re.fullmatch(r'[A-Za-z0-9_.-]+', members):
                raise ValueError('Unsupported classic protection character class.')
            allowed: set[str] = set()
            member = 0
            while member < len(members):
                if member + 2 < len(members) and members[member + 1] == '-':
                    first, last = members[member], members[member + 2]
                    if (first == '-' or last == '-' or ord(first) > ord(last)
                            or not (first.isdigit() and last.isdigit()
                                    or first.islower() and last.islower()
                                    or first.isupper() and last.isupper())):
                        raise ValueError('Unsupported classic protection range.')
                    allowed.update(chr(codepoint) for codepoint in range(ord(first), ord(last) + 1))
                    member += 3
                else:
                    if members[member] == '-' and member not in {0, len(members) - 1}:
                        raise ValueError('Unsupported classic protection range.')
                    allowed.add(members[member])
                    member += 1
            tokens.append(('not_set' if negated else 'set', frozenset(allowed)))
            position = closing + 1
        else:
            tokens.append(('literal', frozenset({character})))
    return tokens


def classic_component_matches(tokens: list[PatternToken], component: str) -> bool:
    """Evaluate one pathname component without backtracking or slash wildcards."""
    positions = [True] + [False] * len(component)
    for kind, members in tokens:
        following = [False] * (len(component) + 1)
        if kind == 'star':
            following[0] = positions[0]
        for index, character in enumerate(component, start=1):
            if kind == 'star':
                following[index] = positions[index] or following[index - 1]
            elif kind == 'any':
                following[index] = positions[index - 1]
            else:
                matches = character not in members if kind == 'not_set' else character in members
                following[index] = positions[index - 1] and matches
        positions = following
    return positions[-1]


def classic_pattern_matches(pattern: str, branch: str) -> bool:
    """Ruby File.fnmatch(..., FNM_PATHNAME) for validated Git branch names."""
    branch_name(branch)
    if (not isinstance(pattern, str) or not 0 < len(pattern) <= 1024 or '\\' in pattern
            or any(ord(character) < 32 or ord(character) == 127 for character in pattern)):
        raise ValueError('Unsupported classic protection pattern.')
    components = pattern.split('/')
    if any(not component for component in components):
        raise ValueError('Unsupported classic protection pathname.')
    parsed = [classic_pattern_tokens(component) for component in components]
    names = branch.split('/')
    positions: set[int] = {0}
    for index, tokens in enumerate(parsed):
        if components[index] == '**' and index < len(components) - 1:
            positions = set(range(min(positions), len(names) + 1)) if positions else set()
        else:
            positions = {position + 1 for position in positions
                         if position < len(names) and classic_component_matches(tokens, names[position])}
    return len(names) in positions


