require('dotenv').config({ path: 'backend/.env' });
const { S3Client, ListObjectsV2Command, DeleteObjectsCommand } = require('@aws-sdk/client-s3');

const bucketName = process.env.R2_BUCKET_NAME;
const s3Client = new S3Client({
    region: process.env.R2_REGION || 'auto',
    endpoint: process.env.R2_ENDPOINT,
    credentials: {
        accessKeyId: process.env.R2_ACCESS_KEY_ID,
        secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
    }
});

async function emptyBucket() {
    try {
        let isTruncated = true;
        let continuationToken = undefined;
        let totalDeleted = 0;

        while (isTruncated) {
            const listCmd = new ListObjectsV2Command({
                Bucket: bucketName,
                Prefix: 'sessions/',
                ContinuationToken: continuationToken
            });
            const response = await s3Client.send(listCmd);
            const objects = response.Contents || [];

            if (objects.length > 0) {
                // Delete in batches of 1000
                for (let i = 0; i < objects.length; i += 1000) {
                    const batch = objects.slice(i, i + 1000).map(o => ({ Key: o.Key }));
                    await s3Client.send(new DeleteObjectsCommand({
                        Bucket: bucketName,
                        Delete: { Objects: batch, Quiet: true },
                    }));
                    totalDeleted += batch.length;
                    console.log(`Deleted batch of ${batch.length} objects...`);
                }
            }

            isTruncated = response.IsTruncated;
            continuationToken = response.NextContinuationToken;
        }

        console.log(`✅ Cleared ${totalDeleted} objects from R2 bucket: ${bucketName}`);
    } catch (e) {
        console.error("Error emptying bucket:", e);
    }
}

emptyBucket();
