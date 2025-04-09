import * as zlib from 'zlib';
import * as crypto from 'crypto';
import { promisify } from 'util';

const gzipAsync = promisify(zlib.gzip);
const gunzipAsync = promisify(zlib.gunzip);

export class MessageTransformer {
  /**
   * Compress a message using gzip
   */
  public async compress(message: string): Promise<string> {
    const buffer = await gzipAsync(Buffer.from(message));
    return buffer.toString('base64');
  }

  /**
   * Decompress a message using gzip
   */
  public async decompress(message: string): Promise<string> {
    const buffer = await gunzipAsync(Buffer.from(message, 'base64'));
    return buffer.toString();
  }

  /**
   * Encrypt a message using AES-256-CBC
   */
  public async encrypt(message: string, secretKey: string): Promise<string> {
    const iv = crypto.randomBytes(16);
    const key = crypto.createHash('sha256').update(String(secretKey)).digest('base64').substr(0, 32);

    const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
    let encrypted = cipher.update(message, 'utf8', 'hex');
    encrypted += cipher.final('hex');

    return `${iv.toString('hex')}:${encrypted}`;
  }

  /**
   * Decrypt a message using AES-256-CBC
   */
  public async decrypt(encryptedMessage: string, secretKey: string): Promise<string> {
    const [ivHex, encrypted] = encryptedMessage.split(':');
    const iv = Buffer.from(ivHex, 'hex');
    const key = crypto.createHash('sha256').update(String(secretKey)).digest('base64').substr(0, 32);

    const decipher = crypto.createDecipheriv('aes-256-cbc', key, iv);
    let decrypted = decipher.update(encrypted, 'hex', 'utf8');
    decrypted += decipher.final('utf8');

    return decrypted;
  }

  /**
   * Transform a message using a custom function
   */
  public async transformWithFunction(message: string, transformFn: string): Promise<string> {
    // Try to parse message as JSON first
    let input: any;
    try {
      input = JSON.parse(message);
    } catch (e) {
      input = message;
    }

    // Create and execute the transform function
    // eslint-disable-next-line @typescript-eslint/no-implied-eval
    const fn = new Function('input', transformFn);
    const result = fn(input);

    // Convert the result back to string
    if (typeof result === 'object') {
      return JSON.stringify(result);
    }

    return String(result);
  }

  /**
   * Validate a message against a JSON schema
   */
  public async validateWithSchema(message: string, schemaStr: string): Promise<boolean> {
    try {
      const schema = JSON.parse(schemaStr);
      const data = typeof message === 'string' ? JSON.parse(message) : message;

      // Simple schema validation
      this.validateObject(data, schema);

      return true;
    } catch (error) {
      throw new Error(`Schema validation failed: ${error.message}`);
    }
  }

  /**
   * Simple schema validation implementation
   */
  private validateObject(obj: any, schema: any): void {
    // Check type
    if (schema.type) {
      const jsType = typeof obj;
      const schemaType = schema.type.toLowerCase();

      if (
        (schemaType === 'object' && jsType !== 'object') ||
        (schemaType === 'string' && jsType !== 'string') ||
        (schemaType === 'number' && jsType !== 'number') ||
        (schemaType === 'integer' && (jsType !== 'number' || !Number.isInteger(obj))) ||
        (schemaType === 'boolean' && jsType !== 'boolean') ||
        (schemaType === 'array' && !Array.isArray(obj))
      ) {
        throw new Error(`Expected type ${schemaType} but got ${jsType}`);
      }
    }

    // Check required properties
    if (schema.required && Array.isArray(schema.required)) {
      for (const prop of schema.required) {
        if (obj[prop] === undefined) {
          throw new Error(`Missing required property: ${prop}`);
        }
      }
    }

    // Check properties
    if (schema.properties && typeof schema.properties === 'object') {
      for (const [prop, propSchema] of Object.entries(schema.properties)) {
        if (obj[prop] !== undefined) {
          this.validateObject(obj[prop], propSchema);
        }
      }
    }

    // Check items in array
    if (schema.items && Array.isArray(obj)) {
      for (const item of obj) {
        this.validateObject(item, schema.items);
      }
    }
  }
}
