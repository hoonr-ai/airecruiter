import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import sqlalchemy
from sqlalchemy import text
from core.config import ENCRYPTION_KEY, DATABASE_URL

def decrypt_field(encrypted_value):
    if not encrypted_value or len(encrypted_value) < 10:
        return encrypted_value

    try:
        print(f"\n  Input: {encrypted_value[:50]}...")
        print(f"  Input length: {len(encrypted_value)}")
        
        # Decode from base64
        decoded = base64.b64decode(encrypted_value)
        decoded_str = decoded.decode('utf-8')
        print(f"  Decoded: {decoded_str[:100]}...")
        print(f"  Decoded length: {len(decoded_str)}")
        
        # Split into parts
        parts = decoded_str.split(':')
        print(f"  Parts count: {len(parts)}")
        
        if len(parts) != 3:
            print(f"  ❌ Expected 3 parts, got {len(parts)}")
            return encrypted_value

        iv_b64, salt_b64, ciphertext_with_tag_b64 = parts
        print(f"  Part 0 (IV) base64 length: {len(iv_b64)}")
        print(f"  Part 1 (Salt) base64 length: {len(salt_b64)}")
        print(f"  Part 2 (Ciphertext+Tag) base64 length: {len(ciphertext_with_tag_b64)}")
        
        # Decode each part from base64
        iv = base64.b64decode(iv_b64)
        per_encryption_salt = base64.b64decode(salt_b64)
        ciphertext_with_tag = base64.b64decode(ciphertext_with_tag_b64)
        
        print(f"  IV bytes: {len(iv)}")
        print(f"  Salt bytes: {len(per_encryption_salt)}")  
        print(f"  Ciphertext+Tag bytes: {len(ciphertext_with_tag)}")
        
        # For AES-256-GCM, the last 16 bytes are the authentication tag
        ciphertext = ciphertext_with_tag[:-16]
        tag = ciphertext_with_tag[-16:]
        
        print(f"  Ciphertext bytes: {len(ciphertext)}")
        print(f"  Tag bytes: {len(tag)}")
        
        # Derive encryption key
        base_key = bytes.fromhex(ENCRYPTION_KEY)
        print(f"  Base key bytes: {len(base_key)}")
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=per_encryption_salt,
            iterations=100000,
            backend=default_backend()
        )
        derived_key = kdf.derive(base_key)
        print(f"  Derived key bytes: {len(derived_key)}")
        
        # Create cipher
        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.GCM(iv, tag),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        
        # Decrypt
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        result = plaintext.decode('utf-8')
        print(f"  ✅ Decryption successful!")
        return result
        
    except Exception as e:
        print(f"  ❌ Decryption error: {e}")
        import traceback
        traceback.print_exc()
        return encrypted_value

# Connect to database
try:
    engine = sqlalchemy.create_engine(DATABASE_URL)
    conn = engine.connect()
    print('✅ Connected to database')
    
    # Fetch a sample encrypted candidate
    result = conn.execute(text('''
        SELECT id, name, email 
        FROM "Candidate" 
        WHERE "optedOut" IS NOT TRUE 
        LIMIT 1
    '''))
    
    row = result.fetchone()
    if not row:
        print('❌ No candidates found')
    else:
        print('\n📊 Sample Candidate:')
        print(f'ID: {row.id}')
        print(f'Encrypted Name: {row.name}')
        print(f'Encrypted Email: {row.email}')
        
        # Try to decrypt the name
        print('\n🔓 Attempting to decrypt name...')
        decrypted_name = decrypt_field(row.name)
        print(f'Decrypted Name: {decrypted_name}')
        
        # Try to decrypt the email
        print('\n🔓 Attempting to decrypt email...')
        decrypted_email = decrypt_field(row.email)
        print(f'Decrypted Email: {decrypted_email}')
    
    conn.close()
    
except Exception as e:
    print(f'❌ Error: {e}')
    import traceback
    traceback.print_exc()
