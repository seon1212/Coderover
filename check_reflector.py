import sys

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

idx = raw.find(b'def reflect(')
sys.stdout.write(f'def reflect at byte: {idx}\n')
if idx >= 0:
    end = min(idx + 400, len(raw))
    sys.stdout.write(raw[idx:end].decode('utf-8', errors='replace'))
