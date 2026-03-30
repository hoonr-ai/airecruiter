import os
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from utils.crypto import decrypt_field, ENCRYPTION_KEY, ENCRYPTION_SALT

def encrypt_mock(text: str) -> str:
    # Mirroring TS Logic
    password = ENCRYPTION_SALT.encode('utf-8')
    salt = ENCRYPTION_KEY.encode('utf-8')

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
        backend=default_backend()
    )
    derived_key = kdf.derive(password)

    iv = os.urandom(16)
    cipher = Cipher(
        algorithms.AES(derived_key),
        modes.GCM(iv),
        backend=default_backend()
    )
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(text.encode('utf-8')) + encryptor.finalize()
    tag = encryptor.tag

    return f"{base64.b64encode(iv).decode('utf-8')}:{base64.b64encode(tag).decode('utf-8')}:{base64.b64encode(ciphertext).decode('utf-8')}"

def test_encryption_roundtrip():
    original = "Hello World - Secret Data"
    print(f"Original: {original}")
    
    encrypted = encrypt_mock(original)
    print(f"Encrypted (New Format): {encrypted}")
    
    decrypted = decrypt_field(encrypted)
    print(f"Decrypted: {decrypted}")
    
    assert original == decrypted
    print("SUCCESS: Roundtrip verified!")

if __name__ == "__main__":
    test_encryption_roundtrip()
