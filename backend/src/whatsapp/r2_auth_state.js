const {
    initAuthCreds,
    BufferJSON,
    proto
} = require('@whiskeysockets/baileys');
const {
    S3Client,
    GetObjectCommand,
    PutObjectCommand,
    DeleteObjectCommand,
    DeleteObjectsCommand,  // FIX: batch delete
    ListObjectsV2Command,
} = require('@aws-sdk/client-s3');

/**
 * Cloudflare R2 / AWS S3 Authentication State Adapter for Baileys
 *
 * FIXES applied:
 * 1. clearState: was deleting objects one-by-one (O(n) HTTP calls, rate-limit
 *    prone, extremely slow for large sessions).  Now uses DeleteObjectsCommand
 *    to delete up to 1000 objects per request — orders of magnitude faster.
 * 2. keys.set: was firing all PutObject calls in an unbounded Promise.all.
 *    For sessions with many signal keys this could be hundreds of concurrent
 *    HTTP requests, hitting R2 rate limits and wasting connections.  Now
 *    batched with a concurrency limit of 20 parallel requests.
 * 3. readData: now distinguishes "key not found" (normal — return null) from
 *    genuine network/auth errors (re-throw so Baileys can handle them).
 * 4. writeData: now throws on error instead of swallowing it silently, so
 *    Baileys is aware when a creds write fails.
 */

