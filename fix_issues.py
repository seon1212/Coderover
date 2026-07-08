"""Fix all identified issues in the codebase."""
import sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 1. Fix reflector.py
# ============================================================
with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()
sys.stdout.write(f'reflector.py size: {len(raw)}\n')

# 1a. Remove the dead debug block (triple-quoted string after return_create_fallback_result)
idx = raw.find(b'return _create_fallback_result(e)')
sys.stdout.write(f'return_create at: {idx}\n')
search_from = idx + len(b'return _create_fallback_result(e)')
tq_start = raw.find(b'"""', search_from)
sys.stdout.write(f'triple-quote open at: {tq_start}\n')

if tq_start > 0:
    tq_end = raw.find(b'"""', tq_start + 3)
    sys.stdout.write(f'triple-quote close at: {tq_end}\n')
    # Remove the block
    raw = raw[:tq_start] + raw[tq_end+3:]
    sys.stdout.write(f'1a. Removed debug block. New size: {len(raw)}\n')

# 1b. Fix en dash (U+2013 or similar) in the f-string
# Search for the special dash character
for i in range(len(raw)):
    if raw[i] == 0x2013.to_bytes(2, 'big')[0] or raw[i] == 0x2014.to_bytes(2, 'big')[0]:
        pass
# Just do a text-based replacement
text = raw.decode('utf-8')

# Fix en dash (byte sequence for UTF-8 en-dash: E2 80 93)
old_dash = '\u2013'
if old_dash in text:
    text = text.replace(old_dash, '-')
    sys.stdout.write('1b. Fixed en dash\n')

# 1c. Add extra_context parameter to reflect()
old_sig = '''def reflect(
    error_summary: List[Any], repo_path: Path | str, config: Config | None = None
) -> ReflectorResult:'''
new_sig = '''def reflect(
    error_summary: List[Any], repo_path: Path | str,
    config: Config | None = None, extra_context: str = ""
) -> ReflectorResult:'''
if old_sig in text:
    text = text.replace(old_sig, new_sig)
    sys.stdout.write('1c. Added extra_context parameter to reflect()\n')
else:
    sys.stdout.write('1c. reflect() signature not found!\n')

# 1d. Include extra_context in prompt template usage
old_prompt = '''    prompt = _SYSTEM_PROMPT.format(
        error_summary=error_text, code_context=code_context
    )'''
new_prompt = '''    prompt = _SYSTEM_PROMPT.format(
        error_summary=error_text, code_context=code_context,
        extra_context=extra_context
    )'''
if old_prompt in text:
    text = text.replace(old_prompt, new_prompt)
    sys.stdout.write('1d. Updated prompt template usage\n')
else:
    sys.stdout.write('1d. Prompt template usage not found\n')

# 1e. Add extra_context placeholder to _SYSTEM_PROMPT
old_sp = '''Error information:
{error_summary}

Code context:
{code_context}

Remember: Output ONLY the JSON, no markdown, no explanations.'''
new_sp = '''Error information:
{error_summary}

Code context:
{code_context}

Similar successful fixes from the past:
{extra_context}

Remember: Output ONLY the JSON, no markdown, no explanations.'''
if old_sp in text:
    text = text.replace(old_sp, new_sp)
    sys.stdout.write('1e. Added extra_context to system prompt\n')
else:
    sys.stdout.write('1e. System prompt not found!\n')

raw = text.encode('utf-8')
with open('coderover/agents/reflector.py', 'wb') as f:
    f.write(raw)
sys.stdout.write('reflector.py saved\n')

# ============================================================
# 2. Fix harness.py
# ============================================================
with open('coderover/core/harness.py', 'rb') as f:
    raw = f.read()
text = raw.decode('utf-8')

# 2a. Fix _header method signature
old = '    def _header(self, repo_path: Path) -> None:'
new = '    def _header(self, repo_path: Path | str) -> None:'
if old in text:
    text = text.replace(old, new)
    sys.stdout.write('2a. Fixed _header type hint\n')
else:
    sys.stdout.write('2a. _header type hint not found\n')

# 2b. Fix verify() call - passes Path but needs str
old = '            verify_result = verify(repo_path)'
new = '            verify_result = verify(str(repo_path))'
if old in text:
    text = text.replace(old, new)
    sys.stdout.write('2b. Fixed verify() call\n')
else:
    sys.stdout.write('2b. verify() call not found\n')

with open('coderover/core/harness.py', 'wb') as f:
    f.write(text.encode('utf-8'))
sys.stdout.write('harness.py saved\n')

# ============================================================
# 3. Fix tools/__init__.py - type hint for get_tool
# ============================================================
with open('coderover/tools/__init__.py', 'rb') as f:
    raw = f.read()
text = raw.decode('utf-8')

old = '''def get_tool(name: str):
    """Look up a tool by name."""
    for t in ALL_TOOLS:
        if t.name == name:'''
new = '''def get_tool(name: str) -> "Tool | None":
    """Look up a tool by name."""
    from .base import Tool
    for t in ALL_TOOLS:
        if isinstance(t, Tool) and t.name == name:'''
if old in text:
    text = text.replace(old, new)
    sys.stdout.write('3. Fixed tools/__init__.py type hint\n')
else:
    sys.stdout.write('3. tools/__init__.py pattern not found\n')

with open('coderover/tools/__init__.py', 'wb') as f:
    f.write(text.encode('utf-8'))
sys.stdout.write('tools/__init__.py saved\n')

# ============================================================
# 4. Fix symbol_index.py - type issues in _remove_file
# ============================================================
with open('coderover/tools/symbol_index.py', 'rb') as f:
    raw = f.read()
text = raw.decode('utf-8')

old = '''        for e in old.calls:
            bucket = self._callers_of.get(e.callee.lower())
            if bucket is not None:
                self._callers_of[e.callee.lower()] = [x for x in bucket if x is not e]'''
new = '''        for e in old.calls:
            bucket_edge: List[CallEdge] | None = self._callers_of.get(e.callee.lower())
            if bucket_edge is not None:
                self._callers_of[e.callee.lower()] = [x for x in bucket_edge if x is not e]'''
if old in text:
    text = text.replace(old, new)
    sys.stdout.write('4. Fixed symbol_index.py _remove_file type issue\n')
else:
    sys.stdout.write('4. symbol_index.py pattern not found\n')

with open('coderover/tools/symbol_index.py', 'wb') as f:
    f.write(text.encode('utf-8'))
sys.stdout.write('symbol_index.py saved\n')

# ============================================================
# Summary
# ============================================================
sys.stdout.write('\nAll fixes applied!\n')
