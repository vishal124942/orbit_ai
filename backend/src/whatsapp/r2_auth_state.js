const {
    initAuthCreds,
    BufferJSON,
    proto
} = require('@whiskeysockets/baileys');
const { S3Client, GetObjectCommand, PutObjectCommand, DeleteObjectCommand, ListObjectsV2Command } = require('@aws-sdk/client-s3');

/**
 * Cloudflare R2 / AWS S3 Authentication State Adapter for Baileys
 * 
 * This adapter allows Baileys to store its WhatsApp encryption keys (creds.json, session-*.json)
 * directly in an S3-compatible bucket, making the backend completely stateless and scalable.
 * 
 * @param {string} sessionName - Unique ID for the session (e.g., user ID or phone number)
 */
const useR2AuthState = async (sessionName) => {
    // Requires AWS SDK v3

    // Environment variables for S3/R2
    const bucketName = process.env.R2_BUCKET_NAME;
    const s3Client = new S3Client({
        region: process.env.R2_REGION || 'auto',
        endpoint: process.env.R2_ENDPOINT,
        credentials: {
            accessKeyId: process.env.R2_ACCESS_KEY_ID,
            secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
        },
    });

    // All keys for this session will be stored in a folder
    const folder = `sessions/${sessionName}/`;

    // Helper: Write data to R2
    const writeData = async (data, file) => {
        try {
            const command = new PutObjectCommand({
                Bucket: bucketName,
                Key: `${folder}${file}`,
                Body: JSON.stringify(data, BufferJSON.replacer),
                ContentType: 'application/json'
            });
            await s3Client.send(command);
        } catch (error) {
            console.error(`[R2 Auth] Error writing ${file}:`, error);
        }
    };

    // Helper: Read data from R2
    const readData = async (file) => {
        try {
            const command = new GetObjectCommand({
                Bucket: bucketName,
                Key: `${folder}${file}`
            });
            const response = await s3Client.send(command);
            const str = await response.Body.transformToString();
            return JSON.parse(str, BufferJSON.reviver);
        } catch (error) {
            if (error.name === 'NoSuchKey' || error.$metadata?.httpStatusCode === 404) {
                return null;
            }
            console.error(`[R2 Auth] Error reading ${file}:`, error);
            return null;
        }
    };

    // Helper: Delete data from R2
    const removeData = async (file) => {
        try {
            const command = new DeleteObjectCommand({
                Bucket: bucketName,
                Key: `${folder}${file}`
            });
            await s3Client.send(command);
        } catch (error) {
            console.error(`[R2 Auth] Error deleting ${file}:`, error);
        }
    };

    // Helper: Clear entire session (logout)
    const clearState = async () => {
        try {
            let isTruncated = true;
            let continuationToken = undefined;

            while (isTruncated) {
                const listCmd = new ListObjectsV2Command({
                    Bucket: bucketName,
                    Prefix: folder,
                    ContinuationToken: continuationToken
                });
                const response = await s3Client.send(listCmd);

                if (response.Contents && response.Contents.length > 0) {
                    // Note: Quick delete loop; for production > 1000 items use DeleteObjectsCommand
                    for (const item of response.Contents) {
                        const delCmd = new DeleteObjectCommand({
                            Bucket: bucketName,
                            Key: item.Key
                        });
                        await s3Client.send(delCmd);
                    }
                }

                isTruncated = response.IsTruncated;
                continuationToken = response.NextContinuationToken;
            }
            console.error(`[R2 Auth] Cleared session ${sessionName}`);
        } catch (error) {
            console.error(`[R2 Auth] Error clearing session:`, error);
        }
    };

    // --- State Management --- 

    // 1. Load initial credentials
    let creds = await readData('creds.json');
    if (!creds) {
        creds = initAuthCreds();
    }

    // 2. Return the custom auth state object expected by Baileys
    return {
        state: {
            creds,
            keys: {
                get: async (type, ids) => {
                    const data = {};
                    await Promise.all(
                        ids.map(async id => {
                            let value = await readData(`${type}-${id}.json`);
                            if (type === 'app-state-sync-key' && value) {
                                value = proto.Message.AppStateSyncKeyData.fromObject(value);
                            }
                            data[id] = value;
                        })
                    );
                    return data;
                },
                set: async (data) => {
                    const tasks = [];
                    for (const category in data) {
                        for (const id in data[category]) {
                            const value = data[category][id];
                            const file = `${category}-${id}.json`;
                            if (value) {
                                tasks.push(writeData(value, file));
                            } else {
                                tasks.push(removeData(file));
                            }
                        }
                    }
                    await Promise.all(tasks);
                }
            }
        },
        saveCreds: () => {
            return writeData(creds, 'creds.json');
        },
        clearState
    };
};

module.exports = { useR2AuthState };