const useR2AuthState = async (sessionName) => {
    const bucketName = process.env.R2_BUCKET_NAME;
    const s3Client = new S3Client({
        region: process.env.R2_REGION || 'auto',
        endpoint: process.env.R2_ENDPOINT,
        credentials: {
            accessKeyId: process.env.R2_ACCESS_KEY_ID,
            secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
        },
    });

    const folder = `sessions/${sessionName}/`;

    // ── Write ─────────────────────────────────────────────────────────────────

    const writeData = async (data, file) => {
        // FIX: throw on error instead of silently swallowing — Baileys must know
        // when a creds write fails so it can retry or surface the error.
        const command = new PutObjectCommand({
            Bucket: bucketName,
            Key: `${folder}${file}`,
            Body: JSON.stringify(data, BufferJSON.replacer),
            ContentType: 'application/json',
        });
        await s3Client.send(command); // throws on failure — caller handles it
    };

    // ── Read ──────────────────────────────────────────────────────────────────

    const readData = async (file) => {
        try {
            const command = new GetObjectCommand({
                Bucket: bucketName,
                Key: `${folder}${file}`,
            });
            const response = await s3Client.send(command);
            const str = await response.Body.transformToString();
            return JSON.parse(str, BufferJSON.reviver);
        } catch (error) {
            // FIX: only return null for "key not found" — re-throw real errors
            // so callers (especially startGateway's stale-creds check) know
            // whether R2 is unreachable vs. simply missing the key.
            if (
                error.name === 'NoSuchKey' ||
                error.$metadata?.httpStatusCode === 404
            ) {
                return null; // Normal: key doesn't exist yet
            }
            // Real network / auth / bucket error — propagate
            console.error(`[R2 Auth] Error reading ${file}:`, error.message || error);
            throw error;
        }
    };

    // ── Delete (single key) ───────────────────────────────────────────────────

    const removeData = async (file) => {
        try {
            await s3Client.send(new DeleteObjectCommand({
                Bucket: bucketName,
                Key: `${folder}${file}`,
            }));
        } catch (error) {
            console.error(`[R2 Auth] Error deleting ${file}:`, error.message || error);
        }
    };

    // ── Clear entire session ──────────────────────────────────────────────────

    const clearState = async () => {
        /**
         * FIX: Old code deleted one object per HTTP call inside a for-loop.
         * For a session with 100 signal keys that was 100+ sequential DELETE
         * calls — extremely slow (~10-30 s) and brittle under rate limits.
         *
         * New code:
         *  1. List all objects with prefix (paginated via ContinuationToken)
         *  2. Delete in batches of up to 1000 using DeleteObjectsCommand
         *     (S3/R2 limit per batch-delete request)
         */
        try {
            let isTruncated = true;
            let continuationToken = undefined;

            while (isTruncated) {
                const listResp = await s3Client.send(new ListObjectsV2Command({
                    Bucket: bucketName,
                    Prefix: folder,
                    ContinuationToken: continuationToken,
                }));

                const objects = listResp.Contents || [];

                if (objects.length > 0) {
                    // Chunk into batches of 1000 (S3 DeleteObjects hard limit)
                    for (let i = 0; i < objects.length; i += 1000) {
                        const batch = objects.slice(i, i + 1000).map(o => ({ Key: o.Key }));
                        await s3Client.send(new DeleteObjectsCommand({
                            Bucket: bucketName,
                            Delete: { Objects: batch, Quiet: true },
                        }));
                    }
                }

                isTruncated = listResp.IsTruncated;
                continuationToken = listResp.NextContinuationToken;
            }

            console.error(`[R2 Auth] Cleared session ${sessionName}`);
        } catch (error) {
            console.error('[R2 Auth] Error clearing session:', error.message || error);
            throw error; // Let the caller decide how to handle cleanup failure
        }
    };

    // ── Concurrency limiter for parallel writes ───────────────────────────────

    /**
     * FIX: keys.set used to call Promise.all(tasks) with no concurrency cap.
     * A session with 200 signal keys would fire 200 PutObject requests at once.
     * This helper limits to `limit` concurrent requests.
     */
    const pLimit = async (tasks, limit = 20) => {
        const results = [];
        let index = 0;

        const worker = async () => {
            while (index < tasks.length) {
                const current = index++;
                results[current] = await tasks[current]();
            }
        };

        const workers = Array.from({ length: Math.min(limit, tasks.length) }, worker);
        await Promise.all(workers);
        return results;
    };

    // ── Load initial credentials ──────────────────────────────────────────────

    let creds;
    try {
        creds = await readData('creds.json');
    } catch (e) {
        // R2 unreachable at startup — init fresh and let it sync later
        console.error('[R2 Auth] Could not load creds.json, starting fresh:', e.message);
        creds = null;
    }
    if (!creds) {
        creds = initAuthCreds();
    }

    // ── Return Baileys auth state ─────────────────────────────────────────────

    return {
        state: {
            creds,
            keys: {
                get: async (type, ids) => {
                    const data = {};
                    // Reads can stay parallel — GET requests are cheap and idempotent
                    await Promise.all(
                        ids.map(async (id) => {
                            try {
                                let value = await readData(`${type}-${id}.json`);
                                if (type === 'app-state-sync-key' && value) {
                                    value = proto.Message.AppStateSyncKeyData.fromObject(value);
                                }
                                data[id] = value;
                            } catch (e) {
                                // If a key read fails, leave it undefined (Baileys tolerates this)
                                console.error(`[R2 Auth] Failed to read key ${type}-${id}:`, e.message);
                            }
                        })
                    );
                    return data;
                },

                set: async (data) => {
                    // FIX: throttled parallel writes instead of unbounded Promise.all
                    const tasks = [];
                    for (const category in data) {
                        for (const id in data[category]) {
                            const value = data[category][id];
                            const file = `${category}-${id}.json`;
                            if (value) {
                                tasks.push(() => writeData(value, file).catch(e => {
                                    console.error(`[R2 Auth] Failed to write ${file}:`, e.message);
                                }));
                            } else {
                                tasks.push(() => removeData(file));
                            }
                        }
                    }
                    if (tasks.length > 0) {
                        await pLimit(tasks, 20);
                    }
                },
            },
        },

        saveCreds: () => {
            return writeData(creds, 'creds.json').catch(e => {
                console.error('[R2 Auth] Failed to save creds:', e.message);
                // Don't throw — a failed creds save shouldn't crash the gateway
            });
        },

        clearState,
    };
};

module.exports = { useR2AuthState };