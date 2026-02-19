import os
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import sqlalchemy
from sqlalchemy import text

ENCRYPTION_KEY = '014f5c76a37102e57a9426964b16a22e358741266b4a7c67dabcd1ed2eedf72e'
ENCRYPTION_SALT = 'b7695998d935a6b4a3f2daff2faed9d2b5cb0f2e50624c7f693ea12c7c6b09c7'

def try_decrypt_with_static_salt_ctr(encrypted_value):
    """Try using the static ENCRYPTION_SALT instead of per-encryption salt"""
    try:
        decoded = base64.b64decode(encrypted_value).decode('utf-8')
        parts = decoded.split(':')
        if len(parts) != 3:
            return None

        iv = base64.b64decode(parts[0])
        ciphertext = base64.b64decode(parts[2])
        
        base_key = bytes.fromhex(ENCRYPTION_KEY)
        static_salt = bytes.fromhex(ENCRYPTION_SALT)
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=static_salt,
            iterations=100000,
            backend=default_backend()
        )
        derived_key = kdf.derive(base_key)
        
        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.CTR(iv),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode('utf-8')
    except Exception as e:
        return None

def try_decrypt_key_as_password(encrypted_value):
    """Try using ENCRYPTION_KEY as password string (not hex)"""
    try:
        decoded = base64.b64decode(encrypted_value).decode('utf-8')
        parts = decoded.split(':')
        if len(parts) != 3:
            return None

        iv = base64.b64decode(parts[0])
        per_encryption_salt = base64.b64decode(parts[1])
        ciphertext = base64.b64decode(parts[2])
        
        # Use key as string, not hex bytes
        password = ENCRYPTION_KEY.encode('utf-8')
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=per_encryption_salt,
            iterations=100000,
            backend=default_backend()
        )
        derived_key = kdf.derive(password)
        
        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.CTR(iv),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode('utf-8')
    except Exception as e:
        return None

def try_decrypt_different_iterations(encrypted_value, iterations):
    """Try different iteration counts"""
    try:
        decoded = base64.b64decode(encrypted_value).decode('utf-8')
        parts = decoded.split(':')
        if len(parts) != 3:
            return None

        iv = base64.b64decode(parts[0])
        per_encryption_salt = base64.b64decode(parts[1])
        ciphertext = base64.b64decode(parts[2])
        
        base_key = bytes.fromhex(ENCRYPTION_KEY)
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=per_encryption_salt,
            iterations=iterations,
            backend=default_backend()
        )
        derived_key = kdf.derive(base_key)
        
        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.CTR(iv),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode('utf-8')
    except Exception as e:
        return None

# Connect to database
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

try:
    engine = sqlalchemy.create_engine(DATABASE_URL)
    conn = engine.connect()
    print('✅ Connected to database\n')
    
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
        print('📊 Testing NAME decryption:\n')
        
        methods = [
            ("CTR with static ENCRYPTION_SALT", lambda x: try_decrypt_with_static_salt_ctr(x)),
            ("CTR with key-as-password", lambda x: try_decrypt_key_as_password(x)),
            ("CTR with 10000 iterations", lambda x: try_decrypt_different_iterations(x, 10000)),
            ("CTR with 1000 iterations", lambda x: try_decrypt_different_iterations(x, 1000)),
            ("CTR with 10 iterations", lambda x: try_decrypt_different_iterations(x, 10)),
        ]
        
        for method_name, method_func in methods:
            print(f'  {method_name}...')
            result_text = method_func(row.name)
            if result_text and result_text.isprintable():
                print(f'    ✅ SUCCESS: "{result_text}"')
                break
            else:
                print(f'    ❌ Failed')
        
    conn.close()
    
except Exception as e:
    print(f'❌ Error: {e}')
    import traceback
    traceback.print_exc()
