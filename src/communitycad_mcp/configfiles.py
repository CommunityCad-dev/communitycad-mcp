"""Edit one server entry in an app's config file and leave every other byte alone.

JSON files may hold comments and trailing commas (VS Code, Zed and OpenCode
write them; Gemini CLI reads them), so they're read as "JSON with comments" and
changed by splicing text, never by re-serialising the whole file. Codex keeps
its servers in TOML; only the `[mcp_servers.<name>]` table is touched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class ConfigError(Exception):
    """A config file we can't safely edit (not JSON/TOML, or not the shape we expect)."""


# -- JSON with comments ---------------------------------------------------------

_MAX_DEPTH = 200


def _skip(t: str, i: int) -> int:
    """Skip whitespace and comments."""
    n = len(t)
    while i < n:
        c = t[i]
        if c in " \t\r\n":
            i += 1
        elif t.startswith("//", i):
            j = t.find("\n", i)
            i = n if j < 0 else j + 1
        elif t.startswith("/*", i):
            j = t.find("*/", i + 2)
            if j < 0:
                raise ConfigError("a /* comment */ isn't closed")
            i = j + 2
        else:
            break
    return i


def _string_end(t: str, i: int) -> int:
    i += 1
    while i < len(t):
        c = t[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "\n":
            break
        else:
            i += 1
    raise ConfigError("a string isn't closed")


@dataclass
class _Member:
    key: str
    key_start: int
    value_start: int
    value_end: int


@dataclass
class _Object:
    open: int
    close: int
    members: list[_Member]


def _value_end(t: str, i: int, depth: int) -> int:
    if depth > _MAX_DEPTH:
        raise ConfigError("nested too deeply")
    c = t[i : i + 1]
    if c == '"':
        return _string_end(t, i)
    if c == "{":
        return _object(t, i, depth + 1).close + 1
    if c == "[":
        i = _skip(t, i + 1)
        while t[i : i + 1] != "]":
            i = _skip(t, _value_end(t, i, depth + 1))
            if t[i : i + 1] == ",":
                i = _skip(t, i + 1)
            elif t[i : i + 1] != "]":
                raise ConfigError(f"expected , or ] at character {i}")
        return i + 1
    m = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null").match(t, i)
    if not m:
        raise ConfigError(f"unexpected text at character {i}")
    return m.end()


def _object(t: str, open_at: int, depth: int = 0) -> _Object:
    members: list[_Member] = []
    i = _skip(t, open_at + 1)
    while t[i : i + 1] != "}":
        if t[i : i + 1] != '"':
            raise ConfigError(f"expected a key at character {i}")
        key_end = _string_end(t, i)
        key = json.loads(t[i:key_end])
        j = _skip(t, key_end)
        if t[j : j + 1] != ":":
            raise ConfigError(f"expected : at character {j}")
        vs = _skip(t, j + 1)
        ve = _value_end(t, vs, depth)
        members.append(_Member(key, i, vs, ve))
        i = _skip(t, ve)
        if t[i : i + 1] == ",":
            i = _skip(t, i + 1)
        elif t[i : i + 1] != "}":
            raise ConfigError(f"expected , or }} at character {i}")
    return _Object(open_at, i, members)


def _root(t: str) -> _Object:
    i = _skip(t, 0)
    if t[i : i + 1] != "{":
        raise ConfigError("doesn't hold a JSON object")
    obj = _object(t, i)
    if _skip(t, obj.close + 1) != len(t):
        raise ConfigError("has text after the closing }")
    return obj


def _plain(t: str) -> str:
    """The text with comments and trailing commas taken out, for json.loads."""
    out: list[str] = []
    i, n = 0, len(t)
    while i < n:
        c = t[i]
        if c == '"':
            j = _string_end(t, i)
            out.append(t[i:j])
            i = j
        elif t.startswith("//", i) or t.startswith("/*", i):
            j = _skip(t, i)
            out.append(" ")
            i = j
        elif c == ",":
            j = _skip(t, i + 1)
            if t[j : j + 1] not in ("}", "]"):
                out.append(",")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def loads(text: str) -> dict[str, Any]:
    """Parse JSON that may have comments and trailing commas. {} for blank text."""
    if not text.strip():
        return {}
    _root(text)  # structure check with readable errors
    try:
        data = json.loads(_plain(text))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError("doesn't hold a JSON object")
    return data


def _indent_of(t: str, at: int) -> str:
    start = t.rfind("\n", 0, at) + 1
    m = re.compile(r"[ \t]*").match(t, start)
    return m.group(0) if m else ""


def _pretty(value: Any, indent: str) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)


