import os
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Encryption configuration from frontend
ENCRYPTION_KEY = '014f5c76a37102e57a9426964b16a22e358741266b4a7c67dabcd1ed2eedf72e'
ENCRYPTION_SALT = 'b7695998d935a6b4a3f2daff2faed9d2b5cb0f2e50624c7f693ea12c7c6b09c7'

def decrypt_field(encrypted_value: str) -> str:
    """
    Decrypt an encrypted field from the Vetted database.
    Format: iv:tag:ciphertext (all base64 encoded strings joined by :)
    Algorithm: AES-256-GCM with PBKDF2 key derivation
    """
    if not encrypted_value:
        return encrypted_value
    
    # Check format roughly
    if ":" not in encrypted_value:
        # Try to decode from Base64 (Double Encoding Check)
        try:
            decoded_value = base64.b64decode(encrypted_value).decode('utf-8')
            if ":" in decoded_value and len(decoded_value.split(':')) == 3:
                encrypted_value = decoded_value
            else:
                 return encrypted_value
        except Exception:
            return encrypted_value

    try:
        parts = encrypted_value.split(':')
        if len(parts) != 3:
            return encrypted_value

        iv_b64, tag_b64, ciphertext_b64 = parts

        iv = base64.b64decode(iv_b64)
        tag = base64.b64decode(tag_b64)
        ciphertext = base64.b64decode(ciphertext_b64)

        # Key Derivation
        # TS: salt = process.env.ENCRYPTION_KEY (the variable name is confusing but we follow TS)
        # TS: secret = process.env.ENCRYPTION_SALT
        
        # Fetch keys dynamically to respect delayed env loading
        # NOTE: Fallback to empty string if missing, which will fail derivation but is safer than hardcoding
        # Correct Decryption Logic (Verified via verify_keys.py Variation 3)
        # uses the Raw String values (utf-8 bytes) of the env vars.
        # Password = ENCRYPTION_KEY
        # Salt = ENCRYPTION_SALT
        password = os.getenv('ENCRYPTION_KEY', '').encode('utf-8')
        salt = os.getenv('ENCRYPTION_SALT', '').encode('utf-8')
        
        if not password or not salt:
            print("Error: Missing ENCRYPTION_SALT or ENCRYPTION_KEY env vars")
            return encrypted_value

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
            backend=default_backend()
        )
        derived_key = kdf.derive(password)

        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.GCM(iv, tag),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()

        # Decrypt
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode('utf-8')

    except Exception as e:
        print(f"Decryption error for '{encrypted_value[:50]}...': {e}")
        # traceback.print_exc() # Reduce noise
        return encrypted_value
