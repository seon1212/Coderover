import sys

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

# Find the _SYSTEM_PROMPT
idx = raw.find(b'_SYSTEM_PROMPT =')
sys.stdout.write(f'_SYSTEM_PROMPT at: {idx}\n')
if idx >= 0:
    # Show 500 bytes
    end = min(idx + 500, len(raw))
    sys.stdout.write(raw[idx:end].decode('utf-8', errors='replace'))
