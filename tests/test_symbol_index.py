"""Tests for the tree-sitter symbol index."""

from pathlib import Path

import pytest

from coderover.tools.symbol_index import (
    CallEdge,
    FileIndex,
    Symbol,
    SymbolIndex,
    SymbolIndexTool,
    render_callees,
    render_callers,
    render_find_definition,
    reset_index,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_src() -> str:
    return '''\
import os
from pathlib import Path

def helper(x):
    return x + 1

class Calculator:
    PI = 3.14

    def add(self, a, b):
        return a + b

    def compute(self, a, b):
        r1 = self.add(a, b)
        r2 = helper(r1)
        return r2 * Calculator.PI

def main():
    c = Calculator()
    result = c.compute(1, 2)
    print(result)
'''


@pytest.fixture
def sample_file(sample_src, tmp_path) -> Path:
    p = tmp_path / "demo.py"
    p.write_text(sample_src, encoding="utf-8")
    return p


@pytest.fixture
def indexed(sample_file) -> SymbolIndex:
    idx = SymbolIndex()
    idx.index_file(sample_file)
    return idx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSymbolIndex:
    def test_finds_top_level_function(self, indexed: SymbolIndex, sample_file):
        defs = indexed.find_definition("helper")
        assert len(defs) == 1
        sym = defs[0]
        assert sym.kind == "function"
        assert sym.name == "helper"
        assert sym.parent is None
        assert sym.start_line == 4

    def test_finds_class(self, indexed: SymbolIndex, sample_file):
        defs = indexed.find_definition("Calculator")
        assert len(defs) == 1
        sym = defs[0]
        assert sym.kind == "class"
        assert sym.name == "Calculator"

    def test_finds_method(self, indexed: SymbolIndex, sample_file):
        defs = indexed.find_definition("add")
        assert len(defs) == 1
        sym = defs[0]
        assert sym.kind == "method"
        assert sym.parent == "Calculator"

    def test_finds_class_variable(self, indexed: SymbolIndex, sample_file):
        defs = indexed.find_definition("PI")
        assert len(defs) == 1
        sym = defs[0]
        assert sym.kind == "variable"
        assert sym.parent == "Calculator"

    def test_callers_of_method(self, indexed: SymbolIndex):
        edges = indexed.find_callers("add")
        assert len(edges) == 1
        assert edges[0].caller == "Calculator.compute"
        assert edges[0].callee == "add"

    def test_callers_of_module_function(self, indexed: SymbolIndex):
        edges = indexed.find_callers("helper")
        assert len(edges) == 1
        assert edges[0].caller == "Calculator.compute"
        assert edges[0].callee == "helper"

    def test_callees_of_compute(self, indexed: SymbolIndex):
        # compute calls self.add(...) and helper(...). Calculator.PI is an
        # attribute access — bare callee name returns the rightmost id.
        callees = indexed.find_callees("Calculator.compute")
        assert "add" in callees
        assert "helper" in callees

    def test_imports(self, indexed: SymbolIndex, sample_file):
        imports = indexed.find_imports(str(sample_file))
        names = {s.name for s in imports}
        assert "os" in names
        assert "pathlib.Path" in names

    def test_summary(self, indexed: SymbolIndex):
        s = indexed.summary()
        assert "files=1" in s
        assert "symbols=" in s
        assert "call_edges=" in s

    def test_definition_is_case_sensitive(self, indexed: SymbolIndex):
        # No class called helper_lowercase or HELPER
        assert indexed.find_definition("HElper") == []
        assert indexed.find_definition("calculator") == []

    def test_empty_index(self):
        idx = SymbolIndex()
        assert idx.find_definition("foo") == []
        assert idx.find_callers("foo") == []
        assert idx.find_callees("foo") == []
        assert idx.summary() == "files=0  symbols=0  call_edges=0"

    def test_remove_file(self, indexed: SymbolIndex, sample_file):
        assert indexed.find_definition("Calculator")
        indexed.remove_file(sample_file)
        assert indexed.find_definition("Calculator") == []

    def test_index_directory(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo(): pass\n")
        (tmp_path / "b.py").write_text("def bar(): return foo()\n")
        idx = SymbolIndex()
        n = idx.index_directory(tmp_path)
        assert n == 2
        assert len(idx.find_definition("foo")) == 1
        assert len(idx.find_definition("bar")) == 1
        edges = idx.find_callers("foo")
        assert len(edges) == 1
        assert edges[0].caller == "bar"

    def test_reindex_after_edit(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text("def foo(): pass\n")
        idx = SymbolIndex()
        idx.index_file(p)
        assert len(idx.find_definition("foo")) == 1
        p.write_text("def foo(): pass\ndef bar(): pass\n")
        idx.index_file(p)
        names = {s.name for s in idx.find_definition("bar")}
        assert "bar" in names
        # Old 'foo' should still be there (not lost when re-indexing)
        assert len(idx.find_definition("foo")) == 1

    def test_signature_includes_def_line(self, indexed: SymbolIndex):
        # Look up the method by its bare name; parent is recorded on the Symbol.
        defs = indexed.find_definition("compute")
        assert defs, "expected to find 'compute' method"
        sym = defs[0]
        assert sym.kind == "method"
        assert sym.parent == "Calculator"
        assert sym.signature.startswith("def compute")

    def test_docstring_extracted_when_present(self, tmp_path):
        src = '''\
def greet(name):
    """Return a greeting."""
    return f"hi {name}"
'''
        p = tmp_path / "g.py"
        p.write_text(src)
        idx = SymbolIndex()
        idx.index_file(p)
        sym = idx.find_definition("greet")[0]
        assert "greeting" in sym.docstring.lower()


class TestRenderers:
    def test_render_find_definition_empty(self):
        out = render_find_definition("foo", [])
        assert "No definition" in out

    def test_render_find_definition_with_results(self, indexed: SymbolIndex):
        defs = indexed.find_definition("Calculator")
        out = render_find_definition("Calculator", defs)
        assert "Calculator" in out
        assert "class" in out

    def test_render_callers_empty(self):
        out = render_callers("foo", [])
        assert "No callers" in out

    def test_render_callers_with_results(self, indexed: SymbolIndex):
        edges = indexed.find_callers("add")
        out = render_callers("add", edges)
        assert "Calculator.compute" in out
        assert "1 call site" in out

    def test_render_callees_empty(self):
        out = render_callees("foo", [])
        assert "no recorded callees" in out

    def test_render_callees_with_results(self, indexed: SymbolIndex):
        out = render_callees("Calculator.compute",
                             indexed.find_callees("Calculator.compute"))
        assert "calls:" in out
        assert "add" in out

    def test_render_callees_with_attribute(self):
        # a.b() resolves to 'b' as the bare callee name
        edges = [CallEdge(caller="x", callee="b", file="f.py", line=3)]
        out = render_callees("x", ["b"])
        assert "b" in out


class TestSymbolIndexTool:
    def setup_method(self):
        reset_index()

    def test_tool_has_schema(self):
        t = SymbolIndexTool()
        schema = t.schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "symbol_query"
        params = schema["function"]["parameters"]
        assert set(params["properties"].keys()) == {"query", "name", "path"}
        assert set(params["required"]) == {"query", "path"}

    def test_tool_definition_query(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="definition", name="Calculator", path=str(sample_file))
        assert "Calculator" in out
        assert "class" in out

    def test_tool_callers_query(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="callers", name="helper", path=str(sample_file))
        assert "Calculator.compute" in out
        assert "helper" in out

    def test_tool_callees_query(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="callees",
                        name="Calculator.compute",
                        path=str(sample_file))
        assert "calls:" in out
        assert "add" in out

    def test_tool_imports_query(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="imports", path=str(sample_file))
        assert "os" in out
        assert "pathlib.Path" in out

    def test_tool_summary_query(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="summary", path=str(sample_file))
        assert "files=" in out

    def test_tool_unknown_query(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="???", path=str(sample_file))
        assert "Unknown query" in out

    def test_tool_callees_requires_name(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="callees", name="", path=str(sample_file))
        assert "Error" in out

    def test_tool_definition_not_found(self, sample_file):
        t = SymbolIndexTool()
        out = t.execute(query="definition", name="does_not_exist",
                        path=str(sample_file))
        assert "No definition" in out


class TestSymbolDataclass:
    def test_qualified(self):
        s = Symbol(kind="function", name="foo", file="a.py",
                   start_line=1, end_line=3)
        assert s.qualified == "foo"
        s2 = Symbol(kind="method", name="bar", file="a.py",
                    start_line=1, end_line=3, parent="Foo")
        assert s2.qualified == "Foo.bar"

    def test_frozen(self):
        s = Symbol(kind="function", name="foo", file="a.py",
                   start_line=1, end_line=3)
        with pytest.raises(Exception):
            s.name = "bar"  # frozen dataclass

    def test_fileindex_defaults(self):
        fi = FileIndex(path="x.py")
        assert fi.symbols == []
        assert fi.calls == []


class TestErrorHandling:
    def test_nonexistent_file(self, tmp_path):
        idx = SymbolIndex()
        with pytest.raises(OSError):
            idx.index_file(tmp_path / "missing.py")

    def test_invalid_python_file_does_not_crash(self, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("def foo(:\n    invalid syntax!!!\n")
        idx = SymbolIndex()
        # Parsing should be tolerant; should not raise, but may miss symbols
        idx.index_file(p)
        # Just ensure summary() doesn't crash
        idx.summary()

    def test_directory_does_not_exist(self):
        idx = SymbolIndex()
        assert idx.index_directory("/nonexistent/path/xyz123") == 0
