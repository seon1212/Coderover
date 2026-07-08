"""Symbol index for Python source code.

Built on top of ``tree_sitter_language_pack`` so the Agent can answer questions
like:

* "Where is ``foo()`` defined?"            -> :py:meth:`SymbolIndex.find_definition`
* "Who calls ``foo()``?"                   -> :py:meth:`SymbolIndex.find_callers`
* "What does ``foo()`` call?"              -> :py:meth:`SymbolIndex.find_callees`
* "What does ``bar.py`` import?"           -> :py:meth:`SymbolIndex.find_imports`
* "List every symbol in this file"         -> :py:meth:`SymbolIndex.symbols_in_file`

Each *symbol* is one of: ``class``, ``function``, ``method``, ``variable``,
``import``.  A *call edge* connects a containing function/method to the bare
callee name at its call site.

The index is held in memory, populated by calling :py:meth:`index_file` or
:py:meth:`index_directory`.  The data is intentionally simple — plain
dataclasses — so the Agent can dump it on demand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from tree_sitter_language_pack import get_parser


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Symbol:
    """A single named construct."""

    kind: str            # "class" | "function" | "method" | "variable" | "import"
    name: str
    file: str
    start_line: int      # 1-based, inclusive
    end_line: int        # 1-based, inclusive
    parent: Optional[str] = None       # enclosing class/function name
    signature: str = ""                # raw source of the "def/class" header
    docstring: str = ""                # first string literal in body, if any

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass(frozen=True)
class CallEdge:
    """A call site: a containing callable invokes a callee name."""

    caller: str          # qualified name of the containing function/method,
                         # or "<module>" for module-level statements
    callee: str          # the bare name being invoked (best effort)
    file: str
    line: int            # 1-based


@dataclass
class FileIndex:
    """All symbols + call edges extracted from one file."""

    path: str
    symbols: List[Symbol] = field(default_factory=list)
    calls: List[CallEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tree-sitter helpers (this codebase uses the 0.25.x API where node accessors
# look like methods but can be invoked without parentheses in some bindings —
# we always call them explicitly to remain version-portable).
# ---------------------------------------------------------------------------

_PARSER = None


def _get_parser():
    global _PARSER
    if _PARSER is None:
        _PARSER = get_parser("python")
    return _PARSER


def _text(src, node) -> str:
    """Slice a node's source text safely.

    ``src`` may be a ``str`` or ``bytes``.  Tree-sitter's ``start_byte`` /
    ``end_byte`` are *byte* offsets — when the file contains non-ASCII chars
    (emoji, CJK) the byte offset diverges from the Python ``str`` index, so
    we always slice the bytes form and decode the result.
    """
    if isinstance(src, str):
        src = src.encode("utf-8")
    return src[node.start_byte():node.end_byte()].decode("utf-8", errors="replace")


def _line(node) -> int:
    return node.start_position().row + 1


def _last_line(node) -> int:
    return node.end_position().row + 1


def _named_children(node) -> Iterator:
    """Yield named children of a node, skipping whitespace/punctuation."""
    for i in range(node.child_count()):
        c = node.child(i)
        if c.is_named():
            yield c


def _walk(node) -> Iterator:
    """Pre-order traversal of a node and all descendants."""
    yield node
    for i in range(node.child_count()):
        yield from _walk(node.child(i))


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

class SymbolIndex:
    """In-memory index of Python symbols and call edges."""

    SKIP_DIRS = frozenset({
        ".git", ".hg", ".svn",
        "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
        ".venv", "venv", "env", ".tox",
        "node_modules", "dist", "build", ".eggs",
    })

    def __init__(self) -> None:
        self._files: Dict[str, FileIndex] = {}
        self._by_name: Dict[str, List[Symbol]] = {}
        self._calls_in_file: Dict[str, List[CallEdge]] = {}
        self._callers_of: Dict[str, List[CallEdge]] = {}

    # ----------------------------------------------------------------------
    # Public indexing API
    # ----------------------------------------------------------------------
    def index_file(self, path: str | os.PathLike) -> FileIndex:
        """Parse one Python file and (re)add its symbols to the index."""
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        fi = self._parse(text, str(p))
        self._remove_file(str(p))
        self._files[str(p)] = fi
        self._register(fi)
        return fi

    def remove_file(self, path: str | os.PathLike) -> None:
        self._remove_file(str(path))

    def index_directory(self, root: str | os.PathLike,
                       include: str = "*.py") -> int:
        """Recursively walk ``root`` and index every matching Python file."""
        base = Path(root).resolve()
        if not base.exists():
            return 0
        if base.is_file():
            self.index_file(base)
            return 1
        n = 0
        for fp in base.rglob(include):
            if not fp.is_file():
                continue
            if any(part in self.SKIP_DIRS for part in fp.parts):
                continue
            try:
                self.index_file(fp)
                n += 1
            except OSError:
                continue
        return n

    def clear(self) -> None:
        self._files.clear()
        self._by_name.clear()
        self._calls_in_file.clear()
        self._callers_of.clear()

    # ----------------------------------------------------------------------
    # Queries
    # ----------------------------------------------------------------------
    def find_definition(self, name: str) -> List[Symbol]:
        """Return every Symbol whose ``name`` matches."""
        return list(self._by_name.get(name, []))

    def symbols_in_file(self, path: str | os.PathLike) -> List[Symbol]:
        fi = self._files.get(str(path))
        return list(fi.symbols) if fi else []

    def find_imports(self, file: str | os.PathLike) -> List[Symbol]:
        return [s for s in self.symbols_in_file(file) if s.kind == "import"]

    def find_callers(self, name: str) -> List[CallEdge]:
        """Return every call site that invokes a callee named ``name``."""
        return list(self._callers_of.get(name.lower(), []))

    def find_callees(self, qualified_name: str) -> List[str]:
        """Bare names invoked by ``qualified_name`` (e.g. ``Foo.bar``)."""
        seen: Set[str] = set()
        out: List[str] = []
        for edges in self._calls_in_file.values():
            for e in edges:
                if e.caller == qualified_name and e.callee not in seen:
                    seen.add(e.callee)
                    out.append(e.callee)
        return out

    def find_references(self, name: str) -> List[Tuple[str, int]]:
        """Cheap grep fallback: definitions + call sites for ``name``."""
        out: List[Tuple[str, int]] = []
        for path, fi in self._files.items():
            for sym in fi.symbols:
                if sym.name == name:
                    out.append((path, sym.start_line))
            for edge in fi.calls:
                if edge.callee == name:
                    out.append((path, edge.line))
        return out

    def summary(self) -> str:
        files = len(self._files)
        syms = sum(len(fi.symbols) for fi in self._files.values())
        calls = sum(len(fi.calls) for fi in self._files.values())
        return f"files={files}  symbols={syms}  call_edges={calls}"

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------
    def _remove_file(self, path: str) -> None:
        if path not in self._files:
            return
        old = self._files.pop(path)
        for s in old.symbols:
            bucket = self._by_name.get(s.name)
            if bucket is not None:
                self._by_name[s.name] = [x for x in bucket if x is not s]
                if not self._by_name[s.name]:
                    self._by_name.pop(s.name, None)
        for e in old.calls:
            bucket_edge: List[CallEdge] | None = self._callers_of.get(e.callee.lower())
            if bucket_edge is not None:
                self._callers_of[e.callee.lower()] = [x for x in bucket_edge if x is not e]
                if not self._callers_of[e.callee.lower()]:
                    self._callers_of.pop(e.callee.lower(), None)
        self._calls_in_file.pop(path, None)

    def _register(self, fi: FileIndex) -> None:
        self._calls_in_file[fi.path] = fi.calls
        for s in fi.symbols:
            self._by_name.setdefault(s.name, []).append(s)
        for e in fi.calls:
            self._callers_of.setdefault(e.callee.lower(), []).append(e)

    def _parse(self, src: str, path: str) -> FileIndex:
        """Single recursive walk — scope chain tells us the parent on entry."""
        fi = FileIndex(path=path)
        parser = _get_parser()
        tree = parser.parse(src)
        root = tree.root_node() if callable(tree.root_node) else tree.root_node
        # root_node is exposed as method in tree-sitter 0.25 bindings
        if not hasattr(root, "kind"):
            root = root()

        ctx = _Context(src=src, path=path, fi=fi)
        _visit(root, ctx)
        return fi


# ---------------------------------------------------------------------------
# Walker — recursive scope-tracked visitor
# ---------------------------------------------------------------------------

@dataclass
class _Context:
    """Per-file mutable state threaded through the AST walk."""

    src: str
    path: str
    fi: FileIndex
    # Qualified names of enclosing class / function, outermost first.
    scope: List[str] = field(default_factory=list)


def _visit(node, ctx: _Context) -> None:
    """Visit a node, dispatching on its kind and recursing into children."""
    kind = node.kind()

    if kind == "class_definition":
        _enter_class(node, ctx)
        return
    if kind == "function_definition":
        _enter_function(node, ctx)
        return
    if kind == "import_statement":
        _enter_import(node, ctx)
    elif kind == "import_from_statement":
        _enter_import_from(node, ctx)
    elif kind == "call":
        _enter_call(node, ctx)

    for child in _named_children(node):
        _visit(child, ctx)


def _enter_class(node, ctx: _Context) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls_name = _text(ctx.src, name_node)

    # Signature = the line containing "class ..."
    header = _header_of(node, ctx.src)
    body = node.child_by_field_name("body")
    docstring = _first_docstring(body, ctx.src)

    ctx.fi.symbols.append(Symbol(
        kind="class",
        name=cls_name,
        file=ctx.path,
        start_line=_line(node),
        end_line=_last_line(node),
        signature=header,
        docstring=docstring,
    ))
    # Push scope and descend into body.  Don't double-count assignments via
    # the default child visitor — handle them inline below.
    prev_scope = ctx.scope
    ctx.scope = prev_scope + [cls_name]
    try:
        if body:
            for child in _named_children(body):
                k = child.kind()
                if k == "function_definition":
                    _enter_function(child, ctx)
                elif k == "class_definition":
                    _enter_class(child, ctx)
                elif k == "expression_statement":
                    inner = child.child(0) if child.child_count() > 0 else None
                    if inner and inner.kind() == "assignment":
                        _enter_class_assignment(inner, cls_name, ctx)
                elif k == "assignment":
                    _enter_class_assignment(child, cls_name, ctx)
    finally:
        ctx.scope = prev_scope


def _enter_function(node, ctx: _Context) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    fn_name = _text(ctx.src, name_node)
    parent = ctx.scope[-1] if ctx.scope else None
    kind = "method" if _enclosing_is_class(ctx.scope) else "function"
    qual = ".".join(ctx.scope + [fn_name])

    body = node.child_by_field_name("body")
    header = _header_of(node, ctx.src)
    docstring = _first_docstring(body, ctx.src)

    ctx.fi.symbols.append(Symbol(
        kind=kind,
        name=fn_name,
        file=ctx.path,
        start_line=_line(node),
        end_line=_last_line(node),
        parent=parent,
        signature=header,
        docstring=docstring,
    ))

    # Push scope for nested defs, and walk body for calls.
    prev_scope = ctx.scope
    ctx.scope = ctx.scope + [fn_name]
    try:
        if body:
            _scan_calls_in(body, qual, ctx)
            for child in _named_children(body):
                k = child.kind()
                if k == "function_definition":
                    _enter_function(child, ctx)
                elif k == "class_definition":
                    _enter_class(child, ctx)
    finally:
        ctx.scope = prev_scope


def _enter_class_assignment(node, parent_class: str, ctx: _Context) -> None:
    """Pick out CLASS_LEVEL variable assignments like ``CONST = 10``."""
    left = node.child_by_field_name("left")
    if left is None or left.kind() != "identifier":
        return
    ctx.fi.symbols.append(Symbol(
        kind="variable",
        name=_text(ctx.src, left),
        file=ctx.path,
        start_line=_line(node),
        end_line=_last_line(node),
        parent=parent_class,
    ))


def _enter_import(node, ctx: _Context) -> None:
    # import x  /  import x.y  /  import x as y
    for child in _named_children(node):
        if child.kind() == "dotted_name":
            ctx.fi.symbols.append(_import_sym(child, ctx))
            return
        if child.kind() == "aliased_import":
            alias = child.child_by_field_name("alias")
            name = child.child_by_field_name("name")
            target = alias or name
            if target is not None:
                ctx.fi.symbols.append(_import_sym(target, ctx))
                return


def _enter_import_from(node, ctx: _Context) -> None:
    module_node = node.child_by_field_name("module_name")
    mod = _text(ctx.src, module_node) if module_node else ""
    # Skip the module_name itself when scanning imported names.
    for child in _named_children(node):
        if module_node is not None and child.start_byte() == module_node.start_byte():
            continue
        if child.kind() == "dotted_name":
            ctx.fi.symbols.append(_import_sym(child, ctx, prefix=mod))
        elif child.kind() == "aliased_import":
            alias = child.child_by_field_name("alias")
            name = child.child_by_field_name("name")
            target = alias or name
            if target is not None:
                ctx.fi.symbols.append(_import_sym(target, ctx, prefix=mod))
        elif child.kind() == "wildcard_import":
            ctx.fi.symbols.append(Symbol(
                kind="import",
                name=f"{mod}.*",
                file=ctx.path,
                start_line=_line(node),
                end_line=_last_line(node),
            ))


def _import_sym(child, ctx: _Context, prefix: str = "") -> Symbol:
    name = _text(ctx.src, child)
    if prefix:
        name = f"{prefix}.{name}"
    return Symbol(
        kind="import",
        name=name,
        file=ctx.path,
        start_line=_line(child),
        end_line=_last_line(child),
    )


def _enter_call(node, ctx: _Context) -> None:
    """Module-level calls (handled via _scan_calls_in for inside functions)."""
    callee = _bare_callee_name(node, ctx.src)
    if callee is None:
        return
    qual = ".".join(ctx.scope) if ctx.scope else "<module>"
    ctx.fi.calls.append(CallEdge(
        caller=qual,
        callee=callee,
        file=ctx.path,
        line=_line(node),
    ))


def _scan_calls_in(node, qualified: str, ctx: _Context) -> None:
    """Walk a subtree, recording every ``call`` node as a CallEdge."""
    for n in _walk(node):
        if n.kind() == "call":
            callee = _bare_callee_name(n, ctx.src)
            if callee is None:
                continue
            ctx.fi.calls.append(CallEdge(
                caller=qualified,
                callee=callee,
                file=ctx.path,
                line=_line(n),
            ))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _enclosing_is_class(scope: List[str]) -> bool:
    """Is the immediately-enclosing scope a class?"""
    return any(_looks_like_class_name(n) for n in scope)


def _looks_like_class_name(s: str) -> bool:
    return s[:1].isupper()


def _header_of(node, src) -> str:
    """Single-line text of the 'def foo(...):' or 'class Foo(...):' header."""
    body = node.child_by_field_name("body")
    head_end = body.start_byte() if body else node.end_byte()
    head_text = _text(src, _Slice(node.start_byte(), head_end))
    head_text = head_text.rstrip()
    # Stop at the colon that closes the header (the colon before the newline).
    head_text = head_text.split("\n", 1)[0].strip()
    return head_text


class _Slice:
    """Minimal node-like object for slicing by byte range via _text()."""

    def __init__(self, start: int, end: int):
        self._start = start
        self._end = end

    def start_byte(self) -> int:
        return self._start

    def end_byte(self) -> int:
        return self._end


def _first_docstring(body_node, src: str) -> str:
    if body_node is None or body_node.child_count() == 0:
        return ""
    first = body_node.child(0)
    # Modern tree-sitter Python grammar emits a bare `string` node as the
    # first statement of a body when it is a docstring; older grammars and
    # some expressions wrap the literal in `expression_statement`.  Handle
    # both forms.
    if first.kind() == "string":
        return _text(src, first).strip().strip("\"'").strip()
    if first.kind() == "expression_statement":
        for c in _named_children(first):
            if c.kind() == "string":
                return _text(src, c).strip().strip("\"'").strip()
    return ""


def _bare_callee_name(call_node, src: str) -> Optional[str]:
    """Return the rightmost identifier of a call, e.g. ``a.b()`` -> ``b``."""
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    k = fn.kind()
    if k == "identifier":
        return _text(src, fn)
    if k == "attribute":
        attr = fn.child_by_field_name("attribute")
        if attr is not None:
            return _text(src, attr)
    return None


# ---------------------------------------------------------------------------
# Tool wrapper — exposes the index to the Agent
# ---------------------------------------------------------------------------

# Process-global index, lazily populated per session.
_GLOBAL_INDEX: Optional[SymbolIndex] = None
_GLOBAL_ROOT: Optional[Path] = None


def _ensure_index(path: str) -> SymbolIndex:
    """Return the global index, (re)indexing ``path`` lazily."""
    global _GLOBAL_INDEX, _GLOBAL_ROOT
    root = Path(path).expanduser().resolve()
    if _GLOBAL_INDEX is None or _GLOBAL_ROOT != root:
        idx = SymbolIndex()
        idx.index_directory(root)
        _GLOBAL_INDEX = idx
        _GLOBAL_ROOT = root
    return _GLOBAL_INDEX


def reset_index() -> None:
    """Drop the cached index (useful between harnesses)."""
    global _GLOBAL_INDEX, _GLOBAL_ROOT
    _GLOBAL_INDEX = None
    _GLOBAL_ROOT = None


def render_find_definition(name: str, defs: List[Symbol]) -> str:
    if not defs:
        return f"No definition found for {name!r}."
    lines = [f"Definitions of {name!r}:"]
    for s in defs:
        loc = f"{s.file}:{s.start_line}"
        sig = f"  {s.signature}" if s.signature else ""
        head = f"  {s.kind} {s.qualified} @ {loc}"
        lines.append(head)
        if sig.strip():
            lines.append(sig)
        if s.docstring:
            lines.append(f"    doc: {s.docstring[:120]}")
    return "\n".join(lines)


def render_callers(name: str, edges: List[CallEdge]) -> str:
    if not edges:
        return f"No callers found for {name!r}."
    # Group call lines by (caller, file) tuple so the output is readable.
    grouped: Dict[Tuple[str, str], List[int]] = {}
    for e in edges:
        grouped.setdefault((e.caller, e.file), []).append(e.line)
    header = f"Callers of {name!r} ({len(edges)} call site(s) in {len(grouped)} caller(s)):"
    lines = [header]
    for (caller, fp), line_list in sorted(grouped.items()):
        path = Path(fp).name
        line_nums = ",".join(str(n) for n in line_list)
        lines.append(f"  {caller}  @ {path}:{line_nums}")
    return "\n".join(lines)


def render_callees(qualified: str, callees: List[str]) -> str:
    if not callees:
        return f"{qualified!r} has no recorded callees."
    return f"{qualified!r} calls: " + ", ".join(callees)


class SymbolIndexTool:
    """Tool for the Agent — answers questions about Python symbols.

    The tool wraps a process-global :class:`SymbolIndex` keyed off ``path``.
    A typical call:

        symbol_query(query="callers", name="foo", path="src/")

    Returns a short text answer the Agent can paste into its reasoning.
    """

    name = "symbol_query"
    description = (
        "Query a tree-sitter symbol index over a Python project. "
        "Use this BEFORE grepping the codebase to find definitions, callers, "
        "or callees of a function/method. Available queries: "
        "'definition' (where is X defined), "
        "'callers' (who calls X), "
        "'callees' (what does qualified_name call), "
        "'imports' (what symbols does file.py import)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "enum": ["definition", "callers", "callees", "imports", "summary"],
                "description": "Type of symbol query to perform.",
            },
            "name": {
                "type": "string",
                "description": (
                    "For definition/callers: the bare function or method name. "
                    "For callees: the qualified name like 'ClassName.method'."
                ),
            },
            "path": {
                "type": "string",
                "description": "Project root or file path. Index is rebuilt per directory.",
            },
        },
        "required": ["query", "path"],
    }

    def execute(self, query: str, path: str, name: str = "") -> str:
        try:
            idx = _ensure_index(path)
        except Exception as e:
            return f"Error indexing {path}: {e}"

        if query == "summary":
            return f"Index for {path}: {idx.summary()}"

        if query == "definition":
            return render_find_definition(name, idx.find_definition(name))
        if query == "callers":
            return render_callers(name, idx.find_callers(name))
        if query == "callees":
            if not name:
                return "Error: 'name' (a qualified name) is required for callees."
            return render_callees(name, idx.find_callees(name))
        if query == "imports":
            target = Path(path)
            if target.is_file():
                syms = idx.find_imports(str(target))
            else:
                # aggregate all imports across the project
                syms = []
                for fp in idx._files.values():
                    syms.extend(idx.find_imports(fp.path))
            if not syms:
                return "No imports found."
            return "Imports:\n" + "\n".join(f"  {s.name} @ {s.file}:{s.start_line}" for s in syms)
        return f"Unknown query {query!r}."

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

