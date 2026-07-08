import sys

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

idx = raw.find(b'return _create_fallback_result(e)')
if idx >= 0:
    end = min(idx + 300, len(raw))
    sys.stdout.write(raw[idx:end].decode('utf-8', errors='replace'))
else:
    sys.stdout.write('NOT FOUND\n')
