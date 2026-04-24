const ENCRYPTION_KEY = process.env.NEXT_PUBLIC_ENCRYPTION_KEY || '';

// Helper to convert hex string to Uint8Array
function hexToBytes(hex: string): Uint8Array {
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < hex.length; i += 2) {
        bytes[i / 2] = parseInt(hex.substr(i, 2), 16);
    }
    return bytes;
}

// Helper to convert base64 to Uint8Array
function base64ToBytes(base64: string): Uint8Array {
    const binaryString = atob(base64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes;
}

// PBKDF2 using Web Crypto API
async function deriveKey(baseKey: Uint8Array, salt: Uint8Array): Promise<CryptoKey> {
    const importedKey = await crypto.subtle.importKey(
        'raw',
        baseKey as any,
        { name: 'PBKDF2' },
        false,
        ['deriveBits', 'deriveKey']
    );

    return await crypto.subtle.deriveKey(
        {
            name: 'PBKDF2',
            salt: salt as any,
            iterations: 100000,
            hash: 'SHA-256'
        },
        importedKey,
        { name: 'AES-GCM', length: 256 },
        false,
        ['decrypt']
    );
}

export async function decryptField(encryptedValue: string): Promise<string> {
    if (!encryptedValue || encryptedValue.length < 10) {
        return encryptedValue;
    }

    try {
        console.log('Decrypting:', encryptedValue.substring(0, 50) + '...');

        // Decode from base64
        const decoded = atob(encryptedValue);
        console.log('Decoded length:', decoded.length, 'Content:', decoded.substring(0, 100));

        // Split into parts: iv:per_encryption_salt:ciphertext+tag
        const parts = decoded.split(':');
        console.log('Parts count:', parts.length);
        if (parts.length !== 3) {
            console.error('Expected 3 parts, got', parts.length);
            return encryptedValue; // Not in expected format
        }

        const [ivB64, saltB64, ciphertextWithTagB64] = parts;
        console.log('Part lengths (base64):', ivB64.length, saltB64.length, ciphertextWithTagB64.length);

        // Decode each part from base64
        const iv = base64ToBytes(ivB64);
        const perEncryptionSalt = base64ToBytes(saltB64);
        const ciphertextWithTag = base64ToBytes(ciphertextWithTagB64);

        console.log('Byte lengths - IV:', iv.length, 'Salt:', perEncryptionSalt.length, 'Ciphertext+Tag:', ciphertextWithTag.length);

        // Derive encryption key from base key + per-encryption salt
        const baseKey = hexToBytes(ENCRYPTION_KEY);
        const derivedKey = await deriveKey(baseKey, perEncryptionSalt);

        console.log('Key derived, attempting decryption...');

        // Decrypt using AES-GCM (tag is automatically validated)
        const plaintext = await crypto.subtle.decrypt(
            {
                name: 'AES-GCM',
                iv: iv as any
            },
            derivedKey,
            ciphertextWithTag as any
        );

        // Convert ArrayBuffer to string
        const decoder = new TextDecoder();
        const result = decoder.decode(plaintext);
        console.log('Decryption successful:', result);
        return result;
    } catch (error) {
        console.error('Decryption error:', error);
        return encryptedValue; // Return original if decryption fails
    }
}
