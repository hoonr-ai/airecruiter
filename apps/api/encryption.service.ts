import { Injectable } from '@nestjs/common';
import * as crypto from 'crypto';
import { LoggerSecurity } from '../logging/LoggerSecurity';
const logger = new LoggerSecurity();

export type EncryptionSecretsKey = {
  key: Buffer
}

export type EncryptionSecretsSecrets = {
  secret: string,
  salt: string,
}

export type EncryptionSecrets = Partial<EncryptionSecretsKey | EncryptionSecretsSecrets>

@Injectable()
export class EncryptionService {
  private readonly algorithm = 'aes-256-gcm';

  getKey(encryptionSecrets?: EncryptionSecrets): Buffer {
    if ("key" in encryptionSecrets) { return encryptionSecrets.key }

    const {
      salt = process.env.ENCRYPTION_KEY,
      secret = process.env.ENCRYPTION_SALT,
    } = encryptionSecrets as EncryptionSecretsSecrets

    if (!secret || !salt) {
      logger.error('ENCRYPTION_KEY_MISSING', 'ENCRYPTION_KEY and ENCRYPTION_SALT must be provided in environment variables or encryption secrets');
      throw new Error('ENCRYPTION_KEY and ENCRYPTION_SALT must be provided');
    }

    // Derive a 32-byte key using PBKDF2
    return crypto.pbkdf2Sync(secret, salt, 100000, 32, 'sha256');
  }

  encrypt(text: string, encryptionSecrets?: EncryptionSecrets): string {
    const key = this.getKey(encryptionSecrets)

    const iv = crypto.randomBytes(16); // Generate a new IV for each encryption
    const cipher = crypto.createCipheriv(this.algorithm, key as Uint8Array, iv as Uint8Array);

    let encrypted = cipher.update(text, 'utf8', 'base64');
    encrypted += cipher.final('base64');

    const authTag = cipher.getAuthTag().toString('base64'); // Use base64 encoding

    // Concatenate IV, authTag, and ciphertext using a delimiter (e.g., colon)
    return `${iv.toString('base64')}:${authTag}:${encrypted}`;
  }

  decrypt(encryptedText: string, encryptionSecrets?: EncryptionSecrets): string {
    const key = this.getKey(encryptionSecrets)

    const parts = encryptedText.split(':');

    if (parts.length !== 3) {
      logger.error('DECRYPTION_FAILED', 'Invalid encrypted text format');
      throw new Error('Invalid encrypted text format');
    }

    const iv = Buffer.from(parts[0], 'base64'); // Parse IV from base64
    const authTag = Buffer.from(parts[1], 'base64'); // Parse authTag from base64
    const ciphertext = parts[2]; // Encrypted text in base64

    const decipher = crypto.createDecipheriv(this.algorithm, key as Uint8Array, iv as Uint8Array);
    decipher.setAuthTag(authTag as Uint8Array);

    let decrypted = decipher.update(ciphertext, 'base64', 'utf8');
    decrypted += decipher.final('utf8');

    return decrypted;
  }
}
