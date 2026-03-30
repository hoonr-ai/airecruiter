const crypto = require('crypto');
const { Client } = require('pg');

const ENCRYPTION_KEY = process.env.ENCRYPTION_KEY || '0000000000000000000000000000000000000000000000000000000000000000'; // Fallback for local tests only

// Database connection
const DATABASE_URL = process.env.DATABASE_URL;

async function testDecryption() {
    const client = new Client({ connectionString: DATABASE_URL });

    try {
        await client.connect();
        console.log('✅ Connected to database');

        // Fetch a sample encrypted candidate
        const result = await client.query(`
      SELECT id, name, email 
      FROM "Candidate" 
      WHERE "optedOut" IS NOT TRUE 
      LIMIT 1
    `);

        if (result.rows.length === 0) {
            console.log('❌ No candidates found');
            return;
        }

        const row = result.rows[0];
        console.log('\n📊 Sample Candidate:');
        console.log('ID:', row.id);
        console.log('Encrypted Name:', row.name);
        console.log('Encrypted Email:', row.email);

        // Try to decrypt the name
        console.log('\n🔓 Attempting to decrypt name...');
        const decryptedName = decryptField(row.name);
        console.log('Decrypted Name:', decryptedName);

        // Try to decrypt the email
        console.log('\n🔓 Attempting to decrypt email...');
        const decryptedEmail = decryptField(row.email);
        console.log('Decrypted Email:', decryptedEmail);

    } catch (error) {
        console.error('❌ Error:', error);
    } finally {
        await client.end();
    }
}

function decryptField(encryptedValue) {
    if (!encryptedValue || encryptedValue.length < 10) {
        return encryptedValue;
    }

    try {
        console.log(`\n  Input: ${encryptedValue.substring(0, 50)}...`);
        console.log(`  Input length: ${encryptedValue.length}`);

        // Decode from base64
        const decoded = Buffer.from(encryptedValue, 'base64').toString('utf-8');
        console.log(`  Decoded: ${decoded.substring(0, 100)}...`);
        console.log(`  Decoded length: ${decoded.length}`);

        // Split into parts: iv:per_encryption_salt:ciphertext+tag
        const parts = decoded.split(':');
        console.log(`  Parts count: ${parts.length}`);

        if (parts.length !== 3) {
            console.log(`  ❌ Expected 3 parts, got ${parts.length}`);
            return encryptedValue;
        }

        const [ivB64, saltB64, ciphertextWithTagB64] = parts;
        console.log(`  Part 0 (IV) base64 length: ${ivB64.length}`);
        console.log(`  Part 1 (Salt) base64 length: ${saltB64.length}`);
        console.log(`  Part 2 (Ciphertext+Tag) base64 length: ${ciphertextWithTagB64.length}`);

        // Decode each part from base64
        const iv = Buffer.from(ivB64, 'base64');
        const perEncryptionSalt = Buffer.from(saltB64, 'base64');
        const ciphertextWithTag = Buffer.from(ciphertextWithTagB64, 'base64');

        console.log(`  IV bytes: ${iv.length}`);
        console.log(`  Salt bytes: ${perEncryptionSalt.length}`);
        console.log(`  Ciphertext+Tag bytes: ${ciphertextWithTag.length}`);

        // For AES-256-GCM, the last 16 bytes are the authentication tag
        const ciphertext = ciphertextWithTag.slice(0, -16);
        const tag = ciphertextWithTag.slice(-16);

        console.log(`  Ciphertext bytes: ${ciphertext.length}`);
        console.log(`  Tag bytes: ${tag.length}`);

        // Derive encryption key from base key + per-encryption salt
        const baseKey = Buffer.from(ENCRYPTION_KEY, 'hex');
        console.log(`  Base key bytes: ${baseKey.length}`);

        const derivedKey = crypto.pbkdf2Sync(baseKey, perEncryptionSalt, 100000, 32, 'sha256');
        console.log(`  Derived key bytes: ${derivedKey.length}`);

        // Create decipher (AES-256-GCM)
        const decipher = crypto.createDecipheriv('aes-256-gcm', derivedKey, iv);
        decipher.setAuthTag(tag);

        // Decrypt
        let plaintext = decipher.update(ciphertext);
        plaintext = Buffer.concat([plaintext, decipher.final()]);

        const result = plaintext.toString('utf-8');
        console.log(`  ✅ Decryption successful!`);
        return result;
    } catch (error) {
        console.error(`  ❌ Decryption error: ${error.message}`);
        console.error(error);
        return encryptedValue;
    }
}

// Run the test
testDecryption();
