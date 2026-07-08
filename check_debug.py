import sys

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

# Search for the llm.chat call
idx = raw.find(b'llm.chat(')
sys.stdout.write(f'llm.chat at byte: {idx}\n')
if idx >= 0:
    end = min(idx + 600, len(raw))
    sys.stdout.write(raw[idx:end].decode('utf-8', errors='replace'))

# Also search for 'return _create_fallback_result'
idx2 = raw.find(b'return _create_fallback_result(e)')
sys.stdout.write(f'\n\nreturn _create at byte: {idx2}\n')
if idx2 >= 0:
    end = min(idx2 + 400, len(raw))
    sys.stdout.write(raw[idx2:end].decode('utf-8', errors='replace'))