def _insert(t: str, obj: _Object, key: str, value: Any) -> str:
    if obj.members:
        last = obj.members[-1]
        indent = _indent_of(t, last.key_start)
        after = _skip(t, last.value_end)
        trailing = t[after : after + 1] == ","  # the file ends its lists with a comma: so will we
        if trailing:
            pos = after + 1
        else:
            t, pos = t[: last.value_end] + "," + t[last.value_end :], last.value_end + 1
        # Keep a // comment that ends the last member's line on that line.
        m = re.compile(r"[ \t]*//[^\n]*").match(t, pos)
        if m:
            pos = m.end()
        member = f"\n{indent}{json.dumps(key, ensure_ascii=False)}: {_pretty(value, indent)}" + ("," if trailing else "")
        return t[:pos] + member + t[pos:]
    outer = _indent_of(t, obj.open)
    indent = outer + "  "
    inner = t[obj.open + 1 : obj.close]
    body = f"\n{indent}{json.dumps(key, ensure_ascii=False)}: {_pretty(value, indent)}\n{outer}"
    if inner.strip():  # only comments inside: keep them
        body = inner.rstrip() + body
    return t[: obj.open + 1] + body + t[obj.close :]


def set_server(text: str, list_key: str, name: str, value: Any) -> str:
    """Put `value` at list_key.name, creating the list if needed."""
    if not text.strip():
        return json.dumps({list_key: {name: value}}, indent=2, ensure_ascii=False) + "\n"
    root = _root(text)
    servers = next((m for m in reversed(root.members) if m.key == list_key), None)
    if servers is None:
        new = _insert(text, root, list_key, {name: value})
    else:
        if text[servers.value_start] != "{":
            raise ConfigError(f'"{list_key}" isn\'t an object')
        obj = _object(text, servers.value_start)
        mine = next((m for m in reversed(obj.members) if m.key == name), None)
        if mine is None:
            new = _insert(text, obj, name, value)
        else:
            indent = _indent_of(text, mine.key_start)
            new = text[: mine.value_start] + _pretty(value, indent) + text[mine.value_end :]
    if loads(new).get(list_key, {}).get(name) != value:  # pragma: no cover - safety net
        raise ConfigError("the edit didn't come out as expected; left the file alone")
    return new


def remove_server(text: str, list_key: str, name: str) -> str:
    root = _root(text)
    servers = next((m for m in reversed(root.members) if m.key == list_key), None)
    if servers is None or text[servers.value_start] != "{":
        return text
    obj = _object(text, servers.value_start)
    for idx in range(len(obj.members) - 1, -1, -1):
        m = obj.members[idx]
        if m.key != name:
            continue
        if idx + 1 < len(obj.members):  # up to the next member's key
            text = text[: m.key_start] + text[obj.members[idx + 1].key_start :]
        elif idx > 0:  # the last one
            after = _skip(text, m.value_end)
            comma = _skip(text, obj.members[idx - 1].value_end)  # the previous member's comma
            note = re.compile(r"[ \t]*//[^\n]*").match(text, comma + 1)
            keep = note.end() if note else comma + 1  # a // comment on that line stays
            if text[after : after + 1] == ",":  # trailing-comma style: the previous comma stays too
                text = text[:keep] + text[after + 1 :]
            else:
                text = text[:comma] + text[comma + 1 : keep] + text[m.value_end :]
        else:  # the only one: it and its trailing comma
            end = _skip(text, m.value_end)
            end = end + 1 if text[end : end + 1] == "," else m.value_end
            text = text[: m.key_start] + text[end:]
            close = _object(text, servers.value_start).close
            if not text[servers.value_start + 1 : close].strip():  # nothing but whitespace left
                text = text[: servers.value_start + 1] + text[close:]
        obj = _object(text, servers.value_start)
    loads(text)
    return text


# -- Codex TOML -----------------------------------------------------------------

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover - Python 3.10: the same parser, as a package
    import tomli as _toml  # type: ignore[no-redef]

_HEADER = re.compile(r"^\s*\[")


def _header_re(table: str, name: str, sub: bool) -> re.Pattern[str]:
    q = rf"""(?:{re.escape(name)}|"{re.escape(name)}"|'{re.escape(name)}')"""
    tail = r"\s*\.[^\]]*\]" if sub else r"\s*\]"
    return re.compile(rf"^\s*\[\s*{table}\s*\.\s*{q}{tail}\s*(#.*)?$")


