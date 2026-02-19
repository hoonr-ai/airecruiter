from utils.crypto import decrypt_field
import traceback

# Test with actual encrypted data from database
test_name = "aldBTEM4ZTA1WTVraExFNXBLM1dyUT09OnVwemw1bW1PVkZXcjBqR2tZMGNXQWc9PTovWnFUYUx4cEZjdU5wckw5TXFiWDFlUHJQUjJZT1R2eERKb3VRSDA4NmpweA=="

print("Testing decryption with detailed error:")
try:
    decrypted_name = decrypt_field(test_name)
    print(f"Result: {decrypted_name}")
except Exception as e:
    print(f"Exception: {e}")
    traceback.print_exc()
