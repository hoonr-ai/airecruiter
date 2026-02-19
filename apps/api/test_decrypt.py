from utils.crypto import decrypt_field

# Test with actual encrypted data from database
test_name = "aldBTEM4ZTA1WTVraExFNXBLM1dyUT09OnVwemw1bW1PVkZXcjBqR2tZMGNXQWc9PTovWnFUYUx4cEZjdU5wckw5TXFiWDFlUHJQUjJZT1R2eERKb3VRSDA4NmpweA=="
test_email = "NEs4blYyRHpOamNYOEN6bjd2dGErZz09OkgxQm5XZkZud2tjTndkbklkTDNvakE9PTpxV2hoTDZlMDlwVWNyWnVTL0ZzdE05Wk9YcnlWajNlTg=="

print("Testing decryption:")
print(f"Encrypted name: {test_name[:50]}...")
decrypted_name = decrypt_field(test_name)
print(f"Decrypted name: {decrypted_name}")

print(f"\nEncrypted email: {test_email[:50]}...")
decrypted_email = decrypt_field(test_email)
print(f"Decrypted email: {decrypted_email}")
