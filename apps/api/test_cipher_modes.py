import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import sqlalchemy
from sqlalchemy import text
from core.config import ENCRYPTION_KEY, DATABASE_URL

def try_decrypt_gcm_no_tag_split(encrypted_value):
    """Try GCM with entire Part 2 as ciphertext (no tag splitting)"""
    try:
        decoded = base64.b64decode(encrypted_value).decode('utf-8')
        parts = decoded.split(':')
        if len(parts) != 3:
            return None

        iv = base64.b64decode(parts[0])
        per_encryption_salt = base64.b64decode(parts[1])
        ciphertext = base64.b64decode(parts[2])  # Don't split tag
        
        base_key = bytes.fromhex(ENCRYPTION_KEY)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=per_encryption_salt,
            iterations=100000,
            backend=default_backend()
        )
        derived_key = kdf.derive(base_key)
        
        # Try with no tag (use ciphertext directly)
        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.GCM(iv),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode('utf-8')
    except Exception as e:
        return None

def try_decrypt_ctr(encrypted_value):
    """Try AES-256-CTR mode"""
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

def try_decrypt_cbc(encrypted_value):
    """Try AES-256-CBC mode"""
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
            iterations=100000,
            backend=default_backend()
        )
        derived_key = kdf.derive(base_key)
        
        cipher = Cipher(
            algorithms.AES(derived_key),
            modes.CBC(iv),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        
        # Remove PKCS7 padding
        padding_length = plaintext[-1]
        plaintext = plaintext[:-padding_length]
        
        return plaintext.decode('utf-8')
    except Exception as e:
        return None

def try_decrypt_direct_key(encrypted_value):
    """Try using base key directly without PBKDF2"""
    try:
        decoded = base64.b64decode(encrypted_value).decode('utf-8')
        parts = decoded.split(':')
        if len(parts) != 3:
            return None

        iv = base64.b64decode(parts[0])
        ciphertext = base64.b64decode(parts[2])
        
        derived_key = bytes.fromhex(ENCRYPTION_KEY)
        
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
        print('📊 Sample Candidate:')
        print(f'ID: {row.id}')
        print(f'Encrypted Name: {row.name}\n')
        
        methods = [
            ("GCM (no tag split)", try_decrypt_gcm_no_tag_split),
            ("AES-CTR with PBKDF2", try_decrypt_ctr),
            ("AES-CBC with PBKDF2", try_decrypt_cbc),
            ("AES-CTR direct key", try_decrypt_direct_key),
        ]
        
        print('🔓 Trying different decryption methods for NAME:\n')
        for method_name, method_func in methods:
            print(f'  Trying {method_name}...')
            result = method_func(row.name)
            if result:
                print(f'    ✅ SUCCESS: {result}')
            else:
                print(f'    ❌ Failed')
        
        print('\n🔓 Trying different decryption methods for EMAIL:\n')
        for method_name, method_func in methods:
            print(f'  Trying {method_name}...')
            result = method_func(row.email)
            if result:
                print(f'    ✅ SUCCESS: {result}')
            else:
                print(f'    ❌ Failed')
    
    conn.close()
    
except Exception as e:
    print(f'❌ Error: {e}')
    import traceback
    traceback.print_exc()
