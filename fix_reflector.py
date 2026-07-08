"""Fix issues in reflector.py"""
import re

with open('coderover/agents/reflector.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: Remove the dead debug string literal
# Find the pattern: a triple-quoted string with debug code between llm.chat and parsing
idx_start = content.find('\n    """\n    # ')
if idx_start >= 0:
    idx_end = content.find('"""', idx_start + 5)
    if idx_end >= 0:
        idx_end += 3  # include closing """
        # Remove the debug block
        content = content[:idx_start] + content[idx_end:]
        print("Fixed: Removed debug string block")

# Fix 2: Fix spacing in function signature
old_sig = 'config: Config | None = None ,extra_context: Optional[str] = None'
new_sig = 'config: Config | None = None, extra_context: Optional[str] = None'
if old_sig in content:
    content = content.replace(old_sig, new_sig)
    print("Fixed: Signature spacing")

with open('coderover/agents/reflector.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Done")
