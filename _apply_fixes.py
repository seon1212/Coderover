# -*- coding: utf-8 -*-
"""Apply remaining fixes to the codebase."""
import sys

# ================================================================
# 1. Remove dead commented-out debug code in verification.py
# ================================================================
with open('coderover/verifier/verification.py', 'rb') as f:
    raw = f.read()

# Find and remove the commented-out debug blocks
# Block 1: after runner(repo_path)
old1 = b'        passed, output = runner(repo_path)\n'
old1 += b'        #\xe6\xb5\x8b\xe8\xaf\x95\xe7\x94\xa8\n'  # ≤‚ ‘”√
old1 += b'        #print(f"\\n===== {name} raw output =====")\n'
old1 += b'        #print(output)\n'
old1 += b'        #print("=============================\\n")\n'
old1 += b'\n'
old1 += b'        raw_outputs[name] = output\n'

new1 = b'        passed, output = runner(repo_path)\n'
new1 += b'\n'
new1 += b'        raw_outputs[name] = output\n'

if old1 in raw:
    raw = raw.replace(old1, new1)
    print('Removed debug block 1')
else:
    print('Debug block 1 not found')

# Block 2: after parser(output)
old2 = b'        parsed = parser(output)\n'
old2 += b'        #\xe6\xb5\x8b\xe8\xaf\x95\xe7\x94\xa8\n'  # ≤‚ ‘”√
old2 += b'        #print(f"{name}: passed={passed}, parsed_errors={len(parsed)}")\n'
old2 += b'        #for e in parsed:\n'
old2 += b'        #   print(e)\n'
old2 += b'\n'
old2 += b'        all_errors.extend(parsed)\n'

new2 = b'        parsed = parser(output)\n'
new2 += b'\n'
new2 += b'        all_errors.extend(parsed)\n'

if old2 in raw:
    raw = raw.replace(old2, new2)
    print('Removed debug block 2')
else:
    print('Debug block 2 not found')

with open('coderover/verifier/verification.py', 'wb') as f:
    f.write(raw)
print('verification.py saved')

# ================================================================
# 2. Fix double blank line in harness.py
# ================================================================
with open('coderover/core/harness.py', 'rb') as f:
    raw = f.read()

old_dbl = b'        actual_attempts = 0\n        modified_files: List[str] = []\n\n\n        # Reset agent context'
new_dbl = b'        actual_attempts = 0\n        modified_files: List[str] = []\n\n        # Reset agent context'

if old_dbl in raw:
    raw = raw.replace(old_dbl, new_dbl)
    print('Fixed double blank line in harness.py')
else:
    print('Double blank line not found in harness.py')

with open('coderover/core/harness.py', 'wb') as f:
    f.write(raw)
print('harness.py saved')

print('All fixes applied!')
