"""Second pass - fix remaining issues in reflector.py"""
import sys

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

# Show the critical sections
# 1. The reflect() function signature
idx_sig = raw.find(b'def reflect(')
sys.stdout.write(f'=== reflect() at byte {idx_sig} ===\n')
sys.stdout.write(raw[idx_sig:idx_sig+150].decode('utf-8', errors='replace'))
sys.stdout.write('\n')

# 2. The prompt template usage
idx_prompt = raw.find(b'prompt = _SYSTEM_PROMPT.format')
sys.stdout.write(f'\n=== prompt.format at byte {idx_prompt} ===\n')
if idx_prompt >= 0:
    sys.stdout.write(raw[idx_prompt:idx_prompt+200].decode('utf-8', errors='replace'))
    sys.stdout.write('\n')

# 3. The system prompt ending
idx_remember = raw.find(b'Remember: Output ONLY')
sys.stdout.write(f'\n=== Remember at byte {idx_remember} ===\n')
if idx_remember >= 0:
    sys.stdout.write(raw[idx_remember:idx_remember+150].decode('utf-8', errors='replace'))
    sys.stdout.write('\n')

# 4. Check en dash in line 177
idx_dash = raw.find(b'infer the deeper issue')
sys.stdout.write(f'\n=== infer deeper issue at byte {idx_dash} ===\n')
if idx_dash >= 0:
    sys.stdout.write(repr(raw[idx_dash-30:idx_dash+50]))
    sys.stdout.write('\n')

# Save to file too
with open('debug_info.txt', 'w', encoding='utf-8') as f:
    f.write(f'reflect() at byte {idx_sig}\n')
    if idx_sig >= 0:
        f.write(raw[idx_sig:idx_sig+150].decode('utf-8', errors='replace'))
    f.write(f'\nprompt.format at byte {idx_prompt}\n')
    if idx_prompt >= 0:
        f.write(raw[idx_prompt:idx_prompt+200].decode('utf-8', errors='replace'))