def _check_toml(text: str) -> dict[str, Any] | None:
    if _toml is None:
        return None
    try:
        return _toml.loads(text)
    except _toml.TOMLDecodeError as exc:
        raise ConfigError(f"isn't valid TOML: {exc}") from exc


def _sections(lines: list[str], table: str, name: str) -> tuple[int | None, list[tuple[int, int]]]:
    """Index of the [table.name] header, and (start, end) line spans of it and its sub-tables."""
    main_re, sub_re = _header_re(table, name, False), _header_re(table, name, True)
    header = None
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        is_main, is_sub = bool(main_re.match(lines[i])), bool(sub_re.match(lines[i]))
        if is_main or is_sub:
            j = i + 1
            while j < len(lines) and not _HEADER.match(lines[j]):
                j += 1
            if is_main:
                header = i
            spans.append((i, j))
            i = j
        else:
            i += 1
    return header, spans


def _toml_value(raw: str) -> Any:
    """A one-line TOML string or array of strings (the shapes we write)."""
    raw = raw.strip()
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return raw[1:end] if end > 0 else None
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except ValueError:
        return None
    return value


def _key_line(lines: list[str], start: int, end: int, key: str) -> int | None:
    pat = re.compile(rf"^\s*{key}\s*=")
    return next((k for k in range(start + 1, end) if pat.match(lines[k])), None)


def toml_server(text: str, table: str, name: str) -> dict[str, Any] | None:
    """Our server's command and args, or None when it isn't there."""
    data = _check_toml(text)
    if data is not None:
        entry = (data.get(table) or {}).get(name)
        return entry if isinstance(entry, dict) else None
    lines = text.splitlines(keepends=True)
    header, spans = _sections(lines, table, name)
    if header is None:
        return None
    end = next(e for s, e in spans if s == header)
    entry: dict[str, Any] = {}
    for key in ("command", "args"):
        k = _key_line(lines, header, end, key)
        if k is not None:
            entry[key] = _toml_value(lines[k].split("=", 1)[1])
    return entry


def _toml_line(key: str, value: Any) -> str:
    return f"{key} = {json.dumps(value)}\n"  # JSON strings and string arrays are valid TOML


def set_toml_server(text: str, table: str, name: str, entry: dict[str, Any]) -> str:
    """Write command/args into [table.name], keeping its other keys and the rest of the file."""
    data = _check_toml(text) if text.strip() else {}
    lines = text.splitlines(keepends=True)
    header, spans = _sections(lines, table, name)
    if header is None:
        if data and isinstance((data.get(table) or {}).get(name), dict):
            raise ConfigError(f"{table}.{name} is written in a form this installer doesn't edit")
        if data and table in data and not isinstance(data[table], dict):
            raise ConfigError(f"`{table}` isn't a table")
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        block = ["\n"] if any(ln.strip() for ln in lines) else []
        block.append(f"[{table}.{name}]\n")
        block += [_toml_line(k, v) for k, v in entry.items()]
        new = "".join(lines + block)
    else:
        end = next(e for s, e in spans if s == header)
        inserts: list[str] = []
        for key, value in entry.items():
            k = _key_line(lines, header, end, key)
            if k is None:
                inserts.append(_toml_line(key, value))
            else:
                after = lines[k].split("=", 1)[1].strip()
                if after.startswith("[") and "]" not in after:
                    raise ConfigError(f"{table}.{name}.{key} spans several lines; edit it by hand")
                lines[k] = re.match(r"^\s*", lines[k]).group(0) + _toml_line(key, value)
        lines[header + 1 : header + 1] = inserts
        new = "".join(lines)
    check = _check_toml(new)
    if check is not None and {k: (check[table][name]).get(k) for k in entry} != entry:  # pragma: no cover
        raise ConfigError("the edit didn't come out as expected; left the file alone")
    return new


def remove_toml_server(text: str, table: str, name: str) -> str:
    data = _check_toml(text)
    lines = text.splitlines(keepends=True)
    header, spans = _sections(lines, table, name)
    if header is None:
        if data and isinstance((data.get(table) or {}).get(name), dict):
            raise ConfigError(f"{table}.{name} is written in a form this installer doesn't edit")
        return text
    for start, end in reversed(spans):
        # Leave the blank lines after the table, and take one before it, so no gap is left behind.
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        if start > 0 and not lines[start - 1].strip():
            start -= 1
        del lines[start:end]
    new = "".join(lines)
    _check_toml(new)
    return new
