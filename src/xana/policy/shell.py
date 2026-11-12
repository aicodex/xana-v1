"""Bash AST risk classification, ported from Kimi 27bd9bab dangerous-command-ask.ts.

This is an approval heuristic, not a shell sandbox. Unrecognized programs can
have arbitrary effects. Nested shell/eval payloads are inspected when literal.
"""
from __future__ import annotations

import re
from typing import Literal

from tree_sitter import Language, Node, Parser
import tree_sitter_bash

ShellRisk = Literal["routine", "dangerous", "unknown"]
_UNSAFE = re.compile(r"[$`*?\[\]~]")
_DANGEROUS = frozenset({"shutdown", "halt", "poweroff", "reboot", "bcdedit", "diskpart",
                       "format", "restart-computer", "stop-computer", "mkfs", "wipefs"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})
_LAUNCHERS = frozenset({"env", "command", "exec", "nohup", "builtin", "nice"})
_PRIVILEGE_VALUES = frozenset({"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
    "-C", "--close-from", "-T", "--command-timeout", "-U", "--other-user", "-r", "--role", "-t", "--type"})
_LAUNCH_VALUES = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string",
                           "-a", "-n", "--adjustment"})
_SAFE_DEVICES = frozenset(f"/dev/{name}" for name in
    ("null", "zero", "full", "random", "urandom", "stdin", "stdout", "stderr"))


class ShellClassifier:
    def __init__(self) -> None:
        self._parser = Parser(Language(tree_sitter_bash.language()))

    def classify(self, source: str, *, depth: int = 0) -> ShellRisk:
        if depth > 4 or len(source) > 1_000_000:
            return "unknown"
        root = self._parser.parse(source.encode()).root_node
        if root.has_error:
            return "unknown"
        stack, commands = [root], []
        count = 0
        while stack:
            node = stack.pop()
            count += 1
            if count > 10_000:
                return "unknown"
            if node.type == "command":
                commands.append(node)
            stack.extend(reversed(node.children))
        result: ShellRisk = "routine"
        for command in commands:
            verdict = self._command(command, depth)
            if verdict == "dangerous":
                return verdict
            if verdict == "unknown":
                result = verdict
        return result

    def _command(self, node: Node, depth: int) -> ShellRisk:
        children = node.children
        index = next((i for i, child in enumerate(children) if child.type == "command_name"), None)
        if index is None or not children[index].named_children:
            return "unknown"
        name = _literal(children[index].named_children[0])
        if not name:
            return "unknown"
        args, dropped = [], False
        for child in children[index + 1:]:
            if child.type in {"variable_assignment", "file_redirect", "heredoc_redirect"}:
                continue
            value = _literal(child)
            if value is None:
                dropped = True
            elif value:
                args.append(value)
        return self._invocation(name, args, dropped, depth)

    def _invocation(self, raw: str, args: list[str], dropped: bool, depth: int) -> ShellRisk:
        name = raw.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        unknown: ShellRisk = "unknown" if dropped else "routine"
        if name in {"sudo", "doas"} or name in _LAUNCHERS or name == "busybox":
            if name == "command" and any(re.fullmatch(r"-[a-zA-Z]*[vV][a-zA-Z]*", arg) for arg in args[:1]):
                return "routine"
            rest = args if name == "busybox" else _drop_options(args,
                _PRIVILEGE_VALUES if name in {"sudo", "doas"} else _LAUNCH_VALUES)
            if name == "env":
                if rest[:1] == ["-"]:
                    rest = rest[1:]
                while rest and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", rest[0]):
                    rest = rest[1:]
            return self._invocation(rest[0], rest[1:], dropped, depth) if rest else unknown
        if name in _SHELLS:
            for index, arg in enumerate(args):
                if not re.fullmatch(r"-[a-zA-Z]+", arg):
                    break
                if "c" in arg:
                    return self.classify(args[index + 1], depth=depth + 1) if index + 1 < len(args) else "unknown"
            return unknown
        if name == "eval":
            return self.classify(" ".join(args), depth=depth + 1) if args and not dropped else unknown
        if name in _DANGEROUS or name.startswith("mkfs."):
            return "dangerous"
        if name in {"init", "telinit"} and any(arg in {"0", "6"} for arg in args):
            return "dangerous"
        if name == "systemctl":
            rest = _drop_options(args, frozenset({"-H", "--host", "-M", "--machine"}))
            return "dangerous" if rest and rest[0] in {"poweroff", "reboot", "halt", "kexec"} else unknown
        if name == "dd":
            return "dangerous" if any(arg.startswith("of=/dev/") and arg[3:] not in _SAFE_DEVICES for arg in args) else unknown
        if name == "rm":
            return _rm(args, dropped)
        return "routine"


def _literal(node: Node) -> str | None:
    raw = node.text.decode()
    if node.type in {"word", "number"}:
        value = re.sub(r"\\(.)", r"\1", raw)
    elif node.type == "raw_string":
        value = raw[1:-1]
    elif node.type == "string" and all(child.type == "string_content" for child in node.named_children):
        value = "".join(child.text.decode() for child in node.named_children)
    else:
        return None
    return None if _UNSAFE.search(value) else value


def _drop_options(args: list[str], takes_value: frozenset[str]) -> list[str]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1:]
        if arg == "-" or not arg.startswith("-"):
            break
        index += 2 if "=" not in arg and arg in takes_value else 1
    return args[index:]


def _rm(args: list[str], dropped: bool) -> ShellRisk:
    recursive, force, ended = False, False, False
    operands = []
    for arg in args:
        if not ended and arg == "--":
            ended = True
        elif not ended and arg == "--recursive":
            recursive = True
        elif not ended and arg == "--force":
            force = True
        elif not ended and re.fullmatch(r"-[a-zA-Z]+", arg):
            recursive |= "r" in arg or "R" in arg
            force |= "f" in arg
        else:
            operands.append(arg)
    if recursive and force:
        safe = bool(operands) and all(".." not in value.split("/") and
            any(value == root or value.startswith(root + "/") for root in ("/tmp", "/temp")) for value in operands)
        return "routine" if safe and not dropped else "dangerous"
    return "unknown" if dropped else "routine"
