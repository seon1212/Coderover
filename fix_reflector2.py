"""Fix reflector.py - remove dead debug code and fix signature."""
import re

with open('coderover/agents/reflector.py', 'rb') as f:
    raw = f.read()

# Find all triple-quoted strings
text = raw.decode('utf-8', errors='replace')

# Find the debug block - it's between the llm.chat call and parsed = _parse_llm_response
# Look for the closing of except block and the opening of the debug string
parts = text.split('return _create_fallback_result(e)')
print(f"Split into {len(parts)} parts")

if len(parts) >= 2:
    after_except = parts[1]
    # Find the next triple-quoted string that contains debug-like content
    if '"""' in after_except:
        start = after_except.find('"""')
        end = after_except.find('"""', start + 3)
        if end >= 0:
            debug_content = after_except[start:end+3]
            print(f"Found debug block ({len(debug_content)} chars)")
            # Remove it
            new_after = after_except[:start] + after_except[end+3:]
            text = parts[0] + 'return _create_fallback_result(e)' + new_after
            print("Removed debug block")
    
    # Fix 2: Fix signature spacing
    text = text.replace(
        'config: Config | None = None ,extra_context: Optional[str] = None',
        'config: Config | None = None, extra_context: Optional[str] = None'
    )
    
    # Fix 3: Clean up garbled Chinese comments (lines with unreadable Chinese)
    # Replace garbled lines with clean English comments
    text = text.replace(
        '# \xe6\xaf\x8f\xe8\xbd\xae\xe8\xbe\x93\xe5\x87\xba\xe9\x87\x87\xe7\x94\xa8\xe7\xbb\x9f\xe4\xb8\x80\xe7\x9a\x84 ascii \xe8\xa1\xa8\xe6\xa0\xbc\xe6\xa0\xbc\xe5\xbc\x8f\xef\xbc\x8c\xe5\x85\xbc\xe5\xae\xb9 GBK/UTF-8 \xe7\xbb\x88\xe7\xab\xaf\xe3\x80\x82',
        '# All output uses uniform ascii table format.'
    )

with open('coderover/agents/reflector.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Done")
