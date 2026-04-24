import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from core.config import ENCRYPTION_KEY, ENCRYPTION_SALT

def decrypt_field(encrypted_value: str) -> str:
    """
    Decrypt an encrypted field from the Vetted database.
    Format: iv:salt:ciphertext+tag (all base64 encoded strings joined by :)
    Algorithm: AES-256-GCM with PBKDF2 key derivation
    """
    if not encrypted_value or len(encrypted_value) < 10:
        return encrypted_value
    
    # Standard format: iv:salt:ciphertext+tag
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

        iv_b64, salt_b64, ciphertext_with_tag_b64 = parts

        iv = base64.b64decode(iv_b64)
        per_encryption_salt = base64.b64decode(salt_b64)
        ciphertext_with_tag = base64.b64decode(ciphertext_with_tag_b64)

        # AES-GCM tag is the last 16 bytes
        ciphertext = ciphertext_with_tag[:-16]
        tag = ciphertext_with_tag[-16:]

        # Key Derivation: Password = ENCRYPTION_KEY (hex -> bytes), Salt = Payload Salt
        if not ENCRYPTION_KEY:
            print("Error: Missing ENCRYPTION_KEY in configuration")
            return encrypted_value

        base_key = bytes.fromhex(ENCRYPTION_KEY)
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=per_encryption_salt,
            iterations=100000,
            backend=default_backend()
        )
        derived_key = kdf.derive(base_key)

        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.GCM(iv, tag),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()

        # Decrypt and decode
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode('utf-8')

    except Exception as e:
        print(f"Decryption error for '{encrypted_value[:50]}...': {e}")
        return encrypted_value
