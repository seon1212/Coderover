"""Fix harness.py type hint"""
import sys

with open('coderover/core/harness.py', 'rb') as f:
    raw = f.read()

text = raw.decode('utf-8')

old = '    def run(self, task: str, repo_path: str) -> Dict[str, Any]:'
new = '    def run(self, task: str, repo_path: Path | str) -> Dict[str, Any]:'
if old in text:
    text = text.replace(old, new)
    sys.stdout.write('Fixed run() type hint\n')
else:
    sys.stdout.write('run() signature not found\n')
    idx = text.find('def run(self')
    if idx >= 0:
        sys.stdout.write(repr(text[idx:idx+100]))

with open('coderover/core/harness.py', 'wb') as f:
    f.write(text.encode('utf-8'))
sys.stdout.write('Done\n')
