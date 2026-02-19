import base64

# Analyze the encrypted format
test_name = "aldBTEM4ZTA1WTVraExFNXBLM1dyUT09OnVwemw1bW1PVkZXcjBqR2tZMGNXQWc9PTovWnFUYUx4cEZjdU5wckw5TXFiWDFlUHJQUjJZT1R2eERKb3VRSDA4NmpweA=="

print("Analyzing encrypted format:")
print(f"Original: {test_name[:80]}...")

# Decode base64
decoded = base64.b64decode(test_name)
print(f"\nAfter base64 decode:")
print(f"Bytes length: {len(decoded)}")
print(f"As string: {decoded[:100]}")

# Try to split by colon
decoded_str = decoded.decode('utf-8')
parts = decoded_str.split(':')
print(f"\nParts count: {len(parts)}")
for i, part in enumerate(parts):
    print(f"Part {i}: {part[:50]}... (length: {len(part)})")
    try:
        part_decoded = base64.b64decode(part)
        print(f"  -> Decoded length: {len(part_decoded)} bytes")
    except:
        print(f"  -> Not valid base64")
