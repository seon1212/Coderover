"""Third pass - fix remaining issues with correct byte patterns"""
import sys

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

text = raw.decode('utf-8')

# 1c. Fix reflect() signature - there's an extra blank line
old_sig = 'def reflect(\n\n    error_summary: List[Any], repo_path: Path | str, config: Config | None = None\n\n) -> ReflectorResult:'
new_sig = 'def reflect(\n\n    error_summary: List[Any], repo_path: Path | str,\n    config: Config | None = None, extra_context: str = ""\n\n) -> ReflectorResult:'
if old_sig in text:
    text = text.replace(old_sig, new_sig)
    sys.stdout.write('1c. Added extra_context parameter to reflect()\n')
else:
    sys.stdout.write('1c. reflect() signature not found!\n')
    # Try without blank line
    old_sig2 = 'def reflect(\n    error_summary: List[Any], repo_path: Path | str, config: Config | None = None\n) -> ReflectorResult:'
    new_sig2 = 'def reflect(\n    error_summary: List[Any], repo_path: Path | str,\n    config: Config | None = None, extra_context: str = ""\n) -> ReflectorResult:'
    if old_sig2 in text:
        text = text.replace(old_sig2, new_sig2)
        sys.stdout.write('1c-alt. Added extra_context parameter\n')
    else:
        sys.stdout.write('1c. Still not found. Debugging...\n')
        idx = text.find('def reflect(')
        if idx >= 0:
            sys.stdout.write(repr(text[idx:idx+200]) + '\n')

# 1d. Fix prompt template usage
old_prompt = '''    prompt = _SYSTEM_PROMPT.format(

        error_summary=error_text, code_context=code_context

    )'''
new_prompt = '''    prompt = _SYSTEM_PROMPT.format(

        error_summary=error_text, code_context=code_context,

        extra_context=extra_context

    )'''
if old_prompt in text:
    text = text.replace(old_prompt, new_prompt)
    sys.stdout.write('1d. Updated prompt template\n')
else:
    sys.stdout.write('1d. Prompt template not found!\n')
    idx = text.find('prompt = _SYSTEM_PROMPT.format')
    if idx >= 0:
        sys.stdout.write(repr(text[idx:idx+200]) + '\n')

# 1e. Fix system prompt
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
    sys.stdout.write('1e. System prompt ending not found!\n')
    idx = text.find('Remember: Output ONLY')
    if idx >= 0:
        sys.stdout.write(repr(text[idx:idx+200]) + '\n')

# Write back
with open('coderover/agents/reflector.py', 'wb') as f:
    f.write(text.encode('utf-8'))
sys.stdout.write('Done\n')
