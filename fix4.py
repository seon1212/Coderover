"""Fourth pass - fix remaining issues with CRLF line endings"""
import sys

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

text = raw.decode('utf-8')

# 1c. Fix reflect() signature - CRLF
old_sig = 'def reflect(\r\n    error_summary: List[Any], repo_path: Path | str, config: Config | None = None\r\n) -> ReflectorResult:'
new_sig = 'def reflect(\r\n    error_summary: List[Any], repo_path: Path | str,\r\n    config: Config | None = None, extra_context: str = ""\r\n) -> ReflectorResult:'
if old_sig in text:
    text = text.replace(old_sig, new_sig)
    sys.stdout.write('1c. Added extra_context parameter to reflect()\n')
else:
    sys.stdout.write('1c. reflect() signature not found!\n')

# 1d. Fix prompt template usage - CRLF
old_prompt = 'prompt = _SYSTEM_PROMPT.format(\r\n        error_summary=error_text, code_context=code_context\r\n    )'
new_prompt = 'prompt = _SYSTEM_PROMPT.format(\r\n        error_summary=error_text, code_context=code_context,\r\n        extra_context=extra_context\r\n    )'
if old_prompt in text:
    text = text.replace(old_prompt, new_prompt)
    sys.stdout.write('1d. Updated prompt template\n')
else:
    sys.stdout.write('1d. Prompt template not found!\n')

# 1e. Fix system prompt - CRLF
old_sp = 'Error information:\r\n{error_summary}\r\n\r\nCode context:\r\n{code_context}\r\n\r\nRemember: Output ONLY the JSON, no markdown, no explanations.'
new_sp = 'Error information:\r\n{error_summary}\r\n\r\nCode context:\r\n{code_context}\r\n\r\nSimilar successful fixes from the past:\r\n{extra_context}\r\n\r\nRemember: Output ONLY the JSON, no markdown, no explanations.'
if old_sp in text:
    text = text.replace(old_sp, new_sp)
    sys.stdout.write('1e. Added extra_context to system prompt\n')
else:
    sys.stdout.write('1e. System prompt not found!\n')

# Write back
with open('coderover/agents/reflector.py', 'wb') as f:
    f.write(text.encode('utf-8'))
sys.stdout.write('All reflector.py fixes done\n')
